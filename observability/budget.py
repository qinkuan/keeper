"""额度与聚合：预算上限检查、按维度聚合用量与成本。

本文件是 keeper.observability 包的一部分，从原来的单体
keeper/observability.py（1856 行）拆分而来。设计见
doc/observability-design.md。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import case, func, select

from ..store import LLMCall, get_session_factory
from .pricing import _context_limit_for, _cost_of, _price_for

logger = logging.getLogger(__name__)


def budget_limits() -> Tuple[int, int]:
    """会话 / 任务的 token 预算上限；**0 = 不限**。

    用环境变量配置（``KEEPER_SESSION_TOKEN_LIMIT`` / ``KEEPER_TASK_TOKEN_LIMIT``）
    而不是再开一张配置表——配额是运维策略，不该混进业务库，也不该让人在页面上
    误改了就立刻生效。
    """
    import os

    def _i(name: str) -> int:
        try:
            return int(os.getenv(name, "0") or 0)
        except (TypeError, ValueError):
            return 0

    return _i("KEEPER_SESSION_TOKEN_LIMIT"), _i("KEEPER_TASK_TOKEN_LIMIT")


async def check_budget(
    *, session_id: Optional[str] = None, task_id: Optional[str] = None
) -> Dict[str, Any]:
    """预算 / 配额检查：本会话（或任务）累计 token 是否超上限。

    - 上限为 0（未配置）→ 不限制；
    - 两个维度都给了就都查，结果取**用得最满**的那个；
    - ``rate >= 0.8`` 置 warning（预警），``>= 1.0`` 置 over（超限，应停止执行）。
    """
    s_limit, t_limit = budget_limits()
    out: Dict[str, Any] = {
        "over": False,
        "scope": None,
        "limit": 0,
        "used": 0,
        "rate": 0.0,
        "warning": False,
    }
    try:
        checks = []
        if session_id and s_limit:
            checks.append(("session", s_limit, await aggregate(session_id=session_id)))
        if task_id and t_limit:
            checks.append(("task", t_limit, await aggregate(task_id=task_id)))
        if not checks:
            return out
        for scope, limit, u in checks:
            used = int(u.get("total_tokens") or 0)
            rate = used / limit if limit else 0.0
            if rate >= (out["rate"] or 0):
                out.update(scope=scope, limit=limit, used=used, rate=round(rate, 4))
        out["warning"] = out["rate"] >= 0.8
        out["over"] = out["rate"] >= 1.0
    except Exception as e:
        logger.debug("预算检查失败: %s", e)
    return out


async def aggregate(
    *,
    step_id: Optional[str] = None,
    message_id: Optional[str] = None,
    session_id: Optional[str] = None,
    task_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> Dict[str, Any]:
    """按任一维度聚合用量与成本（至少给一个维度）。

    成本按 (model, provider, profile_id) 分组分别计价再求和——不同调用可能走
    不同模型，混在一起算会串价。
    """
    out: Dict[str, Any] = {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "cache_write_tokens": 0,
        "duration_ms": 0,
        "ttft_ms": None,
        "cached_reported": False,  # False = provider 未上报缓存，别把 0 当没命中
        "cost": None,
        "cost_currency": None,
        "max_prompt_tokens": 0,  # 单次最大输入：上下文水位用（累加无意义）
        "context_limit": None,
        "context_usage_rate": None,
        "context_warning": False,
        "errors": 0,
        "error_rate": 0.0,
    }
    try:
        stmt = select(
            LLMCall.model,
            LLMCall.provider,
            LLMCall.profile_id,
            func.sum(LLMCall.prompt_tokens),
            func.sum(LLMCall.completion_tokens),
            func.sum(LLMCall.cached_tokens),
            func.sum(LLMCall.cache_write_tokens),
            func.sum(LLMCall.reasoning_tokens),
            func.sum(LLMCall.duration_ms),
            func.count(LLMCall.id),
            func.count(LLMCall.cached_tokens),  # 只有非 NULL 才计数
            func.max(LLMCall.prompt_tokens),  # 单次最大输入：上下文水位用
            # 失败调用数（ok=False）：超时 / 报错都算，用于错误率
            func.sum(case((LLMCall.ok.is_(False), 1), else_=0)),
            # 该组最早的调用时间：用于匹配**当时**生效的单价（模型会调价）
            func.min(LLMCall.created_at),
        ).group_by(LLMCall.model, LLMCall.provider, LLMCall.profile_id)

        if step_id:
            stmt = stmt.where(LLMCall.step_id == step_id)
        if message_id:
            stmt = stmt.where(LLMCall.message_id == message_id)
        if session_id:
            stmt = stmt.where(LLMCall.session_id == session_id)
        if task_id:
            stmt = stmt.where(LLMCall.task_id == task_id)
        if agent_id:
            stmt = stmt.where(LLMCall.agent_id == agent_id)
        if since:
            stmt = stmt.where(LLMCall.created_at >= since)
        if until:
            stmt = stmt.where(LLMCall.created_at <= until)

        factory = get_session_factory()
        async with factory() as db:
            rows = (await db.execute(stmt)).all()

        total_cost = 0.0
        priced = False
        currency = None
        for (
            model,
            provider,
            profile_id,
            s_prompt,
            s_completion,
            s_cached,
            s_cwrite,
            s_reasoning,
            s_dur,
            n_calls,
            n_cached_rows,
            s_max_prompt,
            n_errors,
            s_first_at,
        ) in rows:
            # 上下文是「单次请求」的概念，累加没意义，取单次最大输入
            out["max_prompt_tokens"] = max(
                out["max_prompt_tokens"], int(s_max_prompt or 0)
            )
            out["errors"] += int(n_errors or 0)
            prompt = int(s_prompt or 0)
            completion = int(s_completion or 0)
            cached = int(s_cached or 0)
            out["calls"] += int(n_calls or 0)
            out["prompt_tokens"] += prompt
            out["completion_tokens"] += completion
            out["cached_tokens"] += cached
            out["cache_write_tokens"] += int(s_cwrite or 0)
            out["reasoning_tokens"] += int(s_reasoning or 0)
            out["duration_ms"] += int(s_dur or 0)
            if int(n_cached_rows or 0) > 0:
                out["cached_reported"] = True

            price = await _price_for(profile_id, model, provider, at=s_first_at)
            c = _cost_of(prompt, completion, cached if n_cached_rows else None, price)
            if c is not None:
                total_cost += c
                priced = True
                currency = price[3] if price else currency

            # 上下文水位按组算（不同模型上限不同），整体取**最危险**的那组
            lim = await _context_limit_for(profile_id, model, provider)
            if lim:
                rate = int(s_max_prompt or 0) / lim
                if rate > (out["context_usage_rate"] or 0):
                    out["context_usage_rate"] = round(rate, 4)
                    out["context_limit"] = lim

        out["total_tokens"] = out["prompt_tokens"] + out["completion_tokens"]
        out["error_rate"] = (
            round(out["errors"] / out["calls"], 4) if out["calls"] else 0.0
        )
        # 水位告警阈值 80%：ReAct 多轮很容易把上下文顶满，逼近时提前示警
        out["context_warning"] = (out["context_usage_rate"] or 0) >= 0.8
        if priced:
            out["cost"] = round(total_cost, 6)
            out["cost_currency"] = currency
        if out["prompt_tokens"]:
            out["cache_hit_rate"] = round(
                out["cached_tokens"] / out["prompt_tokens"], 4
            )
        else:
            out["cache_hit_rate"] = 0.0
    except Exception as e:
        logger.debug("聚合用量失败: %s", e)
    return out
