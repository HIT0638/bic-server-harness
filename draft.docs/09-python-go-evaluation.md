# Python 与 Go 实现取舍

## 背景

当前实现使用 Python 标准库，并通过系统 OpenSSH 完成认证、ProxyJump、known_hosts
和远端连接。系统面向单 profile、单远端服务器，主要负载是网络 I/O 和远端磁盘 I/O。

随着 Connection Broker、MCP、Rsync 和长期任务加入，需要评估是否改用 Go。

## 痛点

- Python 应用分发依赖本机 Python 版本。
- 长期 daemon 的并发、取消和生命周期管理需要谨慎实现。
- 标准库没有跨平台统一的 Unix socket/named pipe 抽象。
- Go 重写会重新引入协议、沙箱、错误语义和兼容性风险。
- Go SSH 库不一定完整复用用户现有 OpenSSH 配置和行为。

## 目标

- 在不牺牲 OpenSSH 兼容性的前提下降低连接和并发风险。
- 控制实现成本，避免为了理论性能重写已验证能力。
- 保持未来迁移边界清晰。
- 明确触发语言迁移的实际条件。

## 预期

- 单 profile 场景下，文件和命令延迟主要由网络决定。
- Connection Broker 在 Python 中也能满足少量 channel 并发。
- CLI、Web 和 MCP 可继续复用现有 `ops.py`。
- 如果未来改 Go，可按稳定 IPC 协议逐步替换 broker，而不是一次性重写全部功能。

## 方案

### 当前选择

继续使用 Python：

- 优先完成 Connection Broker 和连接保护。
- 使用系统 OpenSSH，不切换到自建 SSH 认证栈。
- 文件操作维持顺序 SFTP。
- Exec 使用少量线程或 semaphore 管理子进程。
- MCP SDK 作为隔离的可选依赖。

Python GIL 对该场景影响有限，因为主要工作在 OpenSSH 子进程、socket 和远端 I/O 中。

### Go 的适用条件

满足以下条件之一时重新评估：

- 需要面向大量用户分发单个本地二进制。
- 需要长期后台服务、系统服务安装和自动升级。
- profile 数量或并发 channel 数显著增加。
- Python 进程生命周期或内存成为可测量瓶颈。
- Windows named pipe 与服务管理成为核心需求。

### 渐进迁移

- 先稳定 broker 的本地 IPC 协议。
- 保持 CLI、Web 和 MCP 只依赖 IPC，不依赖 broker 语言。
- 如需迁移，用 Go 重写 broker，继续调用系统 `ssh` 和 `rsync`。
- 通过相同契约测试验证 Python 与 Go broker。
- 不同时重写前端、CLI、MCP 和文件语义。

### 不采用

- 不因远端 glibc 旧而改 Go；本项目代码运行在本地。
- 不为了单 profile 的理论吞吐量重写。
- 不用 Go SSH 库替代系统 OpenSSH，除非能完整验证 SSH config、agent、ProxyJump
  和 host key 行为。

## 验收标准

- Python broker 在目标负载下满足延迟和稳定性要求。
- 性能决策基于基准数据，不基于语言偏好。
- IPC 契约允许未来替换 broker 实现。
- 任何 Go 原型都通过现有文件、安全和连接治理测试。
- 重写前有明确的分发、并发或运维收益指标。
