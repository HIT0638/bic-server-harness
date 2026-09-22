# 项目背景与维护说明

## 这个项目解决什么问题

BIC-Server-Harness 是一个自用的远程工作区工具。用户在本地运行 Coding Agent，
通过 MCP 查看和修改 Linux 服务器上的文件，必要时执行命令。思考、规划和上下文管理
由本地 Agent 完成；本项目负责提供文件与命令能力。

正式使用平台是 Windows，日常开发也使用 macOS。远端可能是 glibc 2.17 的旧服务器，
不适合安装现代远程开发服务端。基础功能只依赖远端已有的 SSH、SFTP 和 shell。

使用频率不高，也不追求高并发。核心体验是：

- 像文件编辑器一样浏览目录、查看文件和编辑文本。
- 保持可复用的连接，减少 SSH 握手，避免频繁连接触发服务器封禁。
- 能以简单、可理解的方式安装并注册为 MCP Server。

## 当前组成

- **MCP Server**：给本地 Agent 使用的主要入口，采用 stdio。
- **Web Explorer**：给人使用的目录树和文本编辑器，仅监听本机。
- **CLI**：手动操作、连接管理和排查问题。
- **Connection Broker**：按 profile 管理连接，供各入口共享。
- **macOS Desktop、Rsync**：已有的可选能力，不是当前 Windows 使用流程的重点。

文件操作使用 SFTP，命令执行调用本地系统 OpenSSH。SSH 配置、密钥、Agent、跳板机和
主机身份验证继续交给 OpenSSH，远端不运行本项目的 Agent 或服务程序。

Windows Broker 使用当前用户专属的 named pipe。SFTP 保持长连接；Exec 当前单并发，
每条命令建立独立 SSH 连接。macOS/Linux 在支持时使用 ControlMaster 复用连接。
连接失败后暂停业务请求，由用户显式重连；不承诺永不断线或绝不会被服务器限流。
某些 Windows MCP Host 限制子进程存续，需要从独立终端预先启动 Broker。

## 从哪里读代码

| 内容 | 位置 |
| --- | --- |
| 使用、安装与 MCP 注册 | [README.md](README.md) |
| 文件操作与虚拟路径 | `sshbridge/ops.py`、`sshbridge/paths.py` |
| 连接、IPC 与平台适配 | `sshbridge/broker.py`、`broker_client.py`、`transport.py`、`windows_ipc.py` |
| SFTP 与命令执行 | `sshbridge/sftp_client.py`、`sftp_proto.py`、`exec_client.py`、`exec_jobs.py` |
| 对外入口 | `sshbridge/mcp_server.py`、`cli.py`、`web.py` |
| 配置示例 | [bridge.example.json](bridge.example.json) |
| Windows 测试说明 | [docs/windows-testing.md](docs/windows-testing.md) |
| 设计思考与候选方案 | [draft.docs/README.md](draft.docs/README.md) |

表中省略目录前缀的代码文件均位于 `sshbridge/`。设计草案用于理解背景，不代表待办清单，
也不表示方案已经实现。

## 维护取向

围绕实际使用问题做小改动。优先处理文件浏览、编辑、连接复用和 Windows MCP 接入，
不以产品化、增加功能或统一抽象为目标推进重构。候选架构调整按实际需要再评估。

保持操作语义集中在 `ops.py`，由 Broker 提供给各入口。MCP 只使用 Broker；显式
`direct` 模式留作诊断，不作为连接失败后的隐藏回退。基础实现优先使用标准库，MCP
与 Desktop 的可选依赖保持隔离。

保留直接保护用户文件和连接的机制：虚拟根目录及符号链接检查、同目录临时文件写入、
并发修改检查、读取和任务资源上限、稳定的错误码。`exec` 的工作目录不是命令沙箱；
取消或超时只能确认本地 SSH channel 终止，不能确认远端进程结束。

本地 IPC 限当前用户访问；Web 使用 loopback 和随机 token。MCP stdout 只输出协议，
日志写 stderr。Web/MCP 不返回内部真实路径或凭据。MCP 或文件界面退出不停止共享 Broker。

## 开发与验证

先检查已有改动，保留用户的文档、草案和本地配置。`bridge.json`、SSH 密钥、运行状态、
日志、虚拟环境和打包产物不提交到 Git。

验证以改动风险为准：文档核对示例和链接；行为变动补相关测试；文件安全、连接生命周期
及平台适配使用集成测试。避免为个人工具扩展无关的测试框架或压力测试。

常用回归命令：

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q sshbridge remote.py
```

MCP 改动还需在安装 `requirements-mcp.txt` 的 Python 3.12 环境中验证。Windows 专项
使用云端原生测试，Mac 上的跳过不能算 Windows 通过。报告实际结果和未验证范围。

自动测试使用临时密钥、配置与工作区，不访问真实 profile。`only4test/` 是版本化夹具，
先复制再修改；`tests/local_sshd.py --persistent` 会直接使用它，仅供明确选择的人工测试。
