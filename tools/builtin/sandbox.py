"""工作空间路径沙箱：文件 / git 类工具的**安全底线**。

模型给出的任何路径都必须先过 ``safe_path()``。否则一句
「读取 ../../etc/passwd」就能把工作空间外的文件读走——这是不可接受的。

被挡住的两类情况：

- 用 ``..`` 或绝对路径往外跳；
- 工作空间内有**指向外部的软链**（``resolve()`` 会把它解析到真实位置）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

# 模型侧表示「会话私有临时目录」的前缀。用前缀而不是把绝对路径塞进 prompt：
# 绝对路径每次会话都变（带 session_id / agent_id），模型抄错一个字就写到别处去了；
# 前缀稳定短，解析交给工具层。
TMP_PREFIX = "@tmp"


class PathEscape(ValueError):
    """路径越出了工作空间。"""


def _within(target: Path, base: Path) -> bool:
    target = target.resolve()
    base = base.resolve()
    return target == base or base in target.parents


def resolve_path(root: Path, tmp_root: Optional[Path], rel: str) -> Path:
    """解析模型给出的路径，支持 ``@tmp/`` 前缀指向会话私有临时目录。

    Args:
        root: 工作空间根（用户空间），``@tmp/`` 之外的路径都按它的相对路径解析。
        tmp_root: 本 (会话, agent) 的私有目录（``workspace/session/<sid>/<aid>``）。
            为 ``None``（无工作空间上下文的构建期）时 ``@tmp/`` 一律拒绝。
        rel: 模型给出的路径。

    临时目录**刻意留在工作区之外**，不挪进来：挪进来虽然能省掉改沙箱的功夫，却会在
    用户的文件浏览器里凭空多出一堆临时文件。它是对工作区**追加**的一个合法根，
    不是放开整个文件系统——``@tmp/../../etc/passwd`` 照样被下面的边界检查挡掉。

    Raises:
        PathEscape: 越界时抛出；调用方应把错误转成给模型看的提示，而不是崩掉。
    """
    rel = (rel or ".").strip().lstrip("/")
    if rel == TMP_PREFIX or rel.startswith(TMP_PREFIX + "/"):
        if tmp_root is None:
            raise PathEscape(
                f"{TMP_PREFIX}/ 不可用：当前请求没有会话临时目录（无工作空间上下文）"
            )
        base = Path(tmp_root)
        target = (base / rel[len(TMP_PREFIX):].lstrip("/")).resolve()
        if not _within(target, base):
            raise PathEscape(f"路径越界（不在会话临时目录内）：{rel}")
        return target
    return safe_path(root, rel)


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
