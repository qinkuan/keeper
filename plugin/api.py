"""插件库（只读）。

设计前提：**插件库是一个目录，不是一种来源。** 手放包、plugin-authoring 写完
放进去、将来远程下载安装，都只是"往目录里放东西"。所以这里**只有读**——
没有安装 / 卸载 / 上传接口：加插件就是往目录里放文件，那是文件管理器的事。

这个路由存在的意义是让 UI 能看见库里有什么、清单里声明了什么环境要求，
好让创建 agent 时能提前校验。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from . import plugins_root, scan_library
from .active import activate, available_plugins, deactivate, get_activated

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


# ── 会话级动态激活（对话里 `/` 临时启用未 link 的插件）──────────────────────
# 激活态见 plugin/active.py；装配进当轮工具表在 agent/keeper.prepare() 做。


@router.get("/available")
async def available(agent_id: str) -> dict:
    """库里有、未 link 给该 agent、且支持动态加载（bin 类）的插件——给 `/` 选择器。"""
    return {"plugins": available_plugins(agent_id)}


@router.get("/sessions/{session_id}/activated")
async def activated(session_id: str) -> dict:
    """本会话当前激活的插件名列表（输入框上方的 chip 用它渲染）。"""
    return {"plugins": await get_activated(session_id)}


@router.post("/sessions/{session_id}/activated")
async def activate_one(session_id: str, payload: dict) -> dict:
    """激活一个插件到会话；返回激活后的名单。"""
    name = str((payload or {}).get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="缺少 name")
    try:
        names = await activate(session_id, name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"plugins": names}


@router.delete("/sessions/{session_id}/activated/{name}")
async def deactivate_one(session_id: str, name: str) -> dict:
    """从会话停用一个插件；返回剩余名单。"""
    try:
        names = await deactivate(session_id, name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"plugins": names}
