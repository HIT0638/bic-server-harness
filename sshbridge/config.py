"""Profile configuration (bridge.json): how to reach the host + sandbox root."""

import json
import os
import posixpath
import re
import sys

from .errors import BridgeError
from .paths import normalize_root

DEFAULTS = {
    "port": 22,
    "ssh_bin": "ssh",
    "connect_timeout": 15,                # TCP/banner timeout (ssh -o ConnectTimeout)
    "op_timeout": 60,                     # watchdog for a single SFTP round trip
    "exec_timeout": 60,                   # default remote exec timeout (seconds)
    "max_read_bytes": 10 * 1024 * 1024,   # read_file guard without --limit
    "hard_read_cap": 64 * 1024 * 1024,    # absolute cap even with --limit
    "batch_mode": True,                   # non-interactive auth (keys/agent)
    "strict_host_key": "accept-new",
    "allow_controlmaster": False,         # only relevant on Windows (see ssh_argv)
    "ssh_args": [],                       # extra ssh options, e.g. ["-i", "key", "-J", "jump"]
    "rsync_bin": "rsync",                 # optional local rsync executable
    "remote_rsync_bin": "rsync",          # optional remote rsync executable
}

CONNECTION_POLICY_DEFAULTS = {
    "mode": "direct" if sys.platform == "win32" else "broker",
    "exec_concurrency": 2,
    "min_connect_interval": 10,
    "connect_retries": 0,
    "auto_reconnect": False,
    "cooldown_initial": 60,
    "cooldown_max": 1800,
    "control_master": sys.platform != "win32",
}

_VALID_STRICT = ("yes", "no", "accept-new", "off")
_VALID_CONNECTION_MODES = ("broker", "direct")
_REMOTE_EXECUTABLE = re.compile(
    r"(?:[A-Za-z0-9._+-]+|/(?:[A-Za-z0-9._+-]+/)*[A-Za-z0-9._+-]+)\Z")

DEFAULT_PORT = 7766  # local daemon port (configurable via "daemon_port")


