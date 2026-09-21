"""Per-profile Unix-socket connection broker."""

import argparse
import base64
import errno
import fcntl
import json
import os
import signal
import socket
import stat
import struct
import sys
import threading
import time
import uuid

from . import ops
from .broker_client import (MAX_MESSAGE, PROTOCOL_VERSION, BrokerEndpoint,
                            _read_message)
from .config import Profile, load_config
from .errors import BridgeError
from .exec_client import is_ssh_transport_failure, run_exec, start_exec
from .exec_jobs import EXEC_CANCEL_GRACE, ExecJobManager
from .sftp_client import SftpSession
from .transport import OpenSSHTransport


class BrokerState:
    def __init__(self, profile, endpoint, instance_id):
        self.profile = profile
        self.endpoint = endpoint
        self.instance_id = instance_id
        self.policy = profile.connection_policy
        self.transport = OpenSSHTransport(
            profile, endpoint.control_path, endpoint.log_path)
        self.exec_limit = (
            self.policy["exec_concurrency"]
            if self.transport.multiplexing else 1)
        self.connect_lock = threading.RLock()
        self.sftp_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.session = None
        self.started_at = time.time()
        self.state = "DISCONNECTED"
        self.last_attempt_at = None
        self.last_connected_at = None
        self.last_error = None
        self.failure_count = 0
        self.cooldown_until = 0
        self.ops_served = 0
        self.tcp_generation = 0
        self.exec_jobs = ExecJobManager(
            max_concurrency=self.exec_limit,
            initial_concurrency=1,
            queue_limit=self.policy["exec_queue_limit"],
            queue_timeout=self.policy["exec_queue_timeout"],
            output_limit_bytes=self.policy["exec_output_limit_bytes"],
            job_ttl=self.policy["exec_job_ttl"],
            max_jobs=self.policy["exec_max_jobs"],
            cancel_grace=EXEC_CANCEL_GRACE,
            prepare_exec=self._prepare_exec,
            process_factory=self._start_exec_process,
            transport_failure_callback=self._mark_open,
        )
        self._closed = False

    def snapshot(self):
        exec_snapshot = self.exec_jobs.snapshot()
        with self.state_lock:
            result = {
                "profile": self.profile.name,
                "profile_fingerprint": self.endpoint.fingerprint,
                "pid": os.getpid(),
                "instance_id": self.instance_id,
                "uptime_s": int(time.time() - self.started_at),
                "state": self.state,
                "last_attempt_at": self.last_attempt_at,
                "last_connected_at": self.last_connected_at,
                "last_error": self.last_error,
                "failure_count": self.failure_count,
                "cooldown_until": self.cooldown_until,
                "sftp_alive": (
                    self.session is not None and not self.session._closed),
                "ops_served": self.ops_served,
                "tcp_generation": self.tcp_generation,
                "transport": (
                    "openssh-controlmaster"
                    if self.transport.multiplexing else "openssh-direct"),
                "multiplexing": self.transport.multiplexing,
                "degraded_reason": self.transport.degraded_reason,
                "capabilities": ["exec_jobs_v1"],
            }
        result.update(exec_snapshot)
        return result

    def ensure_ready(self, manual=False):
        with self.connect_lock:
            with self.state_lock:
                current = self.state
                if current == "READY":
                    if self.transport.is_alive():
                        return
                    self._open_state_locked(BridgeError(
                        "SSH_ERROR", "OpenSSH ControlMaster is no longer alive"))
                    current = "OPEN"
                if current == "OPEN" and not manual:
                    raise self._paused_error_locked()
                if current == "CONNECTING" or current == "HALF_OPEN":
                    raise BridgeError(
                        "CONNECTION_PAUSED",
                        "another connection attempt is already in progress")
                now = time.time()
                not_before = (
                    (self.last_attempt_at or 0)
                    + self.policy["min_connect_interval"])
                if manual and now < not_before:
                    raise BridgeError(
                        "CONNECTION_RATE_LIMITED",
                        "connection retry is rate limited for %.1fs"
                        % (not_before - now),
                        retry_after=max(0, not_before - now))
                self.state = "HALF_OPEN" if manual else "CONNECTING"
                if self.transport.multiplexing:
                    self.last_attempt_at = now
            try:
                self.transport.start()
                if not self.transport.multiplexing and manual:
                    self._probe_direct_connection()
            except BridgeError as error:
                self._mark_open(error)
                raise
            with self.state_lock:
                self.state = "READY"
                self.last_connected_at = time.time()
                self.last_error = None
                self.failure_count = 0
                self.cooldown_until = 0
                if self.transport.multiplexing:
                    self.tcp_generation += 1
            self.exec_jobs.set_effective_concurrency(
                self.exec_limit if self.transport.multiplexing else 1)

    def reconnect(self):
        active_exec = self.exec_jobs.pause_dispatch()
        if active_exec:
            self.exec_jobs.resume_dispatch()
            raise BridgeError(
                "BROKER_BUSY",
                "cannot reconnect while Exec jobs are running",
                active_exec=active_exec)
        with self.connect_lock:
            try:
                with self.state_lock:
                    if self.state in ("CONNECTING", "HALF_OPEN"):
                        raise BridgeError(
                            "CONNECTION_PAUSED",
                            "another connection attempt is already in progress")
                    now = time.time()
                    not_before = (
                        (self.last_attempt_at or 0)
                        + self.policy["min_connect_interval"])
                    if now < not_before:
                        raise BridgeError(
                            "CONNECTION_RATE_LIMITED",
                            "connection retry is rate limited for %.1fs"
                            % (not_before - now),
                            retry_after=max(0, not_before - now))
                    if self.state == "READY":
                        self.state = "DISCONNECTED"
                        self.cooldown_until = 0
                        self.last_error = None
                with self.sftp_lock:
                    self._drop_session_unlocked()
                self.transport.stop()
                self.ensure_ready(manual=True)
            except BridgeError as error:
                with self.state_lock:
                    opened = self.state == "OPEN"
                if opened:
                    self.exec_jobs.fail_queued(error)
                self.exec_jobs.resume_dispatch(
                    self.exec_limit if self.transport.multiplexing else 1)
                raise
            else:
                self.exec_jobs.resume_dispatch(
                    self.exec_limit if self.transport.multiplexing else 1)
        return self.snapshot()

    def run(self, op, arguments):
        if op not in {
                "list_dir", "stat", "read_file", "write_file",
                "mkdir", "move", "delete", "exec", "hash",
                "exec_start", "exec_status", "exec_cancel"}:
            raise BridgeError(
                "INVALID_ARG", "unknown broker operation: %s" % op)
        with self.state_lock:
            self.ops_served += 1
        if op == "exec":
            return self._run_exec_op(arguments)
        if op == "exec_start":
            with self.state_lock:
                if self.state == "OPEN":
                    raise self._paused_error_locked()
            request = ops.normalize_exec_request(
                self.profile,
                arguments.get("command"),
                cwd=arguments.get("cwd", "/"),
                timeout=arguments.get("timeout"),
            )
            return self.exec_jobs.start(request)
        if op == "exec_status":
            return self.exec_jobs.status(
                arguments.get("job_id"),
                cursor=arguments.get("cursor", 0),
                max_bytes=arguments.get("max_bytes", 65536),
            )
        if op == "exec_cancel":
            return self.exec_jobs.cancel(arguments.get("job_id"))
        return self._run_sftp_op(op, arguments)

    def _run_sftp_op(self, op, arguments):
        self.ensure_ready()
        with self.sftp_lock:
            with self.state_lock:
                if self.state != "READY":
                    raise self._paused_error_locked()
            try:
                session = self._session_unlocked()
                return self._dispatch_sftp(op, arguments, session)
            except BridgeError as error:
                if error.code in ("SSH_ERROR", "TIMEOUT"):
                    self._drop_session_unlocked()
                    self._mark_open(error)
                raise

    def _session_unlocked(self):
        if self.session is not None and not self.session._closed:
            return self.session
        if not self.transport.multiplexing:
            self._begin_direct_connection()
        try:
            self.session = SftpSession(
                self.transport.channel_profile.sftp_argv(),
                self.profile.op_timeout,
                connect_retries=0)
        except BridgeError as error:
            self._mark_open(error)
            raise
        if not self.transport.multiplexing:
            with self.state_lock:
                self.tcp_generation += 1
                self.last_connected_at = time.time()
        return self.session

    def _dispatch_sftp(self, op, arguments, session):
        if op == "list_dir":
            return ops.op_list_dir(
                self.profile, arguments.get("path", "/"), session=session)
        if op == "stat":
            return ops.op_stat(
                self.profile, arguments["path"], session=session)
        if op == "read_file":
            return ops.op_read_file(
                self.profile, arguments["path"],
                offset=arguments.get("offset", 0),
                limit=arguments.get("limit"), session=session)
        if op == "write_file":
            try:
                data = base64.b64decode(
                    arguments.get("data_b64", ""), validate=True)
            except (ValueError, TypeError):
                raise BridgeError("INVALID_ARG", "bad data_b64")
            return ops.op_write_file(
                self.profile, arguments["path"], data,
                expected_mtime=arguments.get("expected_mtime"),
                expected_size=arguments.get("expected_size"),
                expected_hash=arguments.get("expected_hash"),
                force=bool(arguments.get("force")),
                session=session, exec_runner=self._execute)
        if op == "mkdir":
            return ops.op_mkdir(
                self.profile, arguments["path"],
                parents=bool(arguments.get("parents")), session=session)
        if op == "move":
            return ops.op_move(
                self.profile, arguments["src"], arguments["dst"],
                force=bool(arguments.get("force")), session=session)
        if op == "delete":
            return ops.op_delete(
                self.profile, arguments["path"], session=session)
        if op == "hash":
            return ops.op_hash(
                self.profile, arguments["path"], session=session,
                exec_runner=self._execute)
        raise BridgeError("INVALID_ARG", "unknown broker operation: %s" % op)

    def _run_exec_op(self, arguments):
        return ops.op_exec(
            self.profile, arguments["command"],
            cwd=arguments.get("cwd", "/"),
            timeout=arguments.get("timeout"),
            exec_runner=self._execute)

    def _execute(self, profile, command, cwd, timeout):
        return self.exec_jobs.run_sync(
            profile, command, cwd, timeout)

    def _prepare_exec(self):
        self.ensure_ready()
        if not self.transport.multiplexing:
            self._begin_direct_connection()
            return 1
        return self.exec_limit

    def _start_exec_process(self, command, cwd):
        process = start_exec(
            self.transport.channel_profile, command, cwd)
        if not self.transport.multiplexing:
            with self.state_lock:
                self.tcp_generation += 1
                self.last_connected_at = time.time()
        return process

    def _probe_direct_connection(self):
        self._begin_direct_connection()
        result = run_exec(
            self.transport.channel_profile, "true", self.profile.root,
            self.profile.connect_timeout, connect_retries=0)
        with self.state_lock:
            self.tcp_generation += 1
        if result["exit_code"] == 255 \
                and is_ssh_transport_failure(result["stderr"]):
            raise BridgeError(
                "SSH_ERROR",
                "direct SSH probe failed: %s"
                % result["stderr"].strip()[:400])

    def _begin_direct_connection(self):
        with self.state_lock:
            now = time.time()
            not_before = (
                (self.last_attempt_at or 0)
                + self.policy["min_connect_interval"])
            if now < not_before:
                raise BridgeError(
                    "CONNECTION_RATE_LIMITED",
                    "new SSH connection is rate limited for %.1fs"
                    % (not_before - now),
                    retry_after=max(0, not_before - now))
            self.last_attempt_at = now

    def _mark_open(self, error):
        with self.state_lock:
            if self.state == "OPEN":
                return
            self._open_state_locked(error)
        self.transport.stop()

    def _open_state_locked(self, error):
        self.state = "OPEN"
        self.failure_count += 1
        delay = min(
            self.policy["cooldown_initial"]
            * (2 ** max(0, self.failure_count - 1)),
            self.policy["cooldown_max"])
        self.cooldown_until = time.time() + delay
        self.last_error = error.to_dict()

    def _paused_error_locked(self):
        reconnect_after = (
            (self.last_attempt_at or 0)
            + self.policy["min_connect_interval"])
        retry_after = max(0, reconnect_after - time.time())
        return BridgeError(
            "CONNECTION_PAUSED",
            "connection is paused after a failure; run broker reconnect",
            retry_after=retry_after,
            cooldown_until=self.cooldown_until,
            last_error=self.last_error)

    def _drop_session_unlocked(self):
        if self.session is not None:
            try:
                self.session.shutdown()
            except Exception:
                pass
        self.session = None

    def close(self):
        if self._closed:
            return {
                "canceled_exec_jobs": 0,
                "remote_termination_unknown": False,
            }
        self._closed = True
        summary = self.exec_jobs.close()
        with self.sftp_lock:
            self._drop_session_unlocked()
        self.transport.stop()
        return summary


