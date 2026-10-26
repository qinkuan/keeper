#!/usr/bin/env python3
"""bash 插件的 bin 可执行文件：在 workspace 内执行 shell 命令。

协议（与 keeper/tools/subprocess_tool.py 严格一致）：
- stdin 读取一行 JSON：
  {"tool": "run", "args": {"command": "...", "timeout": 60},
   "context": {"workspace": "...", "read_only": false, "agent_space": "..."}}
- stdout 的**最后一行**必须是 JSON：
  {"ok": true, "result": "..."} 或 {"ok": false, "error": "..."}
- 日志 / 进度一律走 stderr，绝不能打印到 stdout，否则会污染最后一行解析。
- 非零退出码 / 超时 / 输出不是 JSON，keeper 侧均视为调用失败。

安全说明：本工具执行**任意 shell 命令**，本质上是给 agent 的万能钥匙。
它只做最小限度的兜底（read_only 下拒绝明显破坏性命令），真正的隔离
依赖运行它的进程环境（容器 / 受限用户 / 网络禁用）。请勿在未隔离的
生产机上装载此插件。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

MAX_OUTPUT = 64 * 1024  # 单流截断上限，防撑爆上下文

# read_only 模式下拦截的破坏性子串（保护根文件系统；允许 workspace 内清理如 rm -rf node_modules）
_DANGEROUS = ("rm -rf /", "rm -rf /*", "mkfs", "dd if=", ":(){", "shutdown", "reboot")


def _truncate(s: str, limit: int = MAX_OUTPUT) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n... [输出已截断，原始长度 {len(s)} 字节]"


def _format_result(r: dict) -> str:
    return (
        f"returncode: {r['returncode']}\n"
        f"\n--- stdout ---\n{r['stdout']}\n"
        f"\n--- stderr ---\n{r['stderr']}"
    )


def _shell_argv(command: str):
    """跑命令的 argv：优先用内核选好的 shell。

    内核起本插件时会把 KEEPER_SHELL / KEEPER_SHELL_FLAG 注进环境（见
    keeper/framework/shell.py）：POSIX 是 `/bin/sh -c`，Windows 上是随包内置的
    busybox `sh.exe -c`（真 POSIX 语义）—— 不用 cmd.exe /c，因为命令里常有
    管道、&&、$VAR、glob，两种 shell 语义不一样会让 agent 的命令莫名其妙地挂。

    拿不到环境变量（老内核 / 用户改过插件）就退回 create_subprocess_shell，
    行为与从前完全一致。
    """
    exe = (os.environ.get("KEEPER_SHELL") or "").strip()
    flag = (os.environ.get("KEEPER_SHELL_FLAG") or "-c").strip()
    if exe and os.path.exists(exe):
        return [exe, flag, command]
    return None


async def _run(command: str, cwd: str, timeout: int) -> dict:
    # 用哪个 shell 由内核决定（跨平台保持 POSIX 语义）；插件不暴露给 keeper
    # 主进程，崩了只影响这一次调用（见 subprocess_tool 的设计说明）
    argv = _shell_argv(command)
    if argv is None:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PWD": cwd},
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PWD": cwd},
        )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"命令执行超时（{timeout}s）")
    return {
        "stdout": _truncate(out.decode("utf-8", "replace")),
        "stderr": _truncate(err.decode("utf-8", "replace")),
        "returncode": proc.returncode,
    }


async def main() -> int:
    raw = sys.stdin.read()
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"stdin 不是合法 JSON: {e}"}))
        return 1

    tool = msg.get("tool")
    args = msg.get("args") or {}
    ctx = msg.get("context") or {}
    workspace = ctx.get("workspace") or os.getcwd()
    # 默认执行目录用「会话私有草稿区」（agent_space）：模型裸写的相对路径
    # 落到会话空间，而不是用户工作区根。用户工作区（user/default）仅作兜底。
    run_cwd = ctx.get("agent_space") or workspace
    read_only = bool(ctx.get("read_only"))

    if tool != "run":
        print(json.dumps({"ok": False, "error": f"未知工具: {tool}"}))
        return 1

    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        print(json.dumps({"ok": False, "error": "缺少 command 参数"}))
        return 1

    if read_only:
        low = command.lower()
        for pat in _DANGEROUS:
            if pat in low:
                print(json.dumps(
                    {"ok": False, "error": f"read_only 模式下禁止执行破坏性命令：命中 {pat!r}"}
                ))
                return 1

    timeout = int(args.get("timeout") or 60)
    try:
        r = await _run(command, run_cwd, timeout)
    except RuntimeError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1

    print(json.dumps({"ok": True, "result": _format_result(r)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
