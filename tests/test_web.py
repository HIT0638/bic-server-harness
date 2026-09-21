import errno
import json
import os
import socket
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sshbridge.config import Profile
from sshbridge.web import create_server

from tests.local_sshd import LocalSshd, LocalSshdUnavailable

SENTINEL_NAME = ".sshbridge-web-sentinel.txt"
SENTINEL_CONTENT = "sshbridge web sentinel\n"


class TestWebExplorerIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sshd = LocalSshd()
        cls.previous_state_dir = os.environ.get("SSHBRIDGE_STATE_DIR")
        try:
            cls.sshd.start()
            os.environ["SSHBRIDGE_STATE_DIR"] = str(cls.sshd.daemon_state)
            (cls.sshd.workspace / SENTINEL_NAME).write_text(
                SENTINEL_CONTENT, encoding="utf-8")
            cls.profile = Profile("local-test", cls.sshd.profile_raw)
            cls.web = create_server(
                cls.profile, config_path=str(cls.sshd.config_path),
                port=0, token="test-token")
            cls.thread = threading.Thread(
                target=cls.web.serve_forever, daemon=True)
            cls.thread.start()
            cls.base_url = "http://127.0.0.1:%d" % cls.web.server_address[1]
        except LocalSshdUnavailable as exc:
            cls.sshd.stop()
            raise unittest.SkipTest(str(exc))
        except Exception:
            cls.sshd.stop()
            cls._restore_state_dir()
            raise

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "web", None) is not None:
            cls.web.shutdown()
            cls.web.server_close()
        if getattr(cls, "thread", None) is not None:
            cls.thread.join(timeout=5)
        broker = getattr(
            getattr(cls, "web", None), "workspace", None)
        if broker is not None and broker._broker is not None:
            broker._broker.stop()
        cls.sshd.stop()
        cls._restore_state_dir()

    @classmethod
    def _restore_state_dir(cls):
        if cls.previous_state_dir is None:
            os.environ.pop("SSHBRIDGE_STATE_DIR", None)
        else:
            os.environ["SSHBRIDGE_STATE_DIR"] = cls.previous_state_dir

    def request(self, path, method="GET", body=None, token="test-token"):
        data = None
        headers = {}
        if token is not None:
            headers["X-SSHBridge-Token"] = token
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=10) as response:
                raw = response.read()
                return response.status, response.headers, raw
        except HTTPError as error:
            return error.code, error.headers, error.read()

    def request_json(self, path, method="GET", body=None, token="test-token"):
        status, headers, raw = self.request(path, method, body, token)
        return status, headers, json.loads(raw.decode("utf-8"))

    def test_static_application_and_api_authentication(self):
        status, headers, raw = self.request("/", token=None)
        self.assertEqual(status, 200)
        self.assertIn(b"Remote Explorer", raw)
        self.assertIn("default-src 'self'",
                      headers["Content-Security-Policy"])

        status, _, payload = self.request_json("/api/info", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "UNAUTHORIZED")

        status, _, payload = self.request_json("/api/info")
        self.assertEqual(status, 200)
        self.assertEqual(payload["profile"], "local-test")
        self.assertEqual(payload["workspace_root"], "/")
        self.assertNotIn("root", payload)

    def test_occupied_port_preserves_bind_error(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            with self.assertRaises(OSError) as caught:
                create_server(
                    self.profile, config_path=str(self.sshd.config_path),
                    port=port, token="test-token")
        self.assertEqual(caught.exception.errno, errno.EADDRINUSE)

    def test_remote_file_workflow(self):
        base = "/web-flow"

        status, _, payload = self.request_json(
            "/api/mkdir", "POST", {"path": base})
        self.assertEqual(status, 200, payload)

        status, _, payload = self.request_json(
            "/api/write", "POST",
            {"path": base + "/note.txt", "content": "first\n"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["bytes_written"], 6)
        self.assertNotIn("real_path", payload)

        status, _, payload = self.request_json("/api/list?path=%2Fweb-flow")
        self.assertEqual(status, 200, payload)
        self.assertEqual(
            [(entry["name"], entry["type"]) for entry in payload["entries"]],
            [("note.txt", "file")])
        self.assertNotIn("real_path", payload)

        status, _, opened = self.request_json(
            "/api/read?path=%2Fweb-flow%2Fnote.txt")
        self.assertEqual(status, 200, opened)
        self.assertEqual(opened["content"], "first\n")
        self.assertFalse(opened["binary"])
        self.assertIsInstance(opened["mtime"], int)

        status, _, conflict = self.request_json(
            "/api/write", "POST", {
                "path": base + "/note.txt",
                "content": "second\n",
                "expected_mtime": opened["mtime"],
                "expected_size": 999,
            })
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "CONFLICT")

        status, _, payload = self.request_json(
            "/api/write", "POST", {
                "path": base + "/note.txt",
                "content": "second\n",
                "expected_mtime": opened["mtime"],
                "expected_size": opened["size"],
            })
        self.assertEqual(status, 200, payload)

        status, _, payload = self.request_json(
            "/api/move", "POST", {
                "src": base + "/note.txt",
                "dst": base + "/renamed.txt",
            })
        self.assertEqual(status, 200, payload)

        status, _, payload = self.request_json(
            "/api/read?path=%2Fweb-flow%2Frenamed.txt")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["content"], "second\n")

    def test_binary_file_is_not_exposed_as_editor_text(self):
        binary = self.sshd.workspace / "binary.dat"
        binary.write_bytes(b"\x00\xff\x01")

        status, _, payload = self.request_json(
            "/api/read?path=%2Fbinary.dat")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["binary"])
        self.assertEqual(payload["content"], "")
        self.assertNotIn("content_b64", payload)

    def test_sandbox_error_and_request_limit(self):
        status, _, payload = self.request_json(
            "/api/read?path=%2F..%2F..%2F" + SENTINEL_NAME)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["content"], SENTINEL_CONTENT)

        status, _, payload = self.request_json(
            "/api/write", "POST", {
                "path": "/too-large.txt",
                "content": "x" * (self.sshd.profile_raw["hard_read_cap"] + 1),
            })
        self.assertEqual(status, 413)
        self.assertEqual(payload["error"]["code"], "TOO_LARGE")


if __name__ == "__main__":
    unittest.main()
