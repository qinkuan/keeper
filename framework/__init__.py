"""运行时 framework：检测本机已安装的 python / node 环境。

设计要点：

- keeper 不再从网络下载可移植运行时，而是**直接检测用户电脑上已装的解释器**
  （PATH 上的 ``python3`` / ``python`` / ``node``，或 config 显式指定的路径）。
- 各 agent 的依赖（工具包 / 脚本 / 本地 MCP 进程）用本机检测到的解释器跑。
- 若本机缺某个版本：provision 阶段会提示不一致，用户需自行安装或在
  ``config.yaml`` 的 ``framework`` 段显式指定该解释器路径。
"""
from __future__ import annotations

from .runtime import (
    KEEPER_HOME,
    agent_runtime_paths,
    current_platform,
    detect_runtimes,
    ensure_runtime,
    framework_root,
    resolve_runtime,
    runtime_dir,
)

__all__ = [
    "KEEPER_HOME",
    "framework_root",
    "runtime_dir",
    "agent_runtime_paths",
    "current_platform",
    "detect_runtimes",
    "ensure_runtime",
    "resolve_runtime",
]
