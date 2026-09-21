# 长命令执行

## 背景

构建、测试、日志分析和数据处理可能持续数分钟或更久。当前 `exec` 使用
`subprocess.run`，等待命令结束后一次性返回 stdout 和 stderr。daemon 又在全局锁内
执行请求，因此一个长命令可能阻塞其他 daemon 请求。

SSH 协议允许一个 TCP transport 同时承载多个 session channel。OpenSSH
ControlMaster 可以让多个本地 `ssh` 子进程共享同一 TCP。

## 痛点

- 长命令占用 daemon 全局锁。
- stdout 和 stderr 完成前不可增量读取。
- 本地 timeout 杀死 ssh 后，远端进程可能继续运行。
- 无 ControlMaster 时，每条命令都产生新的 TCP 握手。
- 无任务 ID，调用方无法查询状态或主动取消。

## 目标

- 长命令不阻塞 SFTP 文件操作。
- 默认允许少量并发命令，但不增加 SSH TCP 数量。
- 支持结构化状态、增量输出和取消。
- 明确区分本地 channel 终止与远端进程终止。
- 对队列和输出缓存设置上限。

## 预期

- 两个长命令可通过同一 ControlMaster TCP 并行。
- SFTP 目录查询在长命令期间正常响应。
- 超出 Exec 并发上限的请求排队，不创建新 TCP。
- Agent 可轮询或订阅命令输出。
- 取消结果说明远端进程是否已确认终止。

## 方案

### 独立队列

- Exec 不使用 SFTP 锁。
- broker 为 Exec 配置独立 semaphore，默认并发 2。
- 每个运行命令对应一个 OpenSSH session channel。
- 等待队列设置最大长度和排队超时。

### 同步 API

保留现有：

```text
exec(command, cwd, timeout)
```

适合短命令。内部仍由 broker 分配 channel。

### 异步任务 API

新增候选操作：

```text
exec_start(command, cwd, timeout)
exec_status(job_id, cursor)
exec_cancel(job_id)
```

- `exec_start` 返回 job ID。
- broker 持续读取 stdout/stderr，写入有上限的环形缓冲。
- `cursor` 用于增量读取，避免重复传输全部日志。
- 完成后保留结果一段可配置时间，再自动清理。

### 取消

- 首先终止本地 ssh channel。
- 如需要可靠取消，在远端 shell wrapper 中记录 PID 或进程组。
- 远端 PID 管理只能在工作区临时目录中进行。
- 无法确认远端终止时返回 `remote_termination_unknown=true`。

### 无 ControlMaster 平台

- 仍限制 Exec 并发和新建连接间隔。
- 不尝试用持久交互 shell 模拟任意命令协议，避免输出边界和转义错误。
- 可配置只允许一个 Exec TCP，或接受最多 2–3 个长期连接。

## 验收标准

- 运行 60 秒命令时，`list_dir` 不被阻塞。
- 两个 Exec channel 共享同一 ControlMaster TCP。
- 第三个命令排队且不产生新 TCP。
- stdout/stderr 可按 cursor 增量获取。
- 输出超过限制时明确标记截断。
- 取消和超时结果不错误宣称远端进程已终止。
