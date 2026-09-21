"""Optional broker-owned Rsync transfer jobs."""

import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections import deque

from .errors import BridgeError
from .exec_client import is_ssh_transport_failure


MAX_QUEUE = 8
MAX_HISTORY = 32
MAX_OUTPUT_BYTES = 256 * 1024
MAX_SOURCES = 128
CANCEL_GRACE_SECONDS = 3

_TERMINAL_STATES = frozenset(("succeeded", "failed", "cancelled"))
_SAFE_HOST = re.compile(r"[A-Za-z0-9._-]+\Z")
_STATS_FILES = re.compile(
    r"^Number of regular files transferred:\s*([0-9,]+)\s*$",
    re.MULTILINE)
_STATS_BYTES = re.compile(
    r"^Total transferred file size:\s*([0-9,]+)(?:\s+bytes)?\s*$",
    re.MULTILINE)


class RsyncManager:
    """Validate, queue, run, inspect, and cancel one-at-a-time Rsync jobs."""

    def __init__(
            self, profile, transport, remote_stat, remote_probe,
            on_transport_error=None, popen_factory=None, run_local=None,
            terminate_process=None, clock=None):
        self.profile = profile
        self.transport = transport
        self._remote_stat = remote_stat
        self._remote_probe = remote_probe
        self._on_transport_error = on_transport_error
        self._popen = popen_factory or subprocess.Popen
        self._run_local = run_local or subprocess.run
        self._terminate_process = (
            terminate_process or _terminate_process_group)
        self._clock = clock or time.time

        self._condition = threading.Condition()
        self._capability_lock = threading.Lock()
        self._jobs = {}
        self._queue = deque()
        self._active_job_id = None
        self._closed = False
        self._resolved_rsync_bin = None
        self._capability = _unknown_capability()
        self._capability_stage = None
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="sshbridge-rsync-worker",
            daemon=True)
        self._worker.start()

    def start(self, direction, sources, destination):
        self._require_capability()
        normalized_sources, normalized_destination = \
            self._validate_request(direction, sources, destination)
        argv = self._build_argv(
            direction, normalized_sources, normalized_destination)
        now = self._clock()
        job = {
            "job_id": uuid.uuid4().hex,
            "direction": direction,
            "state": "queued",
            "transport": "rsync",
            "submitted_at": now,
            "started_at": None,
            "finished_at": None,
            "source_count": len(normalized_sources),
            "files_transferred": None,
            "bytes_transferred": None,
            "exit_code": None,
            "output_truncated": False,
            "cancel_requested": False,
            "remote_termination_unknown": False,
            "error": None,
            "_argv": argv,
            "_process": None,
            "_stdout": bytearray(),
            "_stderr": bytearray(),
        }
        with self._condition:
            if self._closed:
                raise BridgeError(
                    "BROKER_UNAVAILABLE", "rsync manager is closed")
            if len(self._queue) >= MAX_QUEUE:
                raise BridgeError(
                    "SYNC_QUEUE_FULL",
                    "rsync waiting queue is full",
                    queue_limit=MAX_QUEUE)
            self._jobs[job["job_id"]] = job
            self._queue.append(job["job_id"])
            self._condition.notify()
            return _public_job(job)

    def status(self, job_id):
        with self._condition:
            return _public_job(self._get_job_locked(job_id))

    def cancel(self, job_id):
        process = None
        with self._condition:
            job = self._get_job_locked(job_id)
            if job["state"] in _TERMINAL_STATES:
                return _public_job(job)
            job["cancel_requested"] = True
            if job["state"] == "queued":
                try:
                    self._queue.remove(job_id)
                except ValueError:
                    pass
                job["state"] = "cancelled"
                job["finished_at"] = self._clock()
                self._prune_history_locked()
                self._condition.notify_all()
                return _public_job(job)
            process = job["_process"]
            result = _public_job(job)
        if process is not None:
            try:
                self._terminate_process(process)
            except Exception as error:
                raise BridgeError(
                    "SYNC_CANCEL_FAILED",
                    "cannot terminate local rsync process: %s" % error)
        return result

    def snapshot(self):
        with self._condition:
            return {
                "sync_active": sum(
                    1 for job in self._jobs.values()
                    if job["state"] == "running"),
                "sync_queued": sum(
                    1 for job in self._jobs.values()
                    if job["state"] == "queued"),
                "sync_history": sum(
                    1 for job in self._jobs.values()
                    if job["state"] in _TERMINAL_STATES),
                "rsync_capability": dict(self._capability),
            }

    def invalidate_capabilities(self):
        with self._capability_lock:
            self._resolved_rsync_bin = None
            self._capability = _unknown_capability()
            self._capability_stage = None

    def close(self):
        process = None
        with self._condition:
            if self._closed:
                return
            self._closed = True
            for job_id in list(self._queue):
                job = self._jobs[job_id]
                if job["state"] == "queued":
                    job["cancel_requested"] = True
                    job["state"] = "cancelled"
                    job["finished_at"] = self._clock()
            self._queue.clear()
            if self._active_job_id is not None:
                active = self._jobs.get(self._active_job_id)
                if active is not None \
                        and active["state"] == "running":
                    active["cancel_requested"] = True
                    process = active["_process"]
            self._condition.notify_all()
        if process is not None:
            try:
                self._terminate_process(process)
            except Exception:
                pass
        self._worker.join(timeout=5)

    def _require_capability(self):
        if not self.transport.multiplexing \
                or not self.transport.is_alive():
            raise BridgeError(
                "RSYNC_MULTIPLEX_REQUIRED",
                "rsync requires an active OpenSSH ControlMaster")
        if not _SAFE_HOST.fullmatch(self.profile.host):
            raise BridgeError(
                "RSYNC_UNSAFE_CONFIG",
                "profile host cannot be safely represented as an rsync "
                "remote operand; use an SSH config alias")
        with self._capability_lock:
            if self._capability["state"] == "available":
                return
            if self._capability["state"] == "unavailable":
                raise BridgeError(
                    "RSYNC_UNAVAILABLE",
                    self._capability["reason"],
                    stage=self._capability_stage)
            self._probe_capability()

    def _probe_capability(self):
        executable = self._resolve_local_executable()
        env = os.environ.copy()
        env["LC_ALL"] = "C"
        try:
            version = self._run_local(
                [executable, "--version"],
                capture_output=True, text=True, timeout=5, env=env)
            help_result = self._run_local(
                [executable, "--help"],
                capture_output=True, text=True, timeout=5, env=env)
        except (OSError, subprocess.TimeoutExpired) as error:
            self._capability_failed(
                "local", "local rsync capability probe failed: %s" % error)
        if version.returncode != 0 or help_result.returncode != 0:
            self._capability_failed(
                "local", "local rsync capability probe returned nonzero")
        local_help = (help_result.stdout or "") + (help_result.stderr or "")
        if "--protect-args" not in local_help:
            self._capability_failed(
                "local", "local rsync does not support --protect-args")

        remote_bin = shlex.quote(self.profile.remote_rsync_bin)
        if self.profile.remote_rsync_bin.startswith("/"):
            prefix = "test -x %s" % remote_bin
        else:
            prefix = "command -v %s >/dev/null 2>&1" % remote_bin
        command = (
            "%s && %s --version && %s --help"
            % (prefix, remote_bin, remote_bin))
        remote = self._remote_probe(command)
        if remote.get("timed_out"):
            self._capability_failed(
                "remote", "remote rsync capability probe timed out")
        if remote.get("exit_code") != 0:
            self._capability_failed(
                "remote", "remote rsync is unavailable or incompatible")
        remote_text = (
            (remote.get("stdout") or "")
            + (remote.get("stderr") or ""))
        if "--protect-args" not in remote_text:
            self._capability_failed(
                "remote", "remote rsync does not support --protect-args")

        self._resolved_rsync_bin = executable
        self._capability_stage = None
        self._capability = {
            "state": "available",
            "local_version": _first_line(version.stdout),
            "remote_version": _first_line(remote.get("stdout")),
            "reason": None,
        }

    def _resolve_local_executable(self):
        configured = self.profile.rsync_bin
        if os.path.isabs(configured):
            executable = configured
        else:
            executable = shutil.which(configured)
        if not executable or not os.path.isfile(executable) \
                or not os.access(executable, os.X_OK):
            self._capability_failed(
                "local", "local rsync executable is unavailable")
        return os.path.abspath(executable)

    def _capability_failed(self, stage, reason):
        self._capability_stage = stage
        self._capability = {
            "state": "unavailable",
            "local_version": None,
            "remote_version": None,
            "reason": str(reason)[:300],
        }
        raise BridgeError(
            "RSYNC_UNAVAILABLE", self._capability["reason"], stage=stage)

    def _validate_request(self, direction, sources, destination):
        if direction not in ("push", "pull"):
            raise BridgeError(
                "INVALID_ARG", "sync direction must be push or pull")
        if not isinstance(sources, list) or not sources:
            raise BridgeError(
                "INVALID_ARG", "sync sources must be a non-empty list")
        if len(sources) > MAX_SOURCES:
            raise BridgeError(
                "INVALID_ARG",
                "sync supports at most %d sources" % MAX_SOURCES)
        if not isinstance(destination, str) or not destination:
            raise BridgeError(
                "INVALID_ARG", "sync destination must be a non-empty string")
        if direction == "push":
            local_sources = []
            seen = set()
            for source in sources:
                if not isinstance(source, str) or not source \
                        or "\x00" in source:
                    raise BridgeError(
                        "INVALID_ARG",
                        "local source must be a non-empty path")
                absolute = os.path.abspath(source)
                try:
                    os.lstat(absolute)
                except FileNotFoundError:
                    raise BridgeError(
                        "NOT_FOUND",
                        "local source does not exist")
                except OSError:
                    raise BridgeError(
                        "INVALID_ARG",
                        "cannot inspect local source")
                if absolute not in seen:
                    seen.add(absolute)
                    local_sources.append(absolute)
            remote_destination = self._canonical_remote(
                destination, required_type="dir")
            return local_sources, remote_destination

        if len(sources) != 1:
            raise BridgeError(
                "INVALID_ARG", "sync pull accepts exactly one remote source")
        remote_source = self._canonical_remote(sources[0])
        if not os.path.isabs(destination) \
                or not os.path.isdir(destination):
            raise BridgeError(
                "NOT_A_DIR",
                "local pull destination must be an existing absolute directory")
        return [remote_source], os.path.abspath(destination)

    def _canonical_remote(self, path, required_type=None):
        if not isinstance(path, str) or not path \
                or any(char in path for char in ("\x00", "\r", "\n")):
            raise BridgeError(
                "INVALID_ARG", "remote path contains invalid characters")
        result = self._remote_stat(path)
        if required_type is not None and result.get("type") != required_type:
            raise BridgeError(
                "NOT_A_DIR", "remote sync destination is not a directory")
        canonical = result.get("real_path")
        if not isinstance(canonical, str) or not canonical.startswith("/") \
                or any(char in canonical for char in ("\x00", "\r", "\n")):
            raise BridgeError(
                "INTERNAL", "remote path guard returned an invalid path")
        return canonical

    def _build_argv(self, direction, sources, destination):
        ssh_argv = self.profile.ssh_argv([
            "-S", self.transport.control_path,
            "-o", "ControlMaster=no",
        ])
        argv = [
            self._resolved_rsync_bin,
            "--recursive",
            "--links",
            "--safe-links",
            "--times",
            "--partial",
            "--partial-dir=.sshbridge-partial",
            "--stats",
            "--protect-args",
            "--rsync-path=%s" % self.profile.remote_rsync_bin,
            "-e", shlex.join(ssh_argv),
            "--",
        ]
        if direction == "push":
            return argv + list(sources) + [
                "%s:%s" % (self.profile.host, destination)]
        return argv + [
            "%s:%s" % (self.profile.host, sources[0]),
            destination,
        ]

    def _worker_loop(self):
        while True:
            with self._condition:
                while not self._queue and not self._closed:
                    self._condition.wait()
                if not self._queue:
                    return
                job_id = self._queue.popleft()
                job = self._jobs.get(job_id)
                if job is None or job["state"] != "queued":
                    continue
                job["state"] = "running"
                job["started_at"] = self._clock()
                self._active_job_id = job_id
            self._run_job(job)
            with self._condition:
                self._active_job_id = None
                self._prune_history_locked()
                self._condition.notify_all()

    def _run_job(self, job):
        env = os.environ.copy()
        env["LC_ALL"] = "C"
        try:
            process = self._popen(
                job["_argv"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env=env)
        except OSError:
            self._finish_job(
                job, "failed", None,
                BridgeError(
                    "RSYNC_UNAVAILABLE",
                    "local rsync executable could not be started",
                    stage="local"))
            return

        with self._condition:
            job["_process"] = process
            cancel_requested = job["cancel_requested"]
        if cancel_requested:
            try:
                self._terminate_process(process)
            except Exception:
                pass

        readers = [
            threading.Thread(
                target=self._drain_output,
                args=(job, process.stdout, "_stdout"),
                daemon=True),
            threading.Thread(
                target=self._drain_output,
                args=(job, process.stderr, "_stderr"),
                daemon=True),
        ]
        for reader in readers:
            reader.start()
        exit_code = process.wait()
        for reader in readers:
            reader.join(timeout=5)

        with self._condition:
            job["exit_code"] = exit_code
            cancelled = job["cancel_requested"]
            stdout = bytes(job["_stdout"])
            stderr = bytes(job["_stderr"])
        if cancelled:
            with self._condition:
                job["remote_termination_unknown"] = True
            self._finish_job(job, "cancelled", exit_code)
            return
        if exit_code == 0:
            files, transferred_bytes = _parse_stats(stdout)
            with self._condition:
                job["files_transferred"] = files
                job["bytes_transferred"] = transferred_bytes
            self._finish_job(job, "succeeded", exit_code)
            return

        stderr_text = stderr.decode("utf-8", "replace")
        if is_ssh_transport_failure(stderr_text):
            error = BridgeError(
                "SSH_ERROR",
                "rsync SSH channel failed",
                exit_code=exit_code)
            self._finish_job(job, "failed", exit_code, error)
            if self._on_transport_error is not None:
                self._on_transport_error(error)
            return
        self._finish_job(
            job, "failed", exit_code,
            BridgeError(
                "RSYNC_FAILED",
                "rsync exited with status %s" % exit_code,
                exit_code=exit_code))

    def _drain_output(self, job, stream, key):
        try:
            while True:
                chunk = stream.read(32768)
                if not chunk:
                    return
                with self._condition:
                    used = len(job["_stdout"]) + len(job["_stderr"])
                    available = max(0, MAX_OUTPUT_BYTES - used)
                    if available:
                        job[key].extend(chunk[:available])
                    if len(chunk) > available:
                        job["output_truncated"] = True
        except Exception:
            with self._condition:
                job["output_truncated"] = True

    def _finish_job(self, job, state, exit_code, error=None):
        with self._condition:
            job["state"] = state
            job["exit_code"] = exit_code
            job["finished_at"] = self._clock()
            job["error"] = error.to_dict() if error is not None else None
            job["_process"] = None

    def _get_job_locked(self, job_id):
        if not isinstance(job_id, str) or not job_id:
            raise BridgeError("INVALID_ARG", "job_id is required")
        try:
            return self._jobs[job_id]
        except KeyError:
            raise BridgeError(
                "SYNC_JOB_NOT_FOUND", "sync job was not found")

    def _prune_history_locked(self):
        terminal = [
            job_id for job_id, job in self._jobs.items()
            if job["state"] in _TERMINAL_STATES
        ]
        for job_id in terminal[:-MAX_HISTORY]:
            del self._jobs[job_id]


def _unknown_capability():
    return {
        "state": "unknown",
        "local_version": None,
        "remote_version": None,
        "reason": None,
    }


def _first_line(value):
    for line in (value or "").splitlines():
        if line.strip():
            return line.strip()[:160]
    return None


def _parse_stats(stdout):
    text = stdout.decode("utf-8", "replace")
    files_match = _STATS_FILES.search(text)
    bytes_match = _STATS_BYTES.search(text)
    files = _number(files_match.group(1)) if files_match else None
    transferred_bytes = _number(
        bytes_match.group(1)) if bytes_match else None
    return files, transferred_bytes


def _number(value):
    try:
        return int(value.replace(",", ""))
    except (AttributeError, ValueError):
        return None


def _public_job(job):
    return {
        key: job[key]
        for key in (
            "job_id", "direction", "state", "transport",
            "submitted_at", "started_at", "finished_at",
            "source_count", "files_transferred", "bytes_transferred",
            "exit_code", "output_truncated", "cancel_requested",
            "remote_termination_unknown", "error",
        )
    }


def _terminate_process_group(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=CANCEL_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait(timeout=CANCEL_GRACE_SECONDS)
