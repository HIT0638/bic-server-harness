# macOS pywebview Desktop MVP

## 状态

macOS 首期 Desktop MVP 已实现并完成本机 arm64 构建验证。Windows、Linux、
Developer ID 签名、公证、自动更新和原生控件重写不在本期范围。

- 设计提交：`95b29e1`。
- 实现提交：`2406498`。
- App 版本：`0.1.0`。

## 背景

当前 Remote Explorer 以 loopback Web 服务运行，并通过系统浏览器打开。功能上已经
具备目录懒加载、文本编辑、冲突检测、文件操作和显式重连，但使用体验仍表现为一个
Web 页面。

Connection Broker 已将 SSH/SFTP 生命周期从 UI 进程中分离。Desktop App 不需要
重写远端连接逻辑，只需要在本地托管现有 HTTP 服务，并用 macOS 原生窗口承载已有
HTML/CSS/JavaScript。

pywebview 在 macOS 使用 Cocoa WebView，并要求 GUI loop 运行在主线程。其
`create_window`/`start` 生命周期适合单窗口应用；macOS 冻结打包可使用 py2app。

## 当前实现

`sshbridge/desktop.py::run_desktop` 已实现 Desktop 生命周期：

```python
server = server_factory(profile, config_path=config_path, port=0)
thread = threading.Thread(
    target=server.serve_forever,
    kwargs={"poll_interval": 0.2},
    name="sshbridge-desktop-http")
thread.start()
window = webview_module.create_window(
    "%s - %s" % (APP_NAME, profile.name),
    explorer_url(server),
    width=DEFAULT_WIDTH,
    height=DEFAULT_HEIGHT,
    min_size=(MIN_WIDTH, MIN_HEIGHT),
    resizable=True,
    zoomable=False,
    draggable=False)
window.events.closing += _confirm_close
webview_module.start(private_mode=True)
```

GUI loop 正常退出或异常时都会执行 `shutdown`、`server_close` 和最长 5 秒的线程
join。Desktop 不调用 Broker stop。未保存状态由前端同步到
`document.documentElement.dataset.dirty`。Cocoa 的同步关闭回调先取消本次关闭，
再由后台线程通过 pywebview `run_js` 读取状态、按需显示确认框并销毁窗口，避免
主线程等待自身调度的 WebKit JavaScript。该 API 不依赖 `eval`，因此无需放宽现有
CSP。

`sshbridge/web.py` 继续提供随机 loopback 端口、随机 token、CSP 和
Broker-backed `WorkspaceService`。浏览器入口与 Desktop 共用 `explorer_url`：

```python
def explorer_url(server):
    return "http://127.0.0.1:%d/?token=%s" % (
        server.server_address[1], server.token)
```

Desktop 不输出 token URL，也不暴露 `window.pywebview.api`。frozen 模式从
`RESOURCEPATH/web_assets` 加载固定白名单中的 `index.html`、`app.js` 和
`styles.css`。

开发入口已加入 `sshbridge/cli.py`：

```text
python3 remote.py --config bridge.json desktop
```

该入口只接受 broker profile，不支持 `--json`。pywebview 保持懒加载，未安装依赖时
返回 `DESKTOP_DEPENDENCY_MISSING`，不影响其他命令。

`resolve_desktop_config_path` 在普通 CLI 中保持 `./bridge.json` 语义；frozen App
依次读取 `SSHBRIDGE_CONFIG` 和
`~/Library/Application Support/SSHBridge/bridge.json`。配置错误使用转义后的
只读 HTML 错误窗口显示，不启动 Explorer HTTP server。

py2app bundle 包含独立 `Contents/MacOS/sshbridge_broker`。frozen
`BrokerClient.ensure_started` 只允许启动同目录的可执行普通文件；helper 缺失、
为符号链接或不可执行时返回 `BROKER_UNAVAILABLE`，不回退 direct 或 App launcher
的 `-m` 模式。

## 实施前痛点

- `remote serve` 会打开系统浏览器，不具备独立应用窗口和 Dock 身份。
- 浏览器标签页关闭与本地 HTTP server 生命周期没有直接绑定。
- Finder 启动 `.app` 时没有 CLI 的当前目录，无法可靠发现项目根目录下的
  `bridge.json`。
- 当前项目没有桌面可选依赖、macOS app bundle、图标和资源打包约定。
- pywebview GUI loop 必须位于主线程，而现有 HTTP server 是阻塞式
  `serve_forever`。
- 签名、公证和自动更新会显著扩大首期范围。

## 目标

