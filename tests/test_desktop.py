import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from sshbridge import desktop
from sshbridge.broker_client import BrokerClient
from sshbridge.config import Profile
from sshbridge.errors import BridgeError
from sshbridge.web import create_server
from tests.local_sshd import LocalSshd, LocalSshdUnavailable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REMOTE_PY = PROJECT_ROOT / "remote.py"


def profile_raw(**overrides):
    raw = {
        "host": "example.test",
        "port": 22,
        "user": "developer",
        "root": "/srv/workspace",
    }
    raw.update(overrides)
    return raw


class FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class FakeWindow:
    def __init__(self):
        self.events = SimpleNamespace(closing=FakeEvent())
        self.dirty = False
        self.confirmed = True

    def run_js(self, _script):
        return self.dirty

    def create_confirmation_dialog(self, _title, _message):
        return self.confirmed


class FakeWebView:
    def __init__(self, on_start=None, start_error=None):
        self.on_start = on_start
        self.start_error = start_error
        self.created = None
        self.window = FakeWindow()
        self.start_kwargs = None
        self.start_thread = None

    def create_window(self, title, url=None, **kwargs):
        self.created = (title, url, kwargs)
        return self.window

    def start(self, **kwargs):
        self.start_kwargs = kwargs
        self.start_thread = threading.current_thread()
        if self.on_start is not None:
            self.on_start(self)
        if self.start_error is not None:
            raise self.start_error


class FakeServer:
    def __init__(self):
        self.server_address = ("127.0.0.1", 48123)
        self.token = "desktop-token"
        self.running = threading.Event()
        self.stopping = threading.Event()
        self.shutdown_called = False
        self.close_called = False

    def serve_forever(self, poll_interval=0.2):
        _ = poll_interval
        self.running.set()
        self.stopping.wait(timeout=5)

    def shutdown(self):
        self.shutdown_called = True
        self.stopping.set()

    def server_close(self):
        self.close_called = True


class TestDesktopLifecycle(unittest.TestCase):
    def test_rejects_non_macos(self):
        profile = Profile("test", profile_raw())
        with mock.patch.object(desktop.sys, "platform", "linux"):
            with self.assertRaises(BridgeError) as caught:
                desktop.run_desktop(
                    profile, "/tmp/bridge.json",
                    webview_module=FakeWebView(),
                    server_factory=lambda *args, **kwargs: FakeServer())
        self.assertEqual(caught.exception.code, "DESKTOP_UNSUPPORTED")

    def test_requires_broker_mode(self):
        profile = Profile("test", profile_raw(connection_policy={
            "mode": "direct",
        }))
        with mock.patch.object(desktop.sys, "platform", "darwin"):
            with self.assertRaises(BridgeError) as caught:
                desktop.run_desktop(
                    profile, "/tmp/bridge.json",
                    webview_module=FakeWebView(),
                    server_factory=lambda *args, **kwargs: FakeServer())
        self.assertEqual(caught.exception.code, "BROKER_UNSUPPORTED")

    def test_missing_webview_is_structured(self):
        with mock.patch.dict(sys.modules, {"webview": None}):
            with self.assertRaises(BridgeError) as caught:
                desktop._load_webview()
        self.assertEqual(
            caught.exception.code, "DESKTOP_DEPENDENCY_MISSING")

    def test_window_and_server_lifecycle(self):
        profile = Profile("test", profile_raw())
        server = FakeServer()
        webview = FakeWebView(
            on_start=lambda _view: server.running.wait(timeout=1))
        called = {}

        def factory(got_profile, config_path=None, port=None):
            called.update({
                "profile": got_profile,
                "config_path": config_path,
                "port": port,
            })
            return server

        with mock.patch.object(desktop.sys, "platform", "darwin"):
            result = desktop.run_desktop(
                profile, "/tmp/bridge.json",
                webview_module=webview, server_factory=factory)

        self.assertEqual(result, 0)
        self.assertIs(called["profile"], profile)
        self.assertEqual(called["config_path"], "/tmp/bridge.json")
        self.assertEqual(called["port"], 0)
        title, url, options = webview.created
        self.assertEqual(title, "Remote Explorer - test")
        self.assertEqual(
            url, "http://127.0.0.1:48123/?token=desktop-token")
        self.assertEqual(options["width"], 1180)
        self.assertEqual(options["height"], 760)
        self.assertEqual(options["min_size"], (900, 560))
        self.assertEqual(webview.start_kwargs, {"private_mode": True})
        self.assertIs(webview.start_thread, threading.current_thread())
        self.assertEqual(
            webview.window.events.closing.handlers,
            [desktop._confirm_close])
        self.assertTrue(server.shutdown_called)
        self.assertTrue(server.close_called)

    def test_gui_failure_still_closes_server(self):
        profile = Profile("test", profile_raw())
        server = FakeServer()
        webview = FakeWebView(
            on_start=lambda _view: server.running.wait(timeout=1),
            start_error=RuntimeError("cocoa failed"))
        with mock.patch.object(desktop.sys, "platform", "darwin"):
            with self.assertRaises(BridgeError) as caught:
                desktop.run_desktop(
                    profile, "/tmp/bridge.json",
                    webview_module=webview,
                    server_factory=lambda *args, **kwargs: server)
        self.assertEqual(caught.exception.code, "DESKTOP_START_FAILED")
        self.assertTrue(server.shutdown_called)
        self.assertTrue(server.close_called)

    def test_dirty_close_confirmation(self):
        window = FakeWindow()
        self.assertTrue(desktop._confirm_close(window))
        window.dirty = True
        window.confirmed = False
        self.assertFalse(desktop._confirm_close(window))
        window.confirmed = True
        self.assertTrue(desktop._confirm_close(window))

    def test_dirty_probe_failure_allows_close(self):
        window = FakeWindow()
        window.run_js = mock.Mock(side_effect=RuntimeError("not loaded"))
        self.assertTrue(desktop._confirm_close(window))

    def test_startup_error_html_is_escaped(self):
        content = desktop._startup_error_html(
            BridgeError("INVALID_CONFIG", "<script>alert(1)</script>"))
        self.assertNotIn("<script>", content)
        self.assertIn("&lt;script&gt;", content)


