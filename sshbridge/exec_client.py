"""Remote command execution via the system ssh binary (structured output)."""

import shlex
import subprocess
import time

from .errors import BridgeError

# ssh stderr markers that mean the connection failed *before* the command
# could have started (safe to retry). Never retry on anything else, or a
# command might run twice.
_PRE_AUTH_MARKERS = (
    "kex_exchange_identification",
    "banner exchange",
    "connection refused",
    "connection timed out",
    "connect to host",
    "connect to ",
)


def _decode(b):
    return (b or b"").decode("utf-8", "replace")


def _is_pre_auth_failure(stderr_text):
    if isinstance(stderr_text, bytes):
        stderr_text = _decode(stderr_text)
    t = (stderr_text or "").lower()
    return any(m in t for m in _PRE_AUTH_MARKERS)


def is_ssh_transport_failure(stderr_text):
    if _is_pre_auth_failure(stderr_text):
        return True
    if isinstance(stderr_text, bytes):
        stderr_text = _decode(stderr_text)
    text = (stderr_text or "").lower()
    markers = (
        "permission denied",
        "host key verification failed",
        "remote host identification has changed",
        "too many authentication failures",
        "control socket connect",
        "mux_client_request_session",
    )
    return any(marker in text for marker in markers)


def run_exec(profile, command, cwd, timeout, connect_retries=2, retry_delay=3.0):
    """Run `command` on the remote host with cwd as working directory.

    Returns {"exit_code", "stdout", "stderr", "timed_out"}.
    On timeout the local ssh process is killed; the remote process may keep
    running (a limitation of plain OpenSSH without remote cooperation).
    """
    remote = "cd %s && %s" % (shlex.quote(cwd), command)
    argv = profile.exec_argv(remote)
    attempts = connect_retries + 1
    for attempt in range(attempts):
        try:
            cp = subprocess.run(argv, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            return {
                "exit_code": None,
                "stdout": _decode(e.stdout),
                "stderr": _decode(e.stderr),
                "timed_out": True,
            }
        except FileNotFoundError:
            raise BridgeError("SSH_ERROR", "ssh binary not found: %r" % profile.ssh_bin)
        if cp.returncode == 255 and _is_pre_auth_failure(cp.stderr) \
                and attempt < attempts - 1:
            time.sleep(retry_delay * (attempt + 1))
            continue
        break
    return {
        "exit_code": cp.returncode,
        "stdout": _decode(cp.stdout),
        "stderr": _decode(cp.stderr),
        "timed_out": False,
    }
