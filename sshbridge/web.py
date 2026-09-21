"""Local-only Web explorer backed by the reusable bridge operations."""

import base64
import hmac
import json
import mimetypes
import os
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import ops
from .errors import BridgeError
from .sftp_client import SftpSession, _is_connect_failure

ASSET_DIR = os.path.join(os.path.dirname(__file__), "web_assets")
MAX_JSON_OVERHEAD = 64 * 1024

_ERROR_STATUS = {
    "INVALID_ARG": 400,
    "INVALID_PATH": 400,
    "INVALID_CONFIG": 400,
    "SANDBOX_VIOLATION": 403,
    "PERMISSION_DENIED": 403,
    "NOT_FOUND": 404,
    "NOT_A_FILE": 409,
    "NOT_A_DIR": 409,
    "EXISTS": 409,
    "CONFLICT": 409,
    "TOO_LARGE": 413,
    "SSH_ERROR": 502,
    "SFTP_ERROR": 502,
    "TIMEOUT": 504,
}


class WorkspaceService:
    """Serialize access to one persistent SFTP session."""

    def __init__(self, profile):
        self.profile = profile
        self._session = None
        self._lock = threading.Lock()

    def close(self):
        with self._lock:
            self._drop_session()

    def call(self, function, *args, **kwargs):
        with self._lock:
            session = self._get_session()
            try:
                return function(
                    self.profile, *args, session=session, **kwargs)
            except BridgeError as error:
                if _is_connect_failure(error):
                    self._drop_session()
                raise

    def _get_session(self):
        if self._session is None or self._session._closed:
            self._session = SftpSession(
                self.profile.sftp_argv(), self.profile.op_timeout)
        return self._session

    def _drop_session(self):
        if self._session is not None:
            try:
                self._session.shutdown()
            except Exception:
                pass
        self._session = None


class ExplorerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, profile, token):
        self.profile = profile
        self.token = token
        self.workspace = WorkspaceService(profile)
        super().__init__(address, ExplorerHandler)

    def server_close(self):
        self.workspace.close()
        super().server_close()


