# Connection Broker 实施计划

## Summary

将当前 daemon 升级为单 profile 的本地 Connection Broker，并让 CLI 与 Web
统一通过 broker 访问远端。broker 在 macOS/Linux 上使用权限受控的 Unix socket，
管理一个 OpenSSH ControlMaster、一个长期 SFTP channel 和最多两个并发 Exec
channel。

本期核心目标是降低新 SSH TCP 握手频率，并防止网络或认证故障触发重连风暴。
broker 自动启动；broker 不可用时不回退直连。首次连接失败后进入熔断状态，仅允许
用户显式执行 reconnect。

本期不实现 MCP、Rsync、Windows named pipe、异步命令任务和严格 Exec 沙箱，但会
为这些能力保留稳定边界。

## Current State Analysis

### Repository State

- 当前分支：`feat/connection-broker`。
- `main` 与该分支当前均指向 `086e2fd`。
- `only4test/hello.txt` 有用户未提交修改；实施与提交不得覆盖或包含该文件。
- 当前 Python 为 3.9.6，支持 `socket.AF_UNIX`。
- 当前系统 OpenSSH 支持 `ControlMaster`、`ControlPath` 和 `ControlPersist`。

### Connection Ownership

- `sshbridge/ops.py::_maybe_session` 在没有传入 session 时为每次文件操作新建 SFTP。
- `sshbridge/daemon.py` 持有一个 SFTP session，但使用 localhost TCP 且无鉴权。
- `sshbridge/web.py::WorkspaceService` 另持有一个独立 SFTP session。
- CLI 仅在 daemon 已运行且 profile 匹配时路由 daemon，否则直接连接。
- `sshbridge/exec_client.py::run_exec` 每次启动一个系统 `ssh` 子进程。
- 未显式配置 ControlMaster 时，每个 Exec 子进程通常建立新的 SSH TCP。

### Locking

- daemon 的 `state["lock"]` 覆盖 SFTP、Exec 和 hash 的完整执行过程。
- 长命令会阻塞 daemon 内的目录和文件请求。
- Web 只有 SFTP 操作，因此其单锁符合当前顺序 SFTP 客户端要求。

### Retry Behavior

- `SftpSession` 默认 `connect_retries=2`，单次创建最多尝试三次。
- `run_exec` 默认 `connect_retries=2`。
- daemon 在 session 死亡后还会额外重建一次 session。
- daemon 的冷却状态不被 Web、直接 CLI 或其他进程共享。
- SFTP `_is_connect_failure` 包含宽泛的 `"unexpectedly"` 标记，认证关闭也可能被
  误判为可重试连接故障。

### Tests

- 现有测试包含真实本地 OpenSSH/SFTP 集成环境。
- daemon 生命周期、CLI 路由、Web API、文件操作和 Exec 已有基础覆盖。
- 测试运行时支持 `SSHBRIDGE_STATE_DIR`，可用于隔离 broker socket、状态和
  ControlPath。

## Proposed Changes

### 1. 配置 Connection Policy

修改 `sshbridge/config.py`：

- 新增并验证 `connection_policy` profile 配置。
- 配置缺失时，在支持 Unix socket 的 POSIX 平台默认启用 broker。
- 保留显式 `mode: "direct"`，仅用于诊断和兼容，不用于正式受保护 profile。
- Windows 首期保持 direct 兼容路径，并输出 broker 尚未支持的明确提示；后续实现
  named pipe 后改为默认 broker。

首期配置结构：

```json
{
  "connection_policy": {
    "mode": "broker",
    "exec_concurrency": 2,
    "min_connect_interval": 10,
    "connect_retries": 0,
    "auto_reconnect": false,
    "cooldown_initial": 60,
    "cooldown_max": 1800,
    "control_master": true
  }
}
```

校验规则：

- `mode` 仅允许 `broker` 或 `direct`。
- `exec_concurrency` 为 1–3，默认 2。
- `min_connect_interval`、`cooldown_initial`、`cooldown_max` 为非负数。
- `connect_retries` 首期只允许 0；避免调用方绕开 broker 重试策略。
- `cooldown_max >= cooldown_initial`。
- `control_master` 为布尔值。

