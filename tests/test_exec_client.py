import unittest
from unittest import mock

from sshbridge.config import Profile
from sshbridge.errors import BridgeError
from sshbridge.exec_client import (
    _is_pre_auth_failure, build_exec_argv, start_exec)


def profile():
    return Profile("test", {
        "host": "example.test",
        "user": "developer",
        "root": "/srv/workspace",
    })


class TestPreAuthFailureDetection(unittest.TestCase):
    def test_bytes_reset(self):
        self.assertTrue(_is_pre_auth_failure(
            b"kex_exchange_identification: read: Connection reset"))

    def test_str_reset(self):
        self.assertTrue(_is_pre_auth_failure(
            "kex_exchange_identification: read: Connection reset"))

    def test_refused(self):
        self.assertTrue(_is_pre_auth_failure(
            "ssh: connect to host 10.0.0.1 port 22: Connection refused"))

    def test_connect_timeout(self):
        self.assertTrue(_is_pre_auth_failure(
            "ssh: connect to host 10.0.0.1 port 22: Connection timed out"))

    def test_command_failure_not_retried(self):
        self.assertFalse(_is_pre_auth_failure("bash: foo: command not found"))

    def test_remote_exit_255_alone_not_retried(self):
        # a command that legitimately exits 255 must never be retried
        self.assertFalse(_is_pre_auth_failure("some remote error output"))

    def test_empty(self):
        self.assertFalse(_is_pre_auth_failure(b""))
        self.assertFalse(_is_pre_auth_failure(None))


class TestExecProcess(unittest.TestCase):
    def test_build_argv_quotes_cwd_but_preserves_shell_text(self):
        argv = build_exec_argv(
            profile(), "printf '$HOME'", "/srv/work space")
        self.assertEqual(
            argv[-1], "cd '/srv/work space' && printf '$HOME'")

    @mock.patch("sshbridge.exec_client.subprocess.Popen")
    def test_start_exec_uses_binary_pipes_and_no_local_shell(self, popen):
        process = object()
        popen.return_value = process
        result = start_exec(profile(), "printf ok", "/srv/workspace")
        self.assertIs(result, process)
        args, kwargs = popen.call_args
        self.assertEqual(args[0][-1], "cd /srv/workspace && printf ok")
        self.assertEqual(kwargs["stdin"], -3)
        self.assertEqual(kwargs["stdout"], -1)
        self.assertEqual(kwargs["stderr"], -1)
        self.assertEqual(kwargs["bufsize"], 0)
        self.assertNotIn("shell", kwargs)

    @mock.patch(
        "sshbridge.exec_client.subprocess.Popen",
        side_effect=FileNotFoundError)
    def test_start_exec_maps_missing_ssh_binary(self, _popen):
        with self.assertRaises(BridgeError) as caught:
            start_exec(profile(), "true", "/srv/workspace")
        self.assertEqual(caught.exception.code, "SSH_ERROR")


if __name__ == "__main__":
    unittest.main()