- 提供 `remote desktop`，在独立 macOS Cocoa 窗口中运行现有 Explorer。
- Desktop 与 CLI/Web 共用 Connection Broker，不创建第二个 SFTP 所有者。
- 保留 loopback HTTP token、CSP 和虚拟工作区路径边界。
- 关闭最后一个窗口后停止本地 HTTP server，但不停止共享 Broker。
- pywebview 保持可选依赖；基础 CLI、Broker 和测试仍只依赖 Python 标准库。
- 提供可从 Finder 启动的本机 unsigned `.app` 构建产物。
- 保留 `remote serve`，便于诊断和浏览器兼容。

## 非目标

- 不将 HTML/CSS/JavaScript 重写为 AppKit、SwiftUI 或 Qt 原生控件。
- 不通过 pywebview JavaScript bridge 重复实现现有 Web API。
- 不在 Desktop 进程内直接访问 SFTP 或 OpenSSH。
- 不实现多窗口、多 profile 切换、配置编辑器、终端模拟器或插件系统。
- 不实现 Apple Developer ID 签名、notarization、DMG、自动更新或应用商店发布。
- 不改变远端依赖和 Connection Broker 安全语义。

## 预期

- 启动命令后只出现一个 Remote Explorer 应用窗口，不打开系统浏览器。
- 目录、编辑、保存、重命名和重新连接行为与当前 Web Explorer 一致。
- 同一 profile 的 CLI、Web 和 Desktop 继续共享一个 Broker/ControlMaster。
- 关闭窗口后 loopback 端口释放；Broker 根据现有规则继续运行。
- 未安装 pywebview 时，只有 `desktop` 命令返回明确错误，其他命令不受影响。
- unsigned `.app` 在构建机器上可从 Finder 启动并找到用户配置。

## 方案

### 1. 模块边界

新增 `sshbridge/desktop.py`，只负责桌面生命周期：

- 懒加载 `webview`，避免基础 CLI 导入可选依赖。
- 调用现有 `create_server(profile, config_path, port=0)`。
- 在后台线程运行 `server.serve_forever()`。
- 使用 token URL 创建一个 pywebview 窗口。
- 在主线程调用 `webview.start()`。
- GUI loop 返回后调用 `server.shutdown()`、`server.server_close()` 并等待线程退出。
- 不调用 `BrokerClient.stop()`；Broker 是 CLI/Web/Desktop 共享资源。

`sshbridge/web.py` 继续拥有 HTTP API、token、CSP、静态资源和
`WorkspaceService`。`sshbridge/desktop.py` 不复制 handler 或文件操作。

### 2. 进程与线程模型

```text
macOS Desktop process
├── main thread: pywebview Cocoa GUI loop
└── HTTP thread: ExplorerHTTPServer on 127.0.0.1:<random>
                         |
                         v
              per-profile Broker process
                         |
                         v
             OpenSSH ControlMaster / SFTP
```

启动顺序：

1. 加载 config 和 profile。
2. 创建 HTTP server 并完成 loopback bind。
3. 启动 HTTP thread。
4. 创建指向 token URL 的 pywebview 窗口。
5. 在主线程进入 Cocoa GUI loop。

退出顺序：

1. 最后一个窗口关闭，`webview.start()` 返回。
2. 调用 `server.shutdown()` 停止 HTTP loop。
3. 调用 `server.server_close()`，触发 `WorkspaceService.close()`。
4. 等待 HTTP thread 结束。
5. 进程退出；共享 Broker 保持运行。

启动或 GUI 初始化失败时也必须执行相同 HTTP 清理。不得留下监听端口或后台线程。

### 3. CLI

在 `sshbridge/cli.py` 新增：

```text
remote desktop
```

首期沿用全局 `--config` 和 `--profile`。`desktop` 不支持 `--json`，也不接受监听
地址；HTTP server 固定使用 `127.0.0.1` 和随机端口。

已新增稳定错误：

- `DESKTOP_DEPENDENCY_MISSING`：未安装 pywebview/macOS Cocoa 依赖。
- `DESKTOP_UNSUPPORTED`：当前平台不是 macOS。
- `DESKTOP_START_FAILED`：窗口或 GUI loop 初始化失败。

`serve` 与 `desktop` 保持两个独立入口。Desktop 失败不得静默回退浏览器。

### 4. 窗口

MVP 只创建一个窗口：

- 标题：`Remote Explorer - <profile>`。
- 初始尺寸：约 `1180 x 760`。
- 最小尺寸：约 `900 x 560`。
- 允许调整大小。
- 关闭窗口即关闭本地 HTTP server。

窗口内部继续加载当前 token URL。首期不暴露 `window.pywebview.api`，所有远端操作
仍经过已有 HTTP API。这样可复用 Web API 测试，并避免形成第二套权限和错误协议。

必须限制应用内导航：