def load_config(path=None):
    candidates = [path] if path else ["bridge.json"]
    data = None
    used = None
    for c in candidates:
        if c and os.path.isfile(c):
            try:
                with open(c, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except ValueError as e:
                raise BridgeError("INVALID_CONFIG", "cannot parse %s: %s" % (c, e))
            used = c
            break
    if data is None:
        raise BridgeError(
            "INVALID_CONFIG",
            "config file not found (looked for: %s); use --config" % ", ".join(candidates))
    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise BridgeError("INVALID_CONFIG",
                          'config must contain a non-empty "profiles" object')
    default = data.get("default_profile")
    if default not in profiles:
        default = next(iter(profiles))
    daemon_port = data.get("daemon_port", DEFAULT_PORT)
    if not isinstance(daemon_port, int) or not (1 <= daemon_port <= 65535):
        raise BridgeError("INVALID_CONFIG", "daemon_port must be a valid port number")
    return {"file": used, "profiles": profiles, "default_profile": default,
            "daemon_port": daemon_port}


class Profile:
    def __init__(self, name, raw):
        if not isinstance(raw, dict):
            raise BridgeError("INVALID_CONFIG", "profile %s must be an object" % name)
        unknown = set(raw) - set(DEFAULTS) - {
            "host", "user", "root", "connection_policy"}
        if unknown:
            raise BridgeError("INVALID_CONFIG",
                              "unknown keys in profile %s: %s" % (name, sorted(unknown)))
        opts = dict(DEFAULTS)
        opts.update({k: raw[k] for k in raw if k in DEFAULTS})
        self.name = name
        self.host = raw.get("host")
        self.user = raw.get("user")
        if not self.host or not self.user:
            raise BridgeError("INVALID_CONFIG",
                              'profile %s: both "host" and "user" are required' % name)
        self.root = normalize_root(raw.get("root"))
        for key in ("port", "connect_timeout", "op_timeout", "exec_timeout",
                    "max_read_bytes", "hard_read_cap"):
            if not isinstance(opts[key], (int, float)) or opts[key] <= 0:
                raise BridgeError("INVALID_CONFIG",
                                  "profile %s: %s must be a positive number" % (name, key))
        if opts["strict_host_key"] not in _VALID_STRICT:
            raise BridgeError("INVALID_CONFIG",
                              "profile %s: strict_host_key must be one of %s"
                              % (name, _VALID_STRICT))
        if not isinstance(opts["ssh_args"], list):
            raise BridgeError("INVALID_CONFIG", "profile %s: ssh_args must be a list" % name)
        self._validate_rsync_executables(opts)
        self.connection_policy = self._connection_policy(
            raw.get("connection_policy"))
        self.control_path = None
        self.__dict__.update(opts)

    def _validate_rsync_executables(self, opts):
        local = opts["rsync_bin"]
        if not isinstance(local, str) or not local \
                or local.strip() != local or "\x00" in local \
                or (os.sep in local and not os.path.isabs(local)):
            raise BridgeError(
                "INVALID_CONFIG",
                "profile %s: rsync_bin must be an executable name "
                "or absolute path" % self.name)
        remote = opts["remote_rsync_bin"]
        if not isinstance(remote, str) or not remote \
                or not _REMOTE_EXECUTABLE.fullmatch(remote) \
                or posixpath.normpath(remote) != remote:
            raise BridgeError(
                "INVALID_CONFIG",
                "profile %s: remote_rsync_bin must be a safe executable "
                "name or absolute POSIX path" % self.name)

    def _connection_policy(self, raw):
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise BridgeError(
                "INVALID_CONFIG",
                "profile %s: connection_policy must be an object" % self.name)
        unknown = set(raw) - set(CONNECTION_POLICY_DEFAULTS)
        if unknown:
            raise BridgeError(
                "INVALID_CONFIG",
                "profile %s: unknown connection_policy keys: %s"
                % (self.name, sorted(unknown)))
        policy = dict(CONNECTION_POLICY_DEFAULTS)
        policy.update(raw)
        if policy["mode"] not in _VALID_CONNECTION_MODES:
            raise BridgeError(
                "INVALID_CONFIG",
                "profile %s: connection_policy.mode must be one of %s"
                % (self.name, _VALID_CONNECTION_MODES))
        if not isinstance(policy["exec_concurrency"], int) \
                or isinstance(policy["exec_concurrency"], bool) \
                or not (1 <= policy["exec_concurrency"] <= 3):
            raise BridgeError(
                "INVALID_CONFIG",
                "profile %s: connection_policy.exec_concurrency "
                "must be an integer from 1 to 3" % self.name)
        for key in ("min_connect_interval", "cooldown_initial",
                    "cooldown_max"):
            value = policy[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool) \
                    or value < 0:
                raise BridgeError(
                    "INVALID_CONFIG",
                    "profile %s: connection_policy.%s must be >= 0"
                    % (self.name, key))
        if policy["cooldown_max"] < policy["cooldown_initial"]:
            raise BridgeError(
                "INVALID_CONFIG",
                "profile %s: connection_policy.cooldown_max must be "
                ">= cooldown_initial" % self.name)
        if not isinstance(policy["connect_retries"], int) \
                or isinstance(policy["connect_retries"], bool) \
                or policy["connect_retries"] != 0:
            raise BridgeError(
                "INVALID_CONFIG",
                "profile %s: connection_policy.connect_retries must be 0"
                % self.name)
        for key in ("auto_reconnect", "control_master"):
            if not isinstance(policy[key], bool):
                raise BridgeError(
                    "INVALID_CONFIG",
                    "profile %s: connection_policy.%s must be boolean"
                    % (self.name, key))
        return policy

    def ssh_argv(self, extra_args=None):
        argv = [
            self.ssh_bin,
            "-p", str(int(self.port)),
            "-l", self.user,
            "-o", "ConnectTimeout=%s" % self.connect_timeout,
            "-o", "LogLevel=ERROR",
            "-o", "BatchMode=%s" % ("yes" if self.batch_mode else "no"),
            "-o", "StrictHostKeyChecking=%s" % self.strict_host_key,
        ]
        # Win32-OpenSSH cannot multiplex; make sure a global ControlMaster
        # setting in ~/.ssh/config cannot break every invocation.
        if sys.platform == "win32" and not self.allow_controlmaster:
            argv += ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
        if extra_args:
            argv += [str(a) for a in extra_args]
        return argv + [str(a) for a in self.ssh_args]

    def sftp_argv(self):
        extra = None
        if self.control_path:
            extra = ["-S", self.control_path, "-o", "ControlMaster=no"]
        return self.ssh_argv(extra) + ["-s", "--", self.host, "sftp"]

    def exec_argv(self, remote_command):
        extra = None
        if self.control_path:
            extra = ["-S", self.control_path, "-o", "ControlMaster=no"]
        return self.ssh_argv(extra) + ["--", self.host, remote_command]

    def master_argv(self, control_path):
        return self.ssh_argv([
            "-M", "-N", "-S", control_path,
            "-o", "ControlPersist=no",
            "-o", "ExitOnForwardFailure=yes",
        ]) + ["--", self.host]

    def control_argv(self, control_path, operation):
        return self.ssh_argv([
            "-S", control_path,
            "-o", "ControlMaster=no",
            "-O", operation,
        ]) + ["--", self.host]
