"""模型管理：可复用的模型预设 CRUD（``/manage/models``）。

模型归属客户端本地（config.yaml 注释明确「用哪个模型平台不维护」），
因此模型预设在 keeper 本地维护，agent 通过 ``agent_llm_binding`` 引用其中一项。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..llm import build_model
from ..llm.config import LLMConfig
from datetime import datetime

from ..store import LLMProfile, ModelPrice, get_session_factory
from ..store.models import new_id

logger = logging.getLogger(__name__)

models_router = APIRouter(prefix="/manage/models", tags=["model-manage"])


class LLMProfileIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    provider: str = Field(..., min_length=1, max_length=64)
    model_name: str = Field(..., min_length=1, max_length=128)
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: float = 0.2
    timeout: int = 120
    max_retries: int = 3
    # 上下文上限（token）：用于上下文水位告警，留空则不告警
    context_limit: Optional[int] = None


class LLMProfileOut(BaseModel):
    id: str
    name: str
    provider: str
    model_name: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: float
    timeout: int
    max_retries: int
    context_limit: Optional[int] = None


def _to_out(r: LLMProfile) -> LLMProfileOut:
    return LLMProfileOut(
        id=r.id,
        name=r.name,
        provider=r.provider,
        model_name=r.model_name,
        api_key=r.api_key,
        base_url=r.base_url,
        temperature=r.temperature,
        timeout=r.timeout,
        max_retries=r.max_retries,
        context_limit=r.context_limit,
    )


@models_router.get("", response_model=List[LLMProfileOut])
async def list_models() -> List[LLMProfileOut]:
    """列出全部模型预设（下拉选「用哪个模型」用）。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(select(LLMProfile).order_by(LLMProfile.name))
        ).scalars().all()
        return [_to_out(r) for r in rows]


@models_router.post("", response_model=LLMProfileOut, status_code=201)
async def create_model(body: LLMProfileIn) -> LLMProfileOut:
    async with get_session_factory()() as session:
        clash = (
            await session.execute(
                select(LLMProfile).where(LLMProfile.name == body.name)
            )
        ).scalar_one_or_none()
        if clash:
            raise HTTPException(status_code=409, detail=f"模型名称已存在：{body.name}")
        row = LLMProfile(id=new_id(), **body.model_dump())
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return _to_out(row)


@models_router.put("/{model_id}", response_model=LLMProfileOut)
async def update_model(model_id: str, body: LLMProfileIn) -> LLMProfileOut:
    async with get_session_factory()() as session:
        row = await session.get(LLMProfile, model_id)
        if not row:
            raise HTTPException(status_code=404, detail="模型不存在")
        if body.name != row.name:
            clash = (
                await session.execute(
                    select(LLMProfile).where(LLMProfile.name == body.name)
                )
            ).scalar_one_or_none()
            if clash:
                raise HTTPException(
                    status_code=409, detail=f"模型名称已存在：{body.name}"
                )
        for key, value in body.model_dump().items():
            setattr(row, key, value)
        await session.commit()
        await session.refresh(row)
        return _to_out(row)


@models_router.delete("/{model_id}", status_code=204)
async def delete_model(model_id: str) -> Response:
    async with get_session_factory()() as session:
        row = await session.get(LLMProfile, model_id)
        if not row:
            raise HTTPException(status_code=404, detail="模型不存在")
        await session.delete(row)
        await session.commit()
    return Response(status_code=204)


@models_router.post("/{model_id}/test")
async def test_model(model_id: str) -> Dict[str, Any]:
    """连通性测试：用该预设实调一次 LLM。"""
    async with get_session_factory()() as session:
        row = await session.get(LLMProfile, model_id)
        if not row:
            raise HTTPException(status_code=404, detail="模型不存在")
    try:
        # build_model 只收一个 LLMConfig（provider 已在 to_llm_dict 里）；
        # 之前多传了 row.provider，签名不匹配会直接 TypeError。
        llm = build_model(LLMConfig(**row.to_llm_dict()))
        resp = await llm.ainvoke("ping")
        return {"ok": True, "reply": str(resp)[:200]}
    except Exception as e:  # noqa: BLE001
        logger.warning("模型 %s 连通测试失败：%s", model_id, e)
        raise HTTPException(status_code=502, detail=f"连通测试失败：{e}")