- 只允许当前随机 loopback origin。
- 不把 token URL 写入日志。
- 不允许远端页面覆盖 Explorer。
- 后续如增加外部文档链接，应交给系统浏览器，而不是在应用窗口内导航。

### 5. 配置发现

开发模式：

- `python3 remote.py --config /path/to/bridge.json desktop`
- 未传 `--config` 时维持现有 `./bridge.json` 行为。

Finder 启动的 `.app` 没有可靠项目工作目录。打包模式按以下顺序查找：

1. `SSHBRIDGE_CONFIG` 环境变量。
2. `~/Library/Application Support/SSHBridge/bridge.json`。

MVP 不提供配置编辑器。配置不存在或无效时显示启动错误，并明确给出预期路径；不得
在 app bundle 内写入或携带真实 profile、SSH 参数或凭据。

配置路径由纯函数处理，并已单独测试 CLI 与 app bundle 两种上下文。

### 6. 可选依赖

基础安装继续保持零第三方依赖。Desktop 依赖已锁定为：

```text
pywebview==6.2.1
pyobjc-core==12.2.2
pyobjc-framework-Cocoa==12.2.2
pyobjc-framework-Quartz==12.2.2
pyobjc-framework-WebKit==12.2.2
pyobjc-framework-Security==12.2.2
pyobjc-framework-UniformTypeIdentifiers==12.2.2
py2app==0.28.10
setuptools==82.0.1
wheel==0.48.0
```

构建使用 Homebrew Python `3.12.14` 和仓库内被忽略的
`.venv/desktop-macos`，不依赖 macOS Apple Python 隐式提供的 PyObjC。

### 7. 打包

MVP 已交付两个入口：

1. 开发入口：在虚拟环境安装可选依赖后运行 `remote desktop`。
2. 本机 unsigned `.app`：使用 py2app 构建，包含 Python、pywebview、PyObjC、
   `sshbridge/web_assets/` 和必要模块。

已新增：

```text
requirements-desktop-macos.txt
packaging/macos/desktop_app.py
packaging/macos/sshbridge_broker.py
packaging/macos/setup.py
packaging/macos/build_app.sh
tests/test_desktop.py
```

`build_app.sh` 限制在 Darwin arm64 上使用 Homebrew Python 3.12，重建隔离虚拟环境，
清理 `packaging/macos/build` 与 `dist` 后执行 standalone py2app 构建，并验证主
launcher、Broker helper 与三项 Web 资源。`.app`、build/dist 目录和本机签名产物
均由 Git 忽略。

冻结 App 不能使用 `sys.executable -m sshbridge.broker`，因为
`sys.executable` 是 App launcher。`extra_scripts` 将独立 Broker 入口放入
`Contents/MacOS/sshbridge_broker`，与普通 Python 模式保持同一 Broker 协议和
连接治理。

本期 unsigned `.app` 只作为本机验证产物。对其他机器分发前必须另行完成：

- 固定最低 macOS 版本和 CPU 架构策略。
- Apple Developer ID 签名。
- Hardened Runtime 与 entitlements 评估。
- notarization 与 stapling。
- 安装包、升级和回滚策略。

### 8. UI 调整

首期尽量不改变 Explorer 功能，只做桌面窗口必要适配：

- 页面不显示浏览器导航概念。
- 保持 `Cmd+S` 保存和未保存离开确认。
- 验证 `Cmd+W`、`Cmd+Q` 与窗口关闭时的未保存内容行为。
- 验证浅色/深色模式、Retina 缩放和窗口最小尺寸。
- 禁止 UI 元素重叠，目录树和编辑器在最小尺寸下仍可使用。

“新建文件”“选择本地文件上传”等原生文件对话框不纳入首期。后续需要时再通过
pywebview window API 增加，不改变 Broker 协议。

### 9. 失败行为

- config/profile 无效：窗口创建前失败，不启动 HTTP thread。
- Broker 启动失败：沿用 `BROKER_UNAVAILABLE`，不回退 direct。
- HTTP bind 失败：返回结构化本地启动错误，不创建空白窗口。
- pywebview 依赖缺失：返回 `DESKTOP_DEPENDENCY_MISSING`。
- Cocoa 初始化失败：关闭已经创建的 HTTP server 后返回
  `DESKTOP_START_FAILED`。
- 远端连接失败：窗口保持打开，沿用现有“连接已暂停”和显式重连。
- 窗口关闭：停止 HTTP server，不停止 Broker，不删除或修改远端文件。

## 实施记录

1. 抽取 `explorer_url` 和 frozen 资源解析，保持 `serve` 行为不变。
2. 新增 `sshbridge/desktop.py`、`remote desktop` 和生命周期测试。
3. 增加 bundled Broker helper 及 frozen 启动路径安全校验。
4. 使用 Homebrew Python 3.12 安装锁定依赖并完成 py2app standalone 构建。
5. 使用真实 Cocoa、冻结 App、本地 OpenSSH 和 WebKit 快照完成本机验证。
6. 更新 `README.md`、`AGENTS.md`、`.gitignore` 和设计记录。

