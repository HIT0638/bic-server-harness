# MCP Server

## 背景

CLI 和 Web Explorer 已经通过 `sshbridge/ops.py` 提供统一的远程文件操作。下一阶段
需要让 Codex、Claude Code 等本地 Coding Agent 通过 MCP 直接调用相同能力。

MCP Server 运行在本地。远端服务器不安装 MCP 组件，也不运行 Agent Runtime。

## 痛点

- Agent 当前只能通过 shell 拼接 CLI 命令。
- CLI 文本参数不适合可靠传递大段内容和结构化错误。
- 每个 Agent 若直接调用 `ops.py`，可能各自创建 SSH 连接。
- MCP 协议可能演进，手写协议增加兼容风险。
- 大文件内容直接进入 MCP 上下文会增加传输和 token 成本。

## 目标

- 暴露与 CLI 等价的 MCP tools。
- 保持 `ops.py` 为唯一业务语义实现。
- MCP 进程不直接拥有 SSH 连接。
- 所有远端请求通过 Connection Broker。
- 保持错误码、读取限制、冲突检测和路径沙箱一致。
- 支持 Codex、Claude Code 等使用 stdio MCP 的本地 Agent。

## 预期

- Agent 可直接列目录、读取、写入、移动文件和执行命令。
- MCP Server 退出不会导致额外远端连接风暴。
- 多个 Agent 进程共享同一个 profile broker。
- Tool 返回结构稳定，可区分业务错误、连接错误和冲突。
- 大文件请求被明确拒绝或要求分页，不会静默截断。

## 方案

### 进程与传输

- 新增 `sshbridge/mcp_server.py`。
- 默认使用 MCP stdio transport，不监听网络端口。
- MCP Server 启动时连接本地 broker；保护模式下禁止直连远端。
- 优先使用官方 MCP SDK，将其作为可选依赖，CLI/Web 保持标准库可运行。
- 若 SDK 与目标本地 Python 不兼容，协议适配必须隔离在单独模块，并增加协议回归测试。

### Tools

首期暴露：

```text
list_dir(path)
stat(path)
read_file(path, offset, limit)
write_file(path, content, expected_mtime, expected_size, expected_hash, force)
mkdir(path, parents)
move(src, dst, force)
exec(command, cwd, timeout)
```

- `read_file` 返回 UTF-8 文本、长度、mtime、size 和 truncated。
- 二进制文件返回明确错误或 Base64，不自动注入不可读文本。
- `write_file` 默认要求调用方携带读取时获得的版本信息。
- `exec` 返回 stdout、stderr、exit_code 和 timed_out。

### 错误

- 保留 `BridgeError.code`。
- MCP tool failure 中返回稳定的结构化详情。
- `CONFLICT`、`TOO_LARGE`、`SANDBOX_VIOLATION` 不转换为通用内部错误。
- broker 不可用时返回 `BROKER_UNAVAILABLE`，不回退直连。

### 配置

Agent 的 MCP 配置只包含本地启动命令和 profile：

```json
{
  "command": "python3",
  "args": ["-m", "sshbridge.mcp_server", "--profile", "default"]
}
```

SSH 密钥、远端 root 和连接策略仍只存在于 bridge 配置和 OpenSSH 配置中。

## 验收标准

- MCP `tools/list` 返回全部 MVP tools 及 JSON Schema。
- 每个 tool 与 CLI 对同一输入产生一致结果和错误码。
- 两个 MCP 客户端并发运行时只使用一个 broker。
- MCP 进程退出后 broker 可继续服务 CLI/Web。
- 保护模式下 broker 不可用时，不产生 SSH 子进程。
- 使用隔离本地 sshd 完成 MCP 端到端测试。
