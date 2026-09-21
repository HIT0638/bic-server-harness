# Rsync 批量传输

## 背景

SFTP 适合目录浏览、小文件读写和编辑器保存。大文件或大量文件通过逐个 SFTP 请求传输
时，会长时间占用单一 SFTP 队列，并产生较多协议往返。

Rsync 能进行增量、批量和断点友好的文件传输，并可复用系统 OpenSSH。远端是否安装
rsync 不属于当前基础假设，因此该能力只能是可选增强。

## 当前实现

当前没有 Rsync 传输通道。大文件仍通过 `sshbridge/ops.py` 中的 SFTP 分块循环传输：

```python
CHUNK = 32768

for off in range(0, len(data), CHUNK):
    s.write_chunk(handle, off, data[off:off + CHUNK])
```

读取也在同一 SFTP session 中按 chunk 顺序完成。Web 和 daemon 都会串行化各自
session 上的操作。当前没有批量同步 API、能力探测或传输任务状态。

## 痛点

- 单个大文件会延迟同一 SFTP 会话上的目录查询。
- 大量小文件逐个上传会产生较多往返。
- 全量覆盖浪费带宽，慢链路下影响明显。
- Rsync 参数复杂，错误拼接可能越过 remote root。
- 本地与远端 rsync 版本可能不兼容。

## 目标

- 为大文件和目录提供独立批量传输通道。
- 不占用 SFTP 操作锁。
- 复用 broker 的 OpenSSH ControlMaster，不新增 TCP 握手。
- 所有源和目标仍受 remote root 约束。
- 远端无 rsync 时自动回退到 SFTP 或返回明确能力错误。

## 预期

- 大文件传输期间仍可浏览目录和读取小文件。
- 重复同步只传输变化的数据。
- 一次批量任务只占一个受控 Rsync channel。
- 传输状态包含文件数、字节数、退出码和错误摘要。
- 远端路径无法通过参数注入逃离工作区。

## 方案

### 能力发现

- broker 建立连接后低频执行一次 `command -v rsync`。
- 缓存远端 rsync 版本和可用参数。
- 能力探测失败不影响基础 SFTP 功能。

### API

候选操作：

```text
sync_push(local_paths, remote_dir, options)
sync_pull(remote_paths, local_dir, options)
sync_status(job_id)
sync_cancel(job_id)
```

- 默认批量任务异步执行并返回 job ID。
- 默认只允许工作区内的远端路径。
- 本地路径需由调用方明确授权；MCP schema 不接受任意隐式目录。

### 传输

- 使用系统 `rsync`。
- 通过 broker 提供的 ControlPath 调用系统 `ssh`。
- Rsync 并发默认 1。
- 支持 `--partial` 和可兼容的增量参数。
- 参数以 argv 传递，不拼接 shell 字符串。
- 旧版 rsync 不支持的选项通过能力检测关闭。

### 回退

- 远端无 rsync 时，小文件批量可退化为顺序 SFTP。
- 大文件是否自动退化由策略控制，避免长时间占用交互 SFTP。
- 返回结果明确标记 `transport=rsync` 或 `transport=sftp`。

## 验收标准

- Rsync 任务运行时 `list_dir` 延迟不受 SFTP 锁阻塞。
- 传输使用现有 ControlMaster，不增加 SSH TCP。
- 所有远端目标通过 canonical root 校验。
- 无 rsync 的测试主机仍可使用全部基础文件操作。
- 中断和恢复测试不会产生静默成功或未报告的部分结果。
