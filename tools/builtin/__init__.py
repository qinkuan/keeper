"""内置工具：keeper 自带的能力（文件系统 / git），不需要下载。

工具的两种来源：

- ``builtin``：本包里的工厂函数，代码里就有；
- ``external``：打包时放进缓存目录，按 ``impl`` 定位加载。

工厂签名统一：

    make_xxx(root: Path, read_only: bool) -> ProcessorTool

``root`` 是 agent 的工作空间根目录（由装配器传入），工具的全部操作
都限制在它之内——工具自己不找目录、也不认识工作空间以外的路径。
"""
from .fs import (
    make_edit_file,
    make_find,
    make_list_dir,
    make_publish,
    make_read_file,
    make_write_file,
)
from .task import make_mark_item_done

__all__ = [
    "make_list_dir",
    "make_read_file",
    "make_write_file",
    "make_edit_file",
    "make_find",
    "make_publish",
    "make_mark_item_done",
]
