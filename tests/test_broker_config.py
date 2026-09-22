import io
import json
import os
import socket
import stat
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from sshbridge import cli
from sshbridge.broker import BrokerServer, BrokerState
from sshbridge.broker_client import (
    BrokerEndpoint, _broker_launch_argv, profile_fingerprint)
from sshbridge.config import Profile
from sshbridge.errors import BridgeError
from sshbridge.exec_client import is_ssh_transport_failure
from sshbridge.rsync import RsyncManager
from sshbridge.sftp_client import _is_connect_failure


def profile_raw(**overrides):
    raw = {
        "host": "example.test",
        "port": 2222,
        "user": "developer",
        "root": "/srv/workspace",
    }
    raw.update(overrides)
    return raw


class TestConnectionPolicy(unittest.TestCase):
    def test_posix_defaults_enable_broker(self):
        profile = Profile("test", profile_raw())
        self.assertEqual(profile.connection_policy["mode"], "broker")
        self.assertEqual(profile.connection_policy["exec_concurrency"], 2)
        self.assertEqual(profile.connection_policy["connect_retries"], 0)
        self.assertTrue(profile.connection_policy["control_master"])
        self.assertEqual(profile.rsync_bin, "rsync")
        self.assertEqual(profile.remote_rsync_bin, "rsync")

    def test_valid_direct_policy(self):
        profile = Profile("test", profile_raw(connection_policy={
            "mode": "direct",
            "exec_concurrency": 1,
            "min_connect_interval": 0,
            "connect_retries": 0,
            "auto_reconnect": False,
            "cooldown_initial": 5,
            "cooldown_max": 5,
            "control_master": False,
        }))
        self.assertEqual(profile.connection_policy["mode"], "direct")
        self.assertFalse(profile.connection_policy["control_master"])

    def test_invalid_policy_values(self):
        cases = [
            {"mode": "automatic"},
            {"exec_concurrency": 0},
            {"exec_concurrency": 4},
            {"exec_concurrency": True},
            {"min_connect_interval": -1},
            {"connect_retries": 1},
            {"connect_retries": False},
            {"auto_reconnect": "no"},
            {"cooldown_initial": 10, "cooldown_max": 5},
            {"control_master": "yes"},
            {"unknown": True},
        ]
        for policy in cases:
            with self.subTest(policy=policy):
                with self.assertRaises(BridgeError) as caught:
                    Profile(
                        "test",
                        profile_raw(connection_policy=policy))
                self.assertEqual(caught.exception.code, "INVALID_CONFIG")

    def test_rsync_executable_configuration(self):
        profile = Profile("test", profile_raw(
            rsync_bin="/opt/homebrew/bin/rsync",
            remote_rsync_bin="/usr/local/bin/rsync"))
        self.assertEqual(profile.rsync_bin, "/opt/homebrew/bin/rsync")
        self.assertEqual(
            profile.remote_rsync_bin, "/usr/local/bin/rsync")

    def test_invalid_rsync_executable_values(self):
        cases = [
            {"rsync_bin": ""},
            {"rsync_bin": 3},
            {"remote_rsync_bin": ""},
            {"remote_rsync_bin": "rsync --server"},
            {"remote_rsync_bin": "../rsync"},
            {"remote_rsync_bin": "/opt/../bin/rsync"},
            {"remote_rsync_bin": "/opt/bin/rsync;touch"},
            {"remote_rsync_bin": "/opt/bin/rsync dir"},
        ]
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(BridgeError) as caught:
                    Profile("test", profile_raw(**values))
                self.assertEqual(caught.exception.code, "INVALID_CONFIG")

    def test_multiplex_options_precede_host(self):
        profile = Profile("test", profile_raw(
            ssh_args=["-o", "ServerAliveInterval=30"]))
        profile.control_path = "/tmp/test-control"
        for argv in (
                profile.sftp_argv(),
                profile.exec_argv("true"),
                profile.master_argv("/tmp/test-control"),
                profile.control_argv("/tmp/test-control", "check")):
            separator = argv.index("--")
            self.assertGreater(separator, argv.index("/tmp/test-control"))
            self.assertEqual(argv[separator + 1], profile.host)