更新 `bridge.example.json` 和 README 配置示例。`bridge.json` 是本地忽略文件，
不纳入提交。

### 2. 增加 Broker Endpoint 与 Client

新增 `sshbridge/broker_client.py`：

- 计算 profile fingerprint：
  `sha256(abs(config_path), profile_name, host, port, user, root)` 的短摘要。
- runtime 目录优先级：
  `SSHBRIDGE_STATE_DIR`、`XDG_RUNTIME_DIR/sshbridge`、`/tmp/sshbridge-<uid>`。
- 创建 runtime 目录时要求 owner 为当前 UID、mode 为 `0700`。
- endpoint 使用短文件名，避免 Unix socket 路径长度限制。
- metadata 文件保存：
  `protocol_version`、`pid`、`instance_id`、`profile_fingerprint`、`socket_path`。
- metadata 以临时文件加 `os.replace` 原子写入，mode 为 `0600`。
- Unix socket 连接后校验 metadata、profile fingerprint 和 broker ping。
- 提供 `request()`、`ping()`、`ensure_started()` 和管理命令调用。

本地协议继续使用一请求一连接的 newline-delimited JSON。每条请求增加：

```json
{
  "version": 1,
  "request_id": "...",
  "profile": "<fingerprint>",
  "op": "list_dir",
  "args": {}
}
```

broker 响应回显 `request_id`。请求大小继续受限。

### 3. 实现 Broker Server

新增 `sshbridge/broker.py`，逐步替代 `sshbridge/daemon.py` 的连接职责：

- 使用 `AF_UNIX` socket，监听前验证 runtime 目录。
- socket 创建后设置 mode `0600`。
- 使用 `fcntl.flock` 获取 profile 级独占锁，避免启动竞态。
- 检测陈旧 socket 前先验证 owner，再删除。
- 可获取 peer credentials 时校验客户端 UID。
- broker 启动不立即连接远端；首个业务请求时懒连接。
- shutdown 时关闭 SFTP channel、ControlMaster、socket，并清理 metadata。
- 捕获 SIGTERM/SIGINT，执行同样清理。

状态对象至少包含：

- `state`: `DISCONNECTED | CONNECTING | READY | OPEN | HALF_OPEN`
- `last_attempt_at`
- `last_connected_at`
- `last_error`
- `failure_count`
- `cooldown_until`
- `sftp_alive`
- `active_exec`
- `queued_exec`
- `ops_served`
- `tcp_generation`

### 4. 管理 OpenSSH ControlMaster

新增 `sshbridge/transport.py`：

- `OpenSSHTransport` 只由 broker 创建。
- 使用 profile 原有 SSH 参数启动：

```text
ssh <profile options> -M -N -S <control-path> -- <host>
```

- 通过 `ssh -S <control-path> -O check <host>` 判断 master 是否就绪。
- ControlPath 位于受保护 runtime 目录。
- broker 关闭时使用 `-O exit`，失败后再终止 owner process。
- 启动 master 前经过连接门控。
- master 异常退出时进入 `OPEN`，不自动循环重启。
- SFTP 和 Exec 子进程都显式指定同一 ControlPath。

修改 `sshbridge/config.py::Profile`：

- 保留现有直接连接 argv 行为。
- 增加生成 master、multiplexed SFTP、multiplexed Exec argv 的方法。
- 额外 OpenSSH 参数必须位于 host 前。
- 不修改用户 SSH 配置，不创建远端文件。

### 5. 拆分 SFTP 与 Exec 并发

Broker 内部使用：

- `sftp_lock = threading.Lock()`：保护单个顺序 SFTP session。
- `exec_semaphore = threading.BoundedSemaphore(exec_concurrency)`。
- `connect_lock = threading.Lock()`：只保护 TCP 创建和状态转换。
- 独立状态锁：只保护计数和状态快照，不包围远端 I/O。

