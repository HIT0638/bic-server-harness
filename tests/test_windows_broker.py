"""Native Windows IPC and MCP/Broker integration, without a remote SSH target."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from sshbridge.broker_client import BrokerClient, BrokerEndpoint, PROTOCOL_VERSION, _read_message
from sshbridge.config import Profile
from sshbridge.errors import BridgeError

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "native Windows test")
class TestWindowsBroker(unittest.TestCase):
    def setUp(self):
        from sshbridge import windows_ipc
        self.ipc = windows_ipc
        self.temp = tempfile.TemporaryDirectory(prefix="sshbridge-win-")
        self.base = Path(self.temp.name)
        self.patch = mock.patch.dict(os.environ, {
            "SSHBRIDGE_STATE_DIR": str(self.base / "private runtime 中文"),
        })
        self.patch.start()
        # Intentionally missing executable guarantees no SSH connection or
        # credential access, while exercising real Broker failure handling.
        self.raw = {
            "host": "127.0.0.1", "port": 1, "user": "test-user",
            "root": "/srv/private-test-root",
            "ssh_bin": str(self.base / "missing-ssh.exe"),
            "connection_policy": {
                "mode": "broker", "control_master": False,
                "min_connect_interval": 0, "connect_retries": 0,
            },
        }
        self.profile = Profile("test", self.raw)
        self.config = self.base / "test config 中文.json"
        self.config.write_text(json.dumps({
            "default_profile": "test", "profiles": {"test": self.raw},
        }), encoding="utf-8")
        self.client = BrokerClient(str(self.config), self.profile)
        self.extra_clients = []
        self.broker_processes = []

    def tearDown(self):
        try:
            for client in self.extra_clients + [self.client]:
                client.stop()
        finally:
            for process, log in self.broker_processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                log.close()
            self.patch.stop()
            self.temp.cleanup()

    def start_broker(self, client=None, count=1):
        """Explicit foreground services stay inside the CI job, outside MCP jobs.

        Hosted Windows runners prohibit breakaway themselves. Never change that
        policy or substitute a mock for IPC: launch the documented --serve mode.
        """
        client = client or self.client
        for _ in range(count):
            log = (self.base / ("broker-%s.log" % len(self.broker_processes))).open("w+b")
            process = subprocess.Popen(
                [sys.executable, "-m", "sshbridge.broker", "--serve",
                 "--config", str(self.config), "--profile", client.profile.name],
                cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
            self.broker_processes.append((process, log))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                return client.ping()
            except BridgeError:
                time.sleep(0.05)
        messages = []
        for _, log in self.broker_processes:
            log.seek(0)
            messages.append(log.read().decode("utf-8", "replace"))
        self.fail("Foreground Broker failed to start: %s" % " ".join(messages))

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(BridgeError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_windows_defaults_use_broker_without_multiplexing(self):
        profile = Profile("default", {key: value for key, value in self.raw.items()
                                      if key != "connection_policy"})
        self.assertEqual(profile.connection_policy["mode"], "broker")
        self.assertFalse(profile.connection_policy["control_master"])
        self.assertIn("ControlMaster=no", profile.sftp_argv())

    def test_runtime_and_metadata_are_current_user_only(self):
        self.start_broker()
        endpoint = self.client.endpoint
        self.ipc.check_private_path(endpoint.runtime_dir, directory=True)
        self.ipc.check_private_path(endpoint.metadata_path)
        self.ipc.check_private_path(endpoint.lock_path)
        metadata = endpoint.read_metadata()
        self.assertEqual(metadata["instance_id"], self.client.ping()["instance_id"])
        self.assertTrue(endpoint.socket_path.startswith("\\\\.\\pipe\\sshbridge-"))

    def test_public_acl_is_rejected(self):
        path = self.client.endpoint.runtime_dir
        sd = self.ipc.PTR()
        sddl = "D:P(A;OICI;GA;;;WD)(A;OICI;GA;;;%s)" % self.ipc.current_sid()
        self.ipc._check(self.ipc.A.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, C.byref(sd), None))
        setter = self.ipc._api(self.ipc.A, "SetFileSecurityW", W.BOOL, W.LPCWSTR, W.DWORD, self.ipc.PTR)
        try:
            self.ipc._check(setter(path, 0x80000004, sd))
        finally:
            self.ipc.K.LocalFree(sd)
        self.assert_error("BROKER_UNAVAILABLE", self.ipc.check_private_path, path, directory=True)

    def test_anonymous_client_cannot_open_pipe(self):
        self.start_broker()
        impersonate = self.ipc._api(self.ipc.A, "ImpersonateAnonymousToken", W.BOOL, W.HANDLE)
        revert = self.ipc._api(self.ipc.A, "RevertToSelf", W.BOOL)
        current_thread = self.ipc._api(self.ipc.K, "GetCurrentThread", W.HANDLE)
        self.ipc._check(impersonate(current_thread()))
        try:
            handle = self.ipc.K.CreateFileW(
                self.client.endpoint.socket_path, 0xC0000000, 0, None, 3,
                self.ipc.OVERLAPPED_FLAG | 0x110000, None)
            error = C.get_last_error()
            if handle != self.ipc.INVALID_HANDLE:
                self.ipc.K.CloseHandle(handle)
        finally:
            self.ipc._check(revert())
        self.assertEqual(handle, self.ipc.INVALID_HANDLE)
        self.assertEqual(error, 5)

    def test_reparse_runtime_is_rejected(self):
        target = self.base / "target"
        self.ipc.private_runtime_dir(str(target))
        junction = self.base / "junction"
        subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(junction), str(target)],
            capture_output=True, check=True, timeout=10)
        try:
            self.assert_error("BROKER_UNAVAILABLE", self.ipc.private_runtime_dir, str(junction))
        finally:
            os.rmdir(junction)

    def test_concurrent_start_and_clients_share_one_broker(self):
        self.start_broker(count=4)
        clients = [BrokerClient(str(self.config), self.profile) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda client: client.ensure_started(timeout=15), clients))
        self.assertEqual(len({item["instance_id"] for item in results}), 1)
        self.assertEqual(len({item["pid"] for item in results}), 1)
        self.assertEqual(results[0]["state"], "DISCONNECTED")
        self.assertFalse(results[0]["multiplexing"])
        self.assertEqual(results[0]["exec_concurrency"], 1)
        self.assertTrue(self.client.stop()["stopped"])
        self.assertFalse(Path(self.client.endpoint.metadata_path).exists())
        self.assertFalse(self.client.status()["running"])

    def test_failed_connection_pauses_until_explicit_reconnect(self):
        self.start_broker()
        self.assert_error("SSH_ERROR", self.client.request, "list_dir", {"path": "/"})
        opened = self.client.status()
        self.assertEqual(opened["state"], "OPEN")
        self.assert_error("CONNECTION_PAUSED", self.client.request, "list_dir", {"path": "/"})
        self.assertEqual(self.client.status()["last_attempt_at"], opened["last_attempt_at"])
        self.assert_error("SSH_ERROR", self.client.reconnect)
        self.assertEqual(self.client.status()["failure_count"], 2)

    def test_protocol_identity_is_checked(self):
        self.start_broker()
        endpoint = self.client.endpoint
        connection = self.ipc.connect(endpoint.socket_path, 2, endpoint.read_metadata()["pid"])
        try:
            connection.settimeout(2)
            connection.sendall((json.dumps({
                "version": PROTOCOL_VERSION, "request_id": "request-test",
                "profile": "wrong-profile", "op": "ping", "args": {},
            }) + "\n").encode())
            response = json.loads(_read_message(connection))
            connection.sendall(b"\0")
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"]["code"], "BROKER_PROFILE_MISMATCH")
            self.assertEqual(response["request_id"], "request-test")
        finally:
            connection.close()
        self.assert_error(
            "BROKER_UNAVAILABLE", self.ipc.connect,
            endpoint.socket_path, 2, os.getpid())

    def test_invalid_metadata_is_rejected(self):
        self.start_broker()
        path = Path(self.client.endpoint.metadata_path)
        original = path.read_text(encoding="utf-8")
        try:
            for value in ([], {}, {"pid": True, "instance_id": "x"}):
                path.write_text(json.dumps(value), encoding="utf-8")
                self.assert_error("BROKER_UNAVAILABLE", self.client.endpoint.read_metadata)
        finally:
            path.write_text(original, encoding="utf-8")

    def test_profiles_are_isolated(self):
        first = self.start_broker()
        other = BrokerClient(str(self.config), Profile("other", self.raw))
        # Config must include both independently bound profiles.
        self.config.write_text(json.dumps({
            "profiles": {"test": self.raw, "other": self.raw},
        }), encoding="utf-8")
        self.extra_clients.append(other)
        second = self.start_broker(other)
        self.assertNotEqual(first["instance_id"], second["instance_id"])
        self.assertNotEqual(self.client.endpoint.socket_path, other.endpoint.socket_path)
        other.stop()
        self.assertEqual(self.client.ping()["instance_id"], first["instance_id"])

    def test_pipe_timeout_and_first_instance_protection(self):
        name = self.ipc.pipe_name(self.client.endpoint.runtime_dir, "timeout-test")
        listener = self.ipc.PipeListener(name)
        client = None
        server = None
        try:
            with self.assertRaises(OSError):
                self.ipc.PipeListener(name)
            client = self.ipc.connect(name, 1, os.getpid())
            server, _ = listener.accept()
            server.settimeout(0.1)
            start = time.monotonic()
            with self.assertRaises(socket.timeout):
                server.recv(1)
            self.assertLess(time.monotonic() - start, 2)
            client.sendall(b"x")
            self.assertEqual(server.recv(1), b"x")
            client.settimeout(0.1)
            with self.assertRaises(socket.timeout):
                client.sendall(b"x" * (1024 * 1024))
        finally:
            if client:
                client.close()
            if server:
                server.close()
            listener.close()

    def test_host_job_cannot_create_a_short_lived_shared_broker(self):
        source = """