class TestBrokerEndpoint(unittest.TestCase):
    def test_fingerprint_is_stable_and_profile_specific(self):
        profile = Profile("one", profile_raw())
        first = profile_fingerprint("./bridge.json", profile)
        second = profile_fingerprint("./bridge.json", profile)
        other = profile_fingerprint(
            "./bridge.json", Profile("two", profile_raw()))
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_runtime_permissions_and_metadata(self):
        with tempfile.TemporaryDirectory() as parent:
            state = os.path.join(parent, "state")
            profile = Profile("test", profile_raw())
            with mock.patch.dict(
                    os.environ, {"SSHBRIDGE_STATE_DIR": state}, clear=False):
                endpoint = BrokerEndpoint("/tmp/bridge.json", profile)
                metadata = endpoint.write_metadata(123, "instance")
                mode = stat.S_IMODE(os.stat(state).st_mode)
                metadata_mode = stat.S_IMODE(
                    os.stat(endpoint.metadata_path).st_mode)
                self.assertEqual(mode, 0o700)
                self.assertEqual(metadata_mode, 0o600)
                self.assertEqual(
                    endpoint.read_metadata()["instance_id"], "instance")
                self.assertEqual(
                    metadata["profile_fingerprint"], endpoint.fingerprint)

    def test_rejects_open_runtime_directory(self):
        with tempfile.TemporaryDirectory() as parent:
            state = os.path.join(parent, "state")
            os.mkdir(state, 0o755)
            os.chmod(state, 0o755)
            profile = Profile("test", profile_raw())
            with mock.patch.dict(
                    os.environ, {"SSHBRIDGE_STATE_DIR": state}, clear=False):
                with self.assertRaises(BridgeError) as caught:
                    BrokerEndpoint("/tmp/bridge.json", profile)
            self.assertEqual(caught.exception.code, "BROKER_UNAVAILABLE")


class TestBrokerLauncher(unittest.TestCase):
    def test_python_mode_uses_module_entrypoint(self):
        argv = _broker_launch_argv(
            "/tmp/bridge.json", "test",
            frozen=False, executable="/usr/local/bin/python3")
        self.assertEqual(argv[:4], [
            "/usr/local/bin/python3", "-m", "sshbridge.broker", "--serve"])
        self.assertEqual(argv[-4:], [
            "--config", "/tmp/bridge.json", "--profile", "test"])

    def test_frozen_mode_uses_bundled_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = os.path.join(directory, "Remote Explorer")
            helper = os.path.join(directory, "sshbridge_broker")
            for path in (executable, helper):
                with open(path, "w", encoding="utf-8") as stream:
                    stream.write("#!/bin/sh\n")
                os.chmod(path, 0o700)
            argv = _broker_launch_argv(
                "/tmp/bridge.json", "test",
                frozen=True, executable=executable)
        self.assertEqual(argv[0], helper)
        self.assertNotIn("-m", argv)

    def test_frozen_mode_rejects_missing_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = os.path.join(directory, "Remote Explorer")
            with open(executable, "w", encoding="utf-8") as stream:
                stream.write("#!/bin/sh\n")
            with self.assertRaises(BridgeError) as caught:
                _broker_launch_argv(
                    "/tmp/bridge.json", "test",
                    frozen=True, executable=executable)
        self.assertEqual(caught.exception.code, "BROKER_UNAVAILABLE")

    def test_frozen_mode_rejects_symlink_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = os.path.join(directory, "Remote Explorer")
            target = os.path.join(directory, "target")
            helper = os.path.join(directory, "sshbridge_broker")
            for path in (executable, target):
                with open(path, "w", encoding="utf-8") as stream:
                    stream.write("#!/bin/sh\n")
                os.chmod(path, 0o700)
            os.symlink(target, helper)
            with self.assertRaises(BridgeError) as caught:
                _broker_launch_argv(
                    "/tmp/bridge.json", "test",
                    frozen=True, executable=executable)
        self.assertEqual(caught.exception.code, "BROKER_UNAVAILABLE")


