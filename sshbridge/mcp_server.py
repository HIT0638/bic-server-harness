"""Local stdio MCP adapter backed exclusively by the connection broker."""

import argparse
import asyncio
import base64
import json
import os
import sys
from types import SimpleNamespace
from typing import Any, Dict, Literal, Optional, TypedDict

from .broker_client import BrokerClient
from .config import Profile, load_config
from .errors import BridgeError


MCP_MAX_RESULT_BYTES = 1024 * 1024
_READ_TIMEOUT = 300.0
_WRITE_TIMEOUT = 300.0
_FILE_TIMEOUT = 120.0
_HASH_TIMEOUT = 300.0
_PRIVATE_RESULT_KEYS = frozenset(("real_path", "real_cwd", "root"))


class _SuccessResult(TypedDict):
    ok: bool
    result: Dict[str, Any]


class _DependencyMissing(Exception):
    pass


def _load_mcp_sdk():
    try:
        from mcp import types
        from mcp.server import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError as error:
        raise _DependencyMissing(str(error))
    return SimpleNamespace(
        MCPServer=MCPServer,
        ToolAnnotations=types.ToolAnnotations,
        ToolError=ToolError,
    )


def _sdk_namespace(mcp_module):
    if mcp_module is None:
        return _load_mcp_sdk()
    required = ("MCPServer", "ToolAnnotations", "ToolError")
    if all(hasattr(mcp_module, name) for name in required):
        return mcp_module
    raise TypeError(
        "mcp_module must expose MCPServer, ToolAnnotations, and ToolError")


def _sanitize_result(value):
    if isinstance(value, dict):
        return {
            key: _sanitize_result(item)
            for key, item in value.items()
            if key not in _PRIVATE_RESULT_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_result(item) for item in value]
    return value


def _redact_text(value, secrets):
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "<redacted>")
    return text


def _sanitize_error_details(value, secrets):
    if isinstance(value, dict):
        return {
            key: _sanitize_error_details(item, secrets)
            for key, item in value.items()
            if key not in _PRIVATE_RESULT_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_error_details(item, secrets) for item in value]
    if isinstance(value, str):
        return _redact_text(value, secrets)
    return value


class _McpBrokerAdapter:
    def __init__(self, profile, config_path, client, tool_error_class):
        self.profile = profile
        self.config_path = os.path.abspath(config_path)
        self.client = client
        self.tool_error_class = tool_error_class
        self._secrets = (
            profile.root,
            profile.host,
            profile.user,
            self.config_path,
        )

    async def call(self, operation, arguments, timeout):
        try:
            result = await asyncio.to_thread(
                self.client.request, operation, arguments, timeout)
            return self._success(
                result, enforce_size=operation != "read_file")
        except BridgeError as error:
            self._raise_tool_error(error)

    async def status(self):
        try:
            result = await asyncio.to_thread(self.client.status)
            return self._success(result)
        except BridgeError as error:
            self._raise_tool_error(error)

    async def reconnect(self):
        try:
            result = await asyncio.to_thread(self.client.reconnect)
            return self._success(result)
        except BridgeError as error:
            self._raise_tool_error(error)

    def invalid(self, code, message, **details):
        self._raise_tool_error(BridgeError(code, message, **details))

    def _success(self, result, enforce_size=True):
        payload = {"ok": True, "result": _sanitize_result(result)}
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if enforce_size and len(encoded) > MCP_MAX_RESULT_BYTES:
            self._raise_tool_error(BridgeError(
                "TOO_LARGE",
                "MCP result exceeds 1 MiB; the operation may have completed. "
                "Use a smaller read page or redirect command output to a "
                "remote file and read it in pages.",
                max_result_bytes=MCP_MAX_RESULT_BYTES,
            ))
        return payload

    def _raise_tool_error(self, error):
        payload = {
            "ok": False,
            "error": {
                "code": error.code,
                "message": _redact_text(error.message, self._secrets),
                "details": _sanitize_error_details(
                    error.details or {}, self._secrets),
            },
        }
        text = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        raise self.tool_error_class(text)


