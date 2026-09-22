"""Current-user Windows named pipes and private runtime files (stdlib only).

Imported only on Windows. Byte streams preserve the Broker JSON protocol.
Overlapped I/O gives connect/read/write bounded waits without helper threads.
"""

import ctypes as C
from ctypes import wintypes as W
import hashlib
import os
import socket
import stat
import time

from .errors import BridgeError

K = C.WinDLL("kernel32", use_last_error=True)
A = C.WinDLL("advapi32", use_last_error=True)
PTR = C.c_void_p
DWORD = W.DWORD
HANDLE = W.HANDLE
INVALID_HANDLE = C.c_void_p(-1).value
OVERLAPPED_FLAG = 0x40000000


class Overlapped(C.Structure):
    _fields_ = [("Internal", C.c_size_t), ("InternalHigh", C.c_size_t),
                ("Offset", DWORD), ("OffsetHigh", DWORD), ("hEvent", HANDLE)]


class SecurityAttributes(C.Structure):
    _fields_ = [("nLength", DWORD), ("lpSecurityDescriptor", PTR),
                ("bInheritHandle", W.BOOL)]


def _api(lib, name, result, *args):
    function = getattr(lib, name)
    function.restype = result
    function.argtypes = args
    return function


_api(K, "GetCurrentProcess", HANDLE)
_api(K, "OpenProcess", HANDLE, DWORD, W.BOOL, DWORD)
_api(K, "CloseHandle", W.BOOL, HANDLE)
_api(K, "LocalFree", PTR, PTR)
_api(K, "CreateDirectoryW", W.BOOL, W.LPCWSTR, C.POINTER(SecurityAttributes))
_api(K, "CreateEventW", HANDLE, PTR, W.BOOL, W.BOOL, W.LPCWSTR)
_api(K, "WaitForSingleObject", DWORD, HANDLE, DWORD)
_api(K, "CancelIoEx", W.BOOL, HANDLE, C.POINTER(Overlapped))
_api(K, "GetOverlappedResult", W.BOOL, HANDLE, C.POINTER(Overlapped), C.POINTER(DWORD), W.BOOL)
_api(K, "CreateNamedPipeW", HANDLE, W.LPCWSTR, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, C.POINTER(SecurityAttributes))
_api(K, "ConnectNamedPipe", W.BOOL, HANDLE, C.POINTER(Overlapped))
_api(K, "CreateFileW", HANDLE, W.LPCWSTR, DWORD, DWORD, C.POINTER(SecurityAttributes), DWORD, DWORD, HANDLE)
_api(K, "ReadFile", W.BOOL, HANDLE, PTR, DWORD, C.POINTER(DWORD), C.POINTER(Overlapped))
_api(K, "WriteFile", W.BOOL, HANDLE, PTR, DWORD, C.POINTER(DWORD), C.POINTER(Overlapped))
_api(K, "GetNamedPipeClientProcessId", W.BOOL, HANDLE, C.POINTER(DWORD))
_api(K, "GetNamedPipeServerProcessId", W.BOOL, HANDLE, C.POINTER(DWORD))
_api(A, "OpenProcessToken", W.BOOL, HANDLE, DWORD, C.POINTER(HANDLE))
_api(A, "GetTokenInformation", W.BOOL, HANDLE, C.c_int, PTR, DWORD, C.POINTER(DWORD))
_api(A, "ConvertSidToStringSidW", W.BOOL, PTR, C.POINTER(PTR))
_api(A, "ConvertStringSecurityDescriptorToSecurityDescriptorW", W.BOOL, W.LPCWSTR, DWORD, C.POINTER(PTR), C.POINTER(DWORD))
_api(A, "GetNamedSecurityInfoW", DWORD, W.LPWSTR, C.c_int, DWORD, C.POINTER(PTR), C.POINTER(PTR), C.POINTER(PTR), C.POINTER(PTR), C.POINTER(PTR))
_api(A, "GetAce", W.BOOL, PTR, DWORD, C.POINTER(PTR))


def _check(ok):
    if not ok:
        raise C.WinError(C.get_last_error())
    return ok


def _handle(value):
    if value is None or value == INVALID_HANDLE:
        raise C.WinError(C.get_last_error())
    return value


def _sid_text(sid):
    text = PTR()
    _check(A.ConvertSidToStringSidW(sid, C.byref(text)))
    try:
        return C.wstring_at(text)
    finally:
        K.LocalFree(text)


