"""把平台下发的 agent 配置落成本地库——本地运行时的**配置缓存**。

平台是真相，本地库是缓存：装载一个 agent 前，先把它的画像、引用的插件、
以及绑定关系整体写进本地库，再交给 ``build_agent`` 装配。
这样装配器（``keeper.agent.config``）完全不用改——它仍然只读本地库。

幂等：同一个 agent 反复同步只会覆盖，不会重复插入（资源按 name、agent 按 id、绑定整体替换）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from ..config import KeeperConfig, load_config
from ..store import Agent, get_session_factory

logger = logging.getLogger(__name__)


def _dump(value: Any) -> Optional[str]:
    """结构化值 → 库存的 JSON 文本；None 原样存 NULL。"""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)





async def _upsert_agent_row(db, a: Dict[str, Any]) -> None:
    """按平台分配的 id 落地 agent 画像（不重新生成 id）。

    这些字段取自平台；模型与工作区是本地的事，不从平台来。

    ``origin`` 恒为 ``platform``：走这个函数的行都是平台的镜像。本机创建的
    agent 不经过这里（``sync_agent`` 只在装载接口里被调用，且只接受平台上的
    agent），它的 ``origin`` 是 ``local``——这个字段是 UI 区分「同一个 agent
    显示两遍」和「本地改动会不会被平台冲掉」的唯一依据。
    """
    row = await db.get(Agent, a["id"])
    fields = {
        "name": a.get("name"),
        "description": a.get("description"),
        "persona": a.get("persona") or "",
        "status": a.get("status") or "active",
        "origin": "platform",
    }
    if row is None:
        db.add(Agent(id=a["id"], **fields))
    else:
        for k, v in fields.items():
            setattr(row, k, v)



async def sync_agent(a: Dict[str, Any]) -> str:
    """同步单个 agent 的配置到本地库，返回 agent id。"""
    factory = get_session_factory()
    async with factory() as db:
        await _upsert_agent_row(db, a)
        await db.flush()
        await db.commit()
    logger.info("已同步 agent 配置到本地: id=%s name=%s", a["id"], a.get("name"))
    return a["id"]


async def sync_agents(agents: List[Dict[str, Any]]) -> List[str]:
    """批量同步，返回同步成功的 agent id 列表。"""
    return [await sync_agent(a) for a in agents or []]
