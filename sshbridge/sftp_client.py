"""SFTP v3 client speaking over `ssh -s sftp` (system OpenSSH does all auth).

Everything about authentication, ~/.ssh/config, ssh-agent, ProxyJump,
known_hosts and (where supported) ControlMaster multiplexing is delegated
to the ssh binary; this module only speaks the SFTP protocol itself.
"""

import struct
import os
import subprocess
import threading
import time
from contextlib import contextmanager

from . import sftp_proto as P
from .errors import BridgeError

_MAX_PACKET = 64 * 1024 * 1024

_STATUS_CODE_MAP = {
    P.FX_EOF: "EOF",
    P.FX_NO_SUCH_FILE: "NOT_FOUND",
    P.FX_PERMISSION_DENIED: "PERMISSION_DENIED",
    P.FX_FAILURE: "SFTP_ERROR",
    P.FX_BAD_MESSAGE: "SFTP_ERROR",
    P.FX_NO_CONNECTION: "SSH_ERROR",
    P.FX_CONNECTION_LOST: "SSH_ERROR",
    P.FX_OP_UNSUPPORTED: "SFTP_ERROR",
}


def _sftp_err(code, msg):
    return BridgeError(_STATUS_CODE_MAP.get(code, "SFTP_ERROR"), msg, sftp_status=code)


def _is_connect_failure(err):
    """True for pre-auth transport failures that are safe to retry."""
    if not isinstance(err, BridgeError) or err.code not in ("SSH_ERROR", "TIMEOUT"):
        return False
    text = err.message.lower()
    terminal = (
        "permission denied",
        "host key verification failed",
        "remote host identification has changed",
        "no matching host key type found",
        "too many authentication failures",
    )
    if any(marker in text for marker in terminal):
        return False
    markers = ("connection closed", "connection reset", "connection refused",
               "connection timed out", "kex_exchange_identification",
               "banner exchange")
    return any(m in text for m in markers)