class TestConnectionFailureClassification(unittest.TestCase):
    def test_sftp_authentication_failure_is_not_retryable(self):
        error = BridgeError(
            "SSH_ERROR",
            "sftp connection closed unexpectedly: Permission denied")
        self.assertFalse(_is_connect_failure(error))

    def test_sftp_reset_is_retryable(self):
        error = BridgeError(
            "SSH_ERROR", "sftp connection closed: Connection reset")
        self.assertTrue(_is_connect_failure(error))

    def test_exec_authentication_failure_is_transport_failure(self):
        self.assertTrue(is_ssh_transport_failure(
            "user@example: Permission denied (publickey)."))

    def test_remote_exit_255_text_is_not_transport_failure(self):
        self.assertFalse(is_ssh_transport_failure(
            "application intentionally returned 255"))


class TestBrokerProtocol(unittest.TestCase):
    def exchange(self, request):
        with tempfile.TemporaryDirectory() as state:
            with mock.patch.dict(
                    os.environ, {"SSHBRIDGE_STATE_DIR": state}, clear=False):
                profile = Profile("test", profile_raw())
                server = BrokerServer("/tmp/bridge.json", profile)
                client_socket, server_socket = socket.socketpair()
                thread = threading.Thread(
                    target=server._handle_client, args=(server_socket,))
                thread.start()
                client_socket.sendall(
                    (json.dumps(request) + "\n").encode("utf-8"))
                response = bytearray()
                while not response.endswith(b"\n"):
                    response.extend(client_socket.recv(4096))
                thread.join(timeout=2)
                client_socket.close()
                server.close()
                return json.loads(response.decode("utf-8")), server

    def test_ping_echoes_request_and_instance(self):
        request = {
            "version": 1,
            "request_id": "request-one",
            "profile": profile_fingerprint(
                "/tmp/bridge.json", Profile("test", profile_raw())),
            "op": "ping",
            "args": {},
        }
        response, server = self.exchange(request)
        self.assertTrue(response["ok"])
        self.assertEqual(response["request_id"], "request-one")
        self.assertEqual(response["instance_id"], server.instance_id)

    def test_profile_mismatch_is_rejected(self):
        response, _ = self.exchange({
            "version": 1,
            "request_id": "request-two",
            "profile": "wrong-profile",
            "op": "ping",
            "args": {},
        })
        self.assertFalse(response["ok"])
        self.assertEqual(
            response["error"]["code"], "BROKER_PROFILE_MISMATCH")

    def test_protocol_version_is_rejected(self):
        response, _ = self.exchange({
            "version": 999,
            "request_id": "request-three",
            "profile": "irrelevant",
            "op": "ping",
            "args": {},
        })
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "BROKER_UNAVAILABLE")

    def test_profile_lock_allows_only_one_owner(self):
        with tempfile.TemporaryDirectory() as state:
            with mock.patch.dict(
                    os.environ, {"SSHBRIDGE_STATE_DIR": state}, clear=False):
                profile = Profile("test", profile_raw())
                first = BrokerServer("/tmp/bridge.json", profile)
                second = BrokerServer("/tmp/bridge.json", profile)
                try:
                    first._acquire_singleton()
                    with self.assertRaises(BridgeError) as caught:
                        second._acquire_singleton()
                    self.assertEqual(
                        caught.exception.code, "BROKER_UNAVAILABLE")
                    self.assertIsNone(second.lock_file)
                finally:
                    first.close()
                    second.close()


class FakeSyncClient:
    def __init__(self, features=None):
        self.features = features if features is not None else [
            "sftp", "exec", "rsync"]
        self.requests = []

    def ensure_started(self):
        return {"features": self.features}

    def request(self, operation, arguments, timeout=None):
        self.requests.append((operation, arguments, timeout))
        return {
            "job_id": arguments.get("job_id", "job-1"),
            "direction": arguments.get("direction", "push"),
            "state": "queued",
        }


class FakeRsyncManager:
    def __init__(self):
        self.calls = []
        self.invalidated = 0
        self.closed = False

    def start(self, direction, sources, destination):
        self.calls.append(
            ("start", direction, list(sources), destination))
        return {
            "job_id": "job-1",
            "direction": direction,
            "state": "queued",
        }

    def status(self, job_id):
        self.calls.append(("status", job_id))
        return {"job_id": job_id, "state": "running"}

    def cancel(self, job_id):
        self.calls.append(("cancel", job_id))
        return {"job_id": job_id, "state": "cancelled"}

    def snapshot(self):
        return {
            "sync_active": 1,
            "sync_queued": 2,
            "sync_history": 3,
            "rsync_capability": {
                "state": "available",
                "local_version": "3.2.7",
                "remote_version": "3.2.7",
                "reason": None,
            },
        }

    def invalidate_capabilities(self):
        self.invalidated += 1

    def close(self):
        self.closed = True


