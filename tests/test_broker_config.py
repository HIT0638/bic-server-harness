import json
import os
import socket
import stat
import tempfile
import threading
import unittest
from unittest import mock

from sshbridge.broker import BrokerServer
from sshbridge.broker_client import (
    EXEC_JOBS_CAPABILITY, BrokerClient, BrokerEndpoint, _broker_launch_argv,
    profile_fingerprint)
from sshbridge.config import Profile
from sshbridge.errors import BridgeError
from sshbridge.exec_client import is_ssh_transport_failure
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
        self.assertEqual(profile.connection_policy["exec_queue_limit"], 8)
        self.assertEqual(profile.connection_policy["exec_queue_timeout"], 60)
        self.assertEqual(
            profile.connection_policy["exec_output_limit_bytes"], 4194304)
        self.assertEqual(profile.connection_policy["exec_job_ttl"], 600)
        self.assertEqual(profile.connection_policy["exec_max_jobs"], 32)
        self.assertEqual(profile.connection_policy["connect_retries"], 0)
        self.assertTrue(profile.connection_policy["control_master"])

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
            {"exec_queue_limit": -1},
            {"exec_queue_limit": 65},
            {"exec_queue_limit": True},
            {"exec_queue_timeout": 0},
            {"exec_queue_timeout": 86401},
            {"exec_queue_timeout": True},
            {"exec_output_limit_bytes": 65535},
            {"exec_output_limit_bytes": 67108865},
            {"exec_output_limit_bytes": True},
            {"exec_job_ttl": 0},
            {"exec_job_ttl": 86401},
            {"exec_job_ttl": False},
            {"exec_max_jobs": 9},
            {"exec_max_jobs": 257},
            {"exec_max_jobs": True},
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


class TestBrokerCapabilities(unittest.TestCase):
    def test_requires_capability_from_live_broker_status(self):
        client = object.__new__(BrokerClient)
        client.ping = mock.Mock(return_value={
            "capabilities": [EXEC_JOBS_CAPABILITY],
        })
        result = client.require_capability(EXEC_JOBS_CAPABILITY)
        self.assertIn(EXEC_JOBS_CAPABILITY, result["capabilities"])

    def test_missing_capability_requires_explicit_broker_restart(self):
        client = object.__new__(BrokerClient)
        client.ping = mock.Mock(return_value={"capabilities": []})
        with self.assertRaises(BridgeError) as caught:
            client.require_capability(EXEC_JOBS_CAPABILITY)
        self.assertEqual(
            caught.exception.code, "BROKER_RESTART_REQUIRED")
        self.assertIn(EXEC_JOBS_CAPABILITY, caught.exception.message)


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
        self.assertIn(
            EXEC_JOBS_CAPABILITY,
            response["result"]["capabilities"])

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


if __name__ == "__main__":
    unittest.main()
