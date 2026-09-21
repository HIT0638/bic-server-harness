"""CLI frontend: argparse -> ops -> text/JSON rendering."""

import argparse
import base64
import datetime
import json
import os
import socket
import subprocess
import sys
import time

from . import ops
from .config import DEFAULT_PORT, Profile, load_config
from .daemon import _pidfile_path
from .errors import BridgeError


def _ts(mtime):
    if mtime is None:
        return "-"
    return datetime.datetime.fromtimestamp(
        mtime, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_parser():
    # common flags repeatable after any subcommand; SUPPRESS keeps the value
    # already parsed by the main parser (argparse subparser-default gotcha)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", metavar="FILE", default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)
    common.add_argument("--profile", metavar="NAME", default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)

    ap = argparse.ArgumentParser(
        prog="remote",
        description="SSH Remote Workspace Bridge - treat one sandboxed remote "
                    "directory like a local workspace (SFTP for files, ssh for "
                    "commands). Paths are chroot-style: \"/\" is the sandbox root.")
    ap.add_argument("--config", metavar="FILE",
                    help="bridge config (default: ./bridge.json)")
    ap.add_argument("--profile", metavar="NAME",
                    help="connection profile (default: config default_profile)")
    ap.add_argument("--json", action="store_true",
                    help="emit one JSON object; exit 0 unless the bridge itself "
                         "failed (usable anywhere except as an option value)")
    sub = ap.add_subparsers(dest="op", metavar="COMMAND")
    sub.required = True

    p = sub.add_parser("ls", parents=[common],
                       help="list a single directory (no recursion)")
    p.add_argument("path", nargs="?", default="/")

    p = sub.add_parser("stat", parents=[common],
                       help="stat a file/directory (follows symlinks)")
    p.add_argument("path")

    p = sub.add_parser("read", parents=[common],
                       help="read a file (raw bytes to stdout; JSON gets text+b64)")
    p.add_argument("path")
    p.add_argument("--offset", type=int, default=0, metavar="N")
    p.add_argument("--limit", type=int, default=None, metavar="N",
                   help="max bytes to read (default: profile max_read_bytes)")

    p = sub.add_parser("write", parents=[common],
                       help="atomically write a file (tmp file + rename)")
    p.add_argument("path")
    p.add_argument("--content", metavar="TEXT", help="literal text content (utf-8)")
    p.add_argument("--file", metavar="LOCAL", help="upload this local file (binary-safe)")
    p.add_argument("--expected-mtime", type=int, default=None, metavar="TS",
                   help="fail with CONFLICT if remote mtime differs")
    p.add_argument("--expected-size", type=int, default=None, metavar="N",
                   help="fail with CONFLICT if remote size differs")
    p.add_argument("--expected-hash", metavar="SHA256",
                   help="fail with CONFLICT if remote sha256 differs")
    p.add_argument("--force", action="store_true",
                   help="write even when conflict checks fail")

    p = sub.add_parser("mkdir", parents=[common], help="create a directory")
    p.add_argument("path")
    p.add_argument("-p", "--parents", action="store_true",
                   help="create missing parents (mkdir -p)")

    p = sub.add_parser("mv", parents=[common], help="rename/move inside the sandbox")
    p.add_argument("src")
    p.add_argument("dst")
    p.add_argument("--force", action="store_true", help="overwrite existing destination")

    p = sub.add_parser("exec", parents=[common],
                       help="run a shell command remotely (structured result)")
    p.add_argument("--cwd", default="/", metavar="PATH",
                   help="working directory inside the sandbox (default: /)")
    p.add_argument("--timeout", type=float, default=None, metavar="SEC")
    p.add_argument("cmd", nargs=argparse.REMAINDER, metavar="COMMAND",
                   help="command line (quote it as one argument for exact spacing; "
                        "prefix with -- if it starts with a dash)")

    p = sub.add_parser("hash", parents=[common],
                       help="sha256 of a remote file (for conflict checks)")
    p.add_argument("path")

    p = sub.add_parser("serve", parents=[common],
                       help="start the local Web file explorer")
    p.add_argument("--port", type=int, default=8765, metavar="PORT",
                   help="localhost port (default: 8765; use 0 for a random port)")
    p.add_argument("--no-open", action="store_true",
                   help="do not open the browser automatically")

    p = sub.add_parser("daemon", parents=[common],
                       help="manage the local connection-reuse daemon "
                            "(one persistent SFTP session for all file ops)")
    dsub = p.add_subparsers(dest="daemon_cmd")
    dsub.required = True
    dsub.add_parser("start", parents=[common], help="start the daemon")
    dsub.add_parser("stop", parents=[common], help="stop the daemon")
    dsub.add_parser("status", parents=[common], help="show daemon status")

    return ap


def _gather_write_data(args):
    if args.file is not None:
        try:
            with open(args.file, "rb") as f:
                return f.read()
        except OSError as e:
            raise BridgeError("INVALID_ARG",
                              "cannot read local file %s: %s" % (args.file, e))
    if args.content is not None:
        return args.content.encode("utf-8")
    return sys.stdin.buffer.read()


def _exec_command_text(args):
    parts = list(args.cmd or [])
    if parts and parts[0] == "--":
        parts = parts[1:]
    return " ".join(parts).strip()


def dispatch(args, profile):
    op = args.op
    if op == "ls":
        return ops.op_list_dir(profile, args.path), None
    if op == "stat":
        return ops.op_stat(profile, args.path), None
    if op == "read":
        r = ops.op_read_file(profile, args.path, offset=args.offset, limit=args.limit)
        return r, base64.b64decode(r["content_b64"])
    if op == "write":
        data = _gather_write_data(args)
        r = ops.op_write_file(
            profile, args.path, data,
            expected_mtime=args.expected_mtime, expected_size=args.expected_size,
            expected_hash=args.expected_hash, force=args.force)
        return r, None
    if op == "mkdir":
        return ops.op_mkdir(profile, args.path, parents=args.parents), None
    if op == "mv":
        return ops.op_move(profile, args.src, args.dst, force=args.force), None
    if op == "exec":
        cmd = _exec_command_text(args)
        return ops.op_exec(profile, cmd, cwd=args.cwd, timeout=args.timeout), None
    if op == "hash":
        return ops.op_hash(profile, args.path), None
    raise BridgeError("INVALID_ARG", "unknown command: %s" % op)


# -- daemon routing ------------------------------------------------------------


def _daemon_request(port, request, timeout=120.0):
    s = socket.create_connection(("127.0.0.1", port), timeout=2.0)
    try:
        s.settimeout(timeout)
        s.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = bytearray()
        while not buf.endswith(b"\n"):
            chunk = s.recv(1 << 16)
            if not chunk:
                raise ConnectionError("daemon closed connection unexpectedly")
            buf += chunk
    finally:
        try:
            s.close()
        except Exception:
            pass
    return json.loads(bytes(buf).decode("utf-8"))


def _daemon_build_request(args, profile):
    """Map CLI args to a daemon request; returns (request, client_timeout)."""
    op = args.op
    if op == "ls":
        return {"op": "list_dir", "args": {"path": args.path}}, 120
    if op == "stat":
        return {"op": "stat", "args": {"path": args.path}}, 120
    if op == "read":
        return {"op": "read_file",
                "args": {"path": args.path, "offset": args.offset,
                         "limit": args.limit}}, 300
    if op == "write":
        data = _gather_write_data(args)
        return {"op": "write_file",
                "args": {"path": args.path,
                         "data_b64": base64.b64encode(data).decode("ascii"),
                         "expected_mtime": args.expected_mtime,
                         "expected_size": args.expected_size,
                         "expected_hash": args.expected_hash,
                         "force": args.force}}, 300
    if op == "mkdir":
        return {"op": "mkdir",
                "args": {"path": args.path, "parents": args.parents}}, 120
    if op == "mv":
        return {"op": "move",
                "args": {"src": args.src, "dst": args.dst,
                         "force": args.force}}, 120
    if op == "exec":
        t = args.timeout or profile.exec_timeout
        return {"op": "exec",
                "args": {"command": _exec_command_text(args),
                         "cwd": args.cwd, "timeout": args.timeout}}, t + 60
    if op == "hash":
        return {"op": "hash", "args": {"path": args.path}}, 300
    raise BridgeError("INVALID_ARG", "op not routable: %s" % op)


def _try_daemon(port, pname, args, profile):
    """Route through the local daemon if it is up and serves this profile.

    Returns (result, raw) on success, or None when the daemon is not usable
    (the caller falls back to direct mode). Daemon-side op errors raise
    BridgeError - no silent retries (a write may already have happened).
    """
    try:
        pong = _daemon_request(port, {"op": "ping"}, timeout=2.0)
    except (ConnectionError, OSError, ValueError):
        return None
    if not pong.get("ok") or pong.get("result", {}).get("profile") != pname:
        return None
    req, timeout = _daemon_build_request(args, profile)
    resp = _daemon_request(port, req, timeout=timeout)
    if not resp.get("ok"):
        e = resp.get("error", {})
        raise BridgeError(e.get("code", "INTERNAL"),
                          e.get("message", "daemon error"),
                          **(e.get("details") or {}))
    result = resp["result"]
    raw = base64.b64decode(result["content_b64"]) if args.op == "read" else None
    return result, raw


def _daemon_mgmt(args, cfg, pname):
    port = cfg.get("daemon_port", DEFAULT_PORT)
    cmd = args.daemon_cmd
    if cmd == "status":
        try:
            pong = _daemon_request(port, {"op": "ping"}, timeout=2.0)
            r = pong.get("result", {}) if pong.get("ok") else {}
            return {"action": "status", "running": True, "port": port,
                    "profile": r.get("profile"), "pid": r.get("pid"),
                    "uptime_s": r.get("uptime_s"),
                    "ops_served": r.get("ops_served"),
                    "sftp_alive": r.get("sftp_alive")}, None
        except (ConnectionError, OSError, ValueError):
            return {"action": "status", "running": False, "port": port}, None

    if cmd == "start":
        try:
            pong = _daemon_request(port, {"op": "ping"}, timeout=1.5)
            if pong.get("ok"):
                r = pong.get("result", {})
                return {"action": "start", "already_running": True,
                        "port": port, "profile": r.get("profile"),
                        "pid": r.get("pid")}, None
        except (ConnectionError, OSError, ValueError):
            pass
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        argv = [sys.executable, "-m", "sshbridge.daemon", "--serve",
                "--config", os.path.abspath(cfg["file"]),
                "--profile", pname, "--port", str(port)]
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                       | subprocess.DETACHED_PROCESS)
        subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **kwargs)
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                pong = _daemon_request(port, {"op": "ping"}, timeout=1.0)
                if pong.get("ok"):
                    r = pong.get("result", {})
                    return {"action": "start", "started": True, "port": port,
                            "profile": r.get("profile"),
                            "pid": r.get("pid")}, None
            except (ConnectionError, OSError, ValueError):
                continue
        raise BridgeError("SSH_ERROR",
                          "daemon did not come up within 30s "
                          "(see .bridge-daemon.log)")

    if cmd == "stop":
        try:
            _daemon_request(port, {"op": "shutdown"}, timeout=5.0)
            time.sleep(0.5)
            return {"action": "stop", "stopped": True, "port": port}, None
        except (ConnectionError, OSError, ValueError):
            pass
        note = "not running"
        try:
            with open(_pidfile_path(), "r", encoding="utf-8") as f:
                info = json.load(f)
            pid = info.get("pid")
            if pid:
                try:
                    os.kill(pid, 9)
                    note = "killed stale pid %s" % pid
                except OSError:
                    pass
            os.remove(_pidfile_path())
        except (OSError, ValueError):
            pass
        return {"action": "stop", "stopped": False, "note": note,
                "port": port}, None

    raise BridgeError("INVALID_ARG", "unknown daemon command: %s" % cmd)


