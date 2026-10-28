# bash 插件

在 workspace 内执行 shell 命令（`sh -c`），让**写代码 agent 能写完代码自己运行、自己看输出修复**——
实现 `write → exec → observe → fix` 闭环。

## 组件

- `bin/bash_server.py`：可执行文件，严格遵循 keeper 的 bin 协议
  （stdin 收一行 JSON，stdout **最后一行**回 JSON）。
- `skills/SKILL.md`：知识组件，告诉 agent「写完必须自运行验证，直到通过」。

## 工具

装载后 agent 获得工具 **`bash__run`**，参数：

- `command`（必填）：要执行的 shell 命令，支持管道 / 重定向 / `&&`。
- `timeout`（可选，默认 60s）：超时即杀进程。

返回文本含 `returncode` / `stdout` / `stderr`，模型据此判断成败并修复：

```
returncode: 0

--- stdout ---
<命令标准输出>

--- stderr ---
<命令标准错误>
```

## 安全（务必读完）

本插件执行**任意 shell 命令**，是一把万能钥匙。**插件内的防护只是软栅栏，不是安全边界**：

- `read_only=true` 时拒绝 `rm -rf /`、`mkfs`、`dd`、`shutdown` 等破坏性子串；
- 单流输出截断 64KB，防撑爆上下文。

真正的安全靠**运行环境**：写代码 agent 必须跑在**容器 / 受限用户 / 网络禁用**的环境，
且**不要**把 `~/.keeper/memory` 挂进容器（否则它能读其他 agent 的记忆）。注意 bash 能在命令里
`cd /` 逃逸 workspace 限制——`read_only` 只挡显式破坏性命令，挡不住路径逃逸。

## 装载

平台建插件 → 来源选**本地目录** → 路径填 `…/plugins/bash` → 绑给写代码 agent。
或本地 DB 直接加：

```python
AgentBinding(agent_id=<写代码agent>, kind="plugin", ref_name="bash", enabled=True)
```

## 协议（调试用）

stdin：

```json
{"tool":"run","args":{"command":"echo hi"},"context":{"workspace":"/abs/path","read_only":false}}
```

stdout 最后一行：

```json
{"ok":true,"result":"returncode: 0\n\n--- stdout ---\nhi\n\n--- stderr ---\n"}
```

非零退出码不会让 `ok` 变 false（命令本身执行成功了）；只有进程崩溃 / 超时 /
输出不是 JSON 才会 `ok:false`。
