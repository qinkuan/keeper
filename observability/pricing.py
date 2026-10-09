"""定价：按 (profile, model, provider) 匹配价格，支持时段 / 工作日折扣。

本文件是 keeper.observability 包的一部分，从原来的单体
keeper/observability.py（1856 行）拆分而来。设计见
doc/observability-design.md。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy import and_, or_, select

from ..store import LLMProfile, ModelPrice, get_session_factory
from ._guard import raise_if_bug

logger = logging.getLogger(__name__)


def _minute_of_day(when: datetime) -> int:
    """把调用时间换算成**本地时间**的一天分钟数（0–1439），用于错峰时段匹配。

    created_at 存的是 UTC，SQLite 读出来多为 naive，这里统一按 UTC 解释再转本地。
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    local = when.astimezone()
    return local.hour * 60 + local.minute


def _minute_in_range(minute: int, start: Optional[int], end: Optional[int]) -> bool:
    """minute 是否落在时段内；start/end 任一为 None 视为「不限时段」，不匹配。"""
    if start is None or end is None:
        return False
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end  # 跨午夜，如 22:00–02:00


def _weekday_of(when: datetime) -> int:
    """调用时间的**本地**星期号（ISO：1=周一 … 7=周日）。"""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone().isoweekday()


def _weekdays_match(weekday: int, spec: Optional[str]) -> bool:
    """spec 为空表示不限星期；否则 weekday 必须在列表里（"1,2,3,4,5"）。"""
    if not spec:
        return True
    try:
        allowed = {int(x) for x in spec.split(",") if x.strip()}
    except ValueError:
        return True  # 脏数据按「不限」处理，别因此算不出成本
    return not allowed or weekday in allowed


async def _price_for(
    profile_id: Optional[str],
    model: Optional[str],
    provider: Optional[str],
    at: Optional[datetime] = None,
) -> Optional[Tuple[float, float, float, str]]:
    """取**调用发生时**生效的单价 ``(input, cached, output, currency)``；查不到 None。

    ``at`` 是该次调用的时间（聚合时取该组最早的调用时间）；不传则按当前时间。
    匹配优先级：profile_id > model_name+provider > 仅 model_name。
    agent 用内联 llm 配置时 profile_id 为空，会自然退到后两级。

    错峰计费：星期与时段两个维度都要命中才可用（不限的维度视为通过）；可用多段
    时取**最具体**的一段——时段粒度更细优先于星期，同具体度取生效时间最新的一段。
    例如「每天 00:00–08:00 低峰价」会压过「工作日（不限时段）价」。
    """
    if not model and not profile_id:
        return None
    # 关键：按**调用发生的时间**取价，而不是"当前价"——模型会调价，
    # 历史调用必须按当时的单价计算，否则回溯出来的成本全是错的。
    when = at or datetime.now(timezone.utc)
    try:
        factory = get_session_factory()
        async with factory() as db:
            # 三级候选，从精确到宽泛依次尝试
            cands = []
            if profile_id:
                cands.append(ModelPrice.profile_id == profile_id)
            if model and provider:
                cands.append(
                    and_(
                        ModelPrice.model_name == model,
                        ModelPrice.provider == provider,
                    )
                )
            if model:
                cands.append(ModelPrice.model_name == model)

            minute = _minute_of_day(when)
            for cond in cands:
                stmt = (
                    select(ModelPrice)
                    .where(
                        cond,
                        ModelPrice.effective_from <= when,
                        or_(
                            ModelPrice.effective_to.is_(None),
                            ModelPrice.effective_to > when,
                        ),
                    )
                    .order_by(ModelPrice.effective_from.desc())
                )
                rows = (await db.execute(stmt)).scalars().all()
                if not rows:
                    continue
                # ① 过滤：限定的维度必须命中（不限的维度视为通过）
                weekday = _weekday_of(when)
                usable = [
                    r
                    for r in rows
                    if _weekdays_match(weekday, r.weekdays)
                    and (
                        r.time_from_minute is None
                        or _minute_in_range(
                            minute, r.time_from_minute, r.time_to_minute
                        )
                    )
                ]
                if not usable:
                    continue
                # ② 取最具体的一段：时段粒度更细（+2）优先于星期（+1）；
                #    同具体度取生效时间最新的一段。
                row = max(
                    usable,
                    key=lambda r: (
                        (2 if r.time_from_minute is not None else 0)
                        + (1 if r.weekdays else 0),
                        # effective_from 理论非空，但脏数据为 None 时会让元组
                        # 比较抛 TypeError——兜个底，别因此算不出成本
                        r.effective_from or datetime.min,
                    ),
                )
                if row is not None:
                    return (
                        float(row.input_price_per_1m),
                        float(row.cached_price_per_1m),
                        float(row.output_price_per_1m),
                        row.currency or "CNY",
                    )
        return None
    except Exception as e:
        raise_if_bug(e, "查询模型单价")
        logger.debug("查询模型单价失败: %s", e)
        return None


async def _context_limit_for(
    profile_id: Optional[str], model: Optional[str], provider: Optional[str]
) -> Optional[int]:
    """取模型的上下文上限（token）；查不到返回 None（表示不告警）。

    匹配优先级与 ``_price_for`` 一致：profile_id > model_name+provider > model_name。
    """
    if not model and not profile_id:
        return None
    try:
        factory = get_session_factory()
        async with factory() as db:
            row: Optional[LLMProfile] = None
            if profile_id:
                row = await db.get(LLMProfile, profile_id)
                if row is not None and row.context_limit is None:
                    row = None  # 该预设没配上限定，继续按 model 找
            if row is None and model:
                stmt = select(LLMProfile).where(LLMProfile.model_name == model)
                if provider:
                    stmt = stmt.where(LLMProfile.provider == provider)
                row = (await db.execute(stmt)).scalars().first()
                if row is None and provider:
                    # 带 provider 没匹配到（写法不同等），退到只看 model
                    row = (
                        await db.execute(
                            select(LLMProfile).where(LLMProfile.model_name == model)
                        )
                    ).scalars().first()
            return row.context_limit if row else None
    except Exception as e:
        raise_if_bug(e, "查询模型上下文上限")
        logger.debug("查询模型上下文上限失败: %s", e)
        return None


def _cost_of(
    prompt: int, completion: int, cached: Optional[int], price
) -> Optional[float]:
    """按单价算成本。``price`` 为 None 时返回 None（展示为「—」，不瞎算）。

    DeepSeek 语义：``prompt_tokens = hit + miss``，命中部分**已包含**在 prompt
    内，所以未命中部分是 ``prompt - cached``，不能把 cached 再加一遍。
    缓存未上报时按全部未命中计价（保守）。
    """
    if price is None:
        return None
    input_p, cached_p, output_p, _cur = price
    hit = cached or 0
    miss = max(prompt - hit, 0)
    return (miss / 1e6) * input_p + (hit / 1e6) * cached_p + (completion / 1e6) * output_p