SFTP 路径：

- 确保 transport 为 READY。
- 懒创建一个 multiplexed SFTP channel。
- 所有文件操作在 `sftp_lock` 内执行。
- session 死亡后进入 OPEN，不在同一请求内重连。

Exec 路径：

- 不获取 `sftp_lock`。
- ControlMaster 可用时，最多两个 Exec channel 并行。
- 每个命令继续使用独立 `ssh` 子进程，但通过 ControlPath 复用 TCP。
- 超过并发上限的命令排队，并记录 `queued_exec`。
- timeout 只终止命令 channel；结果继续声明远端进程状态可能未知。

ControlMaster 不可用时：

- 保持一个长期 SFTP TCP。
- Exec 强制并发 1。
- 每次 direct Exec 建连经过 `min_connect_interval`。
- direct Exec 不自动重试。
- 状态接口返回 `multiplexing=false` 和降级原因。

### 6. 实现熔断与手动重连

Broker 状态机：

- 初始 `DISCONNECTED`。
- 首个业务请求允许一次连接，进入 `CONNECTING`。
- 成功进入 `READY`。
- 网络、握手、host key 或认证失败进入 `OPEN`。
- `OPEN` 状态业务请求立即返回 `CONNECTION_PAUSED`，不创建 ssh 子进程。
- `broker reconnect` 在满足最小连接间隔后进入 `HALF_OPEN`，只允许一个连接尝试。
- 成功回到 `READY`，失败回到 `OPEN` 并更新冷却。

修改 `sshbridge/sftp_client.py` 和 `sshbridge/exec_client.py`：

- broker 调用路径显式传入 `connect_retries=0`。
- 收窄 SFTP 连接错误识别，移除宽泛的 `"unexpectedly"` 判断。
- 认证失败和 host key 错误永不自动重试。
- 已可能执行的命令永不自动重放。
- direct 兼容模式可保留现有重试行为，但文档标记其不适合受保护服务器。

新增稳定错误：

- `BROKER_UNAVAILABLE`
- `BROKER_PROFILE_MISMATCH`
- `CONNECTION_PAUSED`
- `CONNECTION_RATE_LIMITED`
- `MULTIPLEX_UNAVAILABLE`

### 7. 改造 CLI

修改 `sshbridge/cli.py`：

- 新增主命令 `broker`：
  `start`、`stop`、`status`、`reconnect`。
- 保留 `daemon start|stop|status` 作为兼容别名，并显示 deprecated 提示。
- 普通命令在 broker 模式下调用 `ensure_started()`。
- 自动启动只启动本地 broker；远端连接仍由首个业务请求懒触发。
- broker 启动失败时返回 `BROKER_UNAVAILABLE`，禁止 direct fallback。
- `status` 显示 transport、SFTP、Exec 队列和熔断字段。
- `reconnect` 每次只允许一个连接尝试。
- direct 模式继续调用现有 `dispatch()`，用于兼容和诊断。

CLI 的现有 JSON 输出和 Exec 退出码行为保持兼容。

### 8. 改造 Web Explorer

修改 `sshbridge/web.py`：

- 删除 `WorkspaceService` 对 `SftpSession` 的直接持有。
- Web 启动时调用 `ensure_started()` 获取 broker client。
- 所有文件 API 通过 broker request 执行。
- Web HTTP token 与 broker IPC 权限保持独立。
- Web 关闭时只关闭本地 broker client，不关闭共享 broker。
- broker 进入 OPEN 时，Web 返回结构化连接状态，不触发自动重连。

前端 `sshbridge/web_assets/app.js`：

- 识别 `CONNECTION_PAUSED`。
- 显示“连接已暂停”，不进行自动请求循环。
- 增加显式“重新连接”动作，调用本地 Web API 转发 broker reconnect。
- 保持目录懒加载和现有编辑冲突行为。

### 9. 兼容旧 Daemon

修改 `sshbridge/daemon.py`：