class SftpSession:
    def __init__(self, argv, op_timeout=60, connect_retries=2, retry_delay=3.0):
        self.op_timeout = op_timeout
        self._id = 0
        self._timed_out = False
        self._closed = False
        self._stderr = bytearray()
        last_err = None
        for attempt in range(connect_retries + 1):
            self._spawn(argv)
            try:
                self.version = self._handshake()
                return
            except BridgeError as e:
                last_err = e
                self._hard_shutdown()
                if not _is_connect_failure(e) or attempt == connect_retries:
                    raise
                time.sleep(retry_delay * (attempt + 1))
        raise last_err

    def _spawn(self, argv):
        self._timed_out = False
        self._closed = False
        self._stderr = bytearray()
        try:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        except FileNotFoundError:
            raise BridgeError("SSH_ERROR", "ssh binary not found: %r" % argv[0])
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    # -- transport ---------------------------------------------------------

    def _drain_stderr(self):
        try:
            while True:
                chunk = self.proc.stderr.read(4096)
                if not chunk:
                    break
                self._stderr.extend(chunk)
        except Exception:
            pass

    def stderr_text(self):
        return self._stderr.decode("utf-8", "replace").strip()

    def _kill(self):
        self._timed_out = True
        try:
            self.proc.kill()
        except Exception:
            pass

    @contextmanager
    def _watchdog(self):
        timer = threading.Timer(self.op_timeout, self._kill)
        timer.daemon = True
        timer.start()
        try:
            yield
        finally:
            timer.cancel()

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.proc.stdout.read(n - len(buf))
            if not chunk:
                time.sleep(0.2)  # let the stderr drain thread catch up
                if self._timed_out:
                    raise BridgeError("TIMEOUT",
                                      "sftp operation exceeded %ss" % self.op_timeout)
                tail = self.stderr_text()[-400:]
                raise BridgeError(
                    "SSH_ERROR",
                    "sftp connection closed unexpectedly" + (": " + tail if tail else ""))
            buf += chunk
        return bytes(buf)

    def _recv_raw(self):
        (ln,) = struct.unpack(">I", self._recv_exact(4))
        if ln < 1 or ln > _MAX_PACKET:
            raise BridgeError("SSH_ERROR", "bad sftp packet length: %d" % ln)
        body = self._recv_exact(ln)
        return body[0], body[1:]

    def _request(self, ptype, payload):
        self._id += 1
        rid = self._id
        body = P.u32(rid) + payload
        frame = P.u32(1 + len(body)) + bytes([ptype]) + body
        with self._watchdog():
            try:
                self.proc.stdin.write(frame)
                self.proc.stdin.flush()
            except OSError:
                raise BridgeError("SSH_ERROR", "sftp connection is not writable")
            while True:
                t, rest = self._recv_raw()
                if len(rest) < 4:
                    raise BridgeError("SSH_ERROR", "malformed sftp reply")
                (got,) = struct.unpack(">I", rest[:4])
                if got == rid:
                    return t, rest[4:]
                # replies to stale ids should not happen (we are sequential)

    def _status(self, t, body, op):
        if t != P.FXP_STATUS:
            raise BridgeError("SSH_ERROR", "%s: unexpected reply type %d" % (op, t))
        r = P.Reader(body)
        code = r.u32()
        try:
            msg = r.string().decode("utf-8", "replace")
        except Exception:
            msg = ""
        if code == P.FX_OK:
            return
        raise _sftp_err(code, "%s: %s" % (op, msg or "sftp error %d" % code))

    def _handshake(self):
        with self._watchdog():
            try:
                self.proc.stdin.write(struct.pack(">IB", 5, P.FXP_INIT) + P.u32(3))
                self.proc.stdin.flush()
            except OSError:
                raise BridgeError("SSH_ERROR", "sftp connection is not writable")
            t, body = self._recv_raw()
        if t != P.FXP_VERSION:
            raise BridgeError("SSH_ERROR", "unexpected sftp handshake reply type %d" % t)
        ver = P.Reader(body).u32()
        if ver < 3:
            raise BridgeError("SSH_ERROR", "server sftp version too old: %d" % ver)
        return ver

    # -- operations --------------------------------------------------------

    def realpath(self, path):
        t, body = self._request(P.FXP_REALPATH, P.pstr(path))
        if t != P.FXP_NAME:
            self._status(t, body, "realpath")
            raise BridgeError("SSH_ERROR", "realpath: unexpected reply type %d" % t)
        r = P.Reader(body)
        if r.u32() < 1:
            raise BridgeError("SSH_ERROR", "realpath: empty reply")
        return r.string().decode("utf-8", "surrogateescape")

    def stat(self, path, follow=True):
        ptype = P.FXP_STAT if follow else P.FXP_LSTAT
        t, body = self._request(ptype, P.pstr(path))
        if t != P.FXP_ATTRS:
            self._status(t, body, "stat")
            raise BridgeError("SSH_ERROR", "stat: unexpected reply type %d" % t)
        return P.Reader(body).attrs()

    def open_handle(self, path, flags, perms=0o644):
        payload = P.pstr(path) + P.u32(flags) + P.attrs_perms_only(perms)
        t, body = self._request(P.FXP_OPEN, payload)
        if t != P.FXP_HANDLE:
            self._status(t, body, "open")
            raise BridgeError("SSH_ERROR", "open: unexpected reply type %d" % t)
        return P.Reader(body).string()

    def read_chunk(self, handle, offset, length):
        t, body = self._request(P.FXP_READ, P.pstr(handle) + P.u64(offset) + P.u32(length))
        if t == P.FXP_DATA:
            return P.Reader(body).string()
        if t == P.FXP_STATUS:
            r = P.Reader(body)
            code = r.u32()
            if code == P.FX_EOF:
                return None
            try:
                msg = r.string().decode("utf-8", "replace")
            except Exception:
                msg = ""
            raise _sftp_err(code, "read: %s" % (msg or "sftp error %d" % code))
        raise BridgeError("SSH_ERROR", "read: unexpected reply type %d" % t)

    def write_chunk(self, handle, offset, data):
        t, body = self._request(P.FXP_WRITE, P.pstr(handle) + P.u64(offset) + P.pstr(data))
        self._status(t, body, "write")

    def close_handle(self, handle):
        t, body = self._request(P.FXP_CLOSE, P.pstr(handle))
        self._status(t, body, "close")

    def list_dir(self, path):
        t, body = self._request(P.FXP_OPENDIR, P.pstr(path))
        if t != P.FXP_HANDLE:
            self._status(t, body, "opendir")
            raise BridgeError("SSH_ERROR", "opendir: unexpected reply type %d" % t)
        handle = P.Reader(body).string()
        entries = []
        try:
            while True:
                t, body = self._request(P.FXP_READDIR, P.pstr(handle))
                if t == P.FXP_STATUS:
                    r = P.Reader(body)
                    code = r.u32()
                    if code == P.FX_EOF:
                        break
                    try:
                        msg = r.string().decode("utf-8", "replace")
                    except Exception:
                        msg = ""
                    raise _sftp_err(code, "readdir: %s" % (msg or "sftp error %d" % code))
                if t != P.FXP_NAME:
                    raise BridgeError("SSH_ERROR", "readdir: unexpected reply type %d" % t)
                r = P.Reader(body)
                for _ in range(r.u32()):
                    name = r.string().decode("utf-8", "surrogateescape")
                    r.string()  # longname, unused
                    entries.append((name, r.attrs()))
        finally:
            try:
                self.close_handle(handle)
            except BridgeError:
                pass
        return [e for e in entries if e[0] not in (".", "..")]

    def mkdir(self, path, perms=0o755):
        t, body = self._request(P.FXP_MKDIR, P.pstr(path) + P.attrs_perms_only(perms))
        self._status(t, body, "mkdir")

    def rename(self, src, dst):
        """Atomic rename that overwrites dst.

        Prefers the posix-rename@openssh.com extension (real POSIX rename,
        supported since OpenSSH 5.6). Plain SFTP v3 RENAME refuses to
        overwrite an existing destination on OpenSSH servers, so the
        fallback removes dst first (non-atomic, only for exotic servers).
        """
        t, body = self._request(
            P.FXP_EXTENDED,
            P.pstr("posix-rename@openssh.com") + P.pstr(src) + P.pstr(dst))
        if t != P.FXP_STATUS:
            raise BridgeError("SSH_ERROR", "rename: unexpected reply type %d" % t)
        r = P.Reader(body)
        code = r.u32()
        if code == P.FX_OK:
            return
        if code == P.FX_OP_UNSUPPORTED:
            return self._rename_v3(src, dst)
        try:
            msg = r.string().decode("utf-8", "replace")
        except Exception:
            msg = ""
        raise _sftp_err(code, "rename: %s" % (msg or "sftp error %d" % code))

    def _rename_v3(self, src, dst):
        try:
            t, body = self._request(P.FXP_RENAME, P.pstr(src) + P.pstr(dst))
            self._status(t, body, "rename")
            return
        except BridgeError as e:
            if e.details.get("sftp_status") != P.FX_FAILURE:
                raise
        # v3 rename refuses to overwrite: remove dst and retry (non-atomic)
        self.remove(dst)
        t, body = self._request(P.FXP_RENAME, P.pstr(src) + P.pstr(dst))
        self._status(t, body, "rename")

    def remove(self, path):
        t, body = self._request(P.FXP_REMOVE, P.pstr(path))
        self._status(t, body, "remove")

    def rmdir(self, path):
        t, body = self._request(P.FXP_RMDIR, P.pstr(path))
        self._status(t, body, "rmdir")

    # -- lifecycle ---------------------------------------------------------

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass
        for pipe in (self.proc.stdout, self.proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except Exception:
                pass

    def _hard_shutdown(self):
        """Immediate teardown for failed handshakes (retry path)."""
        self._closed = True
        try:
            self.proc.kill()
        except Exception:
            pass
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        _ = (_exc_type, _exc_value, _traceback)
        self.shutdown()