def _render_text(args, r):
    op = args.op
    if op == "ls":
        for e in r["entries"]:
            size = e["size"] if e["size"] is not None else "-"
            print("%s %s %12s %s %s"
                  % (e["type"][:1], e["mode"], size, _ts(e["mtime"]), e["name"]))
    elif op == "stat":
        print("%10s: %s" % ("path", r["path"]))
        print("%10s: %s" % ("real_path", r["real_path"]))
        for k in ("type", "mode", "size", "mtime", "atime", "uid", "gid", "symlink"):
            if r.get(k) is None:
                continue
            v = r[k]
            if k in ("mtime", "atime"):
                v = "%s (%s)" % (v, _ts(v))
            print("%10s: %s" % (k, v))
    elif op == "write":
        print("wrote %d bytes to %s (size=%s mtime=%s%s)"
              % (r["bytes_written"], r["path"], r["size"], r["mtime"],
                 ", overwrote" if r["overwrote"] else ""))
    elif op == "mkdir":
        print("created directory %s (real: %s)" % (r["path"], r["real_path"]))
    elif op == "mv":
        if r.get("noop"):
            print("source and destination are the same: %s" % r["real_path"])
        else:
            print("moved %s -> %s" % (r["src"], r["dst"]))
    elif op == "hash":
        print("sha256:%s  %s" % (r["hash"], r["path"]))
    elif op == "daemon":
        for k in sorted(r):
            print("%-16s %s" % (k, r[k]))
    elif op == "exec":
        sys.stdout.write(r["stdout"])
        sys.stderr.write(r["stderr"])
        if r["timed_out"]:
            sys.stderr.write(
                "[bridge] command timed out after %ss (exit 124); "
                "remote process may still be running\n" % r["timeout"])


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    # allow "--json" anywhere (argparse REMAINDER for `exec` would swallow it)
    raw_argv = list(sys.argv[1:]) if argv is None else list(argv)
    json_mode = "--json" in raw_argv
    argv = [a for a in raw_argv if a != "--json"] if json_mode else raw_argv

    ap = build_parser()
    args = ap.parse_args(argv)
    args.json = json_mode
    try:
        cfg = load_config(args.config)
        pname = args.profile or cfg["default_profile"]
        if pname not in cfg["profiles"]:
            raise BridgeError("INVALID_CONFIG", "unknown profile: %s (have: %s)"
                              % (pname, ", ".join(cfg["profiles"])))
        profile = Profile(pname, cfg["profiles"][pname])
        if args.op == "serve":
            if args.json:
                raise BridgeError(
                    "INVALID_ARG", "--json is not supported with serve")
            from .web import serve
            return serve(
                profile, port=args.port, open_browser=not args.no_open)
        elif args.op == "daemon":
            result, raw = _daemon_mgmt(args, cfg, pname)
        else:
            # transparently reuse the daemon's persistent connection when up
            routed = _try_daemon(cfg.get("daemon_port", DEFAULT_PORT),
                                 pname, args, profile)
            if routed is not None:
                result, raw = routed
            else:
                result, raw = dispatch(args, profile)
    except BridgeError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": e.to_dict()}, ensure_ascii=False))
        else:
            msg = "error [%s]: %s" % (e.code, e.message)
            if e.details:
                msg += " | " + json.dumps(e.details, ensure_ascii=False)
            print(msg, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    if args.json:
        out = {"ok": True}
        out.update(result)
        print(json.dumps(out, ensure_ascii=False))
        return 0

    if raw is not None:
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
    else:
        _render_text(args, result)

    if args.op == "exec":
        if result["timed_out"]:
            return 124
        rc = result["exit_code"]
        if isinstance(rc, int) and 0 <= rc <= 255:
            return rc
        return 1
    return 0