- 保留模块和命令入口，内部转发到 broker 实现。
- 不再监听未鉴权 localhost TCP。
- 旧 `.bridge-daemon.json` 只用于识别并清理陈旧版本，不继续作为新协议状态文件。
- 文档统一使用 broker 名称。

不提供旧 TCP daemon 与新 broker 同时运行模式，避免两个连接所有者。

### 10. Windows 后续边界

本次不实现 Windows named pipe，但必须：

- 保留现有 direct 模式，使 Windows CLI 不因本次改动完全不可用。
- Windows 选择 broker 模式时返回明确的 `BROKER_UNSUPPORTED`。
- 不静默回退到未鉴权 TCP broker。
- 将 named pipe、当前用户 SID ACL、Windows OpenSSH 无 ControlMaster 降级列为独立
  后续任务。

正式 Windows 接入远端前，必须完成该后续任务。

### 11. 文档

更新：

- `README.md`：broker 启动、状态、重连、配置和保护语义。
- `AGENTS.md`：所有新入口必须经过 broker，禁止增加隐藏直连路径。
- `bridge.example.json`：新增 connection policy 示例。
- `draft.docs/01-connection-broker.md`：实现后更新状态和代码摘录。
- `draft.docs/08-reconnect-circuit-breaker.md`：更新已实现状态。
- `draft.docs/03-local-ipc-security.md`：更新 Unix socket 实现状态。

## Assumptions & Decisions

- 当前实施平台为 macOS/Linux。
- Windows named pipe 是必需后续能力，但不在本次实现范围。
- broker 自动启动，并持续运行到显式 stop 或进程退出。
- broker 不会因空闲自动关闭，避免下一次调用重新握手。
- broker 模式下禁止 direct fallback。
- 首次业务请求允许一次连接；失败后仅手动 reconnect。
- ControlMaster 可用时 Exec 默认并发 2。
- ControlMaster 不可用时 Exec 并发降为 1，并受建连最小间隔限制。
- SFTP session 固定为 1。
- Rsync 本期不实现，只保留未来独立 channel 的架构位置。
- MCP 本期不实现，但后续只能调用 broker。
- `only4test/hello.txt` 的用户修改不属于本次实现，不得回退或提交。

## Verification

### Unit Tests

- 配置默认值、类型、范围和非法组合。
- profile fingerprint 稳定性和配置隔离。
- runtime 目录 owner/mode 校验。
- socket 路径长度和陈旧 endpoint 处理。
- broker 请求版本、request ID 和 profile 校验。
- 连接状态机、最小间隔、OPEN/HALF_OPEN 转换。
- 认证失败、host key 错误和命令非零退出分类。
- Exec semaphore 和队列计数。
- direct fallback 在 broker 模式下被禁止。

### Integration Tests

使用 `tests/local_sshd.py`：

- 自动启动 broker 后执行 CLI `list/read/write/mkdir/move/exec`。
- 100 次文件操作只建立一个 master TCP。
- 两个长 Exec 并行，第三个排队。
- 长 Exec 期间 `list_dir` 可完成。
- Web 与 CLI 同时使用时只存在一个 broker 和一个 master TCP。
- 杀死 SFTP channel 后 broker 进入 OPEN，不自动重连。
- `broker reconnect` 只产生一次连接尝试并恢复 READY。
- broker stop 后清理 socket、ControlPath、metadata 和子进程。
- profile mismatch 和未授权 socket 访问失败。
- ControlMaster 降级模式的 Exec 串行和间隔限制生效。

### Regression

```sh
python3 -m unittest discover -v
python3 -m compileall -q sshbridge tests remote.py
```

### Manual Checks

- `remote broker start/status/reconnect/stop` 文本和 JSON 输出。
- `remote serve` 自动使用 broker。
- `ps` 与 `lsof` 验证常态只有一个远端 TCP。
- 修改 `only4test/` 时确认 Git 只显示用户实际修改。
- 在断网或错误端口配置下确认没有连续 ssh 子进程。
