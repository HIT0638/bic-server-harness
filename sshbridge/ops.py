"""Bridge operations: the reusable API layer (CLI today, MCP tools later).

All functions take a Profile plus plain parameters and return plain dicts
(JSON-ready). File operations go through the SFTP subsystem; command
execution goes through the system ssh binary.
"""

import base64
import shlex
import uuid
from contextlib import contextmanager

from . import sftp_proto as P
from .errors import BridgeError
from .exec_client import run_exec
from .paths import (basename_of, ensure_within_root, join, parent_of,
                    resolve_virtual)
from .sftp_client import SftpSession

CHUNK = 32768


def _session(profile):
    return SftpSession(profile.sftp_argv(), profile.op_timeout)


@contextmanager
def _maybe_session(profile, session):
    """Use the caller-provided session (daemon reuse) or open a fresh one."""
    if session is None:
        with _session(profile) as s:
            yield s
    else:
        yield session


def _canon_root(s, profile):
    canon = s.realpath(profile.root)
    st = s.stat(canon)
    if P.file_type(st.get("perms")) != "dir":
        raise BridgeError("INVALID_CONFIG",
                          "sandbox root is not a directory: %s" % profile.root)
    return canon


def _require_dir(s, path, what="path"):
    st = s.stat(path)
    if P.file_type(st.get("perms")) != "dir":
        raise BridgeError("NOT_A_DIR", "%s is not a directory: %s" % (what, path))


def _entry(name, attrs):
    perms = attrs.get("perms")
    return {
        "name": name,
        "type": P.file_type(perms),
        "size": attrs.get("size"),
        "mode": ("%04o" % (perms & 0o7777)) if perms is not None else None,
        "uid": attrs.get("uid"),
        "gid": attrs.get("gid"),
        "mtime": attrs.get("mtime"),
        "atime": attrs.get("atime"),
    }


def _read_bytes(s, path, offset, length):
    handle = s.open_handle(path, P.FXF_READ)
    out = bytearray()
    try:
        while len(out) < length:
            chunk = s.read_chunk(handle, offset + len(out),
                                 min(CHUNK, length - len(out)))
            if chunk is None:
                break
            out += chunk
    finally:
        try:
            s.close_handle(handle)
        except BridgeError:
            pass
    return bytes(out)


def _sha256(profile, real_path, exec_runner=None):
    runner = exec_runner or run_exec
    res = runner(profile, "sha256sum %s" % shlex.quote(real_path),
                 profile.root, profile.exec_timeout)
    parts = res["stdout"].split()
    if res["exit_code"] != 0 or not parts:
        raise BridgeError(
            "EXEC_FAILED",
            "sha256sum failed (exit %s): %s"
            % (res["exit_code"], res["stderr"].strip()[:200]))
    return parts[0]


# -- operations --------------------------------------------------------------


def op_list_dir(profile, path="/", session=None):
    with _maybe_session(profile, session) as s:
        croot = _canon_root(s, profile)
        canon = s.realpath(resolve_virtual(path, profile.root))
        ensure_within_root(canon, croot)
        _require_dir(s, canon, "list_dir target")
        raw = s.list_dir(canon)
    entries = [_entry(n, a) for n, a in sorted(raw, key=lambda e: e[0])]
    return {"op": "list_dir", "path": path, "real_path": canon, "entries": entries}


def op_stat(profile, path, session=None):
    with _maybe_session(profile, session) as s:
        croot = _canon_root(s, profile)
        real = resolve_virtual(path, profile.root)
        lst = s.stat(real, follow=False)  # existence + symlink-ness of the entry itself
        canon = s.realpath(real)
        ensure_within_root(canon, croot)
        attrs = s.stat(canon)
        result = _entry(basename_of(canon), attrs)
    result["op"] = "stat"
    result["path"] = path
    result["real_path"] = canon
    if P.file_type(lst.get("perms")) == "symlink":
        result["symlink"] = True
    return result


def op_read_file(profile, path, offset=0, limit=None, session=None):
    if offset < 0:
        raise BridgeError("INVALID_ARG", "offset must be >= 0, got %s" % offset)
    if limit is not None and limit < 0:
        raise BridgeError("INVALID_ARG", "limit must be >= 0, got %s" % limit)
    with _maybe_session(profile, session) as s:
        croot = _canon_root(s, profile)
        canon = s.realpath(resolve_virtual(path, profile.root))
        ensure_within_root(canon, croot)
        attrs = s.stat(canon)
        if P.file_type(attrs.get("perms")) != "file":
            raise BridgeError("NOT_A_FILE", "not a regular file: %s" % path)
        size = attrs.get("size") or 0
        remaining = max(0, size - offset)
        if limit is None:
            if remaining > profile.max_read_bytes:
                raise BridgeError(
                    "TOO_LARGE",
                    "%d bytes available at offset %d, exceeds max_read_bytes=%d; "
                    "use --limit/--offset or raise max_read_bytes in the profile"
                    % (remaining, offset, profile.max_read_bytes))
            length = remaining
        else:
            length = min(limit, profile.hard_read_cap)
        data = _read_bytes(s, canon, offset, length)
    return {
        "op": "read_file",
        "path": path,
        "real_path": canon,
        "size": size,
        "mtime": attrs.get("mtime"),
        "offset": offset,
        "length": len(data),
        "truncated": (offset + len(data)) < size,
        "content": data.decode("utf-8", "replace"),
        "content_b64": base64.b64encode(data).decode("ascii"),
    }


