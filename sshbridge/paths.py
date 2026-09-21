"""Sandbox (chroot-style) path resolution.

The workspace path "/" maps to the configured sandbox root on the remote
server. ".." clamps at the root, so no lexical path can escape. After the
server canonicalizes a path (SFTP REALPATH resolves symlinks), containment
is checked again before any operation.
"""

from .errors import BridgeError


def normalize_root(root):
    root = (root or "").replace("\\", "/").strip()
    if not root.startswith("/"):
        raise BridgeError("INVALID_CONFIG",
                          "sandbox root must be an absolute remote path: %r" % root)
    parts = [p for p in root.split("/") if p not in ("", ".")]
    if not parts:
        raise BridgeError("INVALID_CONFIG",
                          'sandbox root must not be "/" (refusing to expose the whole server)')
    if any(p == ".." for p in parts):
        raise BridgeError("INVALID_CONFIG",
                          "sandbox root must not contain '..': %r" % root)
    return "/" + "/".join(parts)


def resolve_virtual(virtual, root):
    """Map a workspace path onto the real remote path (chroot semantics)."""
    v = (virtual or "").replace("\\", "/")
    if not v.strip():
        raise BridgeError("INVALID_PATH", "empty path")
    if "\x00" in v:
        raise BridgeError("INVALID_PATH", "path contains NUL byte")
    parts = []
    for seg in v.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()  # clamp at sandbox root
            continue
        parts.append(seg)
    if not parts:
        return root
    return root + "/" + "/".join(parts)


def ensure_within_root(real, root):
    if real != root and not real.startswith(root + "/"):
        raise BridgeError(
            "SANDBOX_VIOLATION",
            "path resolves outside the sandbox root: %r is not under %r" % (real, root),
            real_path=real, sandbox_root=root)


def parent_of(real):
    i = real.rfind("/")
    return real[:i] if i > 0 else "/"


def basename_of(real):
    return real.rsplit("/", 1)[-1]


def join(parent, name):
    return parent.rstrip("/") + "/" + name
