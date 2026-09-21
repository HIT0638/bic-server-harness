# 单 Profile Connection Broker

## 背景

正式环境只使用一个 profile，并连接一台远端服务器。该服务器会对短时间内频繁建立
SSH 连接的来源 IP 实施封禁。当前 CLI、Web、daemon 和未来 MCP 都可能独立创建
OpenSSH 进程。

SSH TCP 连接与 SSH channel 是不同资源。一个持久 SSH TCP 可以承载 SFTP、命令执行
和其他子系统 channel。连接治理应限制 TCP 握手，而不是把所有业务操作强制串行。

## 痛点

- CLI 找不到 daemon 时会回退到直接连接。
- Web Explorer 持有自己的 SFTP 连接。
- daemon 持有另一条 SFTP 连接。
- 每次 `exec` 都会启动一个 `ssh` 进程；没有 ControlMaster 时会建立新 TCP。
- 多个调用方无法共享失败状态、冷却时间和连接计数。
- 当前 daemon 的全局锁覆盖所有操作，长命令可能阻塞文件操作。

## 目标

- 每个 profile 在本机只有一个连接所有者。
- 常态保持一个 SSH TCP，最多允许少量受控连接。
- 文件操作和命令执行使用独立队列，不互相持有业务锁。
- 所有重连经过统一频率限制和熔断逻辑。
- 保护模式下禁止调用方绕过 broker 直连。
- 为 CLI、Web 和 MCP 提供相同的本地调用协议。

## 预期

- 高频目录浏览和小文件编辑不增加 SSH TCP 握手。
- 两个长命令可以并行运行，同时目录浏览继续响应。
- 网络异常不会形成多个进程同时重连的连接风暴。
- broker 状态可观测，包括 TCP 状态、channel 数、队列长度、失败次数和冷却时间。
- broker 重启只影响短期可用性，不改变远端文件语义。

## 方案

### 单实例

- broker 以 profile 名生成本地 endpoint 和进程锁。
- POSIX 使用权限为 `0600` 的 Unix socket。
- Windows 使用带当前用户 ACL 的 named pipe；无法实现时使用随机 token 的 loopback。
- 第二个 broker 检测到已有实例后直接复用，不再创建连接。

### OpenSSH Transport

- POSIX 启动一个受 broker 管理的 OpenSSH ControlMaster。
- SFTP 使用一个长期存在的 subsystem channel。
- Exec 默认允许两个并发 channel，但不创建额外 TCP。
- 可选 Rsync 通过同一个 ControlPath 打开传输 channel。
- ControlMaster 不可用时，SFTP 保持一条 TCP，Exec 新建连接必须经过连接门控。

### 队列

- SFTP 队列并发为 1，匹配当前顺序 SFTP v3 客户端。
- Exec 使用独立 semaphore，默认并发为 2。
- Rsync 使用独立 semaphore，默认并发为 1。
- 队列等待不占用连接创建配额。
- 大文件传输只与其他 channel 共享网络带宽，不占用 SFTP 操作锁。

### 保护模式

建议 profile 配置：

```json
{
  "connection_policy": {
    "protected": true,
    "require_broker": true,
    "exec_concurrency": 2,
    "rsync_concurrency": 1,
    "min_connect_interval": 10,
    "connect_retries": 0
  }
}
```

`require_broker=true` 时，CLI、Web 和 MCP 无法连接 broker 就立即失败，不回退到直连。

## 验收标准

- 并发执行 100 次 `list_dir` 只产生一个 SSH TCP。
- 两个长命令并行时，SFTP 操作仍可完成。
- 同一 profile 不能启动两个连接所有者。
- broker 不可用时，保护模式不创建直连 SSH 进程。
- 状态接口能区分 TCP、SFTP channel、Exec channel 和排队请求。
- 网络故障测试中，连接尝试频率不超过配置上限。
