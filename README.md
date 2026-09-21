# SSH 远程工作区桥接

SSH Remote Workspace Bridge 是一个本地 CLI。它让本地运行的 Coding Agent
通过标准 OpenSSH 与 SFTP 操作单个远程 Linux 工作区。

项目面向无法运行 VS Code Remote、Cursor Remote、Trae Remote 等现代远程
服务端的老旧服务器。远端只需提供带 SFTP 子系统的 SSH 服务和 shell；不需要
Node.js、新版 glibc 或 Agent Runtime。

本项目不是 Agent Framework。不管理 LLM 调用、规划、Agent Loop 或上下文。
它只提供本地 Agent 所需的远程文件系统和命令执行能力。

## 功能

- 经由 SFTP 提供 `ls`、`stat`、`read`、`write`、`mkdir` 与 `mv`。
- 经由系统 `ssh` 提供 `exec`，返回结构化 stdout、stderr、退出码和超时状态。
- 配置工作区根目录。文件路径均为虚拟路径：`/` 映射到该目录，不是远端系统根目录。
- 文件操作执行词法路径收敛及 `REALPATH` 后的根目录包含性检查。
- 服务端支持 `posix-rename@openssh.com` 时，使用临时文件完成原子替换。
- 写入前可选 mtime、文件大小与 SHA-256 冲突检查。
- 限制单次读取大小，并支持 offset/limit 分段读取。
- 复用用户 SSH 配置、SSH Agent、`ProxyJump`、known hosts 与支持的
  OpenSSH 连接复用。
- 可选本地 daemon，为重复文件操作保留一个 SFTP 会话。
- 本地 Web Explorer 提供懒加载目录树、文本查看与编辑、新建和重命名。

## 前置条件

- Python 3。
- 本地可执行 `ssh` 的 OpenSSH 客户端。
- 远端 `sshd` 已启用 SFTP 子系统。
- 仅使用 `hash` 或 `write --expected-hash` 时，远端需要 `sha256sum`。

无需第三方 Python 依赖。

## 配置

从示例创建本地配置：

```sh
cp bridge.example.json bridge.json
```

设置 `host`、`port`、`user` 和 `root`。`host` 可使用 `~/.ssh/config`
中定义的别名。

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

`bridge.json` 已被 Git 忽略。它用于保存本机与远端环境配置。

## CLI 使用

通过根目录包装脚本运行：

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

使用 `--json` 获取结构化输出：

```sh
python3 remote.py --config bridge.json --json ls /
python3 remote.py --config bridge.json --json exec --cwd / -- python3 src/main.py
```

普通模式下，`read` 将原始字节写入 stdout。JSON 模式同时返回 UTF-8 替换文本
与 Base64 数据。

要启用乐观并发控制，先从 `stat` 或 `read` 保存 `mtime` 与 `size`，
再传给 `write`：

```sh
python3 remote.py --config bridge.json write /src/main.py \
  --content "new content" \
  --expected-mtime 1700000000 \
  --expected-size 42
```

## Web 文件浏览器

启动本地 Web Explorer：

```sh
python3 remote.py --config bridge.json serve
```

服务默认监听 `127.0.0.1:8765`，输出带随机访问 token 的 URL，并自动打开浏览器。
端口被占用时可指定其他端口，也可让系统分配随机端口：

```sh
python3 remote.py --config bridge.json serve --port 0 --no-open
```

当前 MVP 提供：

- 展开目录时才执行 `list_dir`，不会递归扫描远端。
- 查看与编辑 UTF-8 文本文件。
- 保存时使用 mtime 与文件大小检查远端并发修改。
- 新建文件、新建目录、重命名和刷新。
- 二进制文件与超过 `max_read_bytes` 的文件只显示元数据，不进入编辑器。
- 一个由服务进程持有并串行访问的常驻 SFTP 会话。

Web 服务固定绑定 `127.0.0.1`，不能通过参数改为外网地址。API 请求必须携带启动时
生成的随机 token。浏览器只使用虚拟工作区路径，API 不返回真实远端根路径或 SSH
凭据。关闭 `remote serve` 后 token 立即失效。

### 固定本地工作区

手动开发可启动固定 localhost sshd，直接将 `only4test/` 作为远端根目录：

```sh
python3 tests/local_sshd.py --persistent
```

