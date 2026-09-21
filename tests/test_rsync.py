import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from sshbridge.config import Profile
from sshbridge.errors import BridgeError
from sshbridge.rsync import (
    MAX_HISTORY, MAX_OUTPUT_BYTES, MAX_QUEUE, RsyncManager)


def profile_raw(**overrides):
    raw = {
        "host": "legacy-host",
        "port": 2222,
        "user": "developer",
        "root": "/srv/workspace",
        "rsync_bin": sys.executable,
        "remote_rsync_bin": "/usr/local/bin/rsync",
        "ssh_args": ["-o", "ProxyJump=jump-host"],
    }
    raw.update(overrides)
    return raw


class FakeTransport:
    def __init__(self, multiplexing=True, alive=True):
        self.multiplexing = multiplexing
        self.alive = alive
        self.control_path = "/tmp/sshbridge-control"

    def is_alive(self):
        return self.alive


class FakeProcess:
    _next_pid = 50000

    def __init__(self, stdout=b"", stderr=b"", returncode=0, blocked=False):
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = None if blocked else returncode
        self._result = returncode
        self._done = threading.Event()
        if not blocked:
            self._done.set()
        self.pid = FakeProcess._next_pid
        FakeProcess._next_pid += 1

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired(["rsync"], timeout)
        return self.returncode

    def poll(self):
        return self.returncode

    def finish(self, returncode=None):
        self.returncode = self._result if returncode is None else returncode
        self._done.set()


class FakePopen:
    def __init__(self, *processes):
        self.processes = list(processes)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        if not self.processes:
            raise AssertionError("unexpected Popen call")
        return self.processes.pop(0)


def capability_result(protect_args=True):
    help_text = "Usage: rsync\n"
    if protect_args:
        help_text += "  -s, --protect-args\n"
    return SimpleNamespace(
        returncode=0,
        stdout="rsync version 3.2.7\n" + help_text,
        stderr="")


def remote_result(protect_args=True, exit_code=0, stderr=""):
    help_text = "Usage: rsync\n"
    if protect_args:
        help_text += "  -s, --protect-args\n"
    return {
        "exit_code": exit_code,
        "stdout": "rsync version 3.2.7\n" + help_text,
        "stderr": stderr,
        "timed_out": False,
    }


