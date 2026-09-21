import unittest

from sshbridge.exec_client import _is_pre_auth_failure


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


if __name__ == "__main__":
    unittest.main()
