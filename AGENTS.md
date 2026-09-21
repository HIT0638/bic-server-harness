# Agent 指南

## 范围

本仓库为已在本地运行的 Coding Agent 提供轻量的远程文件系统和命令执行桥接层。

不得加入 LLM 编排、规划、Agent Loop、上下文管理或远程 Agent Runtime。远端可能是
glibc 2.17 的老旧 Linux 服务器；只假定远端存在 OpenSSH、SFTP 与 shell。

## 设计规则

- 文件操作使用 SFTP。
- 命令执行使用本地系统 OpenSSH。
- 复用 `~/.ssh/config`、SSH Agent、`ProxyJump`、known hosts 和支持的 OpenSSH
  连接复用。不得重写 SSH 认证。
- 不得要求远端安装二进制文件、Node.js、Python 包、daemon 或新版 glibc。
- 目录访问必须懒加载。不得递归扫描远端，也不得依赖长期运行的 `tree`。
- `sshbridge/ops.py` 必须保持传输层无关。CLI 与未来 MCP handler 应调用它，
  不得重复实现操作逻辑。
- Web Explorer 必须固定监听 loopback，API 必须使用随机 token 鉴权。
- Web API 只返回虚拟工作区路径，不得暴露真实远端根路径、SSH 参数或凭据。
- Web 前端不得直接连接 SSH；所有文件操作必须通过本地 API 和 `sshbridge/ops.py`。

## 文件系统安全

- `Profile.root` 必须是绝对远端路径，且不可为 `/`。
- 桥接路径均是虚拟工作区路径，`/` 映射到 `Profile.root`。
- 远端访问前规范化 `.` 和 `..`。
- 每项文件系统操作中，存在的目标路径必须经 SFTP `REALPATH` 规范化，再验证其位于
  规范根目录内。
- 必须保留源、目标和父目录的根目录检查。
- 大文件读取必须受 `max_read_bytes` 与 `hard_read_cap` 限制。
- 写入必须先写入同目录临时文件，再重命名。不得改为直接写入目标文件。
- 必须保留 expected mtime、size 与 SHA-256 的冲突检测。
- 必须返回稳定的 `BridgeError` 错误码和 JSON 就绪的错误详情。

## 命令安全

`exec` 接受任意 shell 文本。其 `cwd` 是起始目录，不是安全沙箱。除非远端账户或
`sshd` 策略提供强制隔离，不得宣称命令级路径约束。

stdout、stderr、退出码和超时状态必须结构化返回。本地 SSH 超时不能证明远端进程已
终止；结果中必须保留该提示。

## Daemon 规则

daemon 串行访问一个常驻 SFTP 会话。不得在独立 `exec` 工作期间持有该锁，因为
`exec` 不访问 SFTP 会话。

不得扩大 daemon 网络暴露面。当前 localhost TCP 协议未认证，仅适用于可信的单用户
本机环境。后续加固优先使用具有限制权限的 Unix socket。

## 兼容性

- 除非依赖能显著降低协议或安全风险，否则只使用 Python 标准库。
- 保持 SFTP v3 支持。老版本 OpenSSH 通常支持该版本。
- 将 `posix-rename@openssh.com` 视为可选扩展。无法保证原子覆盖时，必须明确暴露
  限制，不得静默承诺原子性。
- SSH、SFTP 与 shell 不保证存在 `sha256sum`。不得让基础文件操作依赖它。

## 测试

提交前运行：

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q sshbridge remote.py
```

每次行为改动都应新增测试。优先添加 mock SFTP transport 或一次性 SSH 测试主机的
操作级测试，覆盖沙箱逃逸、符号链接、原子写入、冲突检查、超时、daemon 路由和
CLI JSON 输出。

`tests/local_sshd.py` 提供隔离的真实 OpenSSH 测试环境。集成测试必须使用临时密钥、
随机 localhost 端口、临时工作区和 `SSHBRIDGE_STATE_DIR`，不得访问
`bridge.json`、`~/.ssh` 或系统 SSH 配置。缺少 OpenSSH 工具时可跳过集成测试；
OpenSSH 工具存在但行为回归时必须测试失败。

`only4test/` 保存版本化测试工作区。测试必须先复制其内容到临时工作区，再执行写入、
移动或删除；不得直接修改测试夹具。

`tests/local_sshd.py --persistent` 是人工开发模式，可通过固定 `local-test` profile
直接读写 `only4test/`。该模式的修改是用户工作区变更，不得在自动测试中使用。

## 仓库规范

- `bridge.json` 必须保留本地；只跟踪 `bridge.example.json`。
- 不得提交 SSH 私钥、SSH 配置、主机专属凭据、daemon PID 文件、日志、字节码或
  虚拟环境。
- 文档必须保持事实准确。命令语法、安全边界、配置或支持操作变化时，更新 `README.md`。
