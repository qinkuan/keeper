"""会话级「动态激活插件」：在对话里用 `/` 临时启用**库里有但未 link 给 agent** 的插件。

激活态存在 ``ChatSession.activated_plugins``（JSON 数组，规范名 = 清单 ``name``），
仅对本会话生效；真正把插件工具**注入当轮工具表**在 ``agent/keeper.prepare()`` 里做
（见 ``KeeperAgent._inject_dynamic_plugins``）。本模块只负责两件事：

- ``available_plugins``：给前端 `/` 选择器列候选（库里有 − 已 link − 不支持动态加载的）；
- ``get_activated / activate / deactivate``：读写会话的激活名单。

一期只支持 **bin 类**：mcp 类要现连 session（``agent.keeper._load_mcp`` 启动期才建）、
skill 类固化在跨会话共享的 Process 里，都不适合按会话动态注入——所以选择器把它们过滤掉。
"""
from __future__ import annotations

import json
import logging
from typing import List

from .lib import find_plugin, linked_plugins, scan_library
from ..store import get_session_factory
from ..store.models import ChatSession

logger = logging.getLogger(__name__)


def available_plugins(agent_id: str) -> List[dict]:
    """库里有、未 link 给该 agent、且支持动态加载（bin 类）的插件（给 `/` 选择器）。"""
    linked = set(linked_plugins(agent_id))
    out: List[dict] = []
    for p in scan_library():
        if p.error:
            continue  # 清单坏了装不上的，不往选择器里摆
        if p.name in linked or p.dirname in linked:
            continue  # 已 link 的走正常通道，不用动态加载
        # 一期只支持 bin 类动态注入（mcp 要现连 session、skill 固化在跨会话共享的
        # Process 里，见模块 docstring）。不过滤的后果：选择器摆着 skill/mcp 插件，
        # 用户选中后 chip 亮起来、当轮却什么都不会发生——静默失效比没有更糟。
        # （PluginPicker 的空状态文案一直按"仅 bin"写着，差的正是这一步。）
        if "bin" not in (p.kinds or []):
            continue
        out.append(p.to_dict())
    return out


def _parse(raw) -> List[str]:
    if not raw:
        return []
    try:
        arr = json.loads(raw)
    except Exception:  # noqa: BLE001
        return []
    return [n for n in arr if isinstance(n, str)]


async def get_activated(session_id: str) -> List[str]:
    """本会话已激活的插件名（规范名 = 清单 name）。"""
    async with get_session_factory()() as db:
        row = await db.get(ChatSession, session_id)
        if row is None:
            return []
        return _parse(row.activated_plugins)


async def activate(session_id: str, name: str) -> List[str]:
    """激活一个插件到会话，返回激活后的名单。"""
    p = find_plugin(name)
    if p is None or p.error:
        raise ValueError(f"插件不存在或无法加载：{name}")
    # 选择器已按 kind 过滤，这里是直接调 API 的兜底：明确拒绝而不是静默接受——
    # 后者会让 chip 亮着而当轮什么都不发生（skill_items 在注入时被丢弃）。
    if "bin" not in (p.kinds or []):
        kinds = "/".join(p.kinds) or "未知"
        raise ValueError(
            f"插件 {p.name}（{kinds} 类）暂不支持在对话内动态加载（一期仅 bin 类工具）。"
            f"请在智能体信息抽屉的「插件」页把它勾选绑定给 agent，绑定后即时生效。"
        )
    async with get_session_factory()() as db:
        row = await db.get(ChatSession, session_id)
        if row is None:
            raise ValueError("会话不存在")
        names = _parse(row.activated_plugins)
        if p.name not in names:
            names.append(p.name)
        row.activated_plugins = json.dumps(names, ensure_ascii=False)
        await db.commit()
        return names


async def deactivate(session_id: str, name: str) -> List[str]:
    """从会话停用一个插件，返回剩余名单。"""
    async with get_session_factory()() as db:
        row = await db.get(ChatSession, session_id)
        if row is None:
            raise ValueError("会话不存在")
        names = _parse(row.activated_plugins)
        # 同时按传入名与规范名匹配：前端传回 name 或 dirname 都能删
        keys = {name}
        p = find_plugin(name)
        if p is not None:
            keys.add(p.name)
            keys.add(p.dirname)
        names = [n for n in names if n not in keys]
        row.activated_plugins = json.dumps(names, ensure_ascii=False)
        await db.commit()
        return names
