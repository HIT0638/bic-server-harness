"""Client and endpoint management for the local connection broker."""

import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import time
import uuid

from .errors import BridgeError

PROTOCOL_VERSION = 1
MAX_MESSAGE = 256 * 1024 * 1024
_UNIX_PATH_LIMIT = 103


def profile_fingerprint(config_path, profile):
    values = [
        os.path.abspath(config_path),
        profile.name,
        profile.host,
        int(profile.port),
        profile.user,
        profile.root,
    ]
    payload = json.dumps(
        values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def runtime_dir():
    configured = os.environ.get("SSHBRIDGE_STATE_DIR")
    if configured:
        path = os.path.abspath(os.path.expanduser(configured))
    else:
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if xdg:
            path = os.path.join(os.path.abspath(os.path.expanduser(xdg)),
                                "sshbridge")
        else:
            path = os.path.join("/tmp", "sshbridge-%s" % os.getuid())
    return _ensure_runtime_dir(path)


def _ensure_runtime_dir(path):
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        info = os.lstat(path)
    except OSError as error:
        raise BridgeError(
            "BROKER_UNAVAILABLE",
            "cannot prepare broker runtime directory %s: %s" % (path, error))
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise BridgeError(
            "BROKER_UNAVAILABLE",
            "broker runtime path is not a real directory: %s" % path)
    if info.st_uid != os.getuid():
        raise BridgeError(
            "BROKER_UNAVAILABLE",
            "broker runtime directory is not owned by the current user: %s"
            % path)
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise BridgeError(
            "BROKER_UNAVAILABLE",
            "broker runtime directory mode must be 0700: %s" % path)
    return path


def _short_runtime_alias(path):
    base = _ensure_runtime_dir(
        os.path.join("/tmp", "sshbridge-%s" % os.getuid()))
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:12]
    alias = os.path.join(base, "a-" + digest)
    try:
        os.symlink(path, alias)
    except FileExistsError:
        try:
            if os.readlink(alias) != path:
                raise BridgeError(
                    "BROKER_UNAVAILABLE",
                    "broker runtime alias points to an unexpected directory")
        except OSError as error:
            raise BridgeError(
                "BROKER_UNAVAILABLE",
                "cannot validate broker runtime alias: %s" % error)
    except OSError as error:
        raise BridgeError(
            "BROKER_UNAVAILABLE",
            "cannot create broker runtime alias: %s" % error)
    return alias


class BrokerEndpoint:
    def __init__(self, config_path, profile):
        self.config_path = os.path.abspath(config_path)
        self.profile_name = profile.name
        self.fingerprint = profile_fingerprint(self.config_path, profile)
        self.runtime_dir = runtime_dir()
        endpoint_id = self.fingerprint[:12]
        prefix = "b-" + self.fingerprint
        endpoint_dir = self.runtime_dir
        candidate = os.path.join(endpoint_dir, endpoint_id + ".c")
        if len(candidate.encode("utf-8")) > 80:
            endpoint_dir = _short_runtime_alias(self.runtime_dir)
        self.socket_path = os.path.join(endpoint_dir, endpoint_id + ".s")
        self.metadata_path = os.path.join(self.runtime_dir, prefix + ".json")
        self.lock_path = os.path.join(self.runtime_dir, prefix + ".lock")
        self.control_path = os.path.join(endpoint_dir, endpoint_id + ".c")
        self.log_path = os.path.join(endpoint_dir, prefix + ".log")
        if len(self.socket_path.encode("utf-8")) > _UNIX_PATH_LIMIT:
            raise BridgeError(
                "BROKER_UNAVAILABLE",
                "broker socket path is too long: %s" % self.socket_path)

    def write_metadata(self, pid, instance_id):
        metadata = {
            "protocol_version": PROTOCOL_VERSION,
            "pid": pid,
            "instance_id": instance_id,
            "profile_fingerprint": self.fingerprint,
            "socket_path": self.socket_path,
        }
        temporary = self.metadata_path + ".tmp." + uuid.uuid4().hex[:8]
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = None
        try:
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = None
                json.dump(metadata, stream, ensure_ascii=False)
                stream.write("\n")
            os.replace(temporary, self.metadata_path)
            os.chmod(self.metadata_path, 0o600)
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise BridgeError(
                "BROKER_UNAVAILABLE",
                "cannot write broker metadata: %s" % error)
        return metadata

    def read_metadata(self):
        try:
            info = os.lstat(self.metadata_path)
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise BridgeError(
                    "BROKER_UNAVAILABLE", "broker metadata is not a regular file")
            if info.st_uid != os.getuid() \
                    or stat.S_IMODE(info.st_mode) & 0o077:
                raise BridgeError(
                    "BROKER_UNAVAILABLE",
                    "broker metadata has unsafe ownership or permissions")
            with open(self.metadata_path, "r", encoding="utf-8") as stream:
                metadata = json.load(stream)
        except FileNotFoundError:
            raise BridgeError("BROKER_UNAVAILABLE", "broker is not running")
        except (OSError, ValueError) as error:
            raise BridgeError(
                "BROKER_UNAVAILABLE",
                "cannot read broker metadata: %s" % error)
        if metadata.get("protocol_version") != PROTOCOL_VERSION:
            raise BridgeError(
                "BROKER_UNAVAILABLE", "unsupported broker protocol version")
        if metadata.get("profile_fingerprint") != self.fingerprint:
            raise BridgeError(
                "BROKER_PROFILE_MISMATCH",
                "broker metadata belongs to another profile")
        if metadata.get("socket_path") != self.socket_path:
            raise BridgeError(
                "BROKER_PROFILE_MISMATCH",
                "broker metadata points to an unexpected socket")
        return metadata