该模式固定监听 `127.0.0.1:22222`。首次启动会在被 Git 忽略的 `.local-sshd/`
生成并保存测试密钥、known_hosts、PID 和日志。另开终端后使用 `bridge.json` 中的
`local-test` profile：

```sh
python3 remote.py --profile local-test ls /
python3 remote.py --profile local-test serve
```

该模式不创建工作区副本。CLI 和 Web 的写入、移动与新建操作会直接修改
`only4test/`，Git 会正常显示这些变化。停止 sshd 不会删除密钥或工作区，下次启动
继续使用同一个 profile。

## Daemon

可选 daemon 保留一个 SFTP 连接。远端路径限制新建 SSH 连接时，可减少连接次数。

```sh
python3 remote.py --config bridge.json daemon start
python3 remote.py --config bridge.json daemon status
python3 remote.py --config bridge.json daemon stop
```

daemon 一次只服务一个 profile。当前实现使用未认证的 localhost TCP 端口。
在改为权限受控的 Unix socket 或带认证的本地协议前，只应在可信单用户环境运行。

## 安全边界

文件操作将配置的 `root` 视为工作区根目录，且 `root` 不可为 `/`。桥接层通过
SFTP 解析符号链接，并拒绝最终落在规范工作区根目录以外的路径。

`exec` 不同：它在指定工作区目录启动命令，但设计上接受任意 shell 文本。命令仍可
访问其他远端路径。完整命令沙箱需要远端账户、容器、chroot 或 `sshd` 策略；
本桥接层无法在本地保证此限制。

超时时会终止本地 `ssh` 进程，远端进程仍可能继续运行。未提供
`posix-rename@openssh.com` 的服务器会使用非原子的覆盖回退路径。

## 架构

- `sshbridge/cli.py`：参数解析、结果渲染、daemon 路由。
- `sshbridge/ops.py`：可复用且 JSON 就绪的桥接操作 API。
- `sshbridge/paths.py`：虚拟路径规范化与根目录包含性检查。
- `sshbridge/sftp_client.py`：运行于 `ssh -s sftp` 的 SFTP v3 客户端。
- `sshbridge/sftp_proto.py`：SFTP 报文编解码。
- `sshbridge/exec_client.py`：通过 `ssh` 执行远端命令。
- `sshbridge/daemon.py`：可选的常驻本地 SFTP daemon。
- `sshbridge/web.py`：本地 Web/API 服务、token 鉴权和 SFTP 会话复用。
- `sshbridge/web_assets/`：远程目录树与文本编辑界面。
- `sshbridge/config.py`：profile 解析与 OpenSSH 调用选项。

`ops.py` 将作为未来 MCP tools 的后端。新增传输层入口应保持轻量，并复用这些操作函数。

## 测试

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q sshbridge remote.py
```

测试套件会尝试启动隔离的本地 OpenSSH 服务。该服务：

- 只监听 `127.0.0.1` 随机高位端口。
- 以当前用户运行，不需要 `sudo`。
- 将版本化的 `only4test/` 复制为临时远端工作区初始内容。
- 使用临时 host key、client key、`authorized_keys`、known_hosts 配置和工作区。
- 禁用密码认证，只接受临时测试密钥。
- 测试结束后关闭 sshd、bridge daemon 并删除全部临时文件。
- 不读取或修改系统 SSH 配置、`~/.ssh` 与项目 `bridge.json`。

本地缺少 `ssh`、`sshd` 或 `ssh-keygen` 时，集成测试自动跳过；路径与协议单元测试
仍会运行。当前集成测试覆盖真实 SFTP 文件流程、并发冲突、符号链接逃逸、大文件限制、
结构化命令结果、超时、CLI JSON、daemon 连接复用和 Web API。

也可手动启动测试环境：

```sh
python3 tests/local_sshd.py
```

脚本会输出临时 `bridge.local.json` 路径和可直接运行的 `remote.py` 命令。
按 Ctrl-C 后，环境及临时密钥会被清理。

需要长期保留的本地测试文件放在 `only4test/`。运行时只修改临时副本，不会修改
Git 中的原始测试文件。

集成测试通过 `SSHBRIDGE_STATE_DIR` 将 daemon PID 和日志放入临时目录。未设置时，
daemon 状态文件仍位于项目根目录。

## 状态

CLI 与 Web Explorer MVP 已实现。MCP Server 封装仍是后续工作。
