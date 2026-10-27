"""模型管理：可复用的模型预设 CRUD（``/manage/models``）。

模型归属客户端本地（config.yaml 注释明确「用哪个模型平台不维护」），
因此模型预设在 keeper 本地维护，agent 通过 ``agent_llm_binding`` 引用其中一项。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from . import build_model
from .config import LLMConfig
from datetime import datetime, timezone

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
    async with get_session_factory()() as session:
        ids = (
            await session.execute(
                select(AgentLLMBinding.agent_id).where(
                    AgentLLMBinding.llm_profile_id == model_id
                )
            )
        ).scalars().all()
    return await _rebuild_agents(ids)


async def _rebuild_agents(agent_ids: Sequence[str]) -> List[Dict[str, Any]]:
    """按 agent_id 列表重装配（不依赖绑定表）。

    删除模型预设时用到：绑定已经先被删掉了，"谁绑着它"只能靠删除前记下的
    id 列表——否则这些 agent 的内存里还留着**已经删掉的配置**在跑，界面却显示
    未绑定。
    """
    from ..agent.api import _rebuild_if_enabled

    ids = [a for a in agent_ids if a]
    if not ids:
        return []

    async with get_session_factory()() as session:
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
        logger.info("模型预设变更，共重装配 %s 个 agent", ", ".join(names.get(a, a) for a in ids))
    return done


def _to_out(
    r: LLMProfile,
    reloaded_agents: Optional[List[Dict[str, Any]]] = None,
) -> LLMProfileOut:
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


@models_router.get("/preset-prices")
async def list_preset_prices(
    provider: str, model_name: str = ""
) -> Dict[str, Any]:
    """查某个模型的**出厂预设价格**（模型管理页的「单价」弹窗用）。

    匹配规则：预设包里 ``provider`` 相同、且 ``model_name`` 相同的那一条。
    匹配上了就把它配好的价格段（含时段 / 星期错峰）原样给出去，用户点一下
    就能加进自己的价段列表——不用照着官网再抄一遍数字。

    匹配不上就说清楚"没匹配上"，并列出该服务商下有哪些模型有预设价格，
    免得用户对着空白面板猜是不是坏了。
    """
    from .presets import load_presets

    presets = load_presets()
    same_provider = [p for p in presets if p.provider == provider]
    hit = next(
        (p for p in same_provider if p.model_name == model_name),
        None,
    )
    others = [
        {"model_name": p.model_name, "label": p.label, "has_price": p.has_price}
        for p in same_provider
        if p is not hit
    ]
    if hit is None:
        return {
            "matched": False,
            "model_name": model_name,
            "prices": [],
            "others": others,
        }
    return {
        "matched": True,
        "model_name": hit.model_name,
        "prices": [
            {
                "input_price_per_1m": s.input_per_1m,
                "cached_price_per_1m": s.cached_per_1m,
                "output_price_per_1m": s.output_per_1m,
                "currency": s.currency,
                "weekdays": s.weekdays,
                "time_from_minute": s.time_from_minute,
                "time_to_minute": s.time_to_minute,
                "note": s.note,
            }
            for s in hit.prices
        ],
        "others": others,
    }


@models_router.get("/providers")
async def list_providers() -> List[Dict[str, Any]]:
    """列出预设包里的**服务商**及其支持的模型（新建模型向导用）。

    一次返回两级选择所需的全部信息：服务商 → 它支持的模型（模型名、上下文、
    价格段、默认参数）。前端不用再逐个请求。
    """
    from .presets import load_providers

    out: List[Dict[str, Any]] = []
    for p in load_providers():
        out.append(
            {
                "provider": p.provider,
                "label": p.label or p.provider,
                "base_url": p.base_url,
                "docs_url": p.docs_url,
                "models": [
                    {
                        "model_name": m.model_name,
                        "name": m.name,
                        "label": m.label,
                        "context_limit": m.context_limit,
                        "temperature": m.temperature,
                        "timeout": m.timeout,
                        "max_retries": m.max_retries,
                        "prices": [
                            {
                                "input_price_per_1m": s.input_per_1m,
                                "cached_price_per_1m": s.cached_per_1m,
                                "output_price_per_1m": s.output_per_1m,
                                "currency": s.currency,
                                "weekdays": s.weekdays,
                                "time_from_minute": s.time_from_minute,
                                "time_to_minute": s.time_to_minute,
                                "note": s.note,
                            }
                            for s in m.prices
                        ],
                    }
                    for m in p.models
                ],
            }
        )
    return out


class PresetPriceSpec(BaseModel):
    """一段价格（全天兜底，或某个星期/时段的错峰价）。

    字段名与 ``model_prices`` 表、前端的 ``ModelPrice`` 保持一致，避免两套叫法。
    """

    input_price_per_1m: Optional[float] = None
    cached_price_per_1m: Optional[float] = None
    output_price_per_1m: Optional[float] = None
    currency: str = "CNY"
    #: 适用星期：逗号分隔的 ISO 星期号（1=周一 … 7=周日）；空 = 每天
    weekdays: Optional[str] = None
    #: 时段（本地时间口径，一天内分钟数 0–1439）；与 time_to 都为空 = 全天
    time_from_minute: Optional[int] = None
    time_to_minute: Optional[int] = None
    note: Optional[str] = None
    #: 这段价是出厂预设带来的（preset）还是用户自己写的（user）
    source: Optional[str] = None


class PresetModelSpec(BaseModel):
    """一个模型的**完整配置**。

    预设只是帮用户快速填信息（默认值 + 候选项），真正落到库里的一切**以这里
    为准**——用户在向导里改过的每一项都原样写进去。
    """

    name: str
    provider: str
    model_name: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    context_limit: Optional[int] = None
    temperature: float = 0.2
    timeout: int = 120
    max_retries: int = 3
    prices: List[PresetPriceSpec] = Field(default_factory=list)


class PresetModelsCreateIn(BaseModel):
    """向导的最终提交：一个服务商 + 它下面每个模型的完整配置。"""

    provider: str
    #: 服务商级默认值（模型没单独填时的兜底）
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    models: List[PresetModelSpec] = Field(default_factory=list)


class PresetModelsCreateOut(BaseModel):
    """批量创建的结果：建了哪些、跳过了哪些、哪些撞名了。"""

    created: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []


@models_router.post("/from-preset", response_model=PresetModelsCreateOut)
async def create_models_from_preset(body: PresetModelsCreateIn) -> PresetModelsCreateOut:
    """按服务商批量创建模型预设（每个选中的模型建一行 + 写入它的价格段）。

    为什么走"批量"而不是一条条建：这个流程的语义是"我买了这个服务商的账号，
    把它支持的模型都配上"——同一个 Key 一次填好，勾几个模型就建几个条目，
    不用对每个模型重复填一遍地址、上下文、价格。
    """
    # 落库的值**一律取入参**：预设只是前端帮忙填表，服务端不记账、不合并。
    if not body.models:
        raise HTTPException(status_code=400, detail="没有选中任何模型")

    created: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []

    now = datetime.now(timezone.utc)
    async with get_session_factory()() as session:
        rows = (await session.execute(select(LLMProfile))).scalars().all()
        taken = {r.name for r in rows}
        for spec in body.models:
            name = (spec.name or spec.model_name).strip()
            if not name:
                continue
            if name in taken:
                conflicts.append(
                    {
                        "name": name,
                        "reason": f"名称已被占用：{name}",
                    }
                )
                continue
            row = LLMProfile(
                id=new_id(),
                name=name,
                provider=spec.provider,
                model_name=spec.model_name,
                base_url=spec.base_url or None,
                api_key=(spec.api_key or body.api_key or None),
                context_limit=spec.context_limit,
                temperature=spec.temperature,
                timeout=spec.timeout,
                max_retries=spec.max_retries,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            await session.flush()
            taken.add(name)
            created.append(
                {
                    "id": row.id,
                    "name": name,
                    "model_name": spec.model_name,
                    "provider": spec.provider,
                    "has_api_key": bool((row.api_key or "").strip()),
                }
            )
            # 价格段：按入参写（可能是多段错峰价，也可能是用户自己改的一段）
            for seg in spec.prices:
                if (
                    seg.input_price_per_1m is None
                    and seg.output_price_per_1m is None
                ):
                    continue
                session.add(
                    ModelPrice(
                        id=new_id(),
                        profile_id=row.id,
                        model_name=spec.model_name,
                        provider=spec.provider,
                        effective_from=now,
                        input_price_per_1m=float(seg.input_price_per_1m or 0.0),
                        cached_price_per_1m=float(seg.cached_price_per_1m or 0.0),
                        output_price_per_1m=float(seg.output_price_per_1m or 0.0),
                        currency=seg.currency or "CNY",
                        note=seg.note,
                        weekdays=seg.weekdays,
                        time_from_minute=seg.time_from_minute,
                        time_to_minute=seg.time_to_minute,
                        created_at=now,
                    )
                )
        await session.commit()

    logger.info(
        "按向导创建模型预设：%s，建了 %d 个，跳过 %d 个（撞名）",
        body.provider,
        len(created),
        len(conflicts),
    )
    return PresetModelsCreateOut(created=created, conflicts=conflicts)


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


@models_router.delete("/{model_id}")
async def delete_model(model_id: str) -> Dict[str, Any]:
    """删除模型预设，并**连带删除与 agent 的绑定**。

    绑定必须一起删：模型没了，绑定还留着就是"绑了个不存在的东西"——界面显示
    未绑定，agent 却还在用内存里那份已经删掉的配置跑（LLM 实例是 build 期固化
    的）。留着这种悬空绑定，界面与实际永远对不上。

    所以这里先把绑定行删掉，再重装配这些 agent，让它们立刻落到无模型的纯
    索引模式；返回值里带上解绑了几个，便于如实告诉用户。

    价段也一起删：价段按 profile_id 关联，profile 没了这段价就成了悬空的
    "孤儿"——而取价会退化到按 model_name+provider 匹配，把它捡走，于是新配
    的价算出来是旧价。删预设就该连价一起删干净。
    """
    async with get_session_factory()() as session:
        row = await session.get(LLMProfile, model_id)
        if not row:
            raise HTTPException(status_code=404, detail="模型不存在")
        bound = (
            await session.execute(
                select(AgentLLMBinding.agent_id).where(
                    AgentLLMBinding.llm_profile_id == model_id
                )
            )
        ).scalars().all()
        bound = list(bound)
        if bound:
            await session.execute(
                delete(AgentLLMBinding).where(
                    AgentLLMBinding.llm_profile_id == model_id
                )
            )
        await session.execute(
            delete(ModelPrice).where(ModelPrice.profile_id == model_id)
        )
        await session.delete(row)
        await session.commit()
    # 绑定已删，"谁绑着它"只能靠上面记下的 id 列表
    reloaded = await _rebuild_agents(bound)
    logger.info(
        "模型预设 %s 已删除（解绑 %s 个 agent），重装配 %s 个 agent",
        model_id,
        len(bound),
        len(reloaded),
    )
    return {
        "id": model_id,
        "removed": True,
        "unbound_agents": len(bound),
        "reloaded_agents": reloaded,
    }


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
