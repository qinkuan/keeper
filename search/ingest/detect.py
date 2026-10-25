"""文件类型识别与变更检测。"""
import hashlib
from pathlib import Path

MARKDOWN_EXTS = {".md", ".markdown", ".mdown", ".mkd"}
PDF_EXTS = {".pdf"}


def is_markdown(path: Path) -> bool:
    return Path(path).suffix.lower() in MARKDOWN_EXTS


def is_pdf(path: Path) -> bool:
    return Path(path).suffix.lower() in PDF_EXTS


def is_indexable(path: Path) -> bool:
    """这个文件要不要进检索索引。

    目前两种：Markdown（结构化切片）与 PDF（大纲标题树 + 页级切片）。
    """
    return is_markdown(path) or is_pdf(path)


def _list_files(root: Path, recursive: bool, predicate):
    root = Path(root)
    if not root.exists():
        return []
    if recursive:
        return sorted(
            (p for p in root.rglob("*") if p.is_file() and predicate(p)),
            key=lambda p: str(p),
        )
    return sorted(
        (p for p in root.iterdir() if p.is_file() and predicate(p)),
        key=lambda p: str(p),
    )


def list_markdown_files(root: Path, recursive: bool = True):
    """列出 root 下所有 markdown 文件，按路径排序。"""
    return _list_files(root, recursive, is_markdown)


def list_indexable_files(root: Path, recursive: bool = True):
    """列出 root 下所有**可索引**文件（markdown + pdf），按路径排序。"""
    return _list_files(root, recursive, is_indexable)


def compute_hash(path: Path) -> str:
    """计算文件内容 sha256，用于精确变更检测。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def stat_info(path: Path):
    """返回 (mtime, size)。"""
    st = path.stat()
    return st.st_mtime, st.st_size
