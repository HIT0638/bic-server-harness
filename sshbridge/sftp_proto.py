"""Minimal SFTP v3 wire-protocol codec (draft-ietf-secsh-filexfer-02)."""

import struct

# packet types
FXP_INIT = 1
FXP_VERSION = 2
FXP_OPEN = 3
FXP_CLOSE = 4
FXP_READ = 5
FXP_WRITE = 6
FXP_LSTAT = 7
FXP_FSTAT = 8
FXP_SETSTAT = 9
FXP_FSETSTAT = 10
FXP_OPENDIR = 11
FXP_READDIR = 12
FXP_REMOVE = 13
FXP_MKDIR = 14
FXP_RMDIR = 15
FXP_REALPATH = 16
FXP_STAT = 17
FXP_RENAME = 18
FXP_READLINK = 19
FXP_SYMLINK = 20
FXP_STATUS = 101
FXP_HANDLE = 102
FXP_DATA = 103
FXP_NAME = 104
FXP_ATTRS = 105
FXP_EXTENDED = 200
FXP_EXTENDED_REPLY = 201

# status codes
FX_OK = 0
FX_EOF = 1
FX_NO_SUCH_FILE = 2
FX_PERMISSION_DENIED = 3
FX_FAILURE = 4
FX_BAD_MESSAGE = 5
FX_NO_CONNECTION = 6
FX_CONNECTION_LOST = 7
FX_OP_UNSUPPORTED = 8

# open flags
FXF_READ = 0x00000001
FXF_WRITE = 0x00000002
FXF_CREAT = 0x00000008
FXF_TRUNC = 0x00000010
FXF_EXCL = 0x00000020

# attribute flags
ATTR_SIZE = 0x00000001
ATTR_UIDGID = 0x00000002
ATTR_PERMISSIONS = 0x00000004
ATTR_ACMODTIME = 0x00000008
ATTR_EXTENDED = 0x80000000

# file types (st_mode bits)
S_IFMT = 0o170000
S_IFDIR = 0o040000
S_IFREG = 0o100000
S_IFLNK = 0o120000


def u32(n):
    return struct.pack(">I", n & 0xFFFFFFFF)


def u64(n):
    return struct.pack(">Q", n & 0xFFFFFFFFFFFFFFFF)


def pstr(b):
    if isinstance(b, str):
        b = b.encode("utf-8", "surrogateescape")
    return u32(len(b)) + b


class Reader:
    """Cursor over one SFTP packet payload."""

    __slots__ = ("buf", "off")

    def __init__(self, data):
        self.buf = data
        self.off = 0

    def _take(self, n):
        if n < 0 or self.off + n > len(self.buf):
            raise ValueError("truncated sftp packet")
        b = self.buf[self.off:self.off + n]
        self.off += n
        return b

    def u32(self):
        return struct.unpack(">I", self._take(4))[0]

    def u64(self):
        return struct.unpack(">Q", self._take(8))[0]

    def string(self):
        return self._take(self.u32())

    def attrs(self):
        flags = self.u32()
        a = {}
        if flags & ATTR_SIZE:
            a["size"] = self.u64()
        if flags & ATTR_UIDGID:
            a["uid"] = self.u32()
            a["gid"] = self.u32()
        if flags & ATTR_PERMISSIONS:
            a["perms"] = self.u32()
        if flags & ATTR_ACMODTIME:
            a["atime"] = self.u32()
            a["mtime"] = self.u32()
        if flags & ATTR_EXTENDED:
            for _ in range(self.u32()):
                self.string()
                self.string()
        return a


def attrs_perms_only(perms):
    return u32(ATTR_PERMISSIONS) + u32(perms)


def file_type(perms):
    fmt = (perms or 0) & S_IFMT
    if fmt == S_IFDIR:
        return "dir"
    if fmt == S_IFLNK:
        return "symlink"
    if fmt == S_IFREG:
        return "file"
    return "other"
