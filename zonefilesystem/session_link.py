"""在会话工作区里给「我的空间」建软链接。

为什么需要它
------------
会话空间（``~/.keeper/workspace/session/<sid>/``）是 agent 唯一的工作目录，而
「我的空间」（``settings.yaml`` 的 ``user_space.path``）是用户的地盘，两者原本
不相干：agent 要知道「我的空间」的绝对路径才能访问它（``@我的空间`` mention 会
把绝对路径交给模型，见 ``chat/service.py`` 的 ``resolve_workspace``）。

建了这个链接之后，agent 在会话空间里 ``ls`` 就能看到我的空间的内容，用相对路径
（``my-space/xxx``）就能读写——不用先知道绝对路径，也不用每次 mention。

安全边界
--------
- **不扩大访问面**：``resolve_workspace`` 早就把「我的空间」放进了守卫的
  ``extra_roots``（见 ``agent/process.py``），agent 用绝对路径本来就能访问它。
  链接只是让它「可见 + 相对可达」，没有放开任何新的东西。
- **绝对链接而非相对链接**：macOS 的 ``/var`` 是 ``/private/var`` 的软链接，
  相对链接会悬空（``plugin/lib.py`` 里踩过并记着这个坑）。
- **防成环**：目标不能等于或包含链接自身所在目录，否则 ``ELOOP``
  （照搬 ``chat/api.py`` 的 ``_guard_symlink_target`` 思路）。
- **失败不阻断建会话**：链接是便利功能，我的空间没配 / 不存在 / 没权限都不该
  让「新建会话」这个核心动作失败。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: 会话工作区里那个链接的名字。
#:
#: 用英文而不是「我的空间」：它会进 agent 的 shell 命令与工具参数，中文名在
#: bash 里要额外引号，编码出问题时更难查。语义由 persona / skill 告诉模型。
LINK_NAME = "my-space"


def my_space_root() -> Optional[Path]:
    """「我的空间」的绝对路径；未配置回 ``None``。

    读的是原始 settings（无缓存），与 ``resolve_workspace`` 取的是同一份，
    保证链接指向的就是 agent 被允许访问的那个目录。
    """
    try:
        from ..setting import get_user_space_path

        p = get_user_space_path()
    except Exception as e:  # noqa: BLE001
        logger.warning("读「我的空间」路径失败（跳过建链接）：%s", e)
        return None
    p = (p or "").strip()
    return Path(p).expanduser() if p else None


def _is_loop(target: Path, link: Path) -> bool:
    """目标与链接是否会成环：互为父子、或指向自己都算。"""
    try:
        t = target.resolve()
        e = link.parent.resolve()
    except OSError:
        # resolve 失败（比如权限）时保守当成有环，不建
        return True
    return t == e or t in e.parents or e in t.parents


def ensure_my_space_link(agent_space: Path) -> Optional[Path]:
    """确保 ``agent_space`` 下有指向「我的空间」的软链接，返回链接路径。

    幂等：已存在且指向正确就直接返回；指向别的（比如用户改过我的空间路径）
    就重建。任何失败都只记日志并回 ``None``——**不抛异常**，调用方不该因为
    一个便利功能而失败。

    ``agent_space`` 不存在时会先建好（新建会话那一刻它还没被创建）。
    """
    link = Path(agent_space) / LINK_NAME
    # 先把会话工作区建出来：调用方是「新建会话」，那一刻目录还不存在
    # （resolve_workspace 要到第一次发消息才建）。即便后面因我的空间没配而
    # 不建链接，这个目录本身也该在会话创建时就位。
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("建会话工作区目录失败（%s）：%s", link.parent, e)
        return None

    target = my_space_root()
    if target is None:
        logger.debug("「我的空间」未配置，跳过在 %s 建软链接", agent_space)
        return None

    if not target.is_dir():
        # 用户可能填了个不存在的路径：不建链接，也别让会话建不起来
        logger.warning(
            "「我的空间」目录不存在（%s），跳过在会话空间建软链接", target
        )
        return None

    if _is_loop(target, link):
        logger.warning(
            "「我的空间」（%s）与会话空间（%s）互为包含，建软链接会成环，已跳过",
            target,
            link.parent,
        )
        return None

    try:
        # 已存在：指向对了就不动，指向错了（我的空间改过路径）才重建
        if link.is_symlink():
            try:
                if os.readlink(link) == str(target):
                    return link
            except OSError:
                pass
            link.unlink()
        elif link.exists():
            # 同名真实文件/目录：不动它（可能是用户的文件），跳过
            logger.warning("%s 已存在且不是软链接，跳过建「我的空间」链接", link)
            return None

        os.symlink(str(target), str(link))
        logger.debug("已在会话空间建「我的空间」软链接：%s -> %s", link, target)
        return link
    except OSError as e:
        # 权限不足 / 文件系统不支持软链接（某些挂载的 FAT/ExFAT）都落这里
        logger.warning("建「我的空间」软链接失败（%s -> %s）：%s", link, target, e)
        return None
