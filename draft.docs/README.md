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

## 编写规则

- 每份设计文档至少包含：背景、当前实现、痛点、目标、预期、方案、验收标准。
- “当前实现”只描述仓库中已经存在并可定位的行为。
- 当前已有相关实现时，摘录最小必要代码，并标明文件和函数。
- 代码摘录必须与当前源码一致；源码行为变化时同步更新文档。
- 当前尚未实现时，直接写“当前未实现”，无需提供伪代码。
- 未来设计、候选 API 和配置示例统一放入“方案”，不得写入“当前实现”。
- 明确区分已验证事实、已知限制和待验证假设。
- 示例不得包含真实凭据、SSH 私钥、生产主机信息或敏感文件内容。
- 每项方案应说明失败行为、兼容性边界和可测试的验收标准。
- 草案进入实现后，更新状态并链接对应提交；废弃方案需记录替代原因。