def create_mcp_server(
        profile,
        config_path,
        mcp_module=None,
        broker_client_factory=BrokerClient):
    """Create one profile-bound MCPServer and start its local broker."""
    if not os.path.isabs(config_path):
        raise BridgeError(
            "INVALID_CONFIG", "MCP config path must be absolute")
    if profile.connection_policy["mode"] != "broker":
        raise BridgeError(
            "BROKER_UNSUPPORTED",
            "MCP requires connection_policy.mode=broker")
    sdk = _sdk_namespace(mcp_module)
    client = broker_client_factory(config_path, profile)
    client.ensure_started()
    adapter = _McpBrokerAdapter(
        profile, config_path, client, sdk.ToolError)
    server = sdk.MCPServer(
        "sshbridge",
        description=(
            "Sandboxed remote workspace files and commands through the local "
            "SSHBridge connection broker."),
        version="0.1.0",
    )

    read_annotations = sdk.ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
    write_annotations = sdk.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=False,
    )
    create_annotations = sdk.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    )
    exec_annotations = sdk.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    )
    reconnect_annotations = sdk.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    )

    @server.tool(
        description="List one remote workspace directory without recursion.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def list_dir(path: str = "/") -> _SuccessResult:
        return await adapter.call(
            "list_dir", {"path": path}, _FILE_TIMEOUT)

    @server.tool(
        description="Return metadata for one remote workspace path.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def stat(path: str) -> _SuccessResult:
        return await adapter.call("stat", {"path": path}, _FILE_TIMEOUT)

    @server.tool(
        description=(
            "Read one page of a remote file as strict UTF-8 text or Base64. "
            "Use offset and limit to page large files."),
        annotations=read_annotations,
        structured_output=True,
    )
    async def read_file(
            path: str,
            offset: int = 0,
            limit: Optional[int] = None,
            encoding: Literal["text", "base64"] = "text",
    ) -> _SuccessResult:
        effective_limit = min(
            int(profile.max_read_bytes), MCP_MAX_RESULT_BYTES)
        if limit is not None:
            if limit > MCP_MAX_RESULT_BYTES:
                adapter.invalid(
                    "TOO_LARGE",
                    "read limit exceeds 1 MiB; request the file in smaller pages",
                    max_limit=MCP_MAX_RESULT_BYTES,
                )
            effective_limit = limit
        response = await adapter.call(
            "read_file",
            {"path": path, "offset": offset, "limit": effective_limit},
            _READ_TIMEOUT,
        )
        result = response["result"]
        encoded = result.pop("content_b64")
        result.pop("content", None)
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise RuntimeError("broker returned invalid Base64") from error
        if encoding == "text":
            try:
                result["content"] = data.decode("utf-8", "strict")
            except UnicodeDecodeError:
                adapter.invalid(
                    "BINARY_FILE",
                    "file page is not valid UTF-8; retry with "
                    'encoding="base64"',
                    path=path,
                    offset=offset,
                )
        else:
            result["content_b64"] = encoded
        return adapter._success(result)

    @server.tool(
        description="Compute the SHA-256 digest of one remote file.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def hash_file(path: str) -> _SuccessResult:
        return await adapter.call("hash", {"path": path}, _HASH_TIMEOUT)

    @server.tool(
        description="Return local Broker and remote connection state.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def connection_status() -> _SuccessResult:
        return await adapter.status()

    @server.tool(
        description=(
            "Atomically write UTF-8 text to a remote file. Supply expected "
            "mtime, size, or SHA-256 to detect concurrent changes."),
        annotations=write_annotations,
        structured_output=True,
    )
    async def write_file(
            path: str,
            content: str,
            expected_mtime: Optional[int] = None,
            expected_size: Optional[int] = None,
            expected_hash: Optional[str] = None,
            force: bool = False,
    ) -> _SuccessResult:
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError:
            adapter.invalid(
                "INVALID_ARG", "content must be valid UTF-8 text")
        if len(data) > MCP_MAX_RESULT_BYTES:
            adapter.invalid(
                "TOO_LARGE",
                "write content exceeds the 1 MiB MCP input limit",
                max_content_bytes=MCP_MAX_RESULT_BYTES,
            )
        return await adapter.call(
            "write_file",
            {
                "path": path,
                "data_b64": base64.b64encode(data).decode("ascii"),
                "expected_mtime": expected_mtime,
                "expected_size": expected_size,
                "expected_hash": expected_hash,
                "force": force,
            },
            _WRITE_TIMEOUT,
        )

    @server.tool(
        description="Create one remote directory, optionally with parents.",
        annotations=create_annotations,
        structured_output=True,
    )
    async def mkdir(
            path: str,
            parents: bool = False,
    ) -> _SuccessResult:
        return await adapter.call(
            "mkdir", {"path": path, "parents": parents}, _FILE_TIMEOUT)

    @server.tool(
        description=(
            "Move or rename a remote workspace entry. Set force to replace "
            "an existing destination where the SFTP server permits it."),
        annotations=write_annotations,
        structured_output=True,
    )
    async def move(
            src: str,
            dst: str,
            force: bool = False,
    ) -> _SuccessResult:
        return await adapter.call(
            "move",
            {"src": src, "dst": dst, "force": force},
            _FILE_TIMEOUT,
        )

    @server.tool(
        description=(
            "Delete one remote file, symlink, or empty directory. The "
            "workspace root and non-empty directories cannot be deleted."),
        annotations=write_annotations,
        structured_output=True,
    )
    async def delete(path: str) -> _SuccessResult:
        return await adapter.call(
            "delete", {"path": path}, _FILE_TIMEOUT)

    @server.tool(
        description=(
            "Run arbitrary shell text on the remote host. cwd is only the "
            "starting directory, not a command sandbox; the command can access "
            "anything allowed to the remote account."),
        annotations=exec_annotations,
        structured_output=True,
    )
    async def exec(
            command: str,
            cwd: str = "/",
            timeout: Optional[float] = None,
    ) -> _SuccessResult:
        if timeout is not None and timeout <= 0:
            adapter.invalid("INVALID_ARG", "timeout must be > 0")
        request_timeout = (
            float(profile.exec_timeout) if timeout is None else timeout) + 60.0
        return await adapter.call(
            "exec",
            {"command": command, "cwd": cwd, "timeout": timeout},
            request_timeout,
        )

    @server.tool(
        description=(
            "Explicitly make one Broker-gated attempt to recover a paused "
            "remote connection."),
        annotations=reconnect_annotations,
        structured_output=True,
    )
    async def reconnect() -> _SuccessResult:
        return await adapter.reconnect()

    return server


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m sshbridge.mcp_server",
        description="Run the SSHBridge stdio MCP Server.")
    parser.add_argument(
        "--config", required=True,
        help="absolute path to bridge.json")
    parser.add_argument(
        "--profile",
        help="profile name (default: config default_profile)")
    return parser


def app_main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not os.path.isabs(args.config):
        print(
            "INVALID_CONFIG: --config must be an absolute path",
            file=sys.stderr,
        )
        return 2
    try:
        cfg = load_config(args.config)
        profile_name = args.profile or cfg["default_profile"]
        if profile_name not in cfg["profiles"]:
            raise BridgeError(
                "INVALID_CONFIG",
                "unknown profile: %s (have: %s)" % (
                    profile_name, ", ".join(cfg["profiles"])))
        profile = Profile(profile_name, cfg["profiles"][profile_name])
        if profile.connection_policy["mode"] != "broker":
            raise BridgeError(
                "BROKER_UNSUPPORTED",
                "MCP requires connection_policy.mode=broker")
        server = create_mcp_server(profile, args.config)
        server.run()
        return 0
    except _DependencyMissing:
        print(
            "MCP_DEPENDENCY_MISSING: install requirements-mcp.txt with "
            "Python 3.12",
            file=sys.stderr,
        )
        return 2
    except BridgeError as error:
        print(
            "%s: %s" % (error.code, error.message),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(app_main())