class BrokerClient:
    def __init__(self, config_path, profile):
        if os.name == "nt":
            raise BridgeError(
                "BROKER_UNSUPPORTED",
                "broker mode requires a Unix socket; use direct mode on Windows")
        self.config_path = os.path.abspath(config_path)
        self.profile = profile
        self.endpoint = BrokerEndpoint(self.config_path, profile)

    def request(self, op, args=None, timeout=120.0):
        metadata = self.endpoint.read_metadata()
        request_id = uuid.uuid4().hex
        request = {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "profile": self.endpoint.fingerprint,
            "op": op,
            "args": args or {},
        }
        encoded = (json.dumps(request, ensure_ascii=False,
                              separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > MAX_MESSAGE:
            raise BridgeError("INVALID_ARG", "broker request is too large")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(min(2.0, timeout))
            connection.connect(self.endpoint.socket_path)
            connection.settimeout(timeout)
            connection.sendall(encoded)
            raw = _read_message(connection)
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout,
                OSError) as error:
            raise BridgeError(
                "BROKER_UNAVAILABLE", "cannot reach broker: %s" % error)
        finally:
            connection.close()
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise BridgeError(
                "BROKER_UNAVAILABLE", "invalid broker response: %s" % error)
        if response.get("version") != PROTOCOL_VERSION \
                or response.get("request_id") != request_id:
            raise BridgeError(
                "BROKER_UNAVAILABLE", "broker response identity mismatch")
        if response.get("instance_id") != metadata.get("instance_id"):
            raise BridgeError(
                "BROKER_UNAVAILABLE", "broker instance changed during request")
        if not response.get("ok"):
            error = response.get("error") or {}
            raise BridgeError(
                error.get("code", "INTERNAL"),
                error.get("message", "broker request failed"),
                **(error.get("details") or {}))
        return response.get("result") or {}

    def ping(self):
        return self.request("ping", timeout=2.0)

    def ensure_started(self, timeout=10.0):
        try:
            return self.ping()
        except BridgeError as error:
            if error.code not in ("BROKER_UNAVAILABLE",
                                  "BROKER_PROFILE_MISMATCH"):
                raise
        frozen = bool(getattr(sys, "frozen", False))
        argv = _broker_launch_argv(
            self.config_path, self.profile.name, frozen=frozen)
        root = None if frozen else \
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        kwargs = {}
        if os.name != "nt":
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(
                argv, cwd=root, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **kwargs)
        except OSError as error:
            raise BridgeError(
                "BROKER_UNAVAILABLE", "cannot start broker: %s" % error)
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            time.sleep(0.05)
            try:
                result = self.ping()
                _reap_launcher(process)
                return result
            except BridgeError as error:
                last_error = error
            if process.poll() is not None:
                # Another racing starter may own the lock; keep probing.
                continue
        message = "broker did not become ready within %ss" % timeout
        if last_error is not None:
            message += ": " + last_error.message
        raise BridgeError("BROKER_UNAVAILABLE", message)

    def status(self):
        try:
            result = self.ping()
            result["running"] = True
            return result
        except BridgeError as error:
            if error.code != "BROKER_UNAVAILABLE":
                raise
            return {
                "running": False,
                "profile": self.profile.name,
                "profile_fingerprint": self.endpoint.fingerprint,
            }

    def reconnect(self):
        return self.request(
            "reconnect",
            timeout=self.profile.connect_timeout + 10)

    def stop(self):
        try:
            result = self.request("shutdown", timeout=5.0)
        except BridgeError as error:
            if error.code != "BROKER_UNAVAILABLE":
                raise
            return {"stopped": False, "note": "not running"}
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not os.path.exists(self.endpoint.socket_path):
                break
            time.sleep(0.05)
        stopped = not os.path.exists(self.endpoint.socket_path)
        result["stopped"] = stopped
        if not stopped:
            result["note"] = "broker shutdown is still in progress"
        return result


def _read_message(connection):
    buffer = bytearray()
    while not buffer.endswith(b"\n"):
        chunk = connection.recv(1 << 16)
        if not chunk:
            if not buffer:
                raise BridgeError(
                    "BROKER_UNAVAILABLE", "broker closed the connection")
            break
        buffer += chunk
        if len(buffer) > MAX_MESSAGE:
            raise BridgeError(
                "BROKER_UNAVAILABLE", "broker response is too large")
    return bytes(buffer)


def _broker_launch_argv(
        config_path, profile_name, frozen=None, executable=None):
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    executable = os.path.abspath(executable or sys.executable)
    if frozen:
        helper = os.path.join(
            os.path.dirname(executable), "sshbridge_broker")
        try:
            info = os.lstat(helper)
        except OSError as error:
            raise BridgeError(
                "BROKER_UNAVAILABLE",
                "bundled broker helper is unavailable: %s" % error)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) \
                or not os.access(helper, os.X_OK):
            raise BridgeError(
                "BROKER_UNAVAILABLE",
                "bundled broker helper is not an executable regular file")
        command = [helper]
    else:
        command = [executable, "-m", "sshbridge.broker"]
    return command + [
        "--serve", "--detach",
        "--config", os.path.abspath(config_path),
        "--profile", profile_name,
    ]


def _reap_launcher(process):
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
