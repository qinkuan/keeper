---
name: bash
description: 写完或改动代码后必须自己运行验证（跑脚本 / 跑测试 / 看输出）；这是强制规矩，不是可选流程
always: true
keywords: [运行, 执行, 跑测试, pytest, 命令, shell, 自运行, 自检]
tools: [bash__run]
---

# 自运行自检

你是写代码的 agent。写完或改动代码后，**必须自己运行验证**，而不是只交付文本。

## 闭环流程

1. 把代码写入当前目录（用 bash 的 `cat >`；`fs.write_file` 等 fs.* 工具已废弃，一律走 bash）。
2. 用 `bash__run` 执行命令运行它，例如：
   - `python main.py`
   - `pytest -q`
   - `node test.js`
   - `go test ./...`
3. 读取返回的 `stdout` / `stderr` / `returncode`：
   - `returncode != 0` 或 `stderr` 有报错 → 说明运行/编译失败；
   - 输出与预期不符 → 说明逻辑错误。
4. 根据输出修正代码，重复步骤 2-3，直到**运行通过且输出符合预期**。

## 注意

- 命令在当前目录下执行；`read_only` 模式下不能跑破坏性命令（如 `rm -rf /`）。
- 单次命令有超时（默认 60s），重型任务拆成多条命令分步执行。
- 长输出会被截断；必要时把日志重定向到文件，再用 bash（`cat` / `tail`）读取完整内容。
- 不要凭空声称"已验证通过"——必须以 `bash__run` 的真实返回为准。
