# 本地 IPC 安全

## 背景

当前 daemon 监听 `127.0.0.1` TCP 端口，并接受换行分隔 JSON。协议支持远端文件
读写、任意命令执行和 daemon 停止。daemon 继承启动用户的 SSH 配置和认证能力。

Web Explorer 已使用随机 token，但 daemon 协议没有调用者认证。

## 当前实现

`sshbridge/daemon.py::serve` 创建普通 TCP socket，并绑定调用参数指定的地址和端口：

```python
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind((host, port))
srv.listen(16)
```

`sshbridge/daemon.py::_client` 直接解析 JSON 并按 `op` 执行，没有 token、UID 或
peer credential 校验：

```python
req = json.loads(raw.decode("utf-8"))
op = req.get("op")
if op == "shutdown":
    _send(conn, {"ok": True, "result": {"bye": True}})
    _SHUTDOWN.set()
    return
result = _run_op(profile, state, op, req.get("args", {}))
```

默认调用路径固定使用 `127.0.0.1`，因此当前风险主要来自同一台机器上的其他进程。

## 痛点

- 任意本机进程都可以尝试连接 daemon 端口。
- 多用户机器上的其他用户可能借用 daemon 的 SSH 身份。
- 恶意进程可读取或修改远端文件、执行命令或发送 shutdown。
- 端口被容器、代理或端口转发暴露后，风险不再局限于本机。
- 固定端口还会发生误连、旧进程占用和 profile 混淆。

## 目标

- 只有启动用户可以访问 broker。
- broker endpoint 不对局域网和外部网络开放。
- CLI、Web 和 MCP 能确认连接的是预期 profile 和 broker 实例。
- shutdown、exec 和 write 等高权限请求不能匿名调用。
- 不引入远端认证协议或替代 OpenSSH。

## 预期

- 同机其他普通用户无法连接 broker。
- 浏览器页面不能直接访问 broker 原始协议。
- endpoint 文件和认证材料随 broker 生命周期安全创建和清理。
- 异常退出后可以识别并清理陈旧 socket，不误杀其他进程。

## 方案

### POSIX

- 使用 Unix domain socket 替换 localhost TCP。
- socket 放在用户私有运行目录，目录权限 `0700`，socket 权限 `0600`。
- 启动时验证 socket owner 和 mode。
- 使用 PID、随机实例 ID 和 profile fingerprint 检测陈旧 endpoint。
- 可读取 peer credentials 的系统上，额外校验调用者 UID。

### Windows

- 优先使用 named pipe。
- pipe ACL 只允许当前用户 SID。
- 若只能使用 loopback TCP，则每次启动生成高强度随机 token。
- token 通过受保护状态文件传递，不放入进程列表或日志。

### 协议

- 每条请求携带协议版本、profile fingerprint 和 request ID。
- broker 返回实例 ID，客户端避免连接到旧 profile。
- 限制请求大小、读取超时和并发客户端数。
- shutdown 仅接受已认证客户端。
- 日志不记录文件内容、token、密钥和完整敏感命令。

## 验收标准

- socket 权限不是 `0600` 时 broker 拒绝启动。
- 其他 UID 连接被拒绝。
- profile fingerprint 不匹配时请求失败。
- 陈旧 socket 可恢复，但不会删除其他运行实例的 endpoint。
- 浏览器只能访问 Web API，不能直接调用 broker socket。
- 安全测试覆盖未认证 read、write、exec 和 shutdown。
