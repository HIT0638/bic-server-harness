# 重连与熔断

## 状态

已在 `feat/connection-broker` 分支实现首次失败熔断、连接门控和显式 reconnect。

## 背景

正式服务器可能根据短时间连接或认证失败次数封禁来源 IP。多个 CLI 或 Web 进程各自
自动重试会放大故障，因此所有受保护连接尝试必须由单一 Broker 计数和执行。

## 当前实现

`sshbridge/broker.py::BrokerState.ensure_ready` 在 `OPEN` 状态直接拒绝业务请求：

```python
if current == "OPEN" and not manual:
    raise self._paused_error_locked()
```

首次连接失败由 `_mark_open` 记录失败次数、最后错误和冷却截止时间：

```python
def _open_state_locked(self, error):
    self.state = "OPEN"
    self.failure_count += 1
    delay = min(
        self.policy["cooldown_initial"]
        * (2 ** max(0, self.failure_count - 1)),
        self.policy["cooldown_max"])
    self.cooldown_until = time.time() + delay
    self.last_error = error.to_dict()
```

Broker 创建 SFTP 与 Exec channel 时显式传入 `connect_retries=0`。SFTP 的连接失败
分类先排除认证和 host key 错误，不再使用宽泛的 `"unexpectedly"` 标记。Exec 只把
已知 SSH transport stderr 与返回码 255 组合识别为连接故障；普通远端非零退出仍是
业务结果。

`BrokerState.reconnect` 在连接锁内检查 `min_connect_interval`。允许后关闭旧
SFTP/ControlMaster，并通过 `HALF_OPEN` 执行一次连接尝试。成功回到 `READY`，
失败回到 `OPEN`。`cooldown_until` 记录指数冷却建议，但不会阻止用户在最小建连
间隔后进行显式重连。

CLI 和 Web 均不会自动调用 reconnect。Web 收到 `CONNECTION_PAUSED` 后显示“连接已
暂停”和显式重连按钮。

## 痛点

旧 SFTP 和 Exec 客户端一次调用最多自动尝试三次，daemon 还可能额外重建 session。
多个进程不共享失败状态，认证或网络故障可能迅速放大为连接风暴并延长远端封禁。

## 目标

- 所有受保护连接尝试由 Broker 串行门控。
- 单次业务请求最多进行一次连接尝试。
- 首次失败后停止自动探测。
- 用户可查看失败状态并显式触发一次重连。
- 远端命令失败与 SSH transport 失败保持区分。

## 预期

- 并发客户端在故障时只产生一个连接尝试。
- `OPEN` 状态请求立即失败，不等待网络超时。
- 认证和 host key 错误不会自动重试。
- 显式 reconnect 成功恢复，失败后继续保持熔断。

## 方案

Broker 使用 `DISCONNECTED`、`CONNECTING`、`READY`、`OPEN` 和 `HALF_OPEN`
状态机，并结合 `connect_lock`、`min_connect_interval` 和指数冷却字段控制连接。
业务请求不能从 `OPEN` 自动转换；只有 reconnect 可进入 `HALF_OPEN`。

## 状态机

```text
DISCONNECTED -> CONNECTING -> READY
                     |
                     v
                   OPEN
                     |
              manual reconnect
                     v
                 HALF_OPEN
                  /     \
              READY     OPEN
```

- `DISCONNECTED`：Broker 已启动，但尚未访问远端。
- `CONNECTING`：首个业务请求正在建立连接。
- `READY`：复用现有 transport。
- `OPEN`：连接已暂停，业务请求立即返回 `CONNECTION_PAUSED`。
- `HALF_OPEN`：一次显式 reconnect 正在执行。

## 错误行为

- DNS、路由、拒绝连接、banner/KEX 和握手超时：首次失败后进入 `OPEN`。
- host key 验证失败和 Permission denied：不自动重试，进入 `OPEN`。
- ControlMaster 异常退出：下一个业务请求检测后进入 `OPEN`。
- 远端命令普通非零退出：不计为连接故障。
- 命令超时：只终止本地 SSH channel，并继续说明远端进程可能仍在运行。
- Broker 模式不可用：返回 `BROKER_UNAVAILABLE`，不回退 direct。

## 配置

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

`connect_retries` 本期只接受整数 `0`。`cooldown_max` 不得小于
`cooldown_initial`。

## 操作

```sh
remote broker status
remote broker reconnect
```

`status` 返回 `state`、`last_attempt_at`、`last_connected_at`、`last_error`、
`failure_count` 和 `cooldown_until`。`reconnect` 不循环重试。

## 验收结果

- 坏端口首次失败后进入 `OPEN`，第二次业务请求返回 `CONNECTION_PAUSED`，且
  `last_attempt_at` 不变。
- 显式 reconnect 失败只增加一次 `failure_count`。
- ControlMaster 退出后不会自动创建新 TCP；显式 reconnect 可恢复。
- 认证和 host key 错误不会被 SFTP 客户端自动重试。
- Broker 的 SFTP 和 Exec 路径均使用零次自动连接重试。
