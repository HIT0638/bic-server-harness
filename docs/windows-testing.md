# Windows 自动测试

## 当前范围

`.github/workflows/windows-tests.yml` 在 GitHub 托管的 Windows Server 2022、2025 x64
环境运行 Python 3.12 测试。无需本地 Windows 机器，也无需提供远端主机或 SSH 密钥。

这是一套适配基线，不代表 Windows MCP 正式版已经可用。当前 Broker 仍拒绝 Windows；
named pipe、用户 ACL、单例锁及真实 Windows MCP Host 联调尚未实现或验证。

工作流检查：

- Python 与 OpenSSH 客户端版本。`ssh -V` 不发起连接，也不验证 ControlMaster 功能。
- 路径安全、SFTP 报文和 OpenSSH 参数构造的现有单元测试。
- 原生 Python 子进程的输出、退出码、取消、超时，以及含空格和中文的本地工作目录。
- 真实 MCP SDK 与 mock Broker 的 schema、annotations、参数映射、错误和结果上限。
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
现有全量测试含 `fcntl`、`/bin/sh` 和隔离 POSIX sshd，不能直接在 Windows 全量发现。
Windows 当前只运行脚本明确列出的测试，未选入的测试不是已通过，也不是静默跳过。

## 后续验收

1. Windows Broker 实现后，加入 named pipe 访问控制、启动竞态和多客户端复用测试。
2. 准备隔离 Linux SSH 目标，增加 Windows 到 Linux 的 SFTP、Exec 与断线恢复测试。
3. 加入真实 MCP stdio 子进程与 Broker 联调；现有 mock 测试不覆盖该链路。
4. 发布前在目标 Windows 10/11 与实际 MCP Host 验证安装和启动。

不能以修改平台标识或 mock Windows API 代替以上真实平台验证。
