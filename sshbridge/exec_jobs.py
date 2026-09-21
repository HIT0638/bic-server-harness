"""Bounded asynchronous OpenSSH command jobs owned by one Broker profile."""

import codecs
import collections
import threading
import time
import uuid
from dataclasses import dataclass, field

from .errors import BridgeError
from .exec_client import is_ssh_transport_failure


EXEC_STATUS_MAX_BYTES = 65536
EXEC_READ_CHUNK_BYTES = 32768
EXEC_CANCEL_GRACE = 2.0

_TERMINAL_STATES = frozenset((
    "EXITED", "TIMED_OUT", "CANCELED", "FAILED"))
_REMOTE_TERMINATION_NOTE = (
    "local ssh channel was terminated; remote process may still be running")


@dataclass
class _OutputEvent:
    start_cursor: int
    stream: str
    text: str
    byte_count: int

    @property
    def end_cursor(self):
        return self.start_cursor + len(self.text) - 1


@dataclass
class _ExecJob:
    job_id: str
    command: str
    cwd: str
    real_cwd: str
    timeout: float
    queued_at: float
    queued_mono: float
    retain_terminal: bool
    state: str = "QUEUED"
    started_at: object = None
    started_mono: object = None
    finished_at: object = None
    finished_mono: object = None
    process: object = None
    dispatching: bool = False
    cancel_requested: bool = False
    termination_reason: object = None
    termination_started: bool = False
    exit_code: object = None
    error: object = None
    remote_termination_unknown: bool = False
    events: collections.deque = field(default_factory=collections.deque)
    next_event_cursor: int = 1
    retained_bytes: int = 0
    output_truncated: bool = False
    stderr_probe: str = ""


