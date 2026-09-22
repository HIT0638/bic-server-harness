import base64
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

from sshbridge import mcp_server
from sshbridge.config import Profile
from sshbridge.errors import BridgeError

try:
    from mcp import Client, types
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    MCP_AVAILABLE = True
except ImportError:
    Client = None
    types = None
    MCPServer = None
    ToolError = None
    MCP_AVAILABLE = False


def profile_raw(**overrides):
    raw = {
        "host": "example.test",
        "port": 2222,
        "user": "developer",
        "root": "/srv/workspace",
        "max_read_bytes": 2 * 1024 * 1024,
        "hard_read_cap": 4 * 1024 * 1024,
        "connection_policy": {"mode": "broker"},
    }
    raw.update(overrides)
    return raw


class FakeBrokerClient:
    def __init__(self):
        self.calls = []
        self.ensure_calls = 0
        self.status_calls = 0
        self.reconnect_calls = 0
        self.stop_calls = 0
        self.capability_calls = []

    def ensure_started(self):
        self.ensure_calls += 1
        return {"state": "DISCONNECTED"}

    def request(self, operation, arguments=None, timeout=120.0):
        arguments = dict(arguments or {})
        self.calls.append((operation, arguments, timeout))
        path = arguments.get("path")
        if path == "/bridge-error":
            raise BridgeError(
                "CONFLICT",
                "remote path changed under /srv/workspace",
                path="/srv/workspace/bridge-error")
        if operation == "exec" and arguments.get("command") == "crash":
            raise RuntimeError("credential-secret")
        if operation == "exec" and arguments.get("command") == "huge":
            return {
                "op": "exec",
                "real_cwd": "/srv/workspace",
                "stdout": "x" * mcp_server.MCP_MAX_RESULT_BYTES,
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
            }
        if operation == "read_file":
            data = (
                b"\x00\xffbinary"
                if path == "/binary"
                else b"hello"
            )
            return {
                "op": "read_file",
                "path": path,
                "real_path": "/srv/workspace" + path,
                "size": len(data),
                "mtime": 123,
                "offset": arguments["offset"],
                "length": len(data),
                "truncated": False,
                "content": data.decode("utf-8", "replace"),
                "content_b64": base64.b64encode(data).decode("ascii"),
            }
        if operation == "list_dir":
            return {
                "op": operation,
                "path": path,
                "real_path": "/srv/workspace",
                "entries": [],
            }
        if operation == "stat":
            return {
                "op": operation,
                "path": path,
                "real_path": "/srv/workspace/file",
                "type": "file",
                "size": 5,
            }
        if operation == "hash":
            return {
                "op": operation,
                "path": path,
                "real_path": "/srv/workspace/file",
                "algo": "sha256",
                "hash": "a" * 64,
                "size": 5,
            }
        if operation == "exec":
            return {
                "op": operation,
                "cwd": arguments["cwd"],
                "real_cwd": "/srv/workspace",
                "stdout": "ok",
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
            }
        if operation == "exec_start":
            return {
                "op": operation,
                "job_id": "job-one",
                "state": "QUEUED",
                "cwd": arguments["cwd"],
                "real_cwd": "/srv/workspace",
            }
        if operation == "exec_status":
            return {
                "op": operation,
                "job_id": arguments["job_id"],
                "state": "RUNNING",
                "cwd": "/",
                "real_cwd": "/srv/workspace",
                "events": [{
                    "cursor": 2,
                    "stream": "stdout",
                    "text": "ok",
                }],
                "next_cursor": 2,
                "has_more": False,
                "output_truncated": False,
                "process_pid": 123,
            }
        if operation == "exec_cancel":
            return {
                "op": operation,
                "job_id": arguments["job_id"],
                "state": "CANCELED",
                "remote_termination_unknown": True,
            }
        return {
            "op": operation,
            "path": path,
            "real_path": "/srv/workspace/result",
        }

    def status(self):
        self.status_calls += 1
        return {
            "running": True,
            "state": "READY",
            "root": "/srv/workspace",
        }

    def reconnect(self):
        self.reconnect_calls += 1
        return {"state": "READY", "tcp_generation": 2}

    def require_capability(self, name):
        self.capability_calls.append(name)
        return {"capabilities": [name]}

    def stop(self):
        self.stop_calls += 1


