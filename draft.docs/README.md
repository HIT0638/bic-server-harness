# 设计草案索引

本目录保存尚未进入正式实现的设计草案。草案用于记录问题背景、设计目标和候选方案，
不代表当前代码已经提供对应能力。

## 草案

- `01-connection-broker.md`：单 profile 连接治理与统一入口。
- `02-mcp-server.md`：MCP stdio 适配层。
- `03-local-ipc-security.md`：daemon 本地 IPC 鉴权与访问控制。
- `04-exec-security-boundary.md`：任意命令执行的安全边界。
- `05-atomic-write-portability.md`：不同 SFTP 服务端上的原子覆盖语义。
- `06-rsync-batch-transfer.md`：可选的批量和大文件传输通道。
- `07-long-running-commands.md`：长命令并发、输出与取消。
- `08-reconnect-circuit-breaker.md`：连接失败、重试、冷却和手动恢复。
- `09-python-go-evaluation.md`：Python 与 Go 的实现取舍。

## 共同约束

- 远端只强制要求标准 SSH、SFTP 和 shell。
- 不在远端安装 Agent Server、Node Runtime 或新二进制。
- 继续复用系统 OpenSSH、`~/.ssh/config`、SSH Agent、ProxyJump 和 known_hosts。
- 文件路径必须受 remote root 沙箱约束。
- CLI、Web 和未来 MCP 共用相同操作语义。
- 连接治理优先降低新 SSH TCP 握手频率，而不是无条件压低业务并发。
