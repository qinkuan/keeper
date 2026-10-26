"""跨平台的「跑一条 shell 命令」：显式决定用哪个 shell。

背景
----
agent 唯一能执行命令的入口是 bash 插件（``bash__run``），插件内部用
``asyncio.create_subprocess_shell`` —— 在 POSIX 上它等于 ``sh -c <cmd>``，
在 Windows 上却等于 ``cmd.exe /c <cmd>``。同一份命令（管道、``&&``、重定向、
``$VAR``、glob）在两种 shell 下语义不同：agent 写的 ``a && b``、``ls *.py``、
``echo $HOME`` 到 Windows 上会莫名其妙地挂。

所以这里把「用哪个 shell」变成一个**显式决策**，并且**跨平台保持 POSIX 语义**：

- POSIX：``/bin/sh -c``
- Windows：随包内置的 busybox ``sh.exe``（ash，真 POSIX，~2MB）→ 系统的
  Git Bash / MSYS2 的 ``sh.exe`` → 最后兜底 ``cmd.exe /c``

选内置 busybox 优先而不是用户装的 Git Bash，是为了**行为可预测**（同一台机器
不管装没装 Git，命令语义都一样）；Git Bash 排第二是因为它的 coreutils 更全。

内核起插件进程时把选好的结果用环境变量告诉插件：

==========================  ==========================================
``KEEPER_SHELL``            shell 可执行文件（绝对路径）
``KEEPER_SHELL_FLAG``       传命令用的开关（POSIX 是 ``-c``，cmd 是 ``/c``）
==========================  ==========================================

插件自己不做这套判断（它可能被用户改过、也可能是老版本）：拿不到环境变量就
退回原来的 ``create_subprocess_shell``，行为与今天一致。
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import Dict, List, Optional, Tuple

# Windows 上「本来就有的 POSIX shell」候选（Git for Windows / MSYS2 / Cygwin）。
# 这些是用户自己装的，可能没有，也可能版本很老 —— 所以排在内置 busybox 后面。
_WINDOWS_POSIX_SHELLS = (
    r"C:\Program Files\Git\usr\bin\sh.exe",
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\usr\bin\sh.exe",
    r"C:\msys64\usr\bin\sh.exe",
    r"C\cygwin64\bin\sh.exe",
)


def _bundled_sh() -> Optional[str]:
    """随包内置的 POSIX shell（busybox 的 sh.exe）。

    路径由桌面壳注入：它知道 app 的 Resources 在哪（打包形态下内核自己
    不知道自己在 app 里的位置）。
    """
    p = (os.environ.get("KEEPER_BUNDLED_SH") or "").strip()
    return p if p and os.path.exists(p) else None


def _first_existing(paths) -> Optional[str]:
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def choose_shell() -> Tuple[str, str]:
    """选出「用哪个 shell + 传命令的开关」。

    返回 ``(可执行文件, 开关)``：POSIX 是 ``(/bin/sh, "-c")``；
    Windows 兜底到 cmd 时是 ``(cmd.exe, "/c")``。
    """
    if os.name != "nt":
        return ("/bin/sh", "-c")

    # 1) 随包内置的 busybox sh（行为确定，POSIX 语义）
    bundled = _bundled_sh()
    if bundled:
        return (bundled, "-c")
    # 2) 用户自己装的 POSIX shell
    system_sh = _first_existing(_WINDOWS_POSIX_SHELLS)
    if system_sh:
        return (system_sh, "-c")
    # 3) 兜底：cmd.exe（命令语法不同，但总比跑不了强）
    cmd = shutil.which("cmd.exe") or "cmd.exe"
    return (cmd, "/c")


def shell_argv(command: str) -> List[str]:
    """跑一条命令的 argv（不含 shell 自身的可执行文件）。"""
    exe, flag = choose_shell()
    return [exe, flag, command]


def shell_env() -> Dict[str, str]:
    """要注入给子进程的环境变量（告诉插件用哪个 shell）。"""
    exe, flag = choose_shell()
    return {"KEEPER_SHELL": exe, "KEEPER_SHELL_FLAG": flag}


def describe() -> str:
    """一句话描述当前用的 shell（给日志 / 工具返回用）。"""
    exe, flag = choose_shell()
    return f"{exe} {flag}"


if __name__ == "__main__":  # 手动看一眼当前会选到什么
    print(describe())
    print("argv:", shell_argv("echo hi"))
    print("python:", sys.executable)