class TestMcpCommand(unittest.TestCase):
    def write_config(self, directory, mode="broker"):
        path = os.path.join(directory, "bridge.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({
                "default_profile": "test",
                "profiles": {
                    "test": profile_raw(
                        connection_policy={"mode": mode}),
                },
            }, stream)
        return path

    def test_config_path_must_be_absolute(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = mcp_server.app_main(["--config", "bridge.json"])
        self.assertEqual(result, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("INVALID_CONFIG", stderr.getvalue())

    def test_programmatic_factory_enforces_config_and_broker_mode(self):
        broker_profile = Profile("test", profile_raw())
        with self.assertRaises(BridgeError) as relative:
            mcp_server.create_mcp_server(
                broker_profile, "bridge.json", mcp_module=object())
        self.assertEqual(relative.exception.code, "INVALID_CONFIG")

        direct_profile = Profile(
            "test",
            profile_raw(connection_policy={"mode": "direct"}))
        with self.assertRaises(BridgeError) as direct:
            mcp_server.create_mcp_server(
                direct_profile, os.path.abspath("unused-bridge.json"),
                mcp_module=object())
        self.assertEqual(direct.exception.code, "BROKER_UNSUPPORTED")

    def test_direct_profile_is_rejected_before_sdk_start(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(directory, mode="direct")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = mcp_server.app_main(["--config", path])
        self.assertEqual(result, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("BROKER_UNSUPPORTED", stderr.getvalue())

    def test_missing_sdk_has_stable_stderr_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(directory)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.object(
                    mcp_server, "_load_mcp_sdk",
                    side_effect=mcp_server._DependencyMissing("missing")), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                result = mcp_server.app_main(["--config", path])
        self.assertEqual(result, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("MCP_DEPENDENCY_MISSING", stderr.getvalue())

    def test_app_runs_server_without_stopping_shared_broker(self):
        class FakeServer:
            def __init__(self):
                self.ran = False

            def run(self):
                self.ran = True

        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(directory)
            server = FakeServer()
            with mock.patch.object(
                    mcp_server, "create_mcp_server",
                    return_value=server):
                result = mcp_server.app_main(["--config", path])
        self.assertEqual(result, 0)
        self.assertTrue(server.ran)


@unittest.skipUnless(MCP_AVAILABLE, "MCP Python SDK is not installed")
class TestMcpServer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.profile = Profile("test", profile_raw())
        self.broker = FakeBrokerClient()
        sdk = SimpleNamespace(
            MCPServer=MCPServer,
            ToolAnnotations=types.ToolAnnotations,
            ToolError=ToolError,
        )
        self.server = mcp_server.create_mcp_server(
            self.profile,
            os.path.abspath("unused-bridge.json"),
            mcp_module=sdk,
            broker_client_factory=lambda _config, _profile: self.broker,
        )

    async def call_tool(self, name, arguments):
        async with Client(self.server, raise_exceptions=False) as client:
            return await client.call_tool(name, arguments)

    async def list_tools(self):
        async with Client(self.server, raise_exceptions=False) as client:
            return await client.list_tools()

    @staticmethod
    def error_payload(result):
        text = "\n".join(
            item.text for item in result.content
            if getattr(item, "type", None) == "text")
        start = text.find("{")
        if start < 0:
            raise AssertionError("tool error does not contain JSON: %r" % text)
        return text, json.loads(text[start:])

    async def test_lists_stable_tools_schemas_and_annotations(self):
        listed = await self.list_tools()
        self.assertEqual(
            [tool.name for tool in listed.tools],
            [
                "list_dir", "stat", "read_file", "hash_file",
                "connection_status", "write_file", "mkdir", "move",
                "delete", "exec", "exec_start", "exec_status",
                "exec_cancel", "reconnect",
            ],
        )
        by_name = {tool.name: tool for tool in listed.tools}
        self.assertEqual(
            set(by_name["read_file"].input_schema["properties"]),
            {"path", "offset", "limit", "encoding"})
        self.assertEqual(
            by_name["read_file"].input_schema["properties"]["encoding"][
                "default"],
            "text")
        self.assertTrue(by_name["list_dir"].annotations.read_only_hint)
        self.assertFalse(by_name["mkdir"].annotations.destructive_hint)
        self.assertTrue(by_name["delete"].annotations.destructive_hint)
        self.assertTrue(by_name["exec"].annotations.open_world_hint)
        self.assertFalse(
            by_name["exec_start"].annotations.idempotent_hint)
        self.assertTrue(
            by_name["exec_status"].annotations.read_only_hint)
        self.assertTrue(
            by_name["exec_cancel"].annotations.idempotent_hint)
        self.assertIn(
            "remote process may still be running",
            by_name["exec_cancel"].description)
        self.assertEqual(
            set(by_name["stat"].output_schema["required"]),
            {"ok", "result"})

    async def test_tool_to_broker_mapping_and_result_sanitizing(self):
        calls = [
            ("list_dir", {"path": "/dir"}),
            ("stat", {"path": "/file"}),
            ("read_file", {
                "path": "/file", "offset": 2, "limit": 3,
                "encoding": "text",
            }),
            ("hash_file", {"path": "/file"}),
            ("write_file", {
                "path": "/file", "content": "hello",
                "expected_mtime": 1, "expected_size": 5,
                "expected_hash": "a" * 64, "force": True,
            }),
            ("mkdir", {"path": "/dir", "parents": True}),
            ("move", {"src": "/a", "dst": "/b", "force": True}),
            ("delete", {"path": "/file"}),
            ("exec", {"command": "printf ok", "cwd": "/", "timeout": 2.5}),
            ("exec_start", {
                "command": "sleep 10", "cwd": "/", "timeout": 20,
            }),
            ("exec_status", {
                "job_id": "job-one", "cursor": 1, "max_bytes": 1024,
            }),
            ("exec_cancel", {"job_id": "job-one"}),
        ]
        for name, arguments in calls:
            with self.subTest(tool=name):
                result = await self.call_tool(name, arguments)
                self.assertFalse(result.is_error)
                serialized = json.dumps(result.structured_content)
                self.assertNotIn("/srv/workspace", serialized)
                self.assertNotIn("real_path", serialized)
                self.assertNotIn("real_cwd", serialized)
                self.assertNotIn("process_pid", serialized)

        status = await self.call_tool("connection_status", {})
        reconnect = await self.call_tool("reconnect", {})
        self.assertFalse(status.is_error)
        self.assertFalse(reconnect.is_error)
        self.assertEqual(self.broker.status_calls, 1)
        self.assertEqual(self.broker.reconnect_calls, 1)

        operations = [call[0] for call in self.broker.calls]
        self.assertEqual(operations, [
            "list_dir", "stat", "read_file", "hash", "write_file",
            "mkdir", "move", "delete", "exec", "exec_start",
            "exec_status", "exec_cancel",
        ])
        read_call = self.broker.calls[2]
        self.assertEqual(
            read_call,
            ("read_file", {"path": "/file", "offset": 2, "limit": 3},
             300.0))
        write_call = self.broker.calls[4]
        self.assertEqual(
            base64.b64decode(write_call[1]["data_b64"]), b"hello")
        self.assertEqual(write_call[2], 300.0)
        self.assertEqual(self.broker.calls[8][2], 62.5)
        self.assertEqual(len(self.broker.capability_calls), 3)
        self.assertEqual(self.broker.ensure_calls, 1)
        self.assertEqual(self.broker.stop_calls, 0)

    async def test_read_text_base64_and_binary_error(self):
        text = await self.call_tool(
            "read_file", {"path": "/text"})
        self.assertEqual(
            text.structured_content["result"]["content"], "hello")
        self.assertNotIn(
            "content_b64", text.structured_content["result"])

        encoded = await self.call_tool(
            "read_file", {"path": "/binary", "encoding": "base64"})
        self.assertEqual(
            base64.b64decode(
                encoded.structured_content["result"]["content_b64"]),
            b"\x00\xffbinary")
        self.assertNotIn("content", encoded.structured_content["result"])

        binary = await self.call_tool(
            "read_file", {"path": "/binary"})
        self.assertTrue(binary.is_error)
        _, payload = self.error_payload(binary)
        self.assertEqual(payload["error"]["code"], "BINARY_FILE")

    async def test_bridge_error_is_error_with_canonical_redacted_json(self):
        result = await self.call_tool(
            "stat", {"path": "/bridge-error"})
        self.assertTrue(result.is_error)
        self.assertIsNone(result.structured_content)
        text, payload = self.error_payload(result)
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertTrue(text.endswith(canonical))
        self.assertEqual(payload["error"]["code"], "CONFLICT")
        self.assertNotIn("/srv/workspace", text)
        self.assertIn("<redacted>", text)

    async def test_unexpected_error_does_not_reach_client(self):
        result = await self.call_tool(
            "exec", {"command": "crash"})
        self.assertTrue(result.is_error)
        text = "\n".join(
            item.text for item in result.content
            if getattr(item, "type", None) == "text")
        self.assertIn("Error executing tool exec", text)
        self.assertNotIn("credential-secret", text)

    async def test_input_and_output_limits(self):
        before = len(self.broker.calls)
        too_large_write = await self.call_tool(
            "write_file",
            {
                "path": "/large",
                "content": "x" * (mcp_server.MCP_MAX_RESULT_BYTES + 1),
            },
        )
        self.assertTrue(too_large_write.is_error)
        _, payload = self.error_payload(too_large_write)
        self.assertEqual(payload["error"]["code"], "TOO_LARGE")
        self.assertEqual(len(self.broker.calls), before)

        too_large_read = await self.call_tool(
            "read_file",
            {
                "path": "/large",
                "limit": mcp_server.MCP_MAX_RESULT_BYTES + 1,
            },
        )
        self.assertTrue(too_large_read.is_error)
        _, payload = self.error_payload(too_large_read)
        self.assertEqual(payload["error"]["code"], "TOO_LARGE")

        huge_exec = await self.call_tool(
            "exec", {"command": "huge"})
        self.assertTrue(huge_exec.is_error)
        _, payload = self.error_payload(huge_exec)
        self.assertEqual(payload["error"]["code"], "TOO_LARGE")
        self.assertIn("may have completed", payload["error"]["message"])

    async def test_default_read_limit_and_invalid_timeout(self):
        result = await self.call_tool(
            "read_file", {"path": "/text"})
        self.assertFalse(result.is_error)
        self.assertEqual(
            self.broker.calls[-1][1]["limit"],
            mcp_server.MCP_MAX_RESULT_BYTES)

        before = len(self.broker.calls)
        invalid = await self.call_tool(
            "exec", {"command": "true", "timeout": 0})
        self.assertTrue(invalid.is_error)
        _, payload = self.error_payload(invalid)
        self.assertEqual(payload["error"]["code"], "INVALID_ARG")
        self.assertEqual(len(self.broker.calls), before)

        invalid_start = await self.call_tool(
            "exec_start", {"command": "true", "timeout": 0})
        self.assertTrue(invalid_start.is_error)
        _, payload = self.error_payload(invalid_start)
        self.assertEqual(payload["error"]["code"], "INVALID_ARG")

        invalid_status = await self.call_tool(
            "exec_status", {
                "job_id": "job-one",
                "max_bytes": mcp_server.EXEC_STATUS_MAX_BYTES + 1,
            })
        self.assertTrue(invalid_status.is_error)
        _, payload = self.error_payload(invalid_status)
        self.assertEqual(payload["error"]["code"], "INVALID_ARG")


if __name__ == "__main__":
    unittest.main()
