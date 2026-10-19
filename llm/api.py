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

from . import build_model
from .config import LLMConfig
from datetime import datetime

from ..store import (
    Agent,
    AgentLLMBinding,
    LLMProfile,
    ModelPrice,
    get_session_factory,
)
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
    """**只出不进**：不含 api_key 明文，只回一个「配没配」的标志。

    为什么
    ----
    API Key 是**只写**字段：能设、不能看。原因很实际——

    1. 这个接口返回体直接进浏览器（表格 + 编辑表单），明文一旦返回，就等于
       每个能看到这页的人都能读到密钥（截图、日志、DevTools、前端报错里都可能
       留下痕迹），而"模型管理"页通常谁都能点开；
    2. 看密钥对使用者没有价值——真要确认，页面上给个「已配置」就够了；
    3. 它只需要单向写入，一次都没有读取的必要。

    与 ``LLMProfileIn`` 分开就是这个道理：入参收明文，出参永不回。
    """

    id: str
    name: str
    provider: str
    model_name: str
    #: 是否已配置 API Key（bool，不含任何明文片段）
    has_api_key: bool = False
    base_url: Optional[str] = None
    temperature: float
    timeout: int
    max_retries: int
    context_limit: Optional[int] = None
    #: 保存后被重装配的 agent（如实回报：谁重载了、谁没成功）。空表示没有 agent 绑它
    reloaded_agents: List[Dict[str, Any]] = []


async def _rebuild_agents_using(model_id: str) -> List[Dict[str, Any]]:
    """重装配所有绑定了该预设的 agent。

    为什么模型预设改了必须重载
    ----------------------
    LLM 实例是在 agent **build 期**按当时的配置创建出来的（``build_agent`` →
    ``get_llm`` → provider 客户端），它把 api_key / base_url / model_name /
    temperature 都固化进了那个对象。改预设**不会**影响已经建好的实例——所以以前
    改完必须重启服务才生效，而重启会连带中断所有正在跑的会话。

    这里在改配置后立刻重装配对应 agent：``shutdown`` 旧实例 → 重新 build → 重新注册。
    逐个做、互不影响；某个 agent 起不来（缺插件、配置有问题）只记它自己的失败，
    不影响其他 agent 继续重载（返回列表里如实报告成功与失败）。

    已停用的 agent（本地停用 / 能力被关）跳过——它本来就不在跑，重载它没意义，
    还要重新检查一遍停用状态。``_rebuild_if_enabled`` 已经处理了这种情况。
    """
    from ..agent.api import _rebuild_if_enabled

    async with get_session_factory()() as session:
        ids = (
            await session.execute(
                select(AgentLLMBinding.agent_id).where(
                    AgentLLMBinding.llm_profile_id == model_id
                )
            )
        ).scalars().all()
        names = (
            {
                r[0]: r[1]
                for r in (
                    await session.execute(
                        select(Agent.id, Agent.name).where(Agent.id.in_(ids))
                    )
                ).all()
            }
            if ids
            else {}
        )

    done: List[Dict[str, Any]] = []
    for aid in ids:
        ok, reason = await _rebuild_if_enabled(aid)
        done.append(
            {"id": aid, "name": names.get(aid, aid), "ok": ok, "reason": reason}
        )
        logger.info("模型预设变更，重装配 agent %s：%s", names.get(aid, aid), "成功" if ok else reason)
    if done:
        logger.info("模型预设 %s 变更，共重装配 %s 个 agent", model_id, len(done))
    return done


def _to_out(r: LLMProfile, reloaded_agents: Optional[List[Dict[str, Any]]] = None) -> LLMProfileOut:
    return LLMProfileOut(
        id=r.id,
        name=r.name,
        provider=r.provider,
        model_name=r.model_name,
        # 只回标志位：见 LLMProfileOut 的说明
        has_api_key=bool((r.api_key or "").strip()),
        base_url=r.base_url,
        temperature=r.temperature,
        timeout=r.timeout,
        max_retries=r.max_retries,
        context_limit=r.context_limit,
        reloaded_agents=reloaded_agents or [],
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
    """更新模型预设。**api_key 留空 = 保持原值不变**。

    为什么是这个语义
    --------------
    Key 改成只写之后，编辑页拿不到明文，输入框就是空的。如果按常规的
    「字段为空就写空」处理，用户只想改个模型名，保存后 Key 就被清空了——
    而他**根本看不见**自己清掉了什么，下一次调用直接 401，还查不出原因。

    所以这里显式区分：空 = 不动，要换 Key 就往框里输新值。
    （代价是不能通过这个接口清除 Key，那是刻意的：要清除得先知道它存在。）
    """
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
        data = body.model_dump()
        # 只写字段：空字符串 / 空白 都视为「不改」，别把它覆盖成空
        if not (data.get("api_key") or "").strip():
            data.pop("api_key", None)
        for key, value in data.items():
            setattr(row, key, value)
        await session.commit()
        await session.refresh(row)

    # 改完立刻重装配绑着它的 agent：LLM 实例是 build 期固化的，不重载就得重启
    # 整个服务才生效（而重启会中断所有正在跑的会话）。
    reloaded = await _rebuild_agents_using(model_id)
    return _to_out(row, reloaded_agents=reloaded)


@models_router.delete("/{model_id}", status_code=204)
async def delete_model(model_id: str) -> Response:
    """删除模型预设。

    也会重装配绑着它的 agent：它们的绑定会变成「预设失效」，放任不管的话这些
    agent 会继续用**已经删掉的配置**跑（内存里的实例还活着），界面却显示未绑定——
    又是那种"界面与实际不一致"。删完就重载，让它们立刻落到无模型的纯索引模式，
    并按未绑定提示用户去重新配一个。

    接口仍是 204（无 body），重载结果只进日志。
    """
    async with get_session_factory()() as session:
        row = await session.get(LLMProfile, model_id)
        if not row:
            raise HTTPException(status_code=404, detail="模型不存在")
        await session.delete(row)
        await session.commit()
    reloaded = await _rebuild_agents_using(model_id)
    logger.info("模型预设 %s 已删除，重装配 %s 个 agent", model_id, len(reloaded))
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