class TestBrokerSyncRouting(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_patch = mock.patch.dict(
            os.environ,
            {"SSHBRIDGE_STATE_DIR": self.temp.name},
            clear=False)
        self.state_patch.start()
        self.profile = Profile("test", profile_raw())
        self.endpoint = BrokerEndpoint("/tmp/bridge.json", self.profile)
        self.manager = FakeRsyncManager()
        self.rsync_factory = mock.Mock(return_value=self.manager)
        patcher = mock.patch(
            "sshbridge.broker.RsyncManager",
            self.rsync_factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = BrokerState(
            self.profile, self.endpoint, "instance")
        self.state.ensure_ready = mock.Mock()

    def tearDown(self):
        self.state.close()
        self.state_patch.stop()
        self.temp.cleanup()

    def test_snapshot_exposes_features_and_sync_state(self):
        snapshot = self.state.snapshot()
        self.assertEqual(
            snapshot["features"], ["sftp", "exec", "rsync"])
        self.assertEqual(snapshot["sync_active"], 1)
        self.assertEqual(snapshot["sync_queued"], 2)
        self.assertEqual(snapshot["sync_history"], 3)
        self.assertEqual(
            snapshot["rsync_capability"]["state"], "available")

    def test_sync_operations_route_to_manager(self):
        started = self.state.run("sync_start", {
            "direction": "push",
            "sources": ["one", "two"],
            "destination": "/remote",
        })
        self.assertEqual(started["job_id"], "job-1")
        self.state.ensure_ready.assert_called_once_with()
        self.assertEqual(self.manager.calls[-1], (
            "start", "push", ["one", "two"], "/remote"))

        self.assertEqual(
            self.state.run(
                "sync_status", {"job_id": "job-1"})["state"],
            "running")
        self.assertEqual(
            self.state.run(
                "sync_cancel", {"job_id": "job-1"})["state"],
            "cancelled")

    def test_rsync_probe_callback_adapts_exec_arguments(self):
        remote_probe = self.rsync_factory.call_args.kwargs["remote_probe"]
        self.state._execute = mock.Mock(return_value={"exit_code": 0})
        result = remote_probe("rsync --version")
        self.assertEqual(result, {"exit_code": 0})
        self.state._execute.assert_called_once_with(
            self.profile,
            "rsync --version",
            self.profile.root,
            self.profile.connect_timeout)

    def test_running_sync_does_not_hold_sftp_or_exec_capacity(self):
        class BlockingProcess:
            def __init__(self):
                self.stdout = io.BytesIO()
                self.stderr = io.BytesIO()
                self.returncode = None
                self.pid = 50001
                self.done = threading.Event()

            def wait(self):
                self.done.wait(2)
                return self.returncode

            def finish(self, returncode=0):
                self.returncode = returncode
                self.done.set()

        process = BlockingProcess()
        spawned = threading.Event()

        def popen(*args, **kwargs):
            _ = (args, kwargs)
            spawned.set()
            return process

        capability = SimpleNamespace(
            returncode=0,
            stdout=(
                "rsync version 3.2.7\n"
                "  -s, --secluded-args\n"),
            stderr="")
        exec_result = {
            "command": "true",
            "cwd": self.profile.root,
            "stdout": (
                "rsync version 3.2.7\n"
                "  -s, --secluded-args\n"),
            "stderr": "",
            "exit_code": 0,
            "timed_out": False,
            "remote_may_still_be_running": False,
        }
        session = SimpleNamespace(_closed=False)

        def dispatch_sftp(operation, arguments, active_session):
            _ = active_session
            if operation == "stat":
                return {
                    "path": arguments["path"],
                    "real_path": self.profile.root,
                    "type": "dir",
                }
            return {"op": operation}

        self.state.rsync.close()
        self.state.transport.is_alive = mock.Mock(return_value=True)
        self.state.state = "READY"
        self.state._session_unlocked = mock.Mock(return_value=session)
        self.state._dispatch_sftp = mock.Mock(side_effect=dispatch_sftp)
        self.state.rsync = RsyncManager(
            self.profile,
            self.state.transport,
            remote_stat=self.state._rsync_remote_stat,
            remote_probe=self.state._rsync_remote_probe,
            popen_factory=popen,
            run_local=lambda *args, **kwargs: capability,
            terminate_process=lambda active: active.finish(-15))

        with tempfile.NamedTemporaryFile() as source:
            with mock.patch(
                    "sshbridge.broker.run_exec",
                    return_value=exec_result):
                started = self.state.run("sync_start", {
                    "direction": "push",
                    "sources": [source.name],
                    "destination": "/",
                })
                self.assertTrue(spawned.wait(1))
                self.assertEqual(
                    self.state.rsync.status(started["job_id"])["state"],
                    "running")
                self.assertEqual(
                    self.state.run("list_dir", {"path": "/"})["op"],
                    "list_dir")
                self.assertEqual(
                    self.state.run("exec", {"command": "true"})["exit_code"],
                    0)

        process.finish()
        for _ in range(100):
            status = self.state.rsync.status(started["job_id"])
            if status["state"] == "succeeded":
                break
            threading.Event().wait(0.01)
        self.assertEqual(status["state"], "succeeded")

    def test_reconnect_invalidates_capability_and_close_stops_manager(self):
        self.state.transport.stop = mock.Mock()
        self.state.reconnect()
        self.assertEqual(self.manager.invalidated, 1)
        self.state.close()
        self.assertTrue(self.manager.closed)


class TestSyncCli(unittest.TestCase):
    def test_parser_accepts_sync_commands(self):
        parser = cli.build_parser()
        push = parser.parse_args([
            "sync", "push", "one", "two", "--to", "/remote"])
        self.assertEqual(push.sync_cmd, "push")
        self.assertEqual(push.sources, ["one", "two"])
        self.assertEqual(push.destination, "/remote")

        pull = parser.parse_args([
            "sync", "pull", "/remote/file", "--to", "/local"])
        self.assertEqual(pull.sync_cmd, "pull")
        self.assertEqual(pull.source, "/remote/file")
        self.assertEqual(pull.destination, "/local")

        status = parser.parse_args(["sync", "status", "job-1"])
        self.assertEqual(status.sync_cmd, "status")
        self.assertEqual(status.job_id, "job-1")

    def test_sync_dispatch_builds_broker_requests(self):
        client = FakeSyncClient()
        cases = [
            (
                SimpleNamespace(
                    sync_cmd="push", sources=["one", "two"],
                    destination="/remote"),
                "sync_start",
                {
                    "direction": "push",
                    "sources": ["one", "two"],
                    "destination": "/remote",
                },
            ),
            (
                SimpleNamespace(
                    sync_cmd="pull", source="/remote/file",
                    destination="/local"),
                "sync_start",
                {
                    "direction": "pull",
                    "sources": ["/remote/file"],
                    "destination": "/local",
                },
            ),
            (
                SimpleNamespace(sync_cmd="status", job_id="job-1"),
                "sync_status",
                {"job_id": "job-1"},
            ),
            (
                SimpleNamespace(sync_cmd="cancel", job_id="job-1"),
                "sync_cancel",
                {"job_id": "job-1"},
            ),
        ]
        for arguments, operation, payload in cases:
            with self.subTest(operation=operation):
                cli._broker_sync(arguments, client)
                self.assertEqual(
                    client.requests[-1][:2], (operation, payload))

    def test_sync_requires_new_broker_feature(self):
        client = FakeSyncClient(features=["sftp", "exec"])
        arguments = SimpleNamespace(
            sync_cmd="push", sources=["one"], destination="/remote")
        with self.assertRaises(BridgeError) as caught:
            cli._broker_sync(arguments, client)
        self.assertEqual(
            caught.exception.code, "BROKER_RESTART_REQUIRED")


if __name__ == "__main__":
    unittest.main()
