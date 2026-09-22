"""Disposable WSL1 Ubuntu SSH target; the bridge itself stays native Windows."""

import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


class WslSshd:
    def __init__(self):
        self.distribution = os.environ.get("SSHBRIDGE_WSL_DISTRO", "")
        if os.name != "nt" or os.environ.get("GITHUB_ACTIONS") != "true" \
                or not re.fullmatch(r"sshbridge-ci-[0-9a-f]{32}", self.distribution):
            raise RuntimeError("A provisioned, disposable Windows CI WSL target is required")
        self.temp = tempfile.TemporaryDirectory(prefix="sshbridge-wsl-client-")
        self.base = Path(self.temp.name)
        self.tag = uuid.uuid4().hex
        self.remote_base = "/home/sshbridge-test/" + self.tag
        self.workspace = self.remote_base + "/workspace"
        self.remote_state = self.remote_base + "/state"
        self.broker_process = None
        self.broker_log = None
        self.client = None
        self.port = None

    def linux(self, *args, timeout=30):
        result = subprocess.run(
            ["wsl.exe", "-d", self.distribution, "-u", "root", "--exec", *args],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout)
        if result.returncode:
            raise RuntimeError("WSL test setup failed (%s): %s" % (
                result.returncode, (result.stderr + result.stdout)[-4000:]))
        return result.stdout.strip()

    def shell(self, source, timeout=30):
        return self.linux("/bin/sh", "-c", source, timeout=timeout)

    def mapped(self, path):
        return self.linux("wslpath", "-a", "-u", Path(path).as_posix())

    def start(self):
        from sshbridge.config import Profile
        from sshbridge.broker_client import BrokerClient
        from sshbridge import windows_ipc as ipc
        private = Path(ipc.private_runtime_dir(str(self.base / "private")))
        key = private / "client_key"
        ssh = Path(os.environ["WINDIR"]) / "System32/OpenSSH/ssh.exe"
        keygen = ssh.with_name("ssh-keygen.exe")
        subprocess.run(
            [str(keygen), "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
            check=True, capture_output=True, timeout=15)
        security = ipc.PrivateSecurity()
        setter = ipc._api(ipc.A, "SetFileSecurityW", W.BOOL, W.LPCWSTR, W.DWORD, ipc.PTR)
        try:
            ipc._check(setter(str(key), 0x80000005, security.descriptor))
        finally:
            security.close()
        ipc.check_private_path(str(key))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        source = shlex.quote(self.mapped(ROOT / "only4test"))
        public_key = shlex.quote(self.mapped(Path(str(key) + ".pub")))
        self.shell("\n".join([
            "set -eu",
            "mkdir -p %s %s %s" % (self.workspace, self.remote_state, self.remote_base + "/outside"),
            "cp -R %s/. %s/" % (source, self.workspace),
            "cp %s %s/authorized_keys" % (public_key, self.remote_state),
            "ssh-keygen -q -t ed25519 -N '' -f %s/host_key" % self.remote_state,
            "printf '\\000\\377binary' > %s/binary.bin" % self.workspace,
            "head -c 4096 /dev/zero > %s/large.bin" % self.workspace,
            "printf outside > %s/outside/private.txt" % self.remote_base,
            "ln -s %s/outside %s/escape" % (self.remote_base, self.workspace),
            "chown -R sshbridge-test:sshbridge-test %s" % self.remote_base,
            "chmod 700 %s" % self.remote_state,
            "chmod 600 %s/authorized_keys %s/host_key" % (self.remote_state, self.remote_state),
        ]))
        config = "\n".join([
            "ListenAddress 127.0.0.1", "Port %d" % self.port,
            "HostKey %s/host_key" % self.remote_state,
            "PidFile %s/sshd.pid" % self.remote_state,
            "AuthorizedKeysFile %s/authorized_keys" % self.remote_state,
            "PasswordAuthentication no", "KbdInteractiveAuthentication no",
            "PubkeyAuthentication yes", "PermitRootLogin no", "UsePAM no",
            "StrictModes yes", "AllowUsers sshbridge-test",
            "Subsystem sftp internal-sftp", "LogLevel ERROR", "",
        ])
        sshd_config = self.base / "sshd_config"
        with sshd_config.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(config)
        self.linux("cp", self.mapped(sshd_config), self.remote_state + "/sshd_config")
        known_hosts = private / "known_hosts"
        host_public = self.linux("cat", self.remote_state + "/host_key.pub")
        known_hosts.write_text("[127.0.0.1]:%d %s\n" % (self.port, host_public), encoding="utf-8")
        self.raw = {
            "host": "127.0.0.1", "port": self.port, "user": "sshbridge-test",
            "root": self.workspace, "ssh_bin": str(ssh),
            "connect_timeout": 3, "op_timeout": 5, "exec_timeout": 15,
            "max_read_bytes": 1024, "hard_read_cap": 2048,
            "strict_host_key": "yes",
            "ssh_args": ["-F", "NUL", "-i", str(key),
                         "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                         "-o", "GlobalKnownHostsFile=NUL",
                         "-o", "UserKnownHostsFile=" + known_hosts.as_posix()],
            "connection_policy": {"mode": "broker", "control_master": False,
                                  "min_connect_interval": 0, "connect_retries": 0},
        }
        self.config = self.base / "bridge.test.json"
        self.config.write_text(json.dumps({"default_profile": "wsl-test",
                                           "profiles": {"wsl-test": self.raw}}), encoding="utf-8")
        self.environment = dict(os.environ, SSHBRIDGE_STATE_DIR=str(self.base / "broker-state"))
        self.start_sshd()
        self.broker_log = (self.base / "broker.log").open("w+b")
        self.broker_process = subprocess.Popen(
            [sys.executable, "-m", "sshbridge.broker", "--serve", "--config", str(self.config)],
            cwd=str(ROOT), env=self.environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=self.broker_log,
            creationflags=subprocess.CREATE_NO_WINDOW)
        # BrokerClient resolves the state directory from the current process.
        from unittest import mock
        self.state_patch = mock.patch.dict(os.environ, {
            "SSHBRIDGE_STATE_DIR": self.environment["SSHBRIDGE_STATE_DIR"],
        })
        self.state_patch.start()
        self.client = BrokerClient(str(self.config), Profile("wsl-test", self.raw))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                self.client.ping()
                return self
            except Exception:
                time.sleep(0.05)
        self.broker_log.seek(0)
        raise RuntimeError("Broker did not start: " + self.broker_log.read().decode("utf-8", "replace"))

    def start_sshd(self):
        self.linux("/usr/sbin/sshd", "-t", "-f", self.remote_state + "/sshd_config")
        self.linux("/usr/sbin/sshd", "-f", self.remote_state + "/sshd_config",
                   "-E", self.remote_state + "/sshd.log")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("Isolated sshd did not listen: " + self.linux("cat", self.remote_state + "/sshd.log"))

    def stop_sshd(self):
        self.shell("if [ -f %s/sshd.pid ]; then kill $(cat %s/sshd.pid) 2>/dev/null || true; fi; "
                   "pkill -u sshbridge-test || true" % (self.remote_state, self.remote_state))

    def close(self):
        try:
            if self.client:
                self.client.stop()
        finally:
            if self.broker_process:
                try:
                    self.broker_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.broker_process.kill()
                    self.broker_process.wait(timeout=5)
            if self.broker_log:
                self.broker_log.close()
            if hasattr(self, "state_patch"):
                self.state_patch.stop()
            try:
                self.stop_sshd()
            finally:
                self.temp.cleanup()
