# Windows 自动测试

## 当前范围

`.github/workflows/windows-tests.yml` 在 GitHub 托管的 Windows Server 2022、2025 x64
环境运行 Python 3.12 测试。无需本地 Windows 机器，也无需提供远端主机或 SSH 密钥。

这是一套适配基线，不代表 Windows MCP 正式版已经完成验收。基线包含原生 Windows
Broker；真实 Windows 桌面 MCP Host 安装及 Windows 到远端 Linux 联调仍需验证。

工作流检查：

- Python 与 OpenSSH 客户端版本。`ssh -V` 不发起连接，也不验证 ControlMaster 功能。
- 路径安全、SFTP 报文和 OpenSSH 参数构造的现有单元测试。
- 原生 Python 子进程的输出、退出码、取消、超时，以及含空格和中文的本地工作目录。
- 真实 MCP SDK 与 mock Broker 的 schema、annotations、参数映射、错误和结果上限。
- named pipe 当前用户 ACL、匿名访问拒绝、junction 拒绝、实例抢占保护和 I/O 超时。
- 真实前台 Broker 并发启动、profile 隔离、身份验证、熔断与显式重连。
- 真实 MCP stdio 子进程共享预启动 Broker、错误脱敏与退出后的 Broker 存续。
- Host 禁止独立子进程时，自动启动明确失败，不产生退出时被 Host 连带终止的 Broker。
- Python 编译检查。

SDK 导入失败、任何测试失败或跳过均使工作流失败，不把可选依赖缺失当作成功。
基础 CLI/Broker 不增加第三方运行依赖；SDK 只安装在 CI 的 Python 3.12 环境。

## 触发与查看结果

推送到 `main` 或 `codex/**` 分支、打开或更新 Pull Request 时自动运行。
工作流合入默认分支后，也可在 GitHub 的 Actions 页面选择
**Windows compatibility baseline → Run workflow** 手动运行。

每个 Windows job 的 Summary 会列出实际检查范围和未验证项。Artifacts 包含：

- `report.json`：系统、依赖、测试计数和失败原因。
- `tests.log`：详细测试输出；环境预检失败时不会生成。
- `summary.md`：可读摘要。

日志保留 7 天，单个 job 上限 15 分钟。同一分支的新运行会取消旧运行。
工作流只授予仓库读取权限，不使用仓库 secrets，不访问本地 `bridge.json` 或生产主机。
公开仓库标准 runner 免费；私有仓库额度和超额费用以 GitHub 账户套餐为准。

## 在 Windows 机器复现

在仓库根目录的 PowerShell 执行：

```powershell
py -3.12 -m venv .venv/windows-ci
.venv/windows-ci/Scripts/python.exe -m pip install -r requirements-mcp.txt
.venv/windows-ci/Scripts/python.exe scripts/windows_ci.py --require-windows
```

Mac/Linux 可省略 `--require-windows` 检查同一套基础测试，但结果不能作为 Windows 验收。
现有全量测试含 POSIX 专用断言、`/bin/sh` 和隔离 POSIX sshd，不能直接在 Windows 全量发现。
Windows 当前只运行脚本明确列出的测试，未选入的测试不是已通过，也不是静默跳过。

GitHub Windows runner 本身也使用禁止 breakaway 的 Job Object。测试明确运行独立的
`python -m sshbridge.broker --serve` 进程，仍受 CI 生命周期约束；MCP 子进程连接这个
已运行的 Broker。测试不修改 runner 或 Host 的 Job Object，不绕过其退出清理策略。
自动后台启动成功的路径仍需在允许独立进程的实际用户环境验收；受限环境中的明确拒绝
已纳入自动测试。

## 后续验收

1. 准备隔离 Linux SSH 目标，增加 Windows 到 Linux 的 SFTP、Exec 与断线恢复测试。
2. 扩展 stdio 集成测试，覆盖真实远端文件读写和命令结果；当前原生 Windows 测试使用
   缺失的 SSH executable 验证失败链路，不会连接任何远端或读取用户 SSH 配置。
3. 发布前在目标 Windows 10/11 与实际 MCP Host 验证安装和启动。

不能以修改平台标识或 mock Windows API 代替以上真实平台验证。


## Windows 到 Ubuntu SSH 集成测试

同一 workflow 的 `windows-linux` job 在 Windows Server 2022 上导入一个临时
Ubuntu 22.04 WSL1 实例。MCP Server、Broker、Python 与 `ssh.exe` 均为原生 Windows
进程；测试端使用 Ubuntu 的真实 sshd、SFTP 和 shell，不用模拟 SSH 响应。

WSL1 运行 Linux 用户态但不提供独立 Linux 内核。此结果不能替代独立 Linux 主机、
真实网络故障、旧版 glibc 2.17 或目标 Windows 10/11 桌面验收。

环境只在 GitHub 托管 runner 上创建：从 Ubuntu 官方地址下载 rootfs 并校验其
SHA-256 清单；安装测试端标准 OpenSSH；新建专用普通用户。只监听随机 localhost
端口，使用一次性 host/client key 和预置 known_hosts，禁止密码和 root 登录。
测试先将 `only4test/` 复制到临时 Linux 工作区；不修改原始夹具，也不读取用户 SSH
配置或仓库 `bridge.json`。CI `always()` 清理步骤注销本次生成的唯一 WSL 实例。

三个端到端场景覆盖：

- 中文及空格路径、读写、hash、mtime/size/hash 冲突、移动、删除、二进制读取、
  分页、符号链接越界、多 MCP 客户端共享同一 Broker/SFTP 连接。
- 同步 stdout/stderr/退出码、异步 cursor、排队取消、运行中取消、超时，以及运行
  命令期间仍可写文件。取消仅断言本地 channel 已结束，不断言远端进程终止。
- 关闭测试 sshd 和已有用户会话后进入 OPEN；恢复服务后业务请求仍拒绝连接，
  必须显式 reconnect 才能重新读到原文件。

环境创建失败、依赖错误、测试失败或跳过都不能算通过。Artifacts 仅上传测试日志、
计数和目标镜像公开信息，不上传密钥、配置、WSL 文件系统或 Broker 运行目录。
该 job 上限 25 分钟，不需要仓库 secrets，也不连接生产服务器。