## 测试

### 单元测试

- 非 macOS 返回 `DESKTOP_UNSUPPORTED`。
- 未安装 pywebview 返回 `DESKTOP_DEPENDENCY_MISSING`。
- HTTP server 先完成 bind，随后才创建窗口。
- `webview.start()` 在调用线程执行，HTTP loop 在独立线程执行。
- GUI 正常退出和异常退出均调用 `shutdown`、`server_close` 并 join thread。
- 传给窗口的是随机 loopback token URL，日志不输出 token。
- Desktop 关闭不调用 Broker stop。
- app bundle 配置解析不依赖当前工作目录。

### 集成测试

使用 `tests/local_sshd.py` 和假的 WebView：

- Desktop controller 启动后 `/api/info`、`/api/list` 和编辑流程可用。
- CLI 与 Desktop 同时操作时共用一个 Broker 和一个 `tcp_generation`。
- 远端断开时 Desktop 显示暂停，不自动触发重连。
- controller 关闭后 loopback 端口可立即重新绑定。
- 多次启动/关闭不遗留 HTTP thread、socket、ControlPath 或额外 SSH TCP。

### macOS 人工检查

- `remote desktop` 不打开默认浏览器。
- 窗口在 Dock、Cmd+Tab 和 Mission Control 中表现正常。
- 目录展开、编辑、保存、冲突覆盖、重命名和显式重连正常。
- `Cmd+S`、`Cmd+W`、`Cmd+Q` 和未保存内容提示正常。
- Retina、浅色/深色模式及最小窗口尺寸正常。
- Activity Monitor 和 `lsof` 显示一个 Desktop HTTP listener、一个 Broker 和一个
  ControlMaster TCP。
- 从 Finder 启动 unsigned `.app` 后，静态资源和用户配置可找到。

### 实际验证结果

- Apple Python `3.9.6` 与 Homebrew Python `3.12.14` 均通过完整 86 项测试。
- `python3 -m compileall`、`node --check sshbridge/web_assets/app.js` 与
  `git diff --check` 通过。
- 开发入口已启动真实 Cocoa 窗口；页面完成加载，未保存标记在编辑后由 `false`
  变为 `true`。
- frozen App 已启动真实 Cocoa 进程，加载 bundle 内 Web 资源，并由
  `Contents/MacOS/sshbridge_broker` 启动 Broker。
- frozen 运行中 Broker 达到 `READY`，`tcp_generation=1`，本地 OpenSSH 观测为
  单个复用 TCP。
- WebKit 首屏与未保存编辑状态快照已人工检查，桌面和移动尺寸未发现空白、裁切或
  控件重叠。
- 本机构建产物约 42 MB；主 launcher 为 arm64 Mach-O，Info.plist、bundled helper、
  Web 资源和 ad-hoc code signature 校验通过。

系统屏幕录制权限未开放，因此视觉检查使用 WebKit 页面快照完成。未执行
Developer ID 签名、公证、DMG、跨机器启动和非 arm64 验证。

## 验收标准

- macOS 上 `remote desktop` 打开独立应用窗口，不打开系统浏览器。
- Desktop 所有文件操作仍经过 Web API、Broker 和 `sshbridge/ops.py`。
- 正常与异常关闭后本地 HTTP socket 和线程均清理。
- Desktop 与 CLI 并用时不增加第二个 SFTP session 或 ControlMaster TCP。
- pywebview 未安装时，基础 CLI、Web、Broker 和现有测试不受影响。
- 本机 unsigned `.app` 可从 Finder 启动，并从 Application Support 读取配置。
- 完整现有测试、Desktop 单元测试和 macOS 人工检查通过。

## 工作量估算

以下为 MVP 估算，不含签名、公证和自动更新：

- Desktop controller、CLI 与生命周期测试：1–2 天。
- macOS GUI 行为和前端适配：1 天。
- py2app、资源打包和 Finder 配置发现：1–2 天。
- 回归与人工检查：0.5–1 天。

合计约 3.5–6 个工程日。主要不确定性在 py2app 资源收集、Python/PyObjC 组合和
不同 macOS/CPU 架构验证，不在 Broker 或远端文件操作。

## 参考

- pywebview Introduction：<https://pywebview.flowrl.com/guide/>
- pywebview Usage：<https://pywebview.flowrl.com/guide/usage>
- pywebview macOS Installation：<https://pywebview.flowrl.com/guide/installation>
- pywebview Freezing：<https://pywebview.flowrl.com/guide/freezing>
