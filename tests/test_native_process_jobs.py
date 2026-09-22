"""Exercise job ownership with native Python children on Windows and POSIX."""

from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from sshbridge.exec_jobs import ExecJobManager


class TestNativeProcessJobs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sshbridge-native-")
        self.cwd = Path(self.temp.name) / "workspace with spaces 中文"
        self.cwd.mkdir()
        self.processes = []
        self.transport_errors = []

        def spawn(source, _remote_cwd):
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", source], cwd=str(self.cwd),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0)
            self.processes.append(process)
            return process

        self.manager = ExecJobManager(
            max_concurrency=1, initial_concurrency=1, queue_limit=2,
            queue_timeout=15, output_limit_bytes=65536, job_ttl=30,
            max_jobs=8, cancel_grace=0.2,
            prepare_exec=lambda: 1, process_factory=spawn,
            transport_failure_callback=self.transport_errors.append)

    def tearDown(self):
        try:
            self.manager.close()
        finally:
            for process in self.processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                for pipe in (process.stdout, process.stderr):
                    pipe.close()
            self.temp.cleanup()

    def start(self, source, timeout=15):
        return self.manager.start({
            "command": source, "cwd": "/", "real_cwd": "/unused",
            "timeout": timeout,
        })["job_id"]

    def wait_for(self, job_id, predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = self.manager.status(job_id)
            if predicate(status):
                return status
            time.sleep(0.02)
        self.fail("Native process did not reach expected state: %r" % status)

    @staticmethod
    def stream(status, name):
        return "".join(event["text"] for event in status["events"] if event["stream"] == name)

    def test_unicode_output_and_native_working_directory(self):
        job_id = self.start(
            "import os, sys; "
            "sys.stdout.buffer.write(os.path.basename(os.getcwd()).encode('utf-8')); "
            "sys.stderr.buffer.write('错误输出'.encode('utf-8'))")
        status = self.wait_for(job_id, lambda item: item["state"] == "EXITED")
        self.assertEqual(status["exit_code"], 0)
        self.assertEqual(self.stream(status, "stdout"), self.cwd.name)
        self.assertEqual(self.stream(status, "stderr"), "错误输出")
        self.assertIsNotNone(self.processes[0].poll())

    def test_nonzero_exit_is_not_a_transport_failure(self):
        job_id = self.start("import sys; sys.exit(7)")
        status = self.wait_for(job_id, lambda item: item["state"] == "EXITED")
        self.assertEqual(status["exit_code"], 7)
        self.assertEqual(self.transport_errors, [])

    def test_cancel_terminates_native_child_and_preserves_output(self):
        job_id = self.start("import time; print('ready', flush=True); time.sleep(60)")
        self.wait_for(job_id, lambda item: "ready" in self.stream(item, "stdout"))
        self.manager.cancel(job_id)
        status = self.wait_for(job_id, lambda item: item["state"] == "CANCELED")
        self.assertTrue(status["remote_termination_unknown"])
        self.assertIn("ready", self.stream(status, "stdout"))
        self.assertIsNotNone(self.processes[0].poll())
        self.assertTrue(self.manager.cancel(job_id)["already_terminal"])

    def test_timeout_terminates_native_child(self):
        job_id = self.start("import time; time.sleep(60)", timeout=0.3)
        status = self.wait_for(job_id, lambda item: item["state"] == "TIMED_OUT")
        self.assertTrue(status["remote_termination_unknown"])
        self.assertIsNotNone(self.processes[0].poll())