class ExplorerHandler(BaseHTTPRequestHandler):
    server_version = "SSHBridgeExplorer/0.1"

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/"):
            return self._handle_api_get(parsed)
        return self._serve_asset(parsed.path)

    def do_POST(self):
        parsed = urlsplit(self.path)
        if not parsed.path.startswith("/api/"):
            return self._json_error(404, "NOT_FOUND", "route not found")
        if not self._authorized():
            return self._json_error(401, "UNAUTHORIZED", "invalid access token")
        try:
            payload = self._read_json()
            if parsed.path == "/api/write":
                return self._api_write(payload)
            if parsed.path == "/api/mkdir":
                return self._api_mkdir(payload)
            if parsed.path == "/api/move":
                return self._api_move(payload)
            return self._json_error(404, "NOT_FOUND", "route not found")
        except BridgeError as error:
            return self._bridge_error(error)
        except (KeyError, TypeError, ValueError) as error:
            return self._json_error(400, "INVALID_ARG", str(error))

    def _handle_api_get(self, parsed):
        if not self._authorized():
            return self._json_error(401, "UNAUTHORIZED", "invalid access token")
        query = parse_qs(parsed.query, keep_blank_values=True)
        path = query.get("path", ["/"])[0]
        try:
            if parsed.path == "/api/info":
                return self._json_ok({
                    "profile": self.server.profile.name,
                    "workspace_root": "/",
                    "max_read_bytes": self.server.profile.max_read_bytes,
                })
            if parsed.path == "/api/list":
                result = self.server.workspace.call(ops.op_list_dir, path)
                result.pop("real_path", None)
                result["entries"].sort(
                    key=lambda entry: (
                        entry["type"] != "dir", entry["name"].lower()))
                return self._json_ok(result)
            if parsed.path == "/api/stat":
                result = self.server.workspace.call(ops.op_stat, path)
                result.pop("real_path", None)
                return self._json_ok(result)
            if parsed.path == "/api/read":
                result = self.server.workspace.call(ops.op_read_file, path)
                result.pop("real_path", None)
                raw = base64.b64decode(result.pop("content_b64"))
                try:
                    text = raw.decode("utf-8")
                    binary = b"\x00" in raw
                except UnicodeDecodeError:
                    text = ""
                    binary = True
                result["binary"] = binary
                result["content"] = "" if binary else text
                return self._json_ok(result)
            return self._json_error(404, "NOT_FOUND", "route not found")
        except BridgeError as error:
            return self._bridge_error(error)

    def _api_write(self, payload):
        path = _required_string(payload, "path")
        content = payload.get("content")
        if not isinstance(content, str):
            raise BridgeError("INVALID_ARG", "content must be a string")
        data = content.encode("utf-8")
        if len(data) > self.server.profile.hard_read_cap:
            raise BridgeError(
                "TOO_LARGE",
                "editor write exceeds hard_read_cap=%d"
                % self.server.profile.hard_read_cap)
        result = self.server.workspace.call(
            ops.op_write_file, path, data,
            expected_mtime=payload.get("expected_mtime"),
            expected_size=payload.get("expected_size"),
            force=bool(payload.get("force")))
        result.pop("real_path", None)
        return self._json_ok(result)

    def _api_mkdir(self, payload):
        path = _required_string(payload, "path")
        result = self.server.workspace.call(
            ops.op_mkdir, path, parents=bool(payload.get("parents")))
        result.pop("real_path", None)
        return self._json_ok(result)

    def _api_move(self, payload):
        src = _required_string(payload, "src")
        dst = _required_string(payload, "dst")
        result = self.server.workspace.call(
            ops.op_move, src, dst, force=bool(payload.get("force")))
        result.pop("real_path", None)
        return self._json_ok(result)

    def _authorized(self):
        provided = self.headers.get("X-SSHBridge-Token", "")
        return hmac.compare_digest(provided, self.server.token)

    def _read_json(self):
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise BridgeError("INVALID_ARG", "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError:
            raise BridgeError("INVALID_ARG", "invalid Content-Length")
        max_length = self.server.profile.hard_read_cap * 4 + MAX_JSON_OVERHEAD
        if length < 0 or length > max_length:
            raise BridgeError("TOO_LARGE", "request body is too large")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise BridgeError("INVALID_ARG", "request body must be valid JSON")
        if not isinstance(payload, dict):
            raise BridgeError("INVALID_ARG", "request body must be a JSON object")
        return payload

    def _serve_asset(self, path):
        asset = {
            "/": "index.html",
            "/index.html": "index.html",
            "/app.js": "app.js",
            "/styles.css": "styles.css",
        }.get(path)
        if asset is None:
            return self.send_error(404)
        asset_path = os.path.join(ASSET_DIR, asset)
        try:
            with open(asset_path, "rb") as asset_file:
                content = asset_file.read()
        except OSError:
            return self.send_error(404)
        content_type = mimetypes.guess_type(asset_path)[0] or \
            "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", _content_security_policy())
        self.end_headers()
        self.wfile.write(content)

    def _json_ok(self, result):
        payload = {"ok": True}
        payload.update(result)
        self._send_json(200, payload)

    def _bridge_error(self, error):
        self._send_json(
            _ERROR_STATUS.get(error.code, 500),
            {"ok": False, "error": error.to_dict()})

    def _json_error(self, status, code, message):
        self._send_json(
            status,
            {"ok": False, "error": {"code": code, "message": message}})

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        _ = args
        return


def _required_string(payload, key):
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BridgeError("INVALID_ARG", "%s must be a non-empty string" % key)
    return value


def _content_security_policy():
    return (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'")


def create_server(profile, port=8765, token=None):
    if not isinstance(port, int) or not (0 <= port <= 65535):
        raise BridgeError("INVALID_ARG", "port must be between 0 and 65535")
    return ExplorerHTTPServer(
        ("127.0.0.1", port), profile, token or secrets.token_urlsafe(24))


def serve(profile, port=8765, open_browser=True):
    try:
        server = create_server(profile, port=port)
    except OSError as error:
        raise BridgeError(
            "INVALID_ARG", "cannot listen on 127.0.0.1:%s: %s" % (port, error))
    actual_port = server.server_address[1]
    url = "http://127.0.0.1:%d/?token=%s" % (actual_port, server.token)
    print("Remote Explorer: %s" % url, flush=True)
    if open_browser:
        threading.Timer(0.2, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("Remote Explorer stopped", flush=True)
    finally:
        server.server_close()
    return 0
