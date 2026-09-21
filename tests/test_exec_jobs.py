import subprocess
import time
import unittest

from sshbridge.config import Profile
from sshbridge.errors import BridgeError
from sshbridge.exec_jobs import ExecJobManager
from sshbridge.ops import normalize_exec_request, op_exec


def profile_raw():
    return {
        "host": "example.test",
        "port": 2222,
        "user": "developer",
        "root": "/srv/workspace",
    }


class ExecJobTestCase(unittest.TestCase):
    def setUp(self):
        self.managers = []
        self.started_commands = []
        self.transport_errors = []

    def tearDown(self):
        for manager in self.managers:
            manager.close()

    def manager(
            self, concurrency=1, queue_limit=2, output_limit=1024 * 1024,
            queue_timeout=2, job_ttl=30, max_jobs=16,
            process_factory=None):
        def local_process(command, _cwd):
            self.started_commands.append(command)
            return subprocess.Popen(
                ["/bin/sh", "-c", command],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )

        manager = ExecJobManager(
            max_concurrency=concurrency,
            initial_concurrency=concurrency,
            queue_limit=queue_limit,
            queue_timeout=queue_timeout,
            output_limit_bytes=output_limit,
            job_ttl=job_ttl,
            max_jobs=max_jobs,
            cancel_grace=0.05,
            prepare_exec=lambda: concurrency,
            process_factory=process_factory or local_process,
            transport_failure_callback=self.transport_errors.append,
        )
        self.managers.append(manager)
        return manager

    @staticmethod
    def request(command, timeout=2):
        return {
            "command": command,
            "cwd": "/",
            "real_cwd": "/srv/workspace",
            "timeout": timeout,
        }

    def wait_for(self, manager, job_id, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        latest = None
        while time.monotonic() < deadline:
            latest = manager.status(job_id)
            if predicate(latest):
                return latest
            time.sleep(0.01)
        self.fail("job condition not reached; latest=%r" % latest)

    def test_incremental_output_and_terminal_state(self):
        manager = self.manager()
        started_at = time.monotonic()
        start = manager.start(self.request(
            "printf first; sleep 0.2; printf second"))
        self.assertLess(time.monotonic() - started_at, 0.15)
        self.assertEqual(start["state"], "QUEUED")
        self.assertNotIn("command", start)
        self.assertNotIn("real_cwd", start)

        first = self.wait_for(
            manager, start["job_id"],
            lambda status: any(
                event["text"] == "first" for event in status["events"]))
        self.assertEqual(first["state"], "RUNNING")
        cursor = first["next_cursor"]

        final = self.wait_for(
            manager, start["job_id"],
            lambda status: status["state"] == "EXITED")
        self.assertEqual(final["exit_code"], 0)
        page = manager.status(start["job_id"], cursor=cursor)
        self.assertEqual(
            "".join(event["text"] for event in page["events"]), "second")
        self.assertFalse(page["has_more"])

    def test_queue_limit_and_queued_cancel_never_start_process(self):
        manager = self.manager(concurrency=1, queue_limit=1)
        first = manager.start(self.request("sleep 0.4"))
        self.wait_for(
            manager, first["job_id"],
            lambda status: status["state"] == "RUNNING")
        queued = manager.start(self.request("printf should-not-run"))
        self.assertEqual(
            manager.status(queued["job_id"])["state"], "QUEUED")
        with self.assertRaises(BridgeError) as caught:
            manager.start(self.request("true"))
        self.assertEqual(caught.exception.code, "EXEC_QUEUE_FULL")

        canceled = manager.cancel(queued["job_id"])
        self.assertEqual(canceled["state"], "CANCELED")
        self.assertFalse(canceled["remote_termination_unknown"])
        self.assertNotIn("printf should-not-run", self.started_commands)

    def test_queue_timeout_fails_without_starting_process(self):
        manager = self.manager(
            concurrency=1, queue_limit=1, queue_timeout=0.05)
        running = manager.start(self.request("sleep 0.4"))
        self.wait_for(
            manager, running["job_id"],
            lambda status: status["state"] == "RUNNING")
        queued = manager.start(self.request("printf too-late"))
        time.sleep(0.08)
        expired = manager.status(queued["job_id"])
        self.assertEqual(expired["state"], "FAILED")
        self.assertEqual(
            expired["error"]["code"], "EXEC_QUEUE_TIMEOUT")
        self.assertNotIn("printf too-late", self.started_commands)

    def test_running_cancel_is_idempotent_and_marks_remote_unknown(self):
        manager = self.manager()
        start = manager.start(self.request("sleep 5"))
        self.wait_for(
            manager, start["job_id"],
            lambda status: status["state"] == "RUNNING")
        canceled = manager.cancel(start["job_id"])
        self.assertEqual(canceled["state"], "CANCELED")
        self.assertTrue(canceled["remote_termination_unknown"])
        self.assertIn("remote process may still be running", canceled["note"])
        repeated = manager.cancel(start["job_id"])
        self.assertTrue(repeated["already_terminal"])
        self.assertEqual(repeated["state"], "CANCELED")

    def test_timeout_preserves_output_and_marks_remote_unknown(self):
        manager = self.manager()
        start = manager.start(self.request(
            "printf before-timeout; sleep 5", timeout=0.05))
        final = self.wait_for(
            manager, start["job_id"],
            lambda status: status["state"] == "TIMED_OUT")
        self.assertTrue(final["remote_termination_unknown"])
        self.assertEqual(
            "".join(event["text"] for event in final["events"]),
            "before-timeout")

    def test_output_limit_keeps_tail_and_cursor_reports_truncation(self):
        manager = self.manager(output_limit=5)
        start = manager.start(self.request("printf 123456789"))
        final = self.wait_for(
            manager, start["job_id"],
            lambda status: status["state"] == "EXITED")
        self.assertEqual(
            "".join(event["text"] for event in final["events"]), "56789")
        self.assertTrue(final["truncated_before"])
        self.assertTrue(final["output_truncated"])
        self.assertLessEqual(
            sum(len(event["text"].encode("utf-8"))
                for event in final["events"]),
            5)

    def test_status_paginates_streams_by_cursor_and_byte_limit(self):
        manager = self.manager()
        start = manager.start(self.request(
            "printf abcdef; printf err >&2"))
        self.wait_for(
            manager, start["job_id"],
            lambda status: status["state"] == "EXITED")
        page = manager.status(start["job_id"], max_bytes=4)
        self.assertEqual(
            sum(len(event["text"].encode("utf-8"))
                for event in page["events"]),
            4)
        self.assertTrue(page["has_more"])
        rest = manager.status(
            start["job_id"], cursor=page["next_cursor"], max_bytes=64)
        combined = page["events"] + rest["events"]
        self.assertEqual(
            "".join(event["text"] for event in combined
                    if event["stream"] == "stdout"),
            "abcdef")
        self.assertEqual(
            "".join(event["text"] for event in combined
                    if event["stream"] == "stderr"),
            "err")

    def test_exit_255_only_fails_for_transport_marker(self):
        manager = self.manager()
        ordinary = manager.start(self.request(
            "printf application-error >&2; exit 255"))
        ordinary_status = self.wait_for(
            manager, ordinary["job_id"],
            lambda status: status["state"] in ("EXITED", "FAILED"))
        self.assertEqual(ordinary_status["state"], "EXITED")
        self.assertEqual(ordinary_status["exit_code"], 255)

        failed = manager.start(self.request(
            "printf 'Permission denied' >&2; exit 255"))
        failed_status = self.wait_for(
            manager, failed["job_id"],
            lambda status: status["state"] in ("EXITED", "FAILED"))
        self.assertEqual(failed_status["state"], "FAILED")
        self.assertEqual(failed_status["error"]["code"], "SSH_ERROR")
        self.assertEqual(len(self.transport_errors), 1)

    def test_run_sync_uses_scheduler_and_does_not_retain_job(self):
        manager = self.manager()
        profile = Profile("test", profile_raw())
        result = op_exec(
            profile, "printf out; printf err >&2",
            exec_runner=manager.run_sync)
        self.assertEqual(result["stdout"], "out")
        self.assertEqual(result["stderr"], "err")
        self.assertFalse(result["timed_out"])
        self.assertEqual(manager.snapshot()["retained_exec_jobs"], 0)

    def test_unknown_job_and_invalid_status_parameters(self):
        manager = self.manager()
        with self.assertRaises(BridgeError) as missing:
            manager.status("missing")
        self.assertEqual(missing.exception.code, "EXEC_JOB_NOT_FOUND")
        start = manager.start(self.request("true"))
        for cursor, max_bytes in ((-1, 1), (True, 1), (0, 0), (0, 65537)):
            with self.subTest(cursor=cursor, max_bytes=max_bytes):
                with self.assertRaises(BridgeError) as invalid:
                    manager.status(
                        start["job_id"], cursor=cursor, max_bytes=max_bytes)
                self.assertEqual(invalid.exception.code, "INVALID_ARG")

    def test_terminal_job_expires_after_ttl(self):
        manager = self.manager(job_ttl=0.05)
        start = manager.start(self.request("true"))
        self.wait_for(
            manager, start["job_id"],
            lambda status: status["state"] == "EXITED")
        time.sleep(0.08)
        with self.assertRaises(BridgeError) as expired:
            manager.status(start["job_id"])
        self.assertEqual(expired.exception.code, "EXEC_JOB_NOT_FOUND")

    def test_close_cancels_queued_and_running_jobs(self):
        manager = self.manager(concurrency=1, queue_limit=1)
        running = manager.start(self.request("sleep 5"))
        self.wait_for(
            manager, running["job_id"],
            lambda status: status["state"] == "RUNNING")
        queued = manager.start(self.request("true"))
        summary = manager.close()
        self.assertEqual(summary["canceled_exec_jobs"], 2)
        self.assertTrue(summary["remote_termination_unknown"])
        self.assertEqual(
            manager.status(running["job_id"])["state"], "CANCELED")
        self.assertEqual(
            manager.status(queued["job_id"])["state"], "CANCELED")
        with self.assertRaises(BridgeError) as closed:
            manager.start(self.request("true"))
        self.assertEqual(closed.exception.code, "BROKER_UNAVAILABLE")


class TestNormalizeExecRequest(unittest.TestCase):
    def test_maps_virtual_cwd_and_applies_default_timeout(self):
        profile = Profile("test", profile_raw())
        request = normalize_exec_request(
            profile, "  printf ok  ", "/src/../build")
        self.assertEqual(request, {
            "command": "printf ok",
            "cwd": "/src/../build",
            "real_cwd": "/srv/workspace/build",
            "timeout": 60,
        })

    def test_rejects_empty_command_and_boolean_timeout(self):
        profile = Profile("test", profile_raw())
        for command, timeout in (("", None), ("true", True)):
            with self.subTest(command=command, timeout=timeout):
                with self.assertRaises(BridgeError) as caught:
                    normalize_exec_request(
                        profile, command, timeout=timeout)
                self.assertEqual(caught.exception.code, "INVALID_ARG")


if __name__ == "__main__":
    unittest.main()
