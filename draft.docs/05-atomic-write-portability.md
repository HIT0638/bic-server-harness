# 原子写入兼容性

## 背景

当前写入先在目标目录创建临时文件，写完并关闭后再重命名到目标路径。临时文件与目标
位于同一目录，避免跨文件系统 rename。

OpenSSH 提供 `posix-rename@openssh.com` 扩展，可原子覆盖已有目标。标准 SFTP v3
rename 通常拒绝覆盖，因此当前回退逻辑会先删除目标，再重命名临时文件。

## 当前实现

`sshbridge/ops.py::op_write_file` 在目标目录写临时文件，再调用 rename：

```python
tmp = join(parent, ".sshbridge.tmp." + uuid.uuid4().hex[:12])
handle = s.open_handle(
    tmp, P.FXF_WRITE | P.FXF_CREAT | P.FXF_TRUNC)
for off in range(0, len(data), CHUNK):
    s.write_chunk(handle, off, data[off:off + CHUNK])
s.close_handle(handle)
s.rename(tmp, target)
```

`sshbridge/sftp_client.py::rename` 优先调用 OpenSSH 扩展：

```python
t, body = self._request(
    P.FXP_EXTENDED,
    P.pstr("posix-rename@openssh.com") + P.pstr(src) + P.pstr(dst))
if code == P.FX_OK:
    return
if code == P.FX_OP_UNSUPPORTED:
    return self._rename_v3(src, dst)
```

当前 SFTP v3 回退会删除旧目标后重试：

```python
self.remove(dst)
t, body = self._request(P.FXP_RENAME, P.pstr(src) + P.pstr(dst))
self._status(t, body, "rename")
```

## 痛点

- 删除与重命名之间断线，会留下目标文件缺失状态。
- 其他进程可能在两步之间观察到文件不存在。
- 回退路径不符合严格的原子覆盖承诺。
- SFTP close 后 rename 只保证命名空间切换，不等于断电持久性。
- 当前返回值没有说明本次写入使用了哪种 rename 语义。

## 目标

- 明确区分原子替换、非原子替换和持久落盘。
- 默认不因兼容性回退而静默降低安全级别。
- 保持新文件创建和支持扩展服务端的高效路径。
- 让 CLI、Web 和 MCP 能向调用方报告实际写入保证。

## 预期

- 支持 `posix-rename@openssh.com` 时，覆盖对观察者保持原子。
- 不支持扩展时，默认拒绝覆盖已有文件。
- 用户显式允许 unsafe fallback 后，结果标记 `atomic=false`。
- 临时文件在失败路径中尽量清理。
- 文档不把原子可见性误写成断电持久性。

## 方案

### 能力检测

- SFTP 握手时记录服务端扩展列表。
- 缓存 `posix-rename@openssh.com` 是否可用。
- 不通过一次破坏性操作猜测服务端行为。

### 写入策略

- 新文件：临时文件写入后使用标准 rename。
- 覆盖文件且支持 POSIX rename：执行原子替换。
- 覆盖文件且不支持扩展：默认返回
  `ATOMIC_RENAME_UNSUPPORTED`。
- 可选 `allow_non_atomic_replace=true` 才允许删除后 rename。

### 返回结果

```json
{
  "atomic": true,
  "durability": "not-guaranteed",
  "rename_method": "posix-rename@openssh.com"
}
```

如服务端支持 `fsync@openssh.com`，后续可增加可选的文件同步，但仍需区分文件数据
同步与父目录元数据同步。

## 验收标准

- 支持扩展时，覆盖操作使用 POSIX rename。
- 不支持扩展时，默认覆盖失败且原文件保持不变。
- unsafe fallback 必须显式配置，并返回 `atomic=false`。
- 中断写入不会留下部分目标文件。
- 测试覆盖扩展支持、不支持、rename 失败和临时文件清理。