class RsyncManagerCase(unittest.TestCase):
    def setUp(self):
        self.profile = Profile("test", profile_raw())
        self.transport = FakeTransport()
        self.remote_calls = []
        self.transport_errors = []

    def remote_stat(self, path):
        self.remote_calls.append(("stat", path))
        if path.endswith("/missing"):
            raise BridgeError("NOT_FOUND", "missing")
        entry_type = "file" if path.endswith(".txt") else "dir"
        return {
            "path": path,
            "real_path": "/srv/workspace" + (
                "" if path == "/" else path),
            "type": entry_type,
        }

    def remote_probe(self, command):
        self.remote_calls.append(("probe", command))
        return remote_result()

    def manager(self, popen, run_local=None, transport=None,
                terminate_process=None):
        return RsyncManager(
            self.profile,
            transport or self.transport,
            remote_stat=self.remote_stat,
            remote_probe=self.remote_probe,
            on_transport_error=self.transport_errors.append,
            popen_factory=popen,
            run_local=run_local or (
                lambda *args, **kwargs: capability_result()),
            terminate_process=terminate_process)

    def wait_state(self, manager, job_id, states, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = manager.status(job_id)
            if result["state"] in states:
                return result
            time.sleep(0.01)
        self.fail("job did not reach %s: %s" % (
            sorted(states), manager.status(job_id)))

    def test_push_builds_fixed_argv_and_parses_stats(self):
        output = (
            b"Number of regular files transferred: 2\n"
            b"Total transferred file size: 1,234 bytes\n")
        popen = FakePopen(FakeProcess(stdout=output))
        with tempfile.TemporaryDirectory() as directory:
            first = os.path.join(directory, "one file.txt")
            second = os.path.join(directory, "-two")
            with open(first, "w", encoding="utf-8") as stream:
                stream.write("one")
            with open(second, "w", encoding="utf-8") as stream:
                stream.write("two")
            manager = self.manager(popen)
            try:
                started = manager.start(
                    "push", [first, second, first], "/output")
                result = self.wait_state(
                    manager, started["job_id"], {"succeeded"})
            finally:
                manager.close()

        self.assertEqual(result["source_count"], 2)
        self.assertEqual(result["files_transferred"], 2)
        self.assertEqual(result["bytes_transferred"], 1234)
        argv, kwargs = popen.calls[0]
        self.assertIn("--protect-args", argv)
        self.assertIn("--safe-links", argv)
        self.assertIn(
            "--rsync-path=/usr/local/bin/rsync", argv)
        separator = argv.index("--")
        self.assertEqual(
            argv[separator + 1:separator + 3], [first, second])
        self.assertEqual(
            argv[-1], "legacy-host:/srv/workspace/output")
        shell = argv[argv.index("-e") + 1]
        self.assertIn("-S /tmp/sshbridge-control", shell)
        self.assertIn("ProxyJump=jump-host", shell)
        self.assertNotIn(directory, shell)
        self.assertNotIn("/srv/workspace/output", shell)
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["env"]["LC_ALL"], "C")

    def test_pull_uses_canonical_remote_source(self):
        popen = FakePopen(FakeProcess())
        with tempfile.TemporaryDirectory() as destination:
            manager = self.manager(popen)
            try:
                started = manager.start(
                    "pull", ["/notes.txt"], destination)
                result = self.wait_state(
                    manager, started["job_id"], {"succeeded"})
            finally:
                manager.close()
        argv = popen.calls[0][0]
        separator = argv.index("--")
        self.assertEqual(
            argv[separator + 1],
            "legacy-host:/srv/workspace/notes.txt")
        self.assertEqual(argv[separator + 2], destination)
        self.assertEqual(result["direction"], "pull")

    def test_requires_protect_args_and_controlmaster(self):
        manager = self.manager(
            FakePopen(),
            run_local=lambda *args, **kwargs: capability_result(False))
        try:
            with tempfile.NamedTemporaryFile() as source:
                with self.assertRaises(BridgeError) as caught:
                    manager.start("push", [source.name], "/output")
            self.assertEqual(caught.exception.code, "RSYNC_UNAVAILABLE")
            self.assertEqual(caught.exception.details["stage"], "local")
            self.assertEqual(
                set(manager.snapshot()["rsync_capability"]),
                {"state", "local_version", "remote_version", "reason"})
        finally:
            manager.close()

        manager = self.manager(
            FakePopen(), transport=FakeTransport(multiplexing=False))
        try:
            with tempfile.NamedTemporaryFile() as source:
                with self.assertRaises(BridgeError) as caught:
                    manager.start("push", [source.name], "/output")
            self.assertEqual(
                caught.exception.code, "RSYNC_MULTIPLEX_REQUIRED")
        finally:
            manager.close()

    def test_rejects_unsafe_inputs_before_spawning(self):
        manager = self.manager(FakePopen())
        try:
            with tempfile.NamedTemporaryFile() as source:
                with self.assertRaises(BridgeError) as caught:
                    manager.start("push", [source.name], "/bad\npath")
                self.assertEqual(caught.exception.code, "INVALID_ARG")
                with self.assertRaises(BridgeError) as caught:
                    manager.start(
                        "push", [source.name], "/missing")
                self.assertEqual(caught.exception.code, "NOT_FOUND")
            with self.assertRaises(BridgeError) as caught:
                manager.start("pull", ["/notes.txt", "/more.txt"], "/tmp")
            self.assertEqual(caught.exception.code, "INVALID_ARG")
        finally:
            manager.close()

        unsafe_profile = Profile(
            "unsafe", profile_raw(host="host name"))
        unsafe = RsyncManager(
            unsafe_profile, self.transport,
            remote_stat=self.remote_stat,
            remote_probe=self.remote_probe,
            popen_factory=FakePopen(),
            run_local=lambda *args, **kwargs: capability_result())
        try:
            with tempfile.NamedTemporaryFile() as source:
                with self.assertRaises(BridgeError) as caught:
                    unsafe.start("push", [source.name], "/output")
            self.assertEqual(caught.exception.code, "RSYNC_UNSAFE_CONFIG")
        finally:
            unsafe.close()

    def test_single_worker_queue_limit_and_cancel(self):
        active = FakeProcess(blocked=True)
        queued = [FakeProcess() for _ in range(MAX_QUEUE)]
        popen = FakePopen(active, *queued)

        def terminate(process):
            process.finish(-15)

        with tempfile.NamedTemporaryFile() as source:
            manager = self.manager(
                popen, terminate_process=terminate)
            jobs = []
            try:
                first = manager.start(
                    "push", [source.name], "/output")
                self.wait_state(manager, first["job_id"], {"running"})
                jobs.append(first)
                for _ in range(MAX_QUEUE):
                    jobs.append(manager.start(
                        "push", [source.name], "/output"))
                with self.assertRaises(BridgeError) as caught:
                    manager.start("push", [source.name], "/output")
                self.assertEqual(caught.exception.code, "SYNC_QUEUE_FULL")
                cancelled = manager.cancel(first["job_id"])
                self.assertTrue(cancelled["cancel_requested"])
                finished = self.wait_state(
                    manager, first["job_id"], {"cancelled"})
                self.assertTrue(
                    finished["remote_termination_unknown"])
            finally:
                manager.close()

    def test_output_cap_history_and_missing_job(self):
        payload = b"x" * (MAX_OUTPUT_BYTES + 1024)
        processes = [
            FakeProcess(stdout=payload)
            for _ in range(MAX_HISTORY + 1)
        ]
        popen = FakePopen(*processes)
        with tempfile.NamedTemporaryFile() as source:
            manager = self.manager(popen)
            job_ids = []
            try:
                for _ in range(MAX_HISTORY + 1):
                    started = manager.start(
                        "push", [source.name], "/output")
                    job_ids.append(started["job_id"])
                    result = self.wait_state(
                        manager, started["job_id"], {"succeeded"})
                    self.assertTrue(result["output_truncated"])
                with self.assertRaises(BridgeError) as caught:
                    manager.status(job_ids[0])
                self.assertEqual(
                    caught.exception.code, "SYNC_JOB_NOT_FOUND")
                self.assertEqual(
                    manager.snapshot()["sync_history"], MAX_HISTORY)
            finally:
                manager.close()

    def test_transport_failure_marks_job_and_notifies_broker(self):
        popen = FakePopen(FakeProcess(
            stderr=b"ssh: connect to host legacy-host: Connection refused\n",
            returncode=255))
        with tempfile.NamedTemporaryFile() as source:
            manager = self.manager(popen)
            try:
                started = manager.start(
                    "push", [source.name], "/output")
                result = self.wait_state(
                    manager, started["job_id"], {"failed"})
            finally:
                manager.close()
        self.assertEqual(result["error"]["code"], "SSH_ERROR")
        self.assertEqual(len(self.transport_errors), 1)


if __name__ == "__main__":
    unittest.main()
