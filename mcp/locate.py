"""定位连接器的可执行文件——**只找，不下载**。

约定：**下载只发生在打包镜像时**，运行时不做任何网络获取。
这样启动快、不依赖网络、行为可预测——打包成什么样，跑起来就是什么样。

查找顺序：

1. ``command`` 是绝对路径且存在，或在 PATH 里能找到（如 npx）→ 用它；
2. 否则去 agent 的缓存目录找 ``<cache_dir>/<name>/<可执行文件>``；
3. 都没有 → 抛 ``ExecutableNotFoundError``，由上层**跳过该连接器**（不连它）。

「找不到就不连接」而不是让 agent 起不来：连接器是附加能力，
缺哪个就少哪个，主体画像没问题就该能跑。
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


class ExecutableNotFoundError(RuntimeError):
    """本地没有该连接器的可执行文件（打包时没放进来）。"""


def default_cache_root() -> Path:
    """连接器的**全局共享缓存根**（agent 没配 ``mcp_cache_dir`` 时用它）。

    全局共享的意义：同一台机器上多个 agent 可以共用同一份已打包的资源。
    """
    return Path.home() / ".keeper" / "mcp"


def cache_target(name: str, command: str, cache_root: Path) -> Path:
    """该连接器在缓存目录里应该待的位置：``<cache_root>/<name>/<可执行文件>``。"""
    base = Path(command).name if command else name
    return cache_root / name / base


def locate_executable(name: str, command: str, cache_root: Path) -> str:
    """定位连接器的可执行文件，返回可用的 command。

    Args:
        name: 连接器名（同时是缓存子目录名）
        command: 仓库给的默认命令，或 agent 覆盖后的路径
        cache_root: agent 的缓存根目录（``mcp_cache_dir``），留空则用全局共享

    Raises:
        ExecutableNotFoundError: 本地没有；**不下载**，由上层决定跳过。
    """
    # 1) agent 显式给了可用命令：绝对路径存在，或 PATH 里能找到（如 npx）
    if command:
        p = Path(command)
        if p.is_absolute():
            if p.exists():
                return str(p)
        elif shutil.which(command):
            return command

    # 2) 缓存目录里（打包时放进去的）
    target = cache_target(name, command, cache_root)
    if target.exists():
        return str(target)

    raise ExecutableNotFoundError(
        f"本地没有可执行文件（查过 {target}），应在打包镜像时放进缓存目录"
    )
