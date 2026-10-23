"""会话级「动态激活插件」：在对话里用 `/` 临时启用**库里有但未 link 给 agent** 的插件。

激活态存在 ``ChatSession.activated_plugins``（JSON 数组，规范名 = 清单 ``name``），
仅对本会话生效；真正把插件内容**注入当轮**在 ``agent/keeper.prepare()`` 里做
（见 ``KeeperAgent._inject_dynamic_plugins``）：bin 类注册工具、skill 类把正文
整段注入 system（`/` 是用户显式点开的，高优先级，不走按需加载）。本模块只负责：

- ``available_plugins``：给前端 `/` 选择器列候选（库里有 − 已 link − mcp 类）；
- ``get_activated / activate / deactivate``：读写会话的激活名单。

**mcp 类不支持**：MCP session 在启动期才建（``agent.keeper._load_mcp``），按会话
现连代价与生命周期都不适合动态注入——选择器过滤掉，直接调 API 也会被拒。
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
    """库里有、未 link 给该 agent、且支持动态加载的插件（给 `/` 选择器）。

    支持两类：**bin**（工具注册进当轮表）与 **skill**（正文整段注入 system，
    见 ``agent.keeper._inject_dynamic_plugins``）。**mcp 类除外**——session 在
    启动期才建，按会话现连不合适；带 mcp 组件的包整个跳过（哪怕它同时有 bin），
    免得给用户「选了一半」的期待。
    """
    linked = set(linked_plugins(agent_id))
    out: List[dict] = []
    for p in scan_library():
        if p.error:
            continue  # 清单坏了装不上的，不往选择器里摆
        if p.name in linked or p.dirname in linked:
            continue  # 已 link 的走正常通道，不用动态加载
        if "mcp" in (p.kinds or []):
            continue  # mcp 要现连 session，一期不支持动态加载
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
    # 兜底（选择器已过滤）：mcp 类明确拒绝——它的正文注入不了、工具也连不上，
    # 静默接受的后果是 chip 亮着而当轮什么都不发生。
    if "mcp" in (p.kinds or []):
        raise ValueError(
            f"插件 {p.name}（mcp 类）暂不支持在对话内动态加载"
            f"（MCP 连接在启动期建立）。请在智能体信息抽屉的「插件」页勾选绑定给 agent。"
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