def _process_sid(process):
    token = HANDLE()
    _check(A.OpenProcessToken(process, 8, C.byref(token)))  # TOKEN_QUERY
    try:
        size = DWORD()
        A.GetTokenInformation(token, 1, None, 0, C.byref(size))  # TokenUser
        buffer = C.create_string_buffer(size.value)
        _check(A.GetTokenInformation(token, 1, buffer, size, C.byref(size)))
        return _sid_text(C.cast(buffer, C.POINTER(PTR))[0])
    finally:
        K.CloseHandle(token)


def current_sid():
    return _process_sid(K.GetCurrentProcess())


class PrivateSecurity:
    def __init__(self, directory=False):
        self.descriptor = PTR()
        sid = current_sid()
        inheritance = "OICI" if directory else ""
        sddl = "O:%sD:P(A;%s;GA;;;%s)" % (sid, inheritance, sid)
        _check(A.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, C.byref(self.descriptor), None))
        self.attributes = SecurityAttributes(
            C.sizeof(SecurityAttributes), self.descriptor, False)

    def close(self):
        if self.descriptor:
            K.LocalFree(self.descriptor)
            self.descriptor = PTR()


def check_private_path(path, directory=False):
    """Reject reparse points, foreign owners and ACLs granting other principals."""
    info = os.lstat(path)
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise BridgeError("BROKER_UNAVAILABLE", "unsafe broker runtime file type")
    owner, dacl, descriptor = PTR(), PTR(), PTR()
    error = A.GetNamedSecurityInfoW(
        path, 1, 5, C.byref(owner), None, C.byref(dacl), None, C.byref(descriptor))
    if error:
        raise C.WinError(error)
    try:
        sid = current_sid()
        if _sid_text(owner) != sid or not dacl:
            raise BridgeError("BROKER_UNAVAILABLE", "unsafe broker runtime ownership or ACL")
        # ACL header: revision/unused (2 bytes), size (WORD), ACE count (WORD).
        count = C.c_ushort.from_address(dacl.value + 4).value
        if not count:
            raise BridgeError("BROKER_UNAVAILABLE", "broker runtime ACL has no user grant")
        for index in range(count):
            ace = PTR()
            _check(A.GetAce(dacl, index, C.byref(ace)))
            # Only ACCESS_ALLOWED_ACE for the current user is accepted.
            if C.c_ubyte.from_address(ace.value).value != 0 or _sid_text(ace.value + 8) != sid:
                raise BridgeError("BROKER_UNAVAILABLE", "broker runtime ACL permits another principal")
    finally:
        K.LocalFree(descriptor)


def private_runtime_dir(configured=None):
    if configured:
        path = os.path.abspath(os.path.expanduser(configured))
    else:
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise BridgeError("BROKER_UNAVAILABLE", "LOCALAPPDATA is unavailable")
        path = os.path.join(base, "SSHBridge", "run")
    # Create each missing component with its private ACL from the outset.
    missing = []
    ancestor = path
    while not os.path.exists(ancestor):
        missing.append(ancestor)
        parent = os.path.dirname(ancestor)
        if parent == ancestor:
            raise BridgeError("BROKER_UNAVAILABLE", "invalid broker runtime directory")
        ancestor = parent
    security = PrivateSecurity(directory=True)
    try:
        for component in reversed(missing):
            if not K.CreateDirectoryW(component, C.byref(security.attributes)):
                if C.get_last_error() != 183:  # another starter created it
                    raise C.WinError(C.get_last_error())
            check_private_path(component, directory=True)
        check_private_path(path, directory=True)
    except OSError as error:
        raise BridgeError("BROKER_UNAVAILABLE", "cannot prepare private Windows runtime: %s" % error)
    finally:
        security.close()
    return path


def private_file_descriptor(path, exclusive=False):
    import msvcrt
    security = PrivateSecurity()
    try:
        handle = _handle(K.CreateFileW(
            path, 0xC0000000, 3, C.byref(security.attributes),
            1 if exclusive else 4, 0x80, None))
        try:
            check_private_path(path)
            return msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        except BaseException:
            K.CloseHandle(handle)
            raise
    finally:
        security.close()


def pipe_name(runtime, fingerprint):
    identity = current_sid() + "\0" + os.path.normcase(os.path.abspath(runtime))
    user_runtime = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return "\\\\.\\pipe\\sshbridge-%s-%s" % (user_runtime, fingerprint)