# ---- 模型单价（**按生效时间段**）----
#
# 价格不挂在预设上、单独建表，是因为**模型会调价**：成本必须按调用发生时生效的
# 那一段价来算，否则历史调用会被"当前价"重算，回溯出来的成本全是错的。
# 三个维度对应 DeepSeek 的计费语义：未命中缓存的输入 / 命中缓存的输入 / 输出。
class ModelPriceIn(BaseModel):
    effective_from: datetime
    effective_to: Optional[datetime] = None  # 空 = 至今有效
    # 一天内的时段（本地时间 00:00 起的分钟数，0–1439）：错峰计费用。
    # 两者都为 None = 全天通用价；from > to 表示跨午夜（如 22:00–02:00）。
    time_from_minute: Optional[int] = None
    time_to_minute: Optional[int] = None
    # 适用星期：逗号分隔的 ISO 星期号（1=周一 … 7=周日），如 "1,2,3,4,5"；
    # 空 = 不限（每天）。与时段是 AND 关系。
    weekdays: Optional[str] = None
    input_price_per_1m: float
    cached_price_per_1m: float
    output_price_per_1m: float
    currency: str = "CNY"
    note: Optional[str] = None


class ModelPriceOut(BaseModel):
    id: str
    effective_from: datetime
    effective_to: Optional[datetime] = None
    time_from_minute: Optional[int] = None
    time_to_minute: Optional[int] = None
    weekdays: Optional[str] = None
    input_price_per_1m: float
    cached_price_per_1m: float
    output_price_per_1m: float
    currency: str
    note: Optional[str] = None


def _price_out(r: ModelPrice) -> ModelPriceOut:
    return ModelPriceOut(
        id=r.id,
        effective_from=r.effective_from,
        effective_to=r.effective_to,
        time_from_minute=r.time_from_minute,
        time_to_minute=r.time_to_minute,
        weekdays=r.weekdays,
        input_price_per_1m=r.input_price_per_1m,
        cached_price_per_1m=r.cached_price_per_1m,
        output_price_per_1m=r.output_price_per_1m,
        currency=r.currency,
        note=r.note,
    )


@models_router.get("/{model_id}/prices", response_model=List[ModelPriceOut])
async def list_prices(model_id: str) -> List[ModelPriceOut]:
    """列出该模型的全部价格段（按生效时间倒序，最上面是最新价）。"""
    async with get_session_factory()() as session:
        row = await session.get(LLMProfile, model_id)
        if not row:
            raise HTTPException(status_code=404, detail="模型不存在")
        rows = (
            await session.execute(
                select(ModelPrice)
                .where(ModelPrice.profile_id == model_id)
                .order_by(ModelPrice.effective_from.desc())
            )
        ).scalars().all()
        return [_price_out(r) for r in rows]


@models_router.post(
    "/{model_id}/prices", response_model=ModelPriceOut, status_code=201
)
async def create_price(model_id: str, body: ModelPriceIn) -> ModelPriceOut:
    """新增一段价格。改价时**新增一段**而不是改旧段，历史成本才不会变。"""
    async with get_session_factory()() as session:
        row = await session.get(LLMProfile, model_id)
        if not row:
            raise HTTPException(status_code=404, detail="模型不存在")
        if body.effective_to and body.effective_to <= body.effective_from:
            raise HTTPException(
                status_code=400, detail="effective_to 必须晚于 effective_from"
            )
        p = ModelPrice(
            id=new_id(),
            profile_id=model_id,
            model_name=row.model_name,
            provider=row.provider,
            **body.model_dump(),
        )
        session.add(p)
        await session.commit()
        await session.refresh(p)
        return _price_out(p)


@models_router.delete("/{model_id}/prices/{price_id}", status_code=204)
async def delete_price(model_id: str, price_id: str) -> Response:
    async with get_session_factory()() as session:
        p = await session.get(ModelPrice, price_id)
        if not p or p.profile_id != model_id:
            raise HTTPException(status_code=404, detail="价格不存在")
        await session.delete(p)
        await session.commit()
    return Response(status_code=204)
