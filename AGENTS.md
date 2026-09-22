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
- Desktop 必须复用同一 loopback Web API 和 Connection Broker，不得通过
  pywebview JavaScript bridge 或 Desktop 进程增加第二套文件操作路径。

## Desktop 规则

- pywebview、PyObjC 和 py2app 必须保持为 macOS 隔离可选依赖，不得让基础 CLI、
  Broker、Web 或测试依赖第三方 Python 包。
- Desktop 只支持 `broker` mode；Broker 不可用时不得隐藏回退 direct。
- py2app frozen 进程必须通过 App bundle 中同目录的 `sshbridge_broker` helper
  启动 Broker。helper 缺失、为符号链接、非普通文件或不可执行时必须失败。
- App bundle 不得包含 `bridge.json`、真实 profile、SSH 参数或凭据。Finder 启动时
  只允许通过 `SSHBRIDGE_CONFIG` 或用户 Application Support 目录发现配置。
- Desktop 关闭时必须释放自身 HTTP listener 和线程，但不得停止共享 Broker。
- 不得为了 Python 侧执行 JavaScript 而放宽现有 CSP；未保存状态读取使用不依赖
  `eval` 的窗口 API。

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

## Broker 规则

macOS/Linux 的 CLI、Web 和后续 MCP 入口必须通过 Connection Broker 访问正式
profile，不得增加 Broker 失败后的隐藏直连回退。`direct` 模式只用于显式配置的
诊断和兼容场景。

Broker 对每个 profile 只管理一个 OpenSSH ControlMaster 和一个顺序 SFTP channel。
SFTP 锁不得覆盖独立 Exec I/O；Exec 通过独立 semaphore 限制并发。

Broker 只能监听当前用户私有运行目录中的 Unix socket。运行目录必须为 `0700`，
socket 和 metadata 必须为 `0600`，并保留 profile fingerprint、协议版本、
request ID、实例 ID、单例锁和可用时的 peer UID 校验。

首次连接失败必须进入 `OPEN`，业务请求不得自动重连。只有显式
`broker reconnect` 可以在连接门控允许后执行一次尝试。Broker 调用 SFTP 与 Exec
时必须使用 `connect_retries=0`。

Windows named pipe 尚未实现。Windows 必须保持显式 direct 兼容路径，不得回退到
未认证 localhost TCP Broker。

## Rsync 规则

- Rsync 是可选增强；本地或远端能力不足时，SFTP、Exec、Web 和 Desktop 必须保持
  可用，不得安装远端依赖或自动回退 SFTP。
- 同步只通过 Broker 启动，并要求当前 ControlMaster 存活。传输必须复用其
  ControlPath，不得新建 SSH TCP。
- 远端源和目标必须先经 SFTP `REALPATH` 与 canonical root 检查；传输期间不得持有
  `sftp_lock`、`exec_semaphore` 或 `connect_lock`。
- Rsync 参数必须固定并包含 protected args、`--links` 和 `--safe-links`。能力探测
  接受帮助文本中的 `--protect-args` 或现代名称 `--secluded-args`；调用方不得注入
  options，也不得提供 `--delete`。
- 任务保持单并发和有界队列、输出、历史。取消本地进程组后必须保留
  `remote_termination_unknown=true`，不得宣称远端已确认终止。

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
操作级测试，覆盖沙箱逃逸、符号链接、原子写入、冲突检查、超时、Broker 路由、
熔断、并发队列和 CLI JSON 输出。

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
- 不得提交 SSH 私钥、SSH 配置、主机专属凭据、Broker 状态文件、日志、字节码或
  虚拟环境。
- 不得提交 macOS Desktop 的 `build/`、`dist/`、`.app` 或本机签名产物。
- 文档必须保持事实准确。命令语法、安全边界、配置或支持操作变化时，更新 `README.md`。