class ExecJobManager:
    """Own the queue, workers, child processes, output, and job retention."""

    def __init__(
            self,
            max_concurrency,
            initial_concurrency,
            queue_limit,
            queue_timeout,
            output_limit_bytes,
            job_ttl,
            max_jobs,
            cancel_grace,
            prepare_exec,
            process_factory,
            transport_failure_callback,
            monotonic=time.monotonic,
            wall_clock=time.time):
        self.max_concurrency = int(max_concurrency)
        self.effective_concurrency = int(initial_concurrency)
        self.queue_limit = int(queue_limit)
        self.queue_timeout = float(queue_timeout)
        self.output_limit_bytes = int(output_limit_bytes)
        self.job_ttl = float(job_ttl)
        self.max_jobs = int(max_jobs)
        self.cancel_grace = float(cancel_grace)
        self.prepare_exec = prepare_exec
        self.process_factory = process_factory
        self.transport_failure_callback = transport_failure_callback
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self._condition = threading.Condition()
        self._queue = collections.deque()
        self._jobs = collections.OrderedDict()
        self._running_count = 0
        self._closed = False
        self._paused = False
        self._workers = []
        for index in range(self.max_concurrency):
            worker = threading.Thread(
                target=self._worker,
                name="sshbridge-exec-%d" % (index + 1),
                daemon=True,
            )
            self._workers.append(worker)
            worker.start()

    def start(self, normalized_request):
        """Queue one retained asynchronous job and return initial metadata."""
        job = self._submit(normalized_request, retain_terminal=True)
        with self._condition:
            result = self._base_snapshot_locked(job, "exec_start")
            self._condition.notify_all()
            return result

    def status(self, job_id, cursor=0, max_bytes=EXEC_STATUS_MAX_BYTES):
        """Return one cursor page of output and current structured state."""
        cursor = self._validate_cursor(cursor)
        max_bytes = self._validate_max_bytes(max_bytes)
        with self._condition:
            self._clean_locked()
            job = self._get_job_locked(job_id)
            return self._status_snapshot_locked(job, cursor, max_bytes)

    def cancel(self, job_id):
        """Cancel a queued or running job; repeated terminal calls are safe."""
        process = None
        owns_termination = False
        with self._condition:
            self._clean_locked()
            job = self._get_job_locked(job_id)
            if job.state in _TERMINAL_STATES:
                result = self._base_snapshot_locked(job, "exec_cancel")
                result["already_terminal"] = True
                return result
            if job.state == "QUEUED" and not job.dispatching:
                try:
                    self._queue.remove(job)
                except ValueError:
                    job.dispatching = True
                else:
                    job.cancel_requested = True
                    self._finish_locked(job, "CANCELED")
                    result = self._base_snapshot_locked(job, "exec_cancel")
                    result["already_terminal"] = False
                    return result
            process = job.process

        if process is not None and process.poll() is not None:
            with self._condition:
                while job.state not in _TERMINAL_STATES:
                    self._condition.wait(0.05)
                result = self._base_snapshot_locked(job, "exec_cancel")
                result["already_terminal"] = True
                return result

        with self._condition:
            if job.state in _TERMINAL_STATES:
                result = self._base_snapshot_locked(job, "exec_cancel")
                result["already_terminal"] = True
                return result
            job.cancel_requested = True
            job.termination_reason = "CANCELED"
            if job.process is not None and not job.termination_started:
                job.termination_started = True
                process = job.process
                owns_termination = True
            else:
                process = None
            self._condition.notify_all()

        if owns_termination:
            self._terminate_process(process)

        deadline = self.monotonic() + self.cancel_grace + 5.0
        with self._condition:
            while job.state not in _TERMINAL_STATES:
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(min(0.1, remaining))
            result = self._base_snapshot_locked(job, "exec_cancel")
            result["already_terminal"] = False
            return result

    def run_sync(self, profile, command, cwd, timeout):
        """Run through the same scheduler and project to the legacy result."""
        _ = profile
        request = {
            "command": command,
            "cwd": cwd,
            "real_cwd": cwd,
            "timeout": timeout,
        }
        job = self._submit(request, retain_terminal=False)
        with self._condition:
            self._condition.notify_all()
            while job.state not in _TERMINAL_STATES:
                self._condition.wait()
            stdout = "".join(
                event.text for event in job.events
                if event.stream == "stdout")
            stderr = "".join(
                event.text for event in job.events
                if event.stream == "stderr")
            state = job.state
            error = job.error
            result = {
                "exit_code": job.exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "timed_out": state == "TIMED_OUT",
                "output_truncated": job.output_truncated,
            }
            self._jobs.pop(job.job_id, None)
        if state == "FAILED":
            error = error or {
                "code": "SSH_ERROR",
                "message": "remote command failed before completion",
                "details": {},
            }
            raise BridgeError(
                error["code"], error["message"], **(error.get("details") or {}))
        if state == "CANCELED":
            raise BridgeError("SSH_ERROR", "remote command was canceled")
        return result

    def snapshot(self):
        """Return bounded queue/registry counters for Broker status."""
        with self._condition:
            self._clean_locked()
            return {
                "active_exec": self._running_count,
                "queued_exec": len(self._queue),
                "retained_exec_jobs": sum(
                    1 for job in self._jobs.values()
                    if job.retain_terminal
                    and job.state in _TERMINAL_STATES),
                "exec_concurrency": self.effective_concurrency,
            }

    def pause_dispatch(self):
        with self._condition:
            self._paused = True
            return self._running_count

    def set_effective_concurrency(self, effective_concurrency):
        with self._condition:
            self._set_effective_concurrency_locked(effective_concurrency)

    def resume_dispatch(self, effective_concurrency=None):
        with self._condition:
            if effective_concurrency is not None:
                self._set_effective_concurrency_locked(effective_concurrency)
            self._paused = False
            self._condition.notify_all()

    def fail_queued(self, error):
        """Fail waiting jobs after a reconnect attempt leaves Broker OPEN."""
        payload = self._error_payload(error)
        with self._condition:
            queued = list(self._queue)
            self._queue.clear()
            for job in queued:
                self._finish_locked(job, "FAILED", error=payload)
            self._condition.notify_all()
            return len(queued)

    def close(self):
        """Stop accepting work and terminate all locally owned SSH children."""
        processes = []
        canceled = 0
        had_running = False
        with self._condition:
            if self._closed:
                return {
                    "canceled_exec_jobs": 0,
                    "remote_termination_unknown": False,
                }
            self._closed = True
            self._paused = True
            queued = list(self._queue)
            self._queue.clear()
            for job in queued:
                job.cancel_requested = True
                self._finish_locked(job, "CANCELED")
                canceled += 1
            for job in self._jobs.values():
                if job.state == "QUEUED" and job.dispatching:
                    job.cancel_requested = True
                    job.termination_reason = "CANCELED"
                    canceled += 1
                elif job.state == "RUNNING":
                    had_running = True
                    canceled += 1
                    job.cancel_requested = True
                    job.termination_reason = "CANCELED"
                    if job.process is not None \
                            and not job.termination_started:
                        job.termination_started = True
                        processes.append(job.process)
            self._condition.notify_all()
        terminators = [
            threading.Thread(
                target=self._terminate_process,
                args=(process,),
                name="sshbridge-exec-stop",
                daemon=True,
            )
            for process in processes
        ]
        for terminator in terminators:
            terminator.start()
        deadline = self.monotonic() + self.cancel_grace + 3.0
        for terminator in terminators:
            remaining = deadline - self.monotonic()
            if remaining > 0:
                terminator.join(remaining)
        for worker in self._workers:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                break
            worker.join(remaining)
        return {
            "canceled_exec_jobs": canceled,
            "remote_termination_unknown": had_running,
        }

    def _submit(self, request, retain_terminal):
        required = ("command", "cwd", "real_cwd", "timeout")
        if not isinstance(request, dict) \
                or any(key not in request for key in required):
            raise BridgeError("INVALID_ARG", "invalid normalized Exec request")
        now_mono = self.monotonic()
        with self._condition:
            if self._closed:
                raise BridgeError(
                    "BROKER_UNAVAILABLE", "Exec job manager is closed")
            self._clean_locked(now_mono)
            capacity = self.effective_concurrency + self.queue_limit
            if self._running_count + len(self._queue) >= capacity:
                raise BridgeError(
                    "EXEC_QUEUE_FULL",
                    "Exec queue is full",
                    active_exec=self._running_count,
                    queued_exec=len(self._queue),
                    queue_limit=self.queue_limit,
                )
            self._evict_terminal_locked(self.max_jobs - 1)
            if len(self._jobs) >= self.max_jobs:
                raise BridgeError(
                    "EXEC_QUEUE_FULL",
                    "Exec job registry is full",
                    max_jobs=self.max_jobs,
                )
            job = _ExecJob(
                job_id=uuid.uuid4().hex,
                command=request["command"],
                cwd=request["cwd"],
                real_cwd=request["real_cwd"],
                timeout=float(request["timeout"]),
                queued_at=self.wall_clock(),
                queued_mono=now_mono,
                retain_terminal=retain_terminal,
            )
            self._jobs[job.job_id] = job
            self._queue.append(job)
            return job

    def _worker(self):
        while True:
            job = self._take_job()
            if job is None:
                return
            self._run_job(job)

    def _take_job(self):
        with self._condition:
            while True:
                self._expire_queued_locked()
                if self._closed and not self._queue:
                    return None
                if self._paused or not self._queue \
                        or self._running_count >= self.effective_concurrency:
                    self._condition.wait(0.1)
                    continue
                job = self._queue.popleft()
                if job.state != "QUEUED":
                    continue
                job.dispatching = True
                self._running_count += 1
                return job

    def _run_job(self, job):
        try:
            effective = self.prepare_exec()
        except BridgeError as error:
            self._finish_prepare_failure(job, error)
            return
        except Exception:
            self._finish_prepare_failure(
                job, BridgeError(
                    "SSH_ERROR", "failed to prepare the SSH connection"))
            return

        with self._condition:
            if effective is not None:
                self._set_effective_concurrency_locked(effective)
            if self._closed or job.cancel_requested:
                self._finish_locked(job, "CANCELED")
                return
            job.state = "RUNNING"

        try:
            process = self.process_factory(job.command, job.real_cwd)
        except BridgeError as error:
            self._finish_spawn_failure(job, error)
            return
        except Exception:
            self._finish_spawn_failure(
                job, BridgeError("SSH_ERROR", "cannot start ssh process"))
            return

        with self._condition:
            job.process = process
            job.started_at = self.wall_clock()
            job.started_mono = self.monotonic()
            terminate_now = job.termination_reason is not None
            if terminate_now and not job.termination_started:
                job.termination_started = True
            else:
                terminate_now = False
            self._condition.notify_all()

        readers = [
            threading.Thread(
                target=self._read_stream,
                args=(job, "stdout", process.stdout),
                name="sshbridge-%s-stdout" % job.job_id[:8],
                daemon=True,
            ),
            threading.Thread(
                target=self._read_stream,
                args=(job, "stderr", process.stderr),
                name="sshbridge-%s-stderr" % job.job_id[:8],
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()

        if terminate_now:
            self._terminate_process(process)

        return_code = self._monitor_process(job, process)
        self._join_readers(process, readers)

        callback_error = None
        with self._condition:
            reason = job.termination_reason
            if reason in ("CANCELED", "TIMED_OUT"):
                job.remote_termination_unknown = True
                self._finish_locked(job, reason)
            elif return_code == 255 \
                    and is_ssh_transport_failure(job.stderr_probe):
                callback_error = BridgeError(
                    "SSH_ERROR",
                    "ssh connection failed before command execution: %s"
                    % job.stderr_probe.strip()[:400])
                self._finish_locked(
                    job, "FAILED",
                    error=self._error_payload(callback_error))
            else:
                job.exit_code = return_code
                self._finish_locked(job, "EXITED")
        if callback_error is not None:
            self._notify_transport_failure(callback_error)

    def _monitor_process(self, job, process):
        while True:
            return_code = process.poll()
            if return_code is not None:
                return return_code
            owns_termination = False
            with self._condition:
                if job.termination_reason is None \
                        and job.started_mono is not None \
                        and self.monotonic() - job.started_mono >= job.timeout:
                    job.termination_reason = "TIMED_OUT"
                if job.termination_reason is not None \
                        and not job.termination_started:
                    job.termination_started = True
                    owns_termination = True
            if owns_termination:
                self._terminate_process(process)
            time.sleep(0.02)

    def _read_stream(self, job, stream, pipe):
        if pipe is None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while True:
                chunk = pipe.read(EXEC_READ_CHUNK_BYTES)
                if not chunk:
                    break
                text = decoder.decode(chunk, final=False)
                if text:
                    self._append_output(job, stream, text)
            tail = decoder.decode(b"", final=True)
            if tail:
                self._append_output(job, stream, tail)
        except (OSError, ValueError):
            pass
        finally:
            try:
                pipe.close()
            except (OSError, ValueError):
                pass

    def _append_output(self, job, stream, text):
        byte_count = len(text.encode("utf-8"))
        with self._condition:
            event = _OutputEvent(
                start_cursor=job.next_event_cursor,
                stream=stream,
                text=text,
                byte_count=byte_count,
            )
            job.next_event_cursor += len(text)
            job.events.append(event)
            job.retained_bytes += byte_count
            if stream == "stderr":
                job.stderr_probe = (job.stderr_probe + text)[-16384:]
            self._trim_output_locked(job)
            self._condition.notify_all()

    def _trim_output_locked(self, job):
        while job.retained_bytes > self.output_limit_bytes and job.events:
            event = job.events[0]
            excess = job.retained_bytes - self.output_limit_bytes
            if event.byte_count <= excess:
                job.events.popleft()
                job.retained_bytes -= event.byte_count
                job.output_truncated = True
                continue
            removed_chars = 0
            removed_bytes = 0
            for character in event.text:
                removed_chars += 1
                removed_bytes += len(character.encode("utf-8"))
                if removed_bytes >= excess:
                    break
            event.start_cursor += removed_chars
            event.text = event.text[removed_chars:]
            event.byte_count -= removed_bytes
            job.retained_bytes -= removed_bytes
            job.output_truncated = True

    def _status_snapshot_locked(self, job, cursor, max_bytes):
        result = self._base_snapshot_locked(job, "exec_status")
        events = []
        remaining = max_bytes
        next_cursor = cursor
        first_cursor = (
            job.events[0].start_cursor
            if job.events else job.next_event_cursor)
        for event in job.events:
            if event.end_cursor <= cursor:
                continue
            offset = max(0, cursor - event.start_cursor + 1)
            candidate = event.text[offset:]
            if not candidate:
                continue
            selected = []
            selected_bytes = 0
            for character in candidate:
                size = len(character.encode("utf-8"))
                if selected and selected_bytes + size > remaining:
                    break
                if not selected and size > remaining:
                    break
                selected.append(character)
                selected_bytes += size
            if not selected:
                break
            text = "".join(selected)
            end_cursor = event.start_cursor + offset + len(text) - 1
            events.append({
                "cursor": end_cursor,
                "stream": event.stream,
                "text": text,
            })
            next_cursor = end_cursor
            remaining -= selected_bytes
            if len(text) < len(candidate) or remaining <= 0:
                break
        result.update({
            "events": events,
            "next_cursor": next_cursor,
            "has_more": next_cursor < job.next_event_cursor - 1,
            "truncated_before": cursor < first_cursor - 1,
            "output_truncated": job.output_truncated,
        })
        return result

    def _base_snapshot_locked(self, job, operation):
        result = {
            "op": operation,
            "job_id": job.job_id,
            "state": job.state,
            "cwd": job.cwd,
            "timeout": job.timeout,
            "queued_at": job.queued_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "exit_code": job.exit_code,
            "error": job.error,
            "remote_termination_unknown": job.remote_termination_unknown,
        }
        if job.remote_termination_unknown:
            result["note"] = _REMOTE_TERMINATION_NOTE
        return result

    def _finish_prepare_failure(self, job, error):
        with self._condition:
            if job.cancel_requested or self._closed:
                self._finish_locked(job, "CANCELED")
            else:
                self._finish_locked(
                    job, "FAILED", error=self._error_payload(error))

    def _finish_spawn_failure(self, job, error):
        with self._condition:
            self._finish_locked(
                job, "FAILED", error=self._error_payload(error))
        if error.code == "SSH_ERROR":
            self._notify_transport_failure(error)

    def _finish_locked(self, job, state, error=None):
        if job.state in _TERMINAL_STATES:
            return False
        job.state = state
        job.error = error
        job.finished_at = self.wall_clock()
        job.finished_mono = self.monotonic()
        job.process = None
        if job.dispatching:
            job.dispatching = False
            self._running_count = max(0, self._running_count - 1)
        self._condition.notify_all()
        return True

    def _terminate_process(self, process):
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
        except (OSError, ProcessLookupError):
            return
        deadline = self.monotonic() + self.cancel_grace
        while self.monotonic() < deadline:
            if process.poll() is not None:
                return
            time.sleep(0.02)
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            return

    def _join_readers(self, process, readers):
        deadline = self.monotonic() + self.cancel_grace + 1.0
        for reader in readers:
            remaining = deadline - self.monotonic()
            if remaining > 0:
                reader.join(remaining)
        for pipe in (process.stdout, process.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except (OSError, ValueError):
                pass

    def _clean_locked(self, now=None):
        self._expire_queued_locked(now)
        now = self.monotonic() if now is None else now
        expired = [
            job_id for job_id, job in self._jobs.items()
            if job.retain_terminal
            and job.state in _TERMINAL_STATES
            and job.finished_mono is not None
            and now - job.finished_mono >= self.job_ttl
        ]
        for job_id in expired:
            self._jobs.pop(job_id, None)
        self._evict_terminal_locked(self.max_jobs)

    def _expire_queued_locked(self, now=None):
        now = self.monotonic() if now is None else now
        expired = [
            job for job in self._queue
            if now - job.queued_mono >= self.queue_timeout
        ]
        for job in expired:
            try:
                self._queue.remove(job)
            except ValueError:
                continue
            error = BridgeError(
                "EXEC_QUEUE_TIMEOUT",
                "Exec job exceeded the queue timeout",
                queue_timeout=self.queue_timeout,
            )
            self._finish_locked(
                job, "FAILED", error=self._error_payload(error))

    def _evict_terminal_locked(self, target_size):
        if len(self._jobs) <= target_size:
            return
        terminal = [
            (job.finished_mono, job_id)
            for job_id, job in self._jobs.items()
            if job.retain_terminal
            and job.state in _TERMINAL_STATES
            and job.finished_mono is not None
        ]
        terminal.sort()
        for _, job_id in terminal:
            if len(self._jobs) <= target_size:
                break
            self._jobs.pop(job_id, None)

    def _get_job_locked(self, job_id):
        if not isinstance(job_id, str) or not job_id:
            raise BridgeError("INVALID_ARG", "job_id is required")
        job = self._jobs.get(job_id)
        if job is None or not job.retain_terminal:
            raise BridgeError(
                "EXEC_JOB_NOT_FOUND", "Exec job was not found",
                job_id=job_id)
        return job

    def _set_effective_concurrency_locked(self, value):
        if isinstance(value, bool) or not isinstance(value, int):
            value = int(value)
        value = max(1, min(self.max_concurrency, value))
        if value != self.effective_concurrency:
            self.effective_concurrency = value
            self._condition.notify_all()

    @staticmethod
    def _validate_cursor(cursor):
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise BridgeError(
                "INVALID_ARG", "cursor must be a non-negative integer")
        return cursor

    @staticmethod
    def _validate_max_bytes(max_bytes):
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) \
                or not (1 <= max_bytes <= EXEC_STATUS_MAX_BYTES):
            raise BridgeError(
                "INVALID_ARG",
                "max_bytes must be an integer from 1 to %d"
                % EXEC_STATUS_MAX_BYTES)
        return max_bytes

    @staticmethod
    def _error_payload(error):
        if isinstance(error, BridgeError):
            return error.to_dict()
        return BridgeError(
            "SSH_ERROR", "remote command failed before completion").to_dict()

    def _notify_transport_failure(self, error):
        try:
            self.transport_failure_callback(error)
        except Exception:
            pass
