# Agent Guide

## Scope

This repository provides a thin remote filesystem and command execution bridge
for a coding agent that already runs locally.

Do not add LLM orchestration, planning, agent loops, context management, or a
remote agent runtime. The remote host may be an old Linux server with glibc
2.17. Assume only OpenSSH, SFTP, and a shell exist remotely.

## Design Rules

- Use SFTP for filesystem operations.
- Use local system OpenSSH for command execution.
- Reuse `~/.ssh/config`, SSH agent, `ProxyJump`, known hosts, and supported
  OpenSSH connection multiplexing. Do not reimplement SSH authentication.
- Do not require a remote binary, Node.js, Python package, daemon, or modern
  glibc.
- Keep directory access lazy. Never recursively scan a server or depend on
  long-running `tree`.
- Keep `sshbridge/ops.py` transport-neutral. CLI and future MCP handlers call
  this API rather than duplicate operation logic.

## Filesystem Safety

- `Profile.root` must be an absolute remote path and must never be `/`.
- Treat bridge paths as virtual workspace paths. `/` maps to `Profile.root`.
- Normalize `.` and `..` before remote access.
- For every filesystem operation, canonicalize remote path with SFTP
  `REALPATH` where target exists, then verify containment under canonical root.
- Preserve root checks for source, destination, and parent directories.
- Keep large reads bounded by `max_read_bytes` and `hard_read_cap`.
- Write through a same-directory temporary file, then rename it. Do not replace
  this with direct target writes.
- Preserve conflict detection for expected mtime, size, and SHA-256 values.
- Return stable `BridgeError` codes and JSON-ready error details.

## Command Safety

`exec` accepts arbitrary shell text. Its `cwd` is a starting directory, not a
security sandbox. Do not claim command-level containment unless remote account
or `sshd` policy enforces it.

Keep stdout, stderr, exit code, and timeout state structured. Local SSH timeout
does not prove remote process termination; preserve that warning in results.

## Daemon Rules

The daemon serializes access to one persistent SFTP session. Do not use that
lock around standalone `exec` work, which does not access the SFTP session.

Do not broaden daemon network exposure. Current localhost TCP protocol lacks
caller authentication and is suitable only for trusted single-user local
machines. Prefer a Unix socket with restrictive file permissions for future
hardening.

## Compatibility

- Stay within Python standard library unless a dependency removes substantial
  protocol or security risk.
- Keep SFTP v3 support. Older OpenSSH servers commonly expose it.
- Treat `posix-rename@openssh.com` as an optional extension. If atomic
  overwrite cannot be guaranteed, expose that fact rather than silently
  promising atomicity.
- `sha256sum` is not guaranteed by SSH, SFTP, or shell. Avoid making basic file
  operations depend on it.

## Tests

Run before commit:

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q sshbridge remote.py
```

Add tests for each behavior change. Prioritize operation-level tests with a
mock SFTP transport or disposable SSH host for sandbox escapes, symlink
handling, atomic-write behavior, conflict checks, timeouts, daemon routing, and
CLI JSON output.

## Repository Hygiene

- Keep `bridge.json` local. Track `bridge.example.json` only.
- Never commit SSH keys, SSH configuration, host-specific credentials, daemon
  PID files, logs, bytecode, or virtual environments.
- Keep documentation factual. Update `README.md` when command syntax, security
  boundary, configuration, or supported operation changes.
