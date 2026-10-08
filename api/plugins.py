"""插件库（只读）。

设计前提：**插件库是一个目录，不是一种来源。** 手放包、plugin-authoring 写完
放进去、将来远程下载安装，都只是"往目录里放东西"。所以这里**只有读**——
没有安装 / 卸载 / 上传接口：加插件就是往目录里放文件，那是文件管理器的事。

这个路由存在的意义是让 UI 能看见库里有什么、清单里声明了什么环境要求，
好让创建 agent 时能提前校验。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from ..plugin import plugins_root, scan_library

router = APIRouter(prefix="/manage/plugins", tags=["plugins"])
logger = logging.getLogger(__name__)


@router.get("")
async def list_plugins() -> dict:
    """列出插件库里的全部插件。

    只读、不缓存：插件库就是磁盘上的一个目录，用户随时可能往里丢文件，
    缓存只会让人以为"我放了但它没出现"。目录体积有遍历上限（见
    ``_dir_size``），插件多时也不会卡住。
    """
    root = plugins_root()
    plugins = scan_library(root)
    return {
        "root": str(root),
        "exists": root.is_dir(),
        "plugins": [p.to_dict() for p in plugins],
    }