import json, os, win32api, win32job
from sshbridge.broker_client import BrokerClient
from sshbridge.config import load_config, Profile
from sshbridge.errors import BridgeError
job = win32job.CreateJobObject(None, '')
info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
info['BasicLimitInformation']['LimitFlags'] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
win32job.AssignProcessToJobObject(job, win32api.GetCurrentProcess())
config = load_config(os.environ['TEST_BRIDGE_CONFIG'])
profile = Profile('test', config['profiles']['test'])
try:
    BrokerClient(config['file'], profile).ensure_started()
except BridgeError as error:
    print(json.dumps(error.to_dict()), flush=True)
    os._exit(0)
os._exit(3)
"""
        result = subprocess.run(
            [sys.executable, "-u", "-c", source], cwd=str(ROOT),
            env=dict(os.environ, TEST_BRIDGE_CONFIG=str(self.config)),
            capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        error = json.loads(result.stdout)
        self.assertEqual(error["code"], "BROKER_UNAVAILABLE")
        self.assertIn("separate terminal", error["message"])
        self.assertFalse(self.client.status()["running"])

    def test_real_mcp_stdio_uses_shared_broker_and_reports_errors(self):
        # The SDK deliberately uses a non-breakaway Job Object. Bootstrap the
        # shared Broker outside that host boundary, as documented for users.
        self.start_broker()
        from mcp import Client, StdioServerParameters

        async def scenario():
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "sshbridge.mcp_server", "--config", str(self.config)],
                cwd=str(ROOT), env=dict(os.environ),
            )
            async with Client(parameters, raise_exceptions=False) as first:
                tools = await first.list_tools()
                self.assertEqual(len(tools.tools), 14)
                status = await first.call_tool("connection_status", {})
                self.assertFalse(status.is_error)
                identity = self.client.ping()["instance_id"]
                async with Client(parameters, raise_exceptions=False) as second:
                    status = await second.call_tool("connection_status", {})
                    self.assertFalse(status.is_error)
                    self.assertEqual(self.client.ping()["instance_id"], identity)
                    failure = await second.call_tool("list_dir", {"path": "/"})
                    self.assertTrue(failure.is_error)
                    text = " ".join(getattr(item, "text", "") for item in failure.content)
                    self.assertNotIn(self.raw["root"], text)
                    self.assertNotIn(self.raw["ssh_bin"], text)
                self.assertEqual(self.client.ping()["instance_id"], identity)
            self.assertEqual(self.client.ping()["instance_id"], identity)

        asyncio.run(scenario())