def op_write_file(profile, path, data, expected_mtime=None, expected_size=None,
                  expected_hash=None, force=False, session=None,
                  exec_runner=None):
    if not isinstance(data, (bytes, bytearray)):
        raise BridgeError("INVALID_ARG", "write data must be bytes")
    data = bytes(data)
    if expected_hash:
        expected_hash = expected_hash.strip().lower()
        if expected_hash.startswith("sha256:"):
            expected_hash = expected_hash[7:]
    with _maybe_session(profile, session) as s:
        croot = _canon_root(s, profile)
        real = resolve_virtual(path, profile.root)
        parent = s.realpath(parent_of(real))
        ensure_within_root(parent, croot)
        _require_dir(s, parent, "parent directory")
        target = join(parent, basename_of(real))

        current = None
        try:
            current = s.stat(target)
        except BridgeError as e:
            if e.code != "NOT_FOUND":
                raise
        has_expectation = (expected_mtime is not None or expected_size is not None
                           or bool(expected_hash))
        if has_expectation and not force:
            if current is None:
                raise BridgeError(
                    "CONFLICT",
                    "file does not exist but an existing version was expected (deleted?)",
                    path=path, current=None)
            problems = []
            if expected_mtime is not None and current.get("mtime") != expected_mtime:
                problems.append("mtime expected %s, current %s"
                                % (expected_mtime, current.get("mtime")))
            if expected_size is not None and current.get("size") != expected_size:
                problems.append("size expected %s, current %s"
                                % (expected_size, current.get("size")))
            if expected_hash \
                    and _sha256(profile, target, exec_runner) != expected_hash:
                problems.append("sha256 mismatch")
            if problems:
                raise BridgeError(
                    "CONFLICT",
                    "remote file changed since it was read: " + "; ".join(problems),
                    path=path,
                    current={"mtime": current.get("mtime"),
                             "size": current.get("size")})

        tmp = join(parent, ".sshbridge.tmp." + uuid.uuid4().hex[:12])
        handle = None
        try:
            handle = s.open_handle(tmp, P.FXF_WRITE | P.FXF_CREAT | P.FXF_TRUNC)
            for off in range(0, len(data), CHUNK):
                s.write_chunk(handle, off, data[off:off + CHUNK])
            s.close_handle(handle)
            handle = None
            s.rename(tmp, target)  # atomic replace within the same directory
        except BaseException:
            if handle is not None:
                try:
                    s.close_handle(handle)
                except BridgeError:
                    pass
            try:
                s.remove(tmp)
            except BridgeError:
                pass
            raise
        new = s.stat(target)
    return {
        "op": "write_file",
        "path": path,
        "real_path": target,
        "bytes_written": len(data),
        "size": new.get("size"),
        "mtime": new.get("mtime"),
        "overwrote": current is not None,
    }


def op_mkdir(profile, path, parents=False, session=None):
    with _maybe_session(profile, session) as s:
        croot = _canon_root(s, profile)
        real = resolve_virtual(path, profile.root)
        if real == profile.root:
            raise BridgeError("EXISTS", "path is the sandbox root")
        if parents:
            cur = croot
            rel = real[len(profile.root):].strip("/")
            for seg in [x for x in rel.split("/") if x]:
                cur = join(cur, seg)
                try:
                    st = s.stat(cur, follow=False)
                    if P.file_type(st.get("perms")) != "dir":
                        raise BridgeError("NOT_A_DIR",
                                          "exists and is not a directory: %s" % cur)
                    continue
                except BridgeError as e:
                    if e.code != "NOT_FOUND":
                        raise
                s.mkdir(cur)
            made = cur
        else:
            parent = s.realpath(parent_of(real))
            ensure_within_root(parent, croot)
            _require_dir(s, parent, "parent directory")
            target = join(parent, basename_of(real))
            try:
                s.stat(target, follow=False)
                raise BridgeError("EXISTS", "already exists: %s" % path)
            except BridgeError as e:
                if e.code != "NOT_FOUND":
                    raise
            s.mkdir(target)
            made = target
    return {"op": "mkdir", "path": path, "real_path": made}


