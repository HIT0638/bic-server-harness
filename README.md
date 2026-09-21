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
- 默认通过本地 Connection Broker 复用一个 SSH TCP 和一个 SFTP channel。
- ControlMaster 可用时允许两个 Exec channel 并行，且不阻塞 SFTP 文件操作。
- 首次连接失败后进入熔断状态，只接受显式重连，不自动形成连接风暴。
- 本地 Web Explorer 提供懒加载目录树、文本查看与编辑、新建和重命名。
- macOS Desktop 使用 Cocoa 窗口承载同一套 Explorer，并继续复用 loopback API 与
  Connection Broker。

## 前置条件

- Python 3。
- 本地可执行 `ssh` 的 OpenSSH 客户端。
- 远端 `sshd` 已启用 SFTP 子系统。
- 仅使用 `hash` 或 `write --expected-hash` 时，远端需要 `sha256sum`。

CLI、Broker 和 Web Explorer 无需第三方 Python 依赖。macOS Desktop 的可选依赖
单独锁定在 `requirements-desktop-macos.txt`。

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
  "profiles": {
    "legacy-linux": {
      "host": "legacy-host",
      "port": 22,
      "user": "remote-user",
      "root": "/home/remote-user/project",
      "strict_host_key": "yes",
      "connection_policy": {
        "mode": "broker",
        "exec_concurrency": 2,
        "min_connect_interval": 10,
        "connect_retries": 0,
        "auto_reconnect": false,
        "cooldown_initial": 60,
        "cooldown_max": 1800,
        "control_master": true
      },
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

macOS 和 Linux 默认使用 `broker` 模式。`direct` 模式仅用于诊断和兼容；它不会
提供跨进程连接复用或全局熔断保护。Windows 当前默认使用 `direct`，本期尚未实现
具备当前用户 ACL 的 named pipe，因此显式选择 `broker` 会返回
`BROKER_UNSUPPORTED`。

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
- CLI 与 Web 共用 Broker 持有的 SFTP channel，不另建独立 SSH TCP。
- 连接熔断时显示“连接已暂停”，只能通过界面中的重新连接按钮恢复。

Web 服务固定绑定 `127.0.0.1`，不能通过参数改为外网地址。API 请求必须携带启动时
生成的随机 token。浏览器只使用虚拟工作区路径，API 不返回真实远端根路径或 SSH
凭据。关闭 `remote serve` 后 token 立即失效。

## macOS Desktop

Desktop MVP 使用 pywebview 的 Cocoa 窗口承载现有 Explorer。它不开放 Python
JavaScript API，也不直接连接 SSH；文件操作仍依次经过随机 loopback 端口、token
鉴权 Web API 和 Connection Broker。关闭窗口只释放本次 Desktop 的 HTTP server，
不会停止共享 Broker。

开发模式要求 Apple Silicon macOS、Homebrew Python 3.12 和独立虚拟环境：

```sh
brew install python@3.12
/opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv/desktop-macos
.venv/desktop-macos/bin/python -m pip install \
  -r requirements-desktop-macos.txt
.venv/desktop-macos/bin/python remote.py --config bridge.json desktop
```

`desktop` 只支持 `connection_policy.mode=broker`，不支持 `--json`。未安装 Desktop
依赖时，只有该命令返回 `DESKTOP_DEPENDENCY_MISSING`，基础 CLI 与 Web 不受影响。

Finder 启动的 `.app` 按以下顺序发现配置：

1. `SSHBRIDGE_CONFIG` 环境变量。
2. `~/Library/Application Support/SSHBridge/bridge.json`。

本机 unsigned App 的构建命令：

```sh
packaging/macos/build_app.sh
```

产物位于 `packaging/macos/dist/Remote Explorer.app`。当前 MVP 只验证本机构建环境的
arm64 bundle，不包含 Developer ID 签名、公证、DMG、自动更新或跨机器兼容承诺。
构建目录和 `.app` 均被 Git 忽略。

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

## Connection Broker

Broker 按 profile 自动启动，只监听当前用户可访问的 Unix socket。启动本地 Broker
不会立即连接远端；首个文件或命令请求才会建立 SSH。

```sh
python3 remote.py --config bridge.json broker start
python3 remote.py --config bridge.json broker status
python3 remote.py --config bridge.json broker reconnect
python3 remote.py --config bridge.json broker stop
```

普通 CLI 和 Web 请求会自动启动 Broker。Broker 不可用时不会回退直连。连接或认证
首次失败后状态进入 `OPEN`，后续业务请求立即返回 `CONNECTION_PAUSED`；只有
`broker reconnect` 会执行一次受频率限制的重连。

Broker 的运行目录权限为 `0700`，socket 和 metadata 权限为 `0600`。请求携带协议
版本、request ID 和 profile fingerprint；支持 peer credential 的系统还会校验
客户端 UID。`daemon start|status|reconnect|stop` 暂时保留为弃用别名，不再监听
localhost TCP。

OpenSSH ControlMaster 可用时，Broker 持有一个 TCP、一个顺序 SFTP channel，并允许
最多 `exec_concurrency` 个 Exec channel 并行。ControlMaster 被禁用或本地客户端
不支持时，SFTP 仍保持一个连接，Exec 降为单并发且每次建连受
`min_connect_interval` 限制。

## 安全边界

文件操作将配置的 `root` 视为工作区根目录，且 `root` 不可为 `/`。桥接层通过
SFTP 解析符号链接，并拒绝最终落在规范工作区根目录以外的路径。

`exec` 不同：它在指定工作区目录启动命令，但设计上接受任意 shell 文本。命令仍可
访问其他远端路径。完整命令沙箱需要远端账户、容器、chroot 或 `sshd` 策略；
本桥接层无法在本地保证此限制。

超时时会终止本地 `ssh` 进程，远端进程仍可能继续运行。未提供
`posix-rename@openssh.com` 的服务器会使用非原子的覆盖回退路径。

## 架构

- `sshbridge/cli.py`：参数解析、结果渲染、Broker 路由。
- `sshbridge/broker_client.py`：Unix socket endpoint、自动启动与客户端协议。
- `sshbridge/broker.py`：连接状态机、SFTP/Exec 队列和请求分发。
- `sshbridge/transport.py`：OpenSSH ControlMaster 生命周期。
- `sshbridge/ops.py`：可复用且 JSON 就绪的桥接操作 API。
- `sshbridge/paths.py`：虚拟路径规范化与根目录包含性检查。
- `sshbridge/sftp_client.py`：运行于 `ssh -s sftp` 的 SFTP v3 客户端。
- `sshbridge/sftp_proto.py`：SFTP 报文编解码。
- `sshbridge/exec_client.py`：通过 `ssh` 执行远端命令。
- `sshbridge/daemon.py`：旧 daemon 命令的 Broker 兼容入口。
- `sshbridge/web.py`：本地 Web/API 服务、token 鉴权和 Broker 调用。
- `sshbridge/web_assets/`：远程目录树与文本编辑界面。
- `sshbridge/desktop.py`：macOS Cocoa 窗口、HTTP 生命周期和桌面配置发现。
- `packaging/macos/`：py2app 入口、bundled Broker helper 和 arm64 构建脚本。
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
- 测试结束后关闭 sshd、Broker、ControlMaster 并删除全部临时文件。
- 不读取或修改系统 SSH 配置、`~/.ssh` 与项目 `bridge.json`。

本地缺少 `ssh`、`sshd` 或 `ssh-keygen` 时，集成测试自动跳过；路径与协议单元测试
仍会运行。当前集成测试覆盖真实 SFTP 文件流程、并发冲突、符号链接逃逸、大文件限制、
结构化命令结果、超时、CLI JSON、Broker 连接复用、熔断、并发队列和 Web API。

也可手动启动测试环境：

```sh
python3 tests/local_sshd.py
```

脚本会输出临时 `bridge.local.json` 路径和可直接运行的 `remote.py` 命令。
按 Ctrl-C 后，环境及临时密钥会被清理。

需要长期保留的本地测试文件放在 `only4test/`。运行时只修改临时副本，不会修改
Git 中的原始测试文件。

集成测试通过 `SSHBRIDGE_STATE_DIR` 隔离 Broker socket、metadata、锁、ControlPath
和日志。未设置时，优先使用 `XDG_RUNTIME_DIR/sshbridge`，否则使用当前用户私有的
`/tmp/sshbridge-<uid>`。

## 状态

CLI、Connection Broker、Web Explorer 与 macOS Desktop MVP 已实现。MCP Server、
Rsync channel、Windows named pipe 和可分发的签名 Desktop 安装包仍是后续工作。
