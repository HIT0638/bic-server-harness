# SSH Remote Workspace Bridge

SSH Remote Workspace Bridge is a small local CLI that lets a coding agent work
with one remote Linux workspace through standard OpenSSH and SFTP.

It exists for older servers that cannot run VS Code Remote, Cursor Remote,
Trae Remote, or another modern remote agent server. The remote host needs only
an SSH server with an SFTP subsystem and a shell. It does not need Node.js, a
new glibc version, or an agent runtime.

The project is not an agent framework. It does not manage LLM calls, planning,
agent loops, or context. It exposes remote filesystem and command operations
for an agent already running locally.

## Features

- `ls`, `stat`, `read`, `write`, `mkdir`, and `mv` over SFTP.
- `exec` through the system `ssh` client with structured stdout, stderr, exit
  code, and timeout status.
- A configured workspace root. Filesystem paths are virtual paths: `/` maps to
  that root, not to remote server root.
- Lexical traversal clamping and post-`REALPATH` containment checks for
  filesystem operations.
- Atomic temporary-file replacement when the server supports
  `posix-rename@openssh.com`.
- Optional mtime, size, and SHA-256 conflict checks before writes.
- Read-size limits and optional offset/limit reads.
- Reuse of the user's SSH configuration, SSH agent, `ProxyJump`, known hosts,
  and OpenSSH multiplexing where supported.
- Optional local daemon that retains one SFTP session for repeated file
  operations.

## Requirements

- Python 3.
- Local OpenSSH client available as `ssh`.
- Remote `sshd` with SFTP subsystem enabled.
- `sha256sum` on remote host only when using `hash` or
  `write --expected-hash`.

No third-party Python packages are required.

## Setup

Create a local configuration from the example:

```sh
cp bridge.example.json bridge.json
```

Set `host`, `port`, `user`, and `root`. `host` can be an alias defined in
`~/.ssh/config`.

```json
{
  "default_profile": "legacy-linux",
  "daemon_port": 7766,
  "profiles": {
    "legacy-linux": {
      "host": "legacy-host",
      "port": 22,
      "user": "remote-user",
      "root": "/home/remote-user/project",
      "strict_host_key": "yes",
      "ssh_args": [
        "-o",
        "ServerAliveInterval=60",
        "-o",
        "ServerAliveCountMax=5"
      ]
    }
  }
}
```

`bridge.json` is intentionally ignored by Git. It is local machine and remote
environment configuration.

## CLI

Run through the root wrapper:

```sh
python3 remote.py --config bridge.json ls /
python3 remote.py --config bridge.json stat /src/main.py
python3 remote.py --config bridge.json read /src/main.py
python3 remote.py --config bridge.json read /large.log --offset 1048576 --limit 65536
python3 remote.py --config bridge.json write /src/main.py --content "print('hello')"
printf 'binary-safe input' | python3 remote.py --config bridge.json write /tmp/data.bin
python3 remote.py --config bridge.json mkdir -p /build/output
python3 remote.py --config bridge.json mv /build/a.txt /build/b.txt
python3 remote.py --config bridge.json exec --cwd / -- python3 src/main.py
```

Use `--json` for structured output:

```sh
python3 remote.py --config bridge.json --json ls /
python3 remote.py --config bridge.json --json exec --cwd / -- python3 src/main.py
```

`read` writes raw bytes to stdout in normal mode. JSON mode includes both
UTF-8 replacement text and Base64 data.

For optimistic concurrency, save `mtime` and `size` from `stat` or `read`,
then supply them to `write`:

```sh
python3 remote.py --config bridge.json write /src/main.py \
  --content "new content" \
  --expected-mtime 1700000000 \
  --expected-size 42
```

## Daemon

The optional daemon retains one SFTP connection. It reduces new SSH
connections when the remote path rate-limits them.

```sh
python3 remote.py --config bridge.json daemon start
python3 remote.py --config bridge.json daemon status
python3 remote.py --config bridge.json daemon stop
```

The daemon serves one profile at a time. Current implementation uses an
unauthenticated localhost TCP port. Run it only on a trusted single-user local
environment until it is replaced with a permission-protected Unix socket or
authenticated local protocol.

## Safety Boundaries

Filesystem operations treat configured `root` as workspace root. `root` cannot
be `/`. The bridge resolves symlinks through SFTP and rejects paths that end
outside the canonical workspace root.

`exec` is different: it starts command execution in requested workspace
directory, but accepts arbitrary shell text by design. A command can still
access other remote paths. Full command sandboxing requires remote account,
container, chroot, or `sshd` policy; this bridge cannot guarantee it locally.

On timeout, local `ssh` process is killed. Remote process may remain running.
On servers without `posix-rename@openssh.com`, overwrite fallback is not
atomic.

## Architecture

- `sshbridge/cli.py`: argument parsing, rendering, daemon routing.
- `sshbridge/ops.py`: reusable JSON-ready bridge operation API.
- `sshbridge/paths.py`: virtual-path normalization and root containment.
- `sshbridge/sftp_client.py`: SFTP v3 client over `ssh -s sftp`.
- `sshbridge/sftp_proto.py`: SFTP packet codec.
- `sshbridge/exec_client.py`: remote command execution through `ssh`.
- `sshbridge/daemon.py`: optional persistent local SFTP daemon.
- `sshbridge/config.py`: profile parsing and OpenSSH invocation options.

`ops.py` is intended to become the backend for future MCP tools. Keep new
transport frontends thin and reuse these operation functions.

## Tests

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q sshbridge remote.py
```

Current tests cover path normalization, SFTP packet encoding, and connection
failure classification. Add mock or disposable-host integration tests before
declaring CLI behavior stable.

## Status

CLI MVP is implemented. MCP server wrapping remains future work.
