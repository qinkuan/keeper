"""工作空间路径沙箱：文件 / git 类工具的**安全底线**。

模型给出的任何路径都必须先过 ``resolve_path()``。否则一句
「读取 ../../etc/passwd」就能把工作空间外的文件读走——这是不可接受的。

被挡住的两类情况：

- 用 ``..`` 或绝对路径往外跳；
- 工作空间内有**指向外部的软链**（``resolve()`` 会把它解析到真实位置）。

双工作区寻址
------------
agent 操作两个工作区，用稳定前缀指代（绝对路径每次会话都变、带随机 id，模型抄错就写到别处）：

- ``@user/...``    → 用户工作区（用户的「真相源」）。写操作受 ``read_only`` 约束。
- ``@session/...`` → 会话工作区（本会话专用，agent 的草稿 + 产物都在这里）。
- ``@tmp/...``     → 会话工作区里的「临时草稿」子目录（即 ``@session/tmp/...`` 的别名），
                    每轮末尾被清空，不参与交付。

会话工作区刻意留在用户工作区**之外**（``workspace/session/<sid>/<aid>``），
不挪进用户目录——否则用户文件浏览器里会凭空多出一堆临时文件。它是对工作区
**追加**的一个合法根，不是放开整个文件系统。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

# 模型侧的工作区前缀
USER_PREFIX = "@user"
SESSION_PREFIX = "@session"
TMP_PREFIX = "@tmp"  # 会话内的临时草稿子目录（@session/tmp 的别名）


class PathEscape(ValueError):
    """路径越出了工作空间。"""


def _within(target: Path, base: Path) -> bool:
    target = target.resolve()
    base = base.resolve()
    return target == base or base in target.parents


def classify_prefix(rel: str) -> str:
    """返回路径前缀类别：``user`` / ``session`` / ``tmp`` / ``default``。

    ``default`` 表示未带前缀，按**用户工作区**处理。
    """
    s = (rel or "").strip().lstrip("/")
    if s.startswith(USER_PREFIX + "/"):
        return "user"
    if s == TMP_PREFIX or s.startswith(TMP_PREFIX + "/"):
        return "tmp"
    if s.startswith(SESSION_PREFIX + "/"):
        sub = s[len(SESSION_PREFIX) + 1:].lstrip("/")
        return "tmp" if sub.startswith("tmp/") else "session"
    return "default"


def is_scratch(rel: str) -> bool:
    """是否落在临时草稿区（@tmp/ 或 @session/tmp/）——每轮清空、不参与交付。"""
    return classify_prefix(rel) == "tmp"


def resolve_path(root: Path, tmp_root: Optional[Path], rel: str) -> Path:
    """解析模型给出的路径，支持 ``@user/`` / ``@session/`` / ``@tmp/`` 前缀。

    Args:
        root: 用户工作区根（``@user/`` 与无前缀路径按它解析）。
        tmp_root: 本 (会话, agent) 的私有会话工作区（``workspace/session/<sid>/<aid>``）。
            为 ``None``（无工作空间上下文的构建期）时，任何会话前缀都拒绝。
        rel: 模型给出的路径。

    Raises:
        PathEscape: 越界（或会话上下文缺失却用了会话前缀）时抛出。
    """
    s = (rel or ".").strip().lstrip("/")
    if s.startswith(USER_PREFIX + "/"):
        base = Path(root) if root is not None else None
        sub = s[len(USER_PREFIX) + 1:]
        label = "用户工作区"
    elif s == TMP_PREFIX or s.startswith(TMP_PREFIX + "/"):
        if tmp_root is None:
            raise PathEscape(f"{TMP_PREFIX}/ 不可用：当前请求没有会话工作区")
        base = Path(tmp_root) / "tmp"
        sub = s[len(TMP_PREFIX) + 1:]
        label = "会话临时目录"
    elif s.startswith(SESSION_PREFIX + "/"):
        if tmp_root is None:
            raise PathEscape(f"{SESSION_PREFIX}/ 不可用：当前请求没有会话工作区")
        base = Path(tmp_root)
        sub = s[len(SESSION_PREFIX) + 1:]
        label = "会话工作区"
    else:
        # 无前缀：按用户工作区相对路径解析
        return safe_path(root, rel)

    sub = sub.lstrip("/")
    if base is None:
        raise PathEscape(f"{label}不可用：当前请求没有工作空间上下文")
    target = (base / sub).resolve()
    if not _within(target, base):
        raise PathEscape(f"路径越界（不在{label}内）：{sub}")
    return target


def safe_path(root: Path, rel: str) -> Path:
    """把工作空间内的相对路径解析成绝对路径，并确保不越界。

    Args:
        root: agent 的工作空间根目录
        rel: 模型给出的相对路径

    Returns:
        解析后的绝对路径（保证在 root 之内，或等于 root）

    Raises:
        PathEscape: 越界时抛出；调用方应把错误转成给模型看的提示，而不是崩掉。
    """
    # 去掉开头的 /：绝对路径按相对处理，不让它指向系统根
    rel = (rel or ".").lstrip("/")
    target = (root / rel).resolve()
    base = root.resolve()
    if not (target == base or base in target.parents):
        raise PathEscape(f"路径越界（不在工作空间内）：{rel}")
    return target
