"""Hermetic local OpenSSH server used by integration tests.

The server runs as the current user on a random localhost port. It owns its
host key, client key, authorized_keys file, workspace, daemon state, and bridge
configuration. No system SSH configuration or user SSH files are changed.
"""

import argparse
import getpass
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


class LocalSshdUnavailable(RuntimeError):
    pass


class LocalSshdStartError(RuntimeError):
    pass


def _find_binary(name, fallbacks=()):
    found = shutil.which(name)
    if found:
        return found
    for path in fallbacks:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LocalSshd:
    def __init__(self):
        self._temp = None
        self.base = None
        self.workspace = None
        self.outside = None
        self.daemon_state = None
        self.config_path = None
        self.profile_raw = None
        self.sshd_port = None
        self.daemon_port = None
        self.proc = None
        self._log_file = None

    def start(self):
        ssh = _find_binary("ssh", ("/usr/bin/ssh",))
        sshd = _find_binary("sshd", ("/usr/sbin/sshd",))
        ssh_keygen = _find_binary("ssh-keygen", ("/usr/bin/ssh-keygen",))
        missing = [
            name for name, path in (
                ("ssh", ssh), ("sshd", sshd), ("ssh-keygen", ssh_keygen))
            if path is None
        ]
        if missing:
            raise LocalSshdUnavailable(
                "missing OpenSSH tools: %s" % ", ".join(missing))

        self._temp = tempfile.TemporaryDirectory(prefix="sshbridge-local-")
        self.base = Path(self._temp.name)
        self.workspace = self.base / "workspace"
        self.outside = self.base / "outside"
        self.daemon_state = self.base / "daemon-state"
        for path in (self.workspace, self.outside, self.daemon_state):
            path.mkdir(mode=0o700)
        fixture_root = Path(__file__).resolve().parents[1] / "only4test"
        if fixture_root.is_dir():
            shutil.copytree(fixture_root, self.workspace, dirs_exist_ok=True)

        host_key = self.base / "host_key"
        client_key = self.base / "client_key"
        self._generate_key(ssh_keygen, host_key)
        self._generate_key(ssh_keygen, client_key)
        authorized_keys = self.base / "authorized_keys"
        shutil.copyfile(str(client_key) + ".pub", authorized_keys)
        os.chmod(self.base, 0o700)
        os.chmod(host_key, 0o600)
        os.chmod(client_key, 0o600)
        os.chmod(authorized_keys, 0o600)

        self.sshd_port = _free_port()
        self.daemon_port = _free_port()
        while self.daemon_port == self.sshd_port:
            self.daemon_port = _free_port()
        log_path = self.base / "sshd.log"
        self._log_file = open(log_path, "wb")
        username = getpass.getuser()
        command = [
            sshd, "-D", "-e", "-f", "/dev/null",
            "-h", str(host_key),
            "-p", str(self.sshd_port),
            "-o", "ListenAddress=127.0.0.1",
            "-o", "PidFile=%s" % (self.base / "sshd.pid"),
            "-o", "AuthorizedKeysFile=%s" % authorized_keys,
            "-o", "StrictModes=no",
            "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
            "-o", "PubkeyAuthentication=yes",
            "-o", "UsePAM=no",
            "-o", "PermitRootLogin=no",
            "-o", "Subsystem=sftp internal-sftp",
            "-o", "AllowUsers=%s" % username,
            "-o", "LogLevel=ERROR",
        ]
        self.proc = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=self._log_file,
            stderr=self._log_file)

        ssh_args = [
            "-i", str(client_key),
            "-o", "IdentitiesOnly=yes",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "ControlMaster=no",
            "-o", "ControlPath=none",
        ]
        self.profile_raw = {
            "host": "127.0.0.1",
            "port": self.sshd_port,
            "user": username,
            "root": str(self.workspace),
            "ssh_bin": ssh,
            "connect_timeout": 2,
            "op_timeout": 5,
            "exec_timeout": 5,
            "max_read_bytes": 1024,
            "hard_read_cap": 2048,
            "batch_mode": True,
            "strict_host_key": "no",
            "ssh_args": ssh_args,
        }
        self._wait_until_ready(ssh, username, client_key)

        self.config_path = self.base / "bridge.local.json"
        with open(self.config_path, "w", encoding="utf-8") as config_file:
            json.dump({
                "default_profile": "local-test",
                "daemon_port": self.daemon_port,
                "profiles": {"local-test": self.profile_raw},
            }, config_file, indent=2)
            config_file.write("\n")

        return self

    @staticmethod
    def _generate_key(ssh_keygen, path):
        completed = subprocess.run(
            [ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if completed.returncode != 0:
            raise LocalSshdStartError(
                "ssh-keygen failed: %s"
                % completed.stderr.decode("utf-8", "replace").strip())

    def _wait_until_ready(self, ssh, username, client_key):
        command = [
            ssh,
            "-p", str(self.sshd_port),
            "-i", str(client_key),
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=1",
            "-o", "LogLevel=ERROR",
            "%s@127.0.0.1" % username,
            "true",
        ]
        last_stderr = ""
        for _ in range(50):
            if self.proc.poll() is not None:
                break
            try:
                completed = subprocess.run(
                    command, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, timeout=2)
            except subprocess.TimeoutExpired:
                completed = None
            if completed is not None and completed.returncode == 0:
                return
            if completed is not None:
                last_stderr = completed.stderr.decode("utf-8", "replace").strip()
            time.sleep(0.1)
        log = self.read_log()
        self.stop()
        raise LocalSshdStartError(
            "local sshd did not become ready: %s%s"
            % (last_stderr, "\n" + log if log else ""))

    def read_log(self):
        if self._log_file is not None:
            self._log_file.flush()
        if self.base is None:
            return ""
        try:
            return (self.base / "sshd.log").read_text(
                encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""

    def stop(self):
        self._shutdown_bridge_daemon()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc = None
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None
        if self._temp is not None:
            self._temp.cleanup()
            self._temp = None

    def _shutdown_bridge_daemon(self):
        if self.daemon_port is None:
            return
        try:
            with socket.create_connection(
                    ("127.0.0.1", self.daemon_port), timeout=0.2) as sock:
                sock.sendall(b'{"op":"shutdown"}\n')
                sock.settimeout(0.5)
                sock.recv(4096)
        except OSError:
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, _exc_type, _exc_value, _traceback):
        _ = (_exc_type, _exc_value, _traceback)
        self.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="启动隔离的本地 OpenSSH/SFTP 测试环境")
    parser.parse_args(argv)
    server = LocalSshd()
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        server.start()
        project_root = Path(__file__).resolve().parents[1]
        print("隔离测试环境已启动")
        print("配置文件: %s" % server.config_path)
        print("工作区: %s" % server.workspace)
        print("测试命令:")
        print("  %s %s --config %s ls /"
              % (sys.executable, project_root / "remote.py",
                 server.config_path))
        print("Daemon 测试命令:")
        print("  SSHBRIDGE_STATE_DIR=%s %s %s --config %s daemon start"
              % (server.daemon_state, sys.executable,
                 project_root / "remote.py", server.config_path))
        print("按 Ctrl-C 停止并清理。")
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0
    except (LocalSshdUnavailable, LocalSshdStartError) as exc:
        print("无法启动隔离测试环境: %s" % exc, file=sys.stderr)
        return 1
    finally:
        server.stop()


if __name__ == "__main__":
    sys.exit(main())
