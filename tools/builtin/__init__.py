"""内置工具：keeper 自带的能力（文件交付 / git），不需要下载。

工具的两种来源：

- ``builtin``：本包里的工厂函数，代码里就有；
- ``external``：打包时放进缓存目录，按 ``impl`` 定位加载。

工厂签名统一：

    make_xxx(root: Path, read_only: bool) -> ProcessorTool

``root`` 是 agent 的工作空间根目录（由装配器传入），工具的全部操作
都限制在它之内——工具自己不找目录、也不认识工作空间以外的路径。

读 / 写 / 列目录 / 编辑 / 搜索文件一律走 ``bash``，本包只保留 ``fs.deliver``
这一个正式交付入口（详见 ``fs``）。
"""
from .fs import make_deliver
from .task import make_mark_item_done

__all__ = [
    "make_deliver",
    "make_mark_item_done",
]