def op_move(profile, src, dst, force=False, session=None):
    with _maybe_session(profile, session) as s:
        croot = _canon_root(s, profile)
        sreal = resolve_virtual(src, profile.root)
        sparent = s.realpath(parent_of(sreal))
        ensure_within_root(sparent, croot)
        _require_dir(s, sparent, "source parent")
        sfinal = join(sparent, basename_of(sreal))
        try:
            s.stat(sfinal, follow=False)
        except BridgeError as e:
            if e.code == "NOT_FOUND":
                raise BridgeError("NOT_FOUND", "source does not exist: %s" % src)
            raise
        dreal = resolve_virtual(dst, profile.root)
        dparent = s.realpath(parent_of(dreal))
        ensure_within_root(dparent, croot)
        _require_dir(s, dparent, "destination parent")
        dfinal = join(dparent, basename_of(dreal))
        if sfinal == dfinal:
            return {"op": "move", "src": src, "dst": dst,
                    "real_path": dfinal, "noop": True}
        try:
            s.stat(dfinal, follow=False)
            dst_exists = True
        except BridgeError as e:
            if e.code != "NOT_FOUND":
                raise
            dst_exists = False
        if dst_exists and not force:
            raise BridgeError("EXISTS",
                              "destination already exists: %s (use --force to overwrite)"
                              % dst)
        s.rename(sfinal, dfinal)
    return {"op": "move", "src": src, "dst": dst,
            "real_path": dfinal, "overwrote": dst_exists}


def op_delete(profile, path, session=None):
    with _maybe_session(profile, session) as s:
        croot = _canon_root(s, profile)
        real = resolve_virtual(path, profile.root)
        if real == profile.root:
            raise BridgeError(
                "INVALID_ARG", "cannot delete the sandbox root")
        parent = s.realpath(parent_of(real))
        ensure_within_root(parent, croot)
        _require_dir(s, parent, "parent directory")
        target = join(parent, basename_of(real))
        attrs = s.stat(target, follow=False)
        canon = s.realpath(target)
        ensure_within_root(canon, croot)
        entry_type = P.file_type(attrs.get("perms"))
        if entry_type == "dir":
            if s.list_dir(target):
                raise BridgeError(
                    "NOT_EMPTY", "directory is not empty: %s" % path)
            s.rmdir(target)
        else:
            s.remove(target)
    return {
        "op": "delete",
        "path": path,
        "real_path": target,
        "type": entry_type,
    }


def normalize_exec_request(profile, command, cwd="/", timeout=None):
    """Validate and map one Exec request without performing remote I/O."""
    command = (command or "").strip()
    if not command:
        raise BridgeError("INVALID_ARG", "empty command")
    timeout = profile.exec_timeout if timeout is None else timeout
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or timeout <= 0:
        raise BridgeError("INVALID_ARG", "timeout must be > 0")
    return {
        "command": command,
        "cwd": cwd,
        "real_cwd": resolve_virtual(cwd, profile.root),
        "timeout": timeout,
    }


def op_exec(profile, command, cwd="/", timeout=None, exec_runner=None):
    """Run a shell command remotely.

    Note: exec accepts arbitrary commands by design, so the cwd sandbox is
    lexical only (resolve_virtual clamp) - no extra SFTP connection is spent
    validating it; a bad cwd fails naturally via `cd` in the structured
    stderr. File operations keep the strict REALPATH-based sandbox.
    """
    request = normalize_exec_request(profile, command, cwd, timeout)
    runner = exec_runner or run_exec
    res = runner(
        profile, request["command"], request["real_cwd"], request["timeout"])
    out = {
        "op": "exec",
        "cwd": request["cwd"],
        "real_cwd": request["real_cwd"],
        "timeout": request["timeout"],
        "exit_code": res["exit_code"],
        "stdout": res["stdout"],
        "stderr": res["stderr"],
        "timed_out": res["timed_out"],
    }
    if "output_truncated" in res:
        out["output_truncated"] = bool(res["output_truncated"])
    if res["timed_out"]:
        out["note"] = ("local ssh was killed on timeout; "
                       "the remote process may still be running")
    elif res["exit_code"] == 255:
        out["note"] = ("exit code 255 may be an ssh-level failure "
                       "rather than the command's status")
    return out


def op_hash(profile, path, session=None, exec_runner=None):
    with _maybe_session(profile, session) as s:
        croot = _canon_root(s, profile)
        canon = s.realpath(resolve_virtual(path, profile.root))
        ensure_within_root(canon, croot)
        st = s.stat(canon)
        if P.file_type(st.get("perms")) != "file":
            raise BridgeError("NOT_A_FILE", "not a regular file: %s" % path)
    digest = _sha256(profile, canon, exec_runner)
    return {"op": "hash", "path": path, "real_path": canon,
            "algo": "sha256", "hash": digest, "size": st.get("size")}
