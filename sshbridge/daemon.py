"""Local connection-reuse daemon (the Windows-friendly ControlMaster).

The remote network path rate-limits NEW ssh connections (~15-20/hour), which
a per-command CLI would exhaust immediately. This daemon holds ONE long-lived
`ssh -s sftp` session and serves all file operations over it; the CLI (and
later the MCP layer) transparently routes through it when it is running.

Protocol: newline-delimited JSON over a localhost TCP socket.
Request:  {"op": "list_dir", "args": {"path": "/"}}
Response: {"ok": true, "result": {...}} | {"ok": false, "error": {...}}

Usage: `remote daemon start` / `remote daemon status` / `remote daemon stop`
"""

import base64
import json
import os
import socket
import sys
import threading
import time

from . import ops
from .config import DEFAULT_PORT, Profile, load_config
from .errors import BridgeError
from .sftp_client import SftpSession, _is_connect_failure

_MAX_REQUEST = 256 * 1024 * 1024

_SHUTDOWN = threading.Event()


def _project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pidfile_path():
    return os.path.join(_project_root(), ".bridge-daemon.json")


def _log_path():
    return os.path.join(_project_root(), ".bridge-daemon.log")


def _log(msg):
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# -- request dispatch --------------------------------------------------------


def _dispatch(profile, op, a, session):
    if op == "list_dir":
        return ops.op_list_dir(profile, a.get("path", "/"), session=session)
    if op == "stat":
        return ops.op_stat(profile, a["path"], session=session)
    if op == "read_file":
        return ops.op_read_file(profile, a["path"],
                                offset=a.get("offset", 0),
                                limit=a.get("limit"), session=session)
    if op == "write_file":
        try:
            data = base64.b64decode(a.get("data_b64", ""))
        except Exception:
            raise BridgeError("INVALID_ARG", "bad data_b64")
        return ops.op_write_file(
            profile, a["path"], data,
            expected_mtime=a.get("expected_mtime"),
            expected_size=a.get("expected_size"),
            expected_hash=a.get("expected_hash"),
            force=bool(a.get("force")), session=session)
    if op == "mkdir":
        return ops.op_mkdir(profile, a["path"], parents=bool(a.get("parents")),
                            session=session)
    if op == "move":
        return ops.op_move(profile, a["src"], a["dst"],
                           force=bool(a.get("force")), session=session)
    if op == "exec":
        return ops.op_exec(profile, a["command"],
                           cwd=a.get("cwd", "/"), timeout=a.get("timeout"))
    if op == "hash":
        return ops.op_hash(profile, a["path"], session=session)
    raise BridgeError("INVALID_ARG", "unknown op: %s" % op)


def _run_op(profile, state, op, args):
    """Run one op on the shared session; reconnect once on transport death.

    A cooldown prevents connect-storms from amplifying a network-side
    connection ban (each SftpSession already retries internally).
    """
    with state["lock"]:
        now = time.time()
        if now < state.get("cooldown_until", 0):
            raise BridgeError(
                "SSH_ERROR",
                "server unreachable; retrying paused for %ds "
                "(connection cooldown, fails=%d)"
                % (int(state["cooldown_until"] - now),
                   state.get("cooldown_fails", 0)))
        last_connect_err = None
        for attempt in (0, 1):
            if state["session"] is None or state["session"]._closed:
                try:
                    state["session"] = SftpSession(profile.sftp_argv(),
                                                   profile.op_timeout)
                    state["cooldown_fails"] = 0
                    _log("session (re)connected")
                except BridgeError as e:
                    last_connect_err = e
                    state["session"] = None
                    fails = state.get("cooldown_fails", 0) + 1
                    state["cooldown_fails"] = fails
                    state["cooldown_until"] = time.time() + \
                        min(60 * (2 ** (fails - 1)), 1800)
                    _log("connect failed (%s); cooldown %ds"
                         % (e.code, state["cooldown_until"] - now))
                    raise
            try:
                return _dispatch(profile, op, args, state["session"])
            except BridgeError as e:
                if _is_connect_failure(e) and attempt == 0:
                    _log("session died (%s); reconnecting" % e.code)
                    try:
                        state["session"].shutdown()
                    except Exception:
                        pass
                    state["session"] = None
                    continue
                raise
        raise last_connect_err  # unreachable


# -- socket plumbing ---------------------------------------------------------


def _read_request(conn):
    buf = bytearray()
    while not buf.endswith(b"\n"):
        chunk = conn.recv(1 << 16)
        if not chunk:
            return None if not buf else bytes(buf)
        buf += chunk
        if len(buf) > _MAX_REQUEST:
            raise BridgeError("INVALID_ARG", "request too large")
    return bytes(buf)


def _send(conn, obj):
    conn.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))


def _client(conn, profile, state):
    try:
        conn.settimeout(600)
        raw = _read_request(conn)
        if not raw:
            return
        req = json.loads(raw.decode("utf-8"))
        op = req.get("op")
        if op == "ping":
            with state["lock"]:
                alive = (state["session"] is not None
                         and not state["session"]._closed)
                served = state["ops"]
                started = state["started"]
            _send(conn, {"ok": True, "result": {
                "profile": profile.name,
                "pid": os.getpid(),
                "uptime_s": int(time.time() - started),
                "ops_served": served,
                "sftp_alive": alive,
            }})
            return
        if op == "shutdown":
            _send(conn, {"ok": True, "result": {"bye": True}})
            _SHUTDOWN.set()
            return
        state["ops"] += 1
        result = _run_op(profile, state, op, req.get("args", {}))
        _send(conn, {"ok": True, "result": result})
    except BridgeError as e:
        try:
            _send(conn, {"ok": False, "error": e.to_dict()})
        except Exception:
            pass
    except Exception as e:
        try:
            _send(conn, {"ok": False,
                         "error": {"code": "INTERNAL", "message": repr(e)}})
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def serve(config_path, profile_name, host, port):
    cfg = load_config(config_path)
    profile = Profile(profile_name, cfg["profiles"][profile_name])
    state = {"session": None, "lock": threading.Lock(),
             "ops": 0, "started": time.time(), "cooldown_until": 0}
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((host, port))
    except OSError as e:
        _log("cannot bind %s:%s: %s" % (host, port, e))
        print("daemon: cannot bind %s:%s: %s" % (host, port, e),
              file=sys.stderr)
        return 1
    srv.listen(16)
    srv.settimeout(1.0)
    with open(_pidfile_path(), "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "port": port,
                   "profile": profile.name}, f)
    _log("daemon serving profile=%s on %s:%s" % (profile.name, host, port))
    print("daemon: serving profile=%s on %s:%s" % (profile.name, host, port))
    try:
        while not _SHUTDOWN.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=_client,
                             args=(conn, profile, state), daemon=True).start()
    finally:
        with state["lock"]:
            if state["session"] is not None:
                try:
                    state["session"].shutdown()
                except Exception:
                    pass
        try:
            srv.close()
        except Exception:
            pass
        try:
            os.remove(_pidfile_path())
        except OSError:
            pass
        _log("daemon stopped")
        print("daemon: stopped")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    import argparse
    ap = argparse.ArgumentParser(prog="sshbridge-daemon")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--config", default="bridge.json")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    if not args.serve:
        ap.error("--serve is required")
    cfg = load_config(args.config)
    pname = args.profile or cfg["default_profile"]
    if pname not in cfg["profiles"]:
        print("daemon: unknown profile: %s" % pname, file=sys.stderr)
        return 1
    return serve(args.config, pname, args.host, args.port)


if __name__ == "__main__":
    sys.exit(main())
