# Exec 安全边界

## 背景

`exec(command, cwd, timeout)` 用于在远端工作区运行构建、测试和诊断命令。调用方可以
传入任意 shell 文本。当前实现将 `cwd` 映射到 remote root 内，然后执行：

```sh
cd <workspace-cwd> && <command>
```

文件 API 使用 SFTP `REALPATH` 校验路径，但任意 shell 命令无法通过本地路径解析获得
同等级别的隔离。

## 当前实现

`sshbridge/ops.py::op_exec` 只对 `cwd` 执行虚拟路径映射，然后把原始命令交给
OpenSSH：

```python
command = (command or "").strip()
cwd_real = resolve_virtual(cwd, profile.root)
res = run_exec(profile, command, cwd_real, timeout)
```

`sshbridge/exec_client.py::run_exec` 生成的远端命令如下：

```python
remote = "cd %s && %s" % (shlex.quote(cwd), command)
argv = profile.exec_argv(remote)
cp = subprocess.run(argv, capture_output=True, timeout=timeout)
```

`cd` 只设置起始目录。`command` 仍可使用绝对路径、再次切换目录或调用任何远端程序。

## 痛点

- 命令可使用绝对路径访问工作区以外文件。
- 命令可执行 `cd /`、调用其他程序、跟随符号链接或修改系统目录。
- 远端账户存在 sudo 或高权限时，影响范围进一步扩大。
- Prompt Injection 或错误命令可能绕过用户对“remote root”的安全预期。
- 尝试解析和过滤任意 shell 语法容易产生可绕过的伪沙箱。

## 目标

- 明确区分文件 API 沙箱与命令执行权限。
- 不对无法保证的命令隔离作安全承诺。
- 给生产使用提供可验证的远端隔离方案。
- 保持 Coding Agent 运行任意构建和测试命令的能力。
- 对高风险配置提供清晰状态与警告。

## 预期

- 用户知道 `cwd` 是起始目录，不是安全边界。
- 远端权限由专用账户或运行环境限制。
- 即使 Agent 执行错误命令，也不能越过远端账户的最小权限。
- 文件 API 继续严格执行 remote root 检查。

## 方案

### 默认语义

- 保留任意 shell `exec`。
- 返回 `real_cwd`，但不声明命令被沙箱化。
- 文档和 MCP tool description 明确说明权限边界。
- 配置状态中显示 `exec_isolation: remote-account`。

### 生产隔离

按可靠性从高到低选择：

1. 使用专用、无 sudo 的远端 Unix 用户。
2. 只授予工作区及必要工具链目录权限。
3. 通过容器、chroot 或受限 sshd 配置进一步隔离。
4. 对只需固定动作的环境使用服务端 allowlist wrapper。

### 不采用

- 不使用正则表达式过滤绝对路径。
- 不尝试完整解析 Bash 后再判断路径安全。
- 不把 `cd <root>` 描述为命令沙箱。
- 不默认禁止 shell 特性，因为这会破坏正常编译、测试和脚本能力。

## 验收标准

- README、CLI help 和 MCP schema 均说明 `exec` 不受文件 root 沙箱限制。
- 安全测试证明文件 API 不能越界。
- 生产部署文档要求专用低权限账户。
- 没有代码以命令字符串过滤器冒充安全沙箱。
- timeout 结果继续提示远端进程可能仍在运行。
