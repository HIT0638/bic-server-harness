# 单 Profile Connection Broker

## 状态

已在 `feat/connection-broker` 分支实现。覆盖 macOS/Linux；Windows named pipe、
MCP 和 Rsync 不在本期范围。

## 背景

正式环境可能对短时间内频繁建立 SSH 连接的来源 IP 实施封禁。SSH TCP 连接与 SSH
channel 是不同资源：一个持久 TCP 可以承载 SFTP 和多个命令 channel，因此连接治理
应限制 TCP 握手，而不是把所有操作串行化。

## 当前实现

`sshbridge/config.py::CONNECTION_POLICY_DEFAULTS` 默认在 POSIX 启用 Broker：

```python
CONNECTION_POLICY_DEFAULTS = {
    "mode": "direct" if sys.platform == "win32" else "broker",
    "exec_concurrency": 2,
    "min_connect_interval": 10,
    "connect_retries": 0,
    "auto_reconnect": False,
    "cooldown_initial": 60,
    "cooldown_max": 1800,
    "control_master": sys.platform != "win32",
}
```

`sshbridge/broker.py::BrokerState` 分离连接、SFTP 和 Exec 并发控制：

```python
self.exec_semaphore = threading.BoundedSemaphore(self.exec_limit)
self.connect_lock = threading.RLock()
self.sftp_lock = threading.Lock()
self.state_lock = threading.Lock()
```

`sshbridge/transport.py::OpenSSHTransport` 启动一个 ControlMaster。其
`channel_profile` 为 SFTP 和 Exec 显式配置同一个 ControlPath：

```python
self.channel_profile = copy.copy(profile)
if self.multiplexing:
    self.channel_profile.control_path = control_path
```

Broker 创建 SFTP 和执行 Exec 时分别显式传入 `connect_retries=0`。

CLI 在 broker 模式下先自动启动本地 Broker，再通过 Unix socket 发送请求。启动失败
会返回 `BROKER_UNAVAILABLE`，不会调用 direct 路径。Web Explorer 使用同一
`BrokerClient`，不再持有独立 SFTP 连接。旧 `daemon` 命令只保留为 Broker 的弃用
别名，不再监听 localhost TCP。

## 痛点

实施前 CLI、Web 和 daemon 可分别建立 SSH，Exec 也可能为每条命令建立新 TCP。
失败状态和连接频率无法跨进程共享，全局 daemon 锁还会让长命令阻塞文件操作。

## 目标

- 每个 profile 只有一个连接所有者。
- 常态由一个 SSH TCP 承载 SFTP 与少量并行 Exec channel。
- SFTP 和 Exec 使用独立并发控制。
- Broker 不可用或连接熔断时禁止隐藏直连。
- CLI、Web 和后续 MCP 使用同一本地协议与操作语义。

## 预期

- 高频目录浏览和小文件编辑不增加 SSH TCP generation。
- 两个命令可并行，同时目录读取继续响应。
- 多客户端共享失败状态、冷却时间和连接统计。
- 网络或认证故障不会触发自动重连风暴。

## 方案

当前方案由 `BrokerClient`、Unix socket `BrokerServer`、`OpenSSHTransport` 和保持
传输层无关的 `ops.py` 组成。profile fingerprint 与 `flock` 保证实例隔离；
ControlMaster 负责 TCP 复用；SFTP lock、Exec semaphore 和连接锁分别管理不同资源。

## 已知限制

- Windows 仅保留 `direct` 模式；named pipe 尚未实现。
- ControlMaster 不可用时，Exec 必须建立独立 TCP，因此降为单并发并受最小间隔限制。
- Broker 不实现命令级路径沙箱，也不保证本地 SSH 超时会终止远端进程。
- Exec 仍一次性缓冲 stdout/stderr；流式长命令属于后续设计。
- `direct` 模式不提供跨进程连接治理，只适用于诊断和兼容。

## 配置

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

## 操作

```sh
remote broker start
remote broker status
remote broker reconnect
remote broker stop
```

`start` 只启动本地 Broker。远端连接由首个业务请求懒创建。`reconnect` 每次只执行
一次受连接门控限制的尝试。

## 验收结果

- 100 次 `list_dir` 后 `tcp_generation` 保持为 1。
- 两个长 Exec 并行时第三个进入队列，SFTP 请求仍可完成。
- ControlMaster 退出后业务请求进入 `CONNECTION_PAUSED`。
- 显式 reconnect 恢复连接并只增加一代 TCP。
- 坏端口首次连接失败后，后续请求不产生新连接尝试。
- CLI、Web、daemon 兼容入口和真实 OpenSSH/SFTP 流程均有集成测试。
