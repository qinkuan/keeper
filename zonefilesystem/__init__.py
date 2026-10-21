"""「我的空间」文件操作能力。

把原本散落在 chat/api.py 里的目录 / 文件读写逻辑统一收归到这里，
Web 层（chat/api.py）只负责解析请求、再把活儿交给本包。
"""
from .browse import browse_directory
from .space_ops import (
    copy_from_local,
    copy_in_space,
    delete_in_space,
    download_from_space,
    mkdir_in_space,
    open_in_explorer,
    raw_from_space,
    read_space_file,
    rename_in_space,
    resolve_target,
    space_root,
    upload_to_space,
)

__all__ = [
    "browse_directory",
    "space_root",
    "resolve_target",
    "read_space_file",
    "open_in_explorer",
    "upload_to_space",
    "download_from_space",
    "delete_in_space",
    "rename_in_space",
    "mkdir_in_space",
    "raw_from_space",
    "copy_in_space",
    "copy_from_local",
]
