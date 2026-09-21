"""Profile configuration (bridge.json): how to reach the host + sandbox root."""

import json
import os
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
}

_VALID_STRICT = ("yes", "no", "accept-new", "off")

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
        unknown = set(raw) - set(DEFAULTS) - {"host", "user", "root"}
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
        self.__dict__.update(opts)

    def ssh_argv(self):
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
        return argv + [str(a) for a in self.ssh_args]

    def sftp_argv(self):
        return self.ssh_argv() + ["-s", "--", self.host, "sftp"]

    def exec_argv(self, remote_command):
        return self.ssh_argv() + ["--", self.host, remote_command]
