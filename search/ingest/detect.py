"""文件类型识别与变更检测。"""
import hashlib
from pathlib import Path

MARKDOWN_EXTS = {".md", ".markdown", ".mdown", ".mkd"}
PDF_EXTS = {".pdf"}
#: Word(.docx) 与带宏的 .docm（内部结构相同，都能当 zip 解）
DOCX_EXTS = {".docx", ".docm"}


def is_markdown(path: Path) -> bool:
    return Path(path).suffix.lower() in MARKDOWN_EXTS


def is_pdf(path: Path) -> bool:
    return Path(path).suffix.lower() in PDF_EXTS


def is_docx(path: Path) -> bool:
    return Path(path).suffix.lower() in DOCX_EXTS


def is_indexable(path: Path) -> bool:
    """这个文件要不要进检索索引。

    三种：Markdown（结构化切片）、PDF（大纲标题树 + 页级切片）、
    Word（标题层级 + 段落级切片，见 ingest/docx.py）。
    """
    return is_markdown(path) or is_pdf(path) or is_docx(path)


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
