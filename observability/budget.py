"""额度与聚合：预算上限检查、按维度聚合用量与成本。

本文件是 keeper.observability 包的一部分，从原来的单体
keeper/observability.py（1856 行）拆分而来。设计见
doc/observability-design.md。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import case, func, select

from ..store import ChatSession, LLMCall, get_session_factory
from ._guard import raise_if_bug
from .pricing import _context_limit_for
from .stats import _session_subtree_ids

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
        raise_if_bug(e, "预算检查")
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

    成本直接 **SUM(llm_calls.cost)** —— 金额在每次调用返回时就按当时的单价
    算好落库了（见 ``observability/recording.py``），这里只负责加总。
    不再"展示时按当前价目表现算"：那样一来，删掉模型预设（连带删价段）就会让
    历史金额凭空消失，改价段也会让同一段历史悄悄变成另一个数。
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
        # 主 / 子 agent 明细：按会话拆分（子会话形如 "delegatee:code"），详见 session_breakdown
        "session_breakdown": [],
    }
    try:
        factory = get_session_factory()
        meta: Dict[str, Any] = {}
        async with factory() as db:
            # session_id 级查询 = 主会话 + 全部子会话（子 agent）的子树：
            # 这样「本会话总成本 / 总耗时」会把所有 delegatee 一并算进来。
            sids: Optional[List[str]] = None
            if session_id:
                sids = await _session_subtree_ids(db, session_id)

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
                # 成本：调用返回时就按当时单价算好落库，这里只加总
                func.sum(LLMCall.cost),
                func.max(LLMCall.currency),
            ).group_by(LLMCall.model, LLMCall.provider, LLMCall.profile_id)

            if step_id:
                stmt = stmt.where(LLMCall.step_id == step_id)
            if message_id:
                stmt = stmt.where(LLMCall.message_id == message_id)
            if sids is not None:
                stmt = stmt.where(LLMCall.session_id.in_(sids))
            if task_id:
                stmt = stmt.where(LLMCall.task_id == task_id)
            if agent_id:
                stmt = stmt.where(LLMCall.agent_id == agent_id)
            if since:
                stmt = stmt.where(LLMCall.created_at >= since)
            if until:
                stmt = stmt.where(LLMCall.created_at <= until)

            rows = (await db.execute(stmt)).all()

            # 主 / 子 agent 明细：按「会话」拆（主会话 = 主 agent，子会话 = 子 agent）。
            # 不按 agent_id 拆——delegatee 跑在主 agent 的上下文里，其 LLM 调用的
            # agent_id 仍是主 agent，只有 session_id 才是区分主/子的真实维度。子会话的
            # parent_id 非 NULL、且 agent_id 形如 "delegatee:code"，据此判定并生成标签。
            sess_acc: Dict[str, Dict[str, Any]] = {}
            if sids is not None:
                meta = {
                    r[0]: r
                    for r in (
                        await db.execute(
                            select(
                                ChatSession.id,
                                ChatSession.title,
                                ChatSession.agent_id,
                                ChatSession.parent_id,
                            ).where(ChatSession.id.in_(sids))
                        )
                    ).all()
                }
                sess_rows = (
                    await db.execute(
                        select(
                            LLMCall.session_id,
                            LLMCall.model,
                            LLMCall.provider,
                            LLMCall.profile_id,
                            func.sum(LLMCall.prompt_tokens),
                            func.sum(LLMCall.completion_tokens),
                            func.sum(LLMCall.cached_tokens),
                            func.sum(LLMCall.duration_ms),
                            func.count(LLMCall.id),
                            func.count(LLMCall.cached_tokens),
                            func.sum(case((LLMCall.ok.is_(False), 1), else_=0)),
                            func.sum(LLMCall.cost),
                            func.max(LLMCall.currency),
                        )
                        .where(LLMCall.session_id.in_(sids))
                        .group_by(
                            LLMCall.session_id,
                            LLMCall.model,
                            LLMCall.provider,
                            LLMCall.profile_id,
                        )
                    )
                ).all()
                for (
                    sid,
                    model,
                    provider,
                    profile_id,
                    s_prompt,
                    s_completion,
                    s_cached,
                    s_dur,
                    n_calls,
                    n_cached_rows,
                    n_errors,
                    s_cost,
                    s_currency,
                ) in sess_rows:
                    a = sess_acc.setdefault(
                        sid,
                        {
                            "calls": 0,
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "cached_tokens": 0,
                            "duration_ms": 0,
                            "cost": 0.0,
                            "currency": None,
                            "errors": 0,
                        },
                    )
                    prompt = int(s_prompt or 0)
                    completion = int(s_completion or 0)
                    cached = int(s_cached or 0)
                    a["calls"] += int(n_calls or 0)
                    a["prompt_tokens"] += prompt
                    a["completion_tokens"] += completion
                    a["cached_tokens"] += cached
                    a["duration_ms"] += int(s_dur or 0)
                    a["errors"] += int(n_errors or 0)
                    # 成本已落库，直接加总（NULL = 当时没配价，不计入）
                    c = s_cost
                    if c is not None:
                        a["cost"] += float(c)
                        a["currency"] = s_currency or a["currency"]

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
            s_cost,
            s_currency,
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

            # 成本已落库，直接加总（NULL = 当时没配价，不计入）
            c = s_cost
            if c is not None:
                total_cost += float(c)
                priced = True
                currency = s_currency or currency

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

        # 主 / 子 agent 明细（按会话拆）：主会话标「主 agent」，子会话标「子 agent：xxx」
        breakdown = []
        for sid, a in sess_acc.items():
            mrow = meta.get(sid)
            is_del = bool(
                mrow and (mrow[3] or (mrow[2] or "").startswith("delegatee:"))
            )
            if is_del:
                del_name = (
                    (mrow[2] or "")[len("delegatee:") :]
                    if (mrow[2] or "").startswith("delegatee:")
                    else (mrow[2] or sid)
                )
                label = mrow[1] or f"子 agent：{del_name}"
            else:
                label = (mrow[1] if mrow else None) or "主 agent"
            breakdown.append(
                {
                    "session_id": sid,
                    "label": label,
                    "is_delegatee": is_del,
                    "calls": a["calls"],
                    "prompt_tokens": a["prompt_tokens"],
                    "completion_tokens": a["completion_tokens"],
                    "cached_tokens": a["cached_tokens"],
                    "duration_ms": a["duration_ms"],
                    "cost": round(a["cost"], 6) if a["cost"] else None,
                    "cost_currency": a["currency"],
                    "errors": a["errors"],
                }
            )
        breakdown.sort(key=lambda x: -(x["cost"] or 0))
        out["session_breakdown"] = breakdown
    except Exception as e:
        raise_if_bug(e, "聚合用量")
        logger.debug("聚合用量失败: %s", e)
    return out