class TestDesktopConfig(unittest.TestCase):
    def test_explicit_config_wins(self):
        path = desktop.resolve_desktop_config_path(
            "~/explicit.json",
            environ={"SSHBRIDGE_CONFIG": "/env.json"},
            home="/Users/test", frozen=True)
        self.assertEqual(path, os.path.abspath(
            os.path.expanduser("~/explicit.json")))

    def test_environment_config_wins_for_bundle(self):
        path = desktop.resolve_desktop_config_path(
            environ={"SSHBRIDGE_CONFIG": "~/env.json"},
            home="/Users/test", frozen=True)
        self.assertEqual(path, os.path.abspath(
            os.path.expanduser("~/env.json")))

    def test_bundle_uses_application_support(self):
        path = desktop.resolve_desktop_config_path(
            environ={}, home="/Users/test", frozen=True)
        self.assertEqual(
            path,
            "/Users/test/Library/Application Support/SSHBridge/bridge.json")

    def test_development_uses_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            old_cwd = os.getcwd()
            try:
                os.chdir(directory)
                path = desktop.resolve_desktop_config_path(
                    environ={}, frozen=False)
            finally:
                os.chdir(old_cwd)
        self.assertEqual(
            os.path.realpath(path),
            os.path.realpath(os.path.join(directory, "bridge.json")))


class TestDesktopLocalSshd(unittest.TestCase):
    def test_desktop_controller_reuses_broker_and_releases_port(self):
        server = LocalSshd()
        previous = os.environ.get("SSHBRIDGE_STATE_DIR")
        try:
            server.start()
        except LocalSshdUnavailable as error:
            server.stop()
            raise unittest.SkipTest(str(error))
        try:
            os.environ["SSHBRIDGE_STATE_DIR"] = str(server.daemon_state)
            profile = Profile("local-test", server.profile_raw)
            observed = {}

            def on_start(view):
                parsed = urlsplit(view.created[1])
                token = parse_qs(parsed.query)["token"][0]
                base_url = "%s://%s" % (parsed.scheme, parsed.netloc)
                request = Request(
                    base_url + "/api/list?path=%2F",
                    headers={"X-SSHBridge-Token": token})
                with urlopen(request, timeout=10) as response:
                    observed["list"] = json.loads(
                        response.read().decode("utf-8"))

            webview = FakeWebView(on_start=on_start)
            captured = {}

            def factory(*args, **kwargs):
                captured["server"] = create_server(*args, **kwargs)
                return captured["server"]

            with mock.patch.object(desktop.sys, "platform", "darwin"):
                desktop.run_desktop(
                    profile, str(server.config_path),
                    webview_module=webview, server_factory=factory)

            self.assertTrue(observed["list"]["ok"])
            client = BrokerClient(str(server.config_path), profile)
            self.assertEqual(client.status()["tcp_generation"], 1)
            environment = os.environ.copy()
            completed = subprocess.run(
                [
                    sys.executable, str(REMOTE_PY),
                    "--config", str(server.config_path),
                    "--json", "ls", "/",
                ],
                cwd=PROJECT_ROOT, env=environment,
                capture_output=True, text=True, timeout=20)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(client.status()["tcp_generation"], 1)

            port = captured["server"].server_address[1]
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                self.assertNotEqual(
                    probe.connect_ex(("127.0.0.1", port)), 0)
            client.stop()
        finally:
            server.stop()
            if previous is None:
                os.environ.pop("SSHBRIDGE_STATE_DIR", None)
            else:
                os.environ["SSHBRIDGE_STATE_DIR"] = previous


if __name__ == "__main__":
    unittest.main()
