"""OpenSSH ControlMaster lifecycle for the local connection broker."""

import copy
import os
import subprocess
import threading
import time

from .errors import BridgeError


class OpenSSHTransport:
    def __init__(self, profile, control_path, log_path):
        self.profile = profile
        self.control_path = control_path
        self.log_path = log_path
        self.multiplexing = bool(
            profile.connection_policy["control_master"]
            and os.name != "nt")
        self.degraded_reason = (
            None if self.multiplexing
            else "OpenSSH ControlMaster is disabled")
        self._process = None
        self._log_file = None
        self._lock = threading.Lock()

        self.channel_profile = copy.copy(profile)
        if self.multiplexing:
            self.channel_profile.control_path = control_path

    def start(self):
        """Start one master TCP connection. No retries happen here."""
        if not self.multiplexing:
            return
        with self._lock:
            if self.is_alive():
                return
            self._cleanup_process()
            self._remove_control_socket()
            try:
                self._log_file = open(self.log_path, "ab", buffering=0)
                self._process = subprocess.Popen(
                    self.profile.master_argv(self.control_path),
                    stdin=subprocess.DEVNULL,
                    stdout=self._log_file,
                    stderr=self._log_file)
            except FileNotFoundError:
                self._cleanup_process()
                raise BridgeError(
                    "SSH_ERROR",
                    "ssh binary not found: %r" % self.profile.ssh_bin)
            except OSError as error:
                self._cleanup_process()
                raise BridgeError(
                    "SSH_ERROR", "cannot start ssh master: %s" % error)

            deadline = time.monotonic() + self.profile.connect_timeout + 2
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    message = self._log_tail()
                    self._cleanup_process()
                    if _multiplex_unsupported(message):
                        self._disable_multiplexing(message)
                        return
                    raise BridgeError(
                        "SSH_ERROR",
                        "ssh master exited before ready"
                        + (": " + message if message else ""))
                if os.path.exists(self.control_path) and self._check_master():
                    return
                time.sleep(0.05)

            self._stop_process()
            raise BridgeError(
                "TIMEOUT",
                "ssh master did not become ready within %ss"
                % self.profile.connect_timeout)

    def is_alive(self):
        if not self.multiplexing:
            return True
        return (
            self._process is not None
            and self._process.poll() is None
            and os.path.exists(self.control_path)
        )

    def stop(self):
        with self._lock:
            if self.multiplexing and os.path.exists(self.control_path):
                try:
                    subprocess.run(
                        self.profile.control_argv(self.control_path, "exit"),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=3)
                except Exception:
                    pass
            self._stop_process()

    def _check_master(self):
        try:
            completed = subprocess.run(
                self.profile.control_argv(self.control_path, "check"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2)
            return completed.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _stop_process(self):
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=3)
        self._cleanup_process()
        self._remove_control_socket()

    def _cleanup_process(self):
        self._process = None
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None

    def _remove_control_socket(self):
        try:
            os.remove(self.control_path)
        except FileNotFoundError:
            pass

    def _log_tail(self):
        if self._log_file is not None:
            try:
                self._log_file.flush()
            except OSError:
                pass
        try:
            with open(self.log_path, "rb") as log_file:
                log_file.seek(0, os.SEEK_END)
                size = log_file.tell()
                log_file.seek(max(0, size - 800))
                return log_file.read().decode("utf-8", "replace").strip()
        except OSError:
            return ""

    def _disable_multiplexing(self, message):
        self.multiplexing = False
        self.degraded_reason = (
            "OpenSSH ControlMaster is unavailable"
            + (": " + message[-300:] if message else ""))
        self.channel_profile.control_path = None
        self._remove_control_socket()


def _multiplex_unsupported(message):
    text = (message or "").lower()
    markers = (
        "bad configuration option: controlmaster",
        "bad configuration option: controlpath",
        "multiplexing not supported",
        "unknown option -- m",
    )
    return any(marker in text for marker in markers)
