# 重连与熔断

## 背景

当前 SFTP 和 Exec 客户端默认在连接失败时重试两次，即一次调用最多触发三次连接尝试。
daemon 在共享 SFTP 会话失效后还会再创建新会话，并在部分失败后进入指数冷却。

正式服务器会根据短时间连接或认证失败次数封禁来源 IP，因此常见的自动重试策略可能
放大故障。

## 当前实现

`sshbridge/sftp_client.py::SftpSession` 默认重试两次，因此一次创建最多启动三个
OpenSSH 子进程：

```python
def __init__(self, argv, op_timeout=60,
             connect_retries=2, retry_delay=3.0):
    for attempt in range(connect_retries + 1):
        self._spawn(argv)
        try:
            self.version = self._handshake()
            return
        except BridgeError as e:
            self._hard_shutdown()
            if not _is_connect_failure(e) or attempt == connect_retries:
                raise
            time.sleep(retry_delay * (attempt + 1))
```

`sshbridge/exec_client.py::run_exec` 对返回码 255 且命中连接错误标记的请求采用相同的
默认重试次数：

```python
attempts = connect_retries + 1
for attempt in range(attempts):
    cp = subprocess.run(argv, capture_output=True, timeout=timeout)
    if cp.returncode == 255 and _is_pre_auth_failure(cp.stderr) \
            and attempt < attempts - 1:
        time.sleep(retry_delay * (attempt + 1))
        continue
    break
```

daemon 在 SFTP 建连失败后设置 60 秒起步、最长 1800 秒的冷却，但该状态只存在于
daemon 进程内。Web、直接 CLI 和其他进程不共享该冷却。

## 痛点

- 多个进程可以同时重试，单进程退避无法限制全局频率。
- SFTP 的 `"unexpectedly"` 判断范围过宽，认证失败也可能被视为可重试。
- Web 在连接失效后，下一个请求会立即重新连接。
- 用户无法查看最近连接尝试和剩余冷却时间。
- 当前没有明确的手动重连命令。
- 连接被封禁后继续探测可能延长封禁时间。

## 目标

- 所有连接尝试由 broker 统一计数和执行。
- 保护模式默认不自动连续重试。
- 失败后停止新连接，保留明确的熔断状态。
- 用户可以手动触发一次受控重连。
- 区分网络故障、主机密钥问题、认证失败和远端命令退出。

## 预期

- 单次操作最多触发一次 SSH TCP 建连。
- 多个客户端同时请求时，只允许一个连接尝试。
- 认证失败不会自动重复提交。
- 冷却期间所有调用立即返回，不等待网络超时。
- 用户能看到失败原因、上次尝试时间和下一次允许时间。

## 方案

### 状态机

```text
DISCONNECTED -> CONNECTING -> READY
                     |
                     v
                   OPEN
                     |
        manual retry or cooldown
                     v
                 HALF_OPEN
```

- `READY`：复用现有 transport。
- `OPEN`：熔断，拒绝自动连接。
- `HALF_OPEN`：只允许一个探测连接。
- 探测成功回到 `READY`，失败重新进入 `OPEN`。

### 失败分类

- DNS、路由、拒绝连接、握手超时：网络连接失败。
- host key 不匹配：安全错误，禁止自动重试。
- Permission denied：认证错误，禁止自动重试。
- banner/KEX reset：连接阶段错误，可进入冷却，但保护模式不立即重试。
- 远端命令非零退出：业务结果，不计入连接失败。
- 已执行命令后连接中断：状态不确定，绝不自动重放命令。

### 手动操作

新增候选命令：

```sh
remote broker status
remote broker reconnect
remote broker pause
remote broker resume
```

`reconnect` 只触发一次尝试。失败后继续保持熔断，不进入循环。

### 配置

```json
{
  "connection_policy": {
    "connect_retries": 0,
    "min_connect_interval": 10,
    "auto_reconnect": false,
    "cooldown_initial": 60,
    "cooldown_max": 1800
  }
}
```

## 验收标准

- 认证失败只产生一次连接尝试。
- 20 个并发请求在断网时只触发一个探测连接。
- 熔断期间请求不会创建 ssh 子进程。
- 手动 reconnect 每次只尝试一次。
- 命令状态不确定时不会自动重放。
- 状态接口显示分类后的错误和连接尝试计数。
