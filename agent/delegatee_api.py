"""delegatee（子 agent）配置接口：每个子 agent 独立模型 + 插件。

全局 per-delegatee（code / video / ... 各自一套），所有会话共用。配置落在
``delegatee_configs`` 表，prepare() 按配置造子 agent 的专属 LLM 与工具子集。

路由前缀 ``/manage/delegatees``，挂在 main.py 的 manage 路由组下（带 auth_deps）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from keeper.store import get_session_factory
from keeper.store.models import DelegateeConfig, LLMProfile

from .delegatee_types import DELEGATEE_TYPES, _parse_list, _dump_list

router = APIRouter(prefix="/manage/delegatees", tags=["delegatees"])


class DelegateeUpdate(BaseModel):
    llm_profile_id: Optional[str] = None
    plugins: List[str] = []
    tools: List[str] = []
    enabled: bool = True


@router.get("")
async def list_delegatees() -> Dict[str, Any]:
    """列出所有子 agent 类型及其当前配置（未配置则用默认值）。"""
    async with get_session_factory()() as db:
        cfgs = (await db.execute(select(DelegateeConfig))).scalars().all()
        cfg_map = {c.delegatee: c for c in cfgs}
        profile_ids = [c.llm_profile_id for c in cfgs if c.llm_profile_id]
        profiles = (
            (await db.execute(
                select(LLMProfile).where(LLMProfile.id.in_(profile_ids))
            )).scalars().all()
            if profile_ids else []
        )
        prof_map = {p.id: p for p in profiles}

    out: List[Dict[str, Any]] = []
    for name, cls in DELEGATEE_TYPES.items():
        cfg = cfg_map.get(name)
        profile_name = None
        llm_profile_id = None
        enabled = True
        plugins: List[str] = []
        tools: List[str] = []
        if cfg is not None:
            enabled = cfg.enabled
            llm_profile_id = cfg.llm_profile_id
            plugins = _parse_list(cfg.plugins)
            tools = _parse_list(cfg.tools)
            if cfg.llm_profile_id:
                prof = prof_map.get(cfg.llm_profile_id)
                profile_name = prof.name if prof else None
        out.append({
            "name": name,
            "description": cls.description,
            "enabled": enabled,
            "llm_profile_id": llm_profile_id,
            "profile_name": profile_name,
            "plugins": plugins,
            "tools": tools,
        })
    return {"delegatees": out}


@router.put("/{name}")
async def update_delegatee(name: str, body: DelegateeUpdate) -> Dict[str, Any]:
    """更新某个子 agent 的配置（模型预设 / 插件 / 工具白名单 / 启用开关）。"""
    if name not in DELEGATEE_TYPES:
        raise HTTPException(status_code=404, detail=f"未知的子 agent：{name}")
    from keeper.llm.client import get_llm

    async with get_session_factory()() as db:
        if body.llm_profile_id:
            prof = await db.get(LLMProfile, body.llm_profile_id)
            if prof is None:
                raise HTTPException(status_code=404, detail="模型预设不存在")
            # 提前校验：配置能不能真造出 LLM（避免存了个造不出的预设，跑时才崩）
            try:
                built = get_llm(prof.to_llm_dict())
            except Exception:  # noqa: BLE001
                built = None
            if built is None:
                raise HTTPException(
                    status_code=400, detail="该模型预设无法构建（缺少 provider 或构建失败）"
                )

        cfg = await db.get(DelegateeConfig, name)
        if cfg is None:
            cfg = DelegateeConfig(delegatee=name)
            db.add(cfg)
        cfg.llm_profile_id = body.llm_profile_id
        cfg.plugins = _dump_list(body.plugins)
        cfg.tools = _dump_list(body.tools)
        cfg.enabled = body.enabled
        await db.commit()
        await db.refresh(cfg)

    return {
        "ok": True,
        "name": name,
        "llm_profile_id": cfg.llm_profile_id,
        "enabled": cfg.enabled,
    }