class BrokerServer:
    def __init__(self, config_path, profile):
        self.config_path = os.path.abspath(config_path)
        self.profile = profile
        self.endpoint = BrokerEndpoint(self.config_path, profile)
        self.instance_id = uuid.uuid4().hex
        self.state = BrokerState(profile, self.endpoint, self.instance_id)
        self.shutdown_event = threading.Event()
        self.listener = None
        self.lock_file = None

    def serve_forever(self):
        self._acquire_singleton()
        self._bind()
        self.endpoint.write_metadata(os.getpid(), self.instance_id)
        self._install_signals()
        try:
            while not self.shutdown_event.is_set():
                try:
                    connection, _ = self.listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(
                    target=self._handle_client,
                    args=(connection,), daemon=True).start()
        finally:
            self.close()

    def close(self):
        self.shutdown_event.set()
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass
            self.listener = None
        self.state.close()
        for path in (self.endpoint.metadata_path, self.endpoint.socket_path):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        if self.lock_file is not None:
            try:
                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
                self.lock_file.close()
            except OSError:
                pass
            self.lock_file = None

    def _acquire_singleton(self):
        try:
            self.lock_file = open(self.endpoint.lock_path, "a+b")
            os.chmod(self.endpoint.lock_path, 0o600)
            fcntl.flock(
                self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if self.lock_file is not None:
                self.lock_file.close()
                self.lock_file = None
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise BridgeError(
                    "BROKER_UNAVAILABLE",
                    "a broker already owns this profile")
            raise BridgeError(
                "BROKER_UNAVAILABLE",
                "cannot acquire broker lock: %s" % error)

    def _bind(self):
        self._remove_stale_socket()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.listener.bind(self.endpoint.socket_path)
            os.chmod(self.endpoint.socket_path, 0o600)
            self.listener.listen(32)
            self.listener.settimeout(0.5)
        except OSError as error:
            self.listener.close()
            self.listener = None
            raise BridgeError(
                "BROKER_UNAVAILABLE",
                "cannot bind broker socket: %s" % error)

    def _remove_stale_socket(self):
        try:
            info = os.lstat(self.endpoint.socket_path)
        except FileNotFoundError:
            return
        if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
            raise BridgeError(
                "BROKER_UNAVAILABLE",
                "refusing to remove unsafe broker socket path")
        os.remove(self.endpoint.socket_path)

    def _install_signals(self):
        def stop(_signum, _frame):
            _ = (_signum, _frame)
            self.shutdown_event.set()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

    def _handle_client(self, connection):
        request_id = None
        try:
            connection.settimeout(600)
            self._check_peer_uid(connection)
            raw = _read_message(connection)
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise BridgeError(
                    "INVALID_ARG", "broker request must be an object")
            request_id = request.get("request_id")
            if request.get("version") != PROTOCOL_VERSION:
                raise BridgeError(
                    "BROKER_UNAVAILABLE", "unsupported broker protocol version")
            if not isinstance(request_id, str) or not request_id:
                raise BridgeError("INVALID_ARG", "request_id is required")
            if request.get("profile") != self.endpoint.fingerprint:
                raise BridgeError(
                    "BROKER_PROFILE_MISMATCH",
                    "request belongs to another profile")
            operation = request.get("op")
            arguments = request.get("args") or {}
            if not isinstance(arguments, dict):
                raise BridgeError("INVALID_ARG", "args must be an object")
            if operation == "ping" or operation == "status":
                result = self.state.snapshot()
            elif operation == "reconnect":
                result = self.state.reconnect()
            elif operation == "shutdown":
                result = self.state.close()
                result["stopped"] = True
                self.shutdown_event.set()
            else:
                result = self.state.run(operation, arguments)
            self._send(connection, request_id, True, result=result)
        except BridgeError as error:
            self._send(connection, request_id, False, error=error.to_dict())
        except (UnicodeDecodeError, ValueError, KeyError, TypeError) as error:
            self._send(
                connection, request_id, False,
                error=BridgeError("INVALID_ARG", str(error)).to_dict())
        except Exception as error:
            self._send(
                connection, request_id, False,
                error=BridgeError("INTERNAL", repr(error)).to_dict())
        finally:
            connection.close()

    def _check_peer_uid(self, connection):
        peer_uid = None
        if hasattr(connection, "getpeereid"):
            peer_uid = connection.getpeereid()[0]
        elif hasattr(socket, "SO_PEERCRED"):
            credentials = connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED,
                struct.calcsize("3i"))
            _, peer_uid, _ = struct.unpack("3i", credentials)
        if peer_uid is not None and peer_uid != os.getuid():
            raise BridgeError(
                "BROKER_UNAVAILABLE", "broker client UID is not authorized")

    def _send(self, connection, request_id, ok, result=None, error=None):
        response = {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "instance_id": self.instance_id,
            "ok": ok,
        }
        if ok:
            response["result"] = result
        else:
            response["error"] = error
        try:
            encoded = (
                json.dumps(response, ensure_ascii=False,
                           separators=(",", ":")) + "\n").encode("utf-8")
            if len(encoded) <= MAX_MESSAGE:
                connection.sendall(encoded)
        except OSError:
            pass


def serve(config_path, profile_name):
    config = load_config(config_path)
    if profile_name not in config["profiles"]:
        raise BridgeError(
            "INVALID_CONFIG", "unknown profile: %s" % profile_name)
    profile = Profile(profile_name, config["profiles"][profile_name])
    if profile.connection_policy["mode"] != "broker":
        raise BridgeError(
            "BROKER_UNSUPPORTED",
            "profile %s is configured for direct mode" % profile_name)
    server = BrokerServer(config_path, profile)
    server.serve_forever()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sshbridge-broker")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--detach", action="store_true")
    parser.add_argument("--config", default="bridge.json")
    parser.add_argument("--profile")
    arguments = parser.parse_args(argv)
    if not arguments.serve:
        parser.error("--serve is required")
    if arguments.detach:
        child = os.fork()
        if child:
            return 0
        os.setsid()
    try:
        config = load_config(arguments.config)
        profile_name = arguments.profile or config["default_profile"]
        return serve(arguments.config, profile_name)
    except BridgeError as error:
        print(
            "broker error [%s]: %s" % (error.code, error.message),
            file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