class PendingIO:
    def __init__(self, handle):
        self.handle = handle
        self.overlapped = Overlapped()
        self.overlapped.hEvent = _handle(K.CreateEventW(None, True, False, None))
        self.pending = False

    def finish(self, timeout):
        milliseconds = max(0, min(int(timeout * 1000), 0xFFFFFFFE))
        waited = K.WaitForSingleObject(self.overlapped.hEvent, milliseconds)
        if waited == 258:
            raise socket.timeout("named pipe I/O timed out")
        if waited != 0:
            raise C.WinError(C.get_last_error())
        count = DWORD()
        _check(K.GetOverlappedResult(self.handle, C.byref(self.overlapped), C.byref(count), False))
        self.pending = False
        return count.value

    def close(self):
        if self.pending:
            # Keep OVERLAPPED and buffers alive until cancellation completes.
            K.CancelIoEx(self.handle, C.byref(self.overlapped))
            count = DWORD()
            K.GetOverlappedResult(self.handle, C.byref(self.overlapped), C.byref(count), True)
            self.pending = False
        K.CloseHandle(self.overlapped.hEvent)


class PipeStream:
    def __init__(self, handle):
        self.handle = handle
        self.timeout = 120.0

    def settimeout(self, timeout):
        self.timeout = timeout

    def _io(self, function, buffer, length):
        pending = PendingIO(self.handle)
        count = DWORD()
        try:
            if function(self.handle, buffer, length, C.byref(count), C.byref(pending.overlapped)):
                return count.value
            error = C.get_last_error()
            if error in (109, 232):  # broken/disconnected pipe
                return 0
            if error != 997:
                raise C.WinError(error)
            pending.pending = True
            try:
                return pending.finish(self.timeout)
            except OSError as failure:
                if getattr(failure, "winerror", None) in (109, 232):
                    return 0
                raise
        finally:
            pending.close()

    def recv(self, length):
        buffer = C.create_string_buffer(length)
        count = self._io(K.ReadFile, buffer, length)
        return buffer.raw[:count]

    def sendall(self, data):
        offset = 0
        while offset < len(data):
            chunk = data[offset:offset + 65536]
            count = self._io(K.WriteFile, chunk, len(chunk))
            if not count:
                raise OSError("named pipe closed during write")
            offset += count

    def verify_peer(self, server=False, expected_pid=None):
        pid = DWORD()
        query = K.GetNamedPipeServerProcessId if server else K.GetNamedPipeClientProcessId
        _check(query(self.handle, C.byref(pid)))
        if expected_pid is not None and pid.value != expected_pid:
            raise BridgeError("BROKER_UNAVAILABLE", "named pipe server identity mismatch")
        process = _handle(K.OpenProcess(0x1000, False, pid.value))
        try:
            if _process_sid(process) != current_sid():
                raise BridgeError("BROKER_UNAVAILABLE", "named pipe peer user is not authorized")
        finally:
            K.CloseHandle(process)

    def close(self):
        if self.handle is not None:
            K.CloseHandle(self.handle)
            self.handle = None


def connect(path, timeout, expected_pid):
    deadline = time.monotonic() + timeout
    while True:
        # Identification-only prevents a fake server impersonating the client.
        handle = K.CreateFileW(
            path, 0xC0000000, 0, None, 3,
            OVERLAPPED_FLAG | 0x100000 | 0x10000, None)
        if handle != INVALID_HANDLE:
            stream = PipeStream(_handle(handle))
            try:
                stream.verify_peer(server=True, expected_pid=expected_pid)
                return stream
            except BaseException:
                stream.close()
                raise
        error = C.get_last_error()
        if error not in (2, 231) or time.monotonic() >= deadline:
            raise C.WinError(error)
        time.sleep(0.02)


class PipeListener:
    def __init__(self, path):
        self.path = path
        self.handle = None
        self.pending = None
        self.timeout = 0.5
        self.security = PrivateSecurity()
        try:
            self.handle = self._create(first=True)
        except BaseException:
            self.security.close()
            raise

    def _create(self, first=False):
        return _handle(K.CreateNamedPipeW(
            self.path, 3 | OVERLAPPED_FLAG | (0x80000 if first else 0),
            8, 255, 65536, 65536, 0, C.byref(self.security.attributes)))

    def accept(self):
        if self.pending is None:
            self.pending = PendingIO(self.handle)
            if not K.ConnectNamedPipe(self.handle, C.byref(self.pending.overlapped)):
                error = C.get_last_error()
                if error == 997:
                    self.pending.pending = True
                elif error != 535:  # connected before ConnectNamedPipe
                    self.pending.close()
                    self.pending = None
                    raise C.WinError(error)
        if self.pending.pending:
            self.pending.finish(self.timeout)
        self.pending.close()
        self.pending = None
        connected = self.handle
        # Retain an instance continuously, preventing pipe-name takeover.
        try:
            self.handle = self._create()
        except BaseException:
            K.CloseHandle(connected)
            self.handle = None
            raise
        return PipeStream(connected), None

    def close(self):
        if self.pending is not None:
            self.pending.close()
            self.pending = None
        if self.handle is not None:
            K.CloseHandle(self.handle)
            self.handle = None
        self.security.close()
