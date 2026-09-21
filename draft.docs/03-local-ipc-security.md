# 本地 IPC 安全

## 状态

POSIX Unix socket 方案已在 `feat/connection-broker` 分支实现，核心实现提交为
`65d8063`。Windows named pipe 尚未实现，Windows 不会回退到 localhost TCP
Broker。

## 背景

Broker 协议可读写远端文件、执行任意命令和停止 Broker，并继承启动用户的 SSH
身份，因此本地 IPC 必须限制为当前用户访问。Web HTTP token 与 Broker IPC 是两个
独立安全边界，浏览器不会直接连接 Broker socket。

## 当前实现

`sshbridge/broker_client.py::_ensure_runtime_dir` 要求运行目录由当前 UID 所有且权限
严格为 `0700`：

```python
if info.st_uid != os.getuid():
    raise BridgeError(
        "BROKER_UNAVAILABLE",
        "broker runtime directory is not owned by the current user: %s"
        % path)
if stat.S_IMODE(info.st_mode) != 0o700:
    raise BridgeError(
        "BROKER_UNAVAILABLE",
        "broker runtime directory mode must be 0700: %s" % path)
```

`sshbridge/broker.py::BrokerServer._bind` 使用 Unix socket 并将其权限设为 `0600`：

```python
self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
self.listener.bind(self.endpoint.socket_path)
os.chmod(self.endpoint.socket_path, 0o600)
self.listener.listen(32)
```

每个 endpoint 由 config 绝对路径、profile 名、host、port、user 和 root 的 SHA-256
短摘要区分。metadata 包含协议版本、PID、随机实例 ID、profile fingerprint 和
socket 路径，以同目录临时文件加 `os.replace` 写入，权限为 `0600`。

请求包含：

```json
{
  "version": 1,
  "request_id": "随机请求 ID",
  "profile": "profile fingerprint",
  "op": "list_dir",
  "args": {}
}
```

响应回显 request ID 和实例 ID。客户端同时校验 metadata、profile fingerprint、
socket 路径和 Broker 响应身份。支持 `getpeereid` 或 `SO_PEERCRED` 的系统还会校验
peer UID。

Broker 使用 profile 级 `flock` 防止重复实例。删除陈旧 socket 前验证 owner 和文件
类型；停止时清理 socket、metadata、ControlPath 和 SSH 子进程。旧
`sshbridge.daemon` 只转发到 Broker，不再创建 TCP listener。

## 痛点

旧 daemon 使用未认证 localhost TCP。其他本机用户或被意外暴露到该端口的进程可
借用 daemon 的 SSH 身份执行文件操作、命令和 shutdown，固定端口还存在旧实例与
profile 混淆。

## 目标

- 只有当前用户可以访问 Broker。
- endpoint 不对局域网或外部网络开放。
- 客户端确认 profile、协议版本、请求和 Broker 实例身份。
- 陈旧 endpoint 可以安全恢复，不删除其他用户或其他类型的文件。
- Web 浏览器不能直接访问 Broker 原始协议。

## 预期

- 同机其他普通用户无法打开 Broker socket。
- 启动竞态只产生一个 Broker 和一个连接所有者。
- profile 或实例不匹配时请求失败。
- shutdown、exec 和 write 与其他请求使用相同访问控制。

## 方案

POSIX 使用当前用户私有运行目录中的 Unix socket、原子 metadata、profile 级
`flock` 和可用时的 peer UID 校验。Windows 后续使用带当前用户 SID ACL 的 named
pipe，不提供未认证 loopback TCP 回退。

## 已知限制

- peer credential API 并非所有 POSIX 平台都提供；此时依赖 `0700` 目录和 `0600`
  socket。
- Unix socket 路径受平台长度限制。长 `SSHBRIDGE_STATE_DIR` 会通过当前用户私有
  `/tmp/sshbridge-<uid>` 中的短 symlink 别名寻址，文件仍落在原状态目录。
- 当前未实现 Windows named pipe 和当前用户 SID ACL。
- 同一 UID 下的恶意进程仍处于相同信任边界；系统权限不能区分同一账户的进程。

## 验收结果

- 单元测试覆盖运行目录 `0700`、metadata `0600`、错误目录权限和稳定 fingerprint。
- 集成测试覆盖自动启动、实例复用、stop 清理和 profile 隔离路径。
- Web 与 CLI 同时通过 Broker，不存在第二个 Web SFTP 所有者。
- Windows 选择 broker 模式会返回 `BROKER_UNSUPPORTED`。
