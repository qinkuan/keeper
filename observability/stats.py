"""统计层：用量 / 成本 / 耗时的多维聚合，以及解析、能力、工具维度的统计。

本文件是 keeper.observability 包的一部分，从原来的单体
keeper/observability.py（1856 行）拆分而来。设计见
doc/observability-design.md。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import case, func, or_, select

from ..store import (
    CapabilityLoad,
    LLMCall,
    ReactParseStat,
    ToolCall,
    get_session_factory,
)
from ._context import _resolve_round_anchor
from ._guard import raise_if_bug, warn_bug_only
from .pricing import _cost_of, _price_for

logger = logging.getLogger(__name__)


async def react_parse_stats(agent_id: Optional[str] = None, days: int = 7) -> Dict[str, Any]:
    """「模型想并行」的量化：多 ACTION 出现率、动作被丢弃的总数。

    判读：``multi_rate`` 明显大于 0 → 并行化有真实收益；``dropped_total`` 很大
    → 现在就在白丢信息；``dropped_ratio`` 高但 ``multi_rate`` 低 → 收益集中在
    少数几步（批量查符号之类），针对性地加聚合工具更划算。
    """
    out: Dict[str, Any] = {
        "window_days": days, "total": 0, "multi": 0, "multi_rate": 0.0,
        "dropped_total": 0, "dropped_ratio": 0.0, "by_kind": {},
    }
    try:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        factory = get_session_factory()
        async with factory() as db:
            q = select(ReactParseStat).where(ReactParseStat.created_at >= since)
            if agent_id:
                q = q.where(ReactParseStat.agent_id == agent_id)
            rows = (await db.execute(q)).scalars().all()
        by_kind: Dict[str, int] = {}
        total_actions = 0
        for r in rows:
            out["total"] += 1
            by_kind[r.kind] = by_kind.get(r.kind, 0) + 1
            total_actions += r.action_count or 0
            if r.action_count > 1:
                out["multi"] += 1
            out["dropped_total"] += r.dropped_count or 0
        out["by_kind"] = by_kind
        if out["total"]:
            out["multi_rate"] = round(out["multi"] / out["total"], 4)
        if total_actions:
            out["dropped_ratio"] = round(out["dropped_total"] / total_actions, 4)
    except Exception as e:  # noqa: BLE001
        raise_if_bug(e, "聚合解析统计")
        logger.debug("聚合解析统计失败: %s", e)
    return out


async def capability_stats(
    agent_id: Optional[str] = None, days: int = 7, top: int = 12
) -> Dict[str, Any]:
    """能力加载的聚合视图：按 key 统计次数与来源，外加**抖动**指标。

    抖动 = 同一轮（``message_id``）里同一个 key 被加载两次以上：说明模型自己也不
    确定读过没有，几乎总是 L1 摘要或提示不够清楚造成的。
    """
    out: Dict[str, Any] = {
        "total": 0,
        "window_days": days,
        "by_key": [],
        "by_source": {},
        "redundant_loads": 0,
        "top": top,
    }
    try:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        factory = get_session_factory()
        async with factory() as db:
            q = select(CapabilityLoad).where(CapabilityLoad.created_at >= since)
            if agent_id:
                q = q.where(CapabilityLoad.agent_id == agent_id)
            rows = (await db.execute(q)).scalars().all()

        agg: Dict[str, Dict[str, Any]] = {}
        per_round: Dict[Tuple[str, str], int] = {}
        for r in rows:
            out["total"] += 1
            out["by_source"][r.source] = out["by_source"].get(r.source, 0) + 1
            a = agg.setdefault(
                r.key,
                {
                    "key": r.key,
                    "kind": r.kind,
                    "loads": 0,
                    "preload": 0,
                    "model_load": 0,
                    "auto_disclose": 0,
                    "chars": 0,
                },
            )
            a["loads"] += 1
            a[r.source] = a.get(r.source, 0) + 1
            a["chars"] += r.chars or 0
            if r.message_id:
                per_round[(r.message_id, r.key)] = per_round.get((r.message_id, r.key), 0) + 1
        for a in agg.values():
            a["avg_chars"] = int(a["chars"] / max(1, a["loads"]))
        out["by_key"] = sorted(agg.values(), key=lambda x: -x["loads"])[:top]
        out["redundant_loads"] = sum(1 for n in per_round.values() if n > 1)
    except Exception as e:  # noqa: BLE001
        raise_if_bug(e, "聚合能力加载统计")
        logger.debug("聚合能力加载统计失败: %s", e)
    return out



def _blank_usage() -> Dict[str, Any]:
    return {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "cached_reported": False,
        "cache_hit_rate": 0.0,
        "duration_ms": 0,
        "cost": None,
        "cost_currency": None,
    }


async def usage_by_messages(message_ids: List[str]) -> Dict[str, Any]:
    """**批量**按 message_id 聚合用量（历史消息列表用，避免逐条查询）。

    与 ``aggregate`` 口径一致：成本按 (model, provider, profile_id) 分组分别计价
    再累加——不同轮可能走不同模型，混在一起算会串价。
    """
    out: Dict[str, Any] = {}
    if not message_ids:
        return out
    try:
        stmt = (
            select(
                LLMCall.message_id,
                LLMCall.model,
                LLMCall.provider,
                LLMCall.profile_id,
                func.sum(LLMCall.prompt_tokens),
                func.sum(LLMCall.completion_tokens),
                func.sum(LLMCall.cached_tokens),
                func.sum(LLMCall.reasoning_tokens),
                func.sum(LLMCall.duration_ms),
                func.count(LLMCall.id),
                func.count(LLMCall.cached_tokens),
                func.min(LLMCall.created_at),  # 该轮时间：匹配当时的单价
            )
            .where(LLMCall.message_id.in_(message_ids))
            .group_by(
                LLMCall.message_id,
                LLMCall.model,
                LLMCall.provider,
                LLMCall.profile_id,
            )
        )
        factory = get_session_factory()
        async with factory() as db:
            rows = (await db.execute(stmt)).all()
    except Exception as e:
        logger.debug("批量聚合消息用量失败: %s", e)
        return out

    for (
        mid,
        model,
        provider,
        pid,
        p,
        c,
        cached,
        reasoning,
        dur,
        n,
        n_cached,
        first_at,
    ) in rows:
        acc = out.setdefault(mid, _blank_usage())
        prompt = int(p or 0)
        completion = int(c or 0)
        acc["calls"] += int(n or 0)
        acc["prompt_tokens"] += prompt
        acc["completion_tokens"] += completion
        acc["cached_tokens"] += int(cached or 0)
        acc["reasoning_tokens"] += int(reasoning or 0)
        acc["duration_ms"] += int(dur or 0)
        if int(n_cached or 0) > 0:
            acc["cached_reported"] = True
        price = await _price_for(pid, model, provider, at=first_at)
        cost = _cost_of(prompt, completion, int(cached or 0) if n_cached else None, price)
        if cost is not None:
            acc["cost"] = (acc["cost"] or 0.0) + cost
            acc["cost_currency"] = price[3] if price else acc["cost_currency"]

    for acc in out.values():
        acc["total_tokens"] = acc["prompt_tokens"] + acc["completion_tokens"]
        acc["cache_hit_rate"] = (
            round(acc["cached_tokens"] / acc["prompt_tokens"], 4)
            if acc["prompt_tokens"]
            else 0.0
        )
        if acc["cost"] is not None:
            acc["cost"] = round(acc["cost"], 6)
    return out



def _bucket_start(bucket: str, granularity: str) -> Optional[datetime]:
    """桶字符串 → 该桶起始时刻（UTC），用于「按桶取当时生效的单价」。"""
    fmt = "%Y-%m-%d" if granularity == "day" else "%Y-%m-%d %H:00"
    try:
        return datetime.strptime(bucket, fmt).replace(tzinfo=timezone.utc)
    except Exception as e:  # noqa: BLE001
        # 桶字符串来自 DB，格式本就该对；解析不了只当这一桶没有数据。
        warn_bug_only(e, "解析时间桶")
        return None


async def usage_timeseries(
    *,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
    task_id: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    granularity: str = "day",
) -> Dict[str, Any]:
    """按时间桶聚合用量（趋势图用）：每桶的 token / 缓存 / 成本 / 调用数 / 耗时。

    ``granularity`` 支持 ``day``（按天）与 ``hour``（按小时）。桶按 **UTC** 切分
    （``created_at`` 存的就是 UTC）。成本口径与 ``aggregate`` 一致：桶内按
    (model, provider, profile_id) 分别取「当时生效」的单价计价后再求和。
    """
    if granularity not in ("day", "hour"):
        granularity = "day"
    fmt = "%Y-%m-%d" if granularity == "day" else "%Y-%m-%d %H:00"
    bucket_expr = func.strftime(fmt, LLMCall.created_at)

    conds = []
    if agent_id:
        conds.append(LLMCall.agent_id == agent_id)
    if session_id:
        conds.append(LLMCall.session_id == session_id)
    if task_id:
        conds.append(LLMCall.task_id == task_id)
    if since:
        conds.append(LLMCall.created_at >= since)
    if until:
        conds.append(LLMCall.created_at <= until)

    try:
        factory = get_session_factory()
        async with factory() as db:
            rows = (
                await db.execute(
                    select(
                        bucket_expr.label("bucket"),
                        LLMCall.model,
                        LLMCall.provider,
                        LLMCall.profile_id,
                        func.sum(LLMCall.prompt_tokens),
                        func.sum(LLMCall.completion_tokens),
                        func.sum(LLMCall.cached_tokens),
                        func.sum(LLMCall.duration_ms),
                        func.count(LLMCall.id),
                        func.count(LLMCall.cached_tokens),
                    )
                    .where(*conds)
                    .group_by(
                        bucket_expr,
                        LLMCall.model,
                        LLMCall.provider,
                        LLMCall.profile_id,
                    )
                    .order_by(bucket_expr)
                )
            ).all()
    except Exception as e:  # noqa: BLE001
        raise_if_bug(e, "按时间聚合用量")
        logger.debug("按时间聚合用量失败: %s", e)
        return {"granularity": granularity, "points": [], "cost_currency": None}

    # 二次聚合：同一桶内不同 model 各自计价后合并成一个点
    acc: Dict[str, Dict[str, Any]] = {}
    currency: Optional[str] = None
    for bucket, model, provider, pid, p, c, cached, dur, n, n_cached in rows:
        if not bucket:
            continue
        prompt = int(p or 0)
        completion = int(c or 0)
        cached_n = int(cached or 0) if n_cached else None
        price = await _price_for(
            pid, model, provider, at=_bucket_start(bucket, granularity)
        )
        cost = _cost_of(prompt, completion, cached_n, price)
        item = acc.setdefault(
            bucket,
            {
                "bucket": bucket,
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cached_tokens": 0,
                "duration_ms": 0,
                "cost": None,
            },
        )
        item["calls"] += int(n or 0)
        item["prompt_tokens"] += prompt
        item["completion_tokens"] += completion
        item["cached_tokens"] += int(cached or 0)
        item["duration_ms"] += int(dur or 0)
        if cost is not None:
            item["cost"] = (item["cost"] or 0.0) + cost
            currency = currency or (price[3] if price else None)

    points = [acc[k] for k in sorted(acc)]
    for it in points:
        if it["cost"] is not None:
            it["cost"] = round(it["cost"], 6)
    return {"granularity": granularity, "points": points, "cost_currency": currency}


async def slowest_calls(
    *,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
    task_id: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """最慢的 N 次 LLM 调用：定位「这一轮为什么这么久」。

    只看记到耗时的调用；按 duration_ms 降序返回，带上 token、是否流式、首字
    延迟与归属（message / step / session），便于直接跳到那一轮排查。
    """
    conds = [LLMCall.duration_ms.is_not(None)]
    if agent_id:
        conds.append(LLMCall.agent_id == agent_id)
    if session_id:
        conds.append(LLMCall.session_id == session_id)
    if task_id:
        conds.append(LLMCall.task_id == task_id)
    if since:
        conds.append(LLMCall.created_at >= since)
    if until:
        conds.append(LLMCall.created_at <= until)

    try:
        factory = get_session_factory()
        async with factory() as db:
            rows = (
                await db.execute(
                    select(LLMCall)
                    .where(*conds)
                    .order_by(LLMCall.duration_ms.desc())
                    .limit(max(1, min(limit, 100)))
                )
            ).scalars().all()
    except Exception as e:  # noqa: BLE001
        logger.debug("查询最慢调用失败: %s", e)
        return []

    return [
        {
            "id": r.id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "model": r.model,
            "kind": r.kind,
            "prompt_tokens": int(r.prompt_tokens or 0),
            "completion_tokens": int(r.completion_tokens or 0),
            "cached_tokens": int(r.cached_tokens or 0),
            "duration_ms": r.duration_ms,
            "ttft_ms": r.ttft_ms,
            "is_stream": bool(r.is_stream),
            "ok": bool(r.ok),
            "error": r.error,
            "message_id": r.message_id,
            "step_seq": r.step_seq,
            "session_id": r.session_id,
        }
        for r in rows
    ]


async def error_breakdown(
    *,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
    task_id: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """错误聚合：LLM 与工具分别统计失败率，并按错误信息归类（看哪些错最多）。

    错误信息可能很长且每次略有不同，这里按**前 120 字符**归并统计，
    避免每个不同的报错都单独占一行而看不出主次。
    """
    conds: List[Any] = []
    if agent_id:
        conds.append(LLMCall.agent_id == agent_id)
    if session_id:
        conds.append(LLMCall.session_id == session_id)
    if task_id:
        conds.append(LLMCall.task_id == task_id)
    if since:
        conds.append(LLMCall.created_at >= since)
    if until:
        conds.append(LLMCall.created_at <= until)

    out: Dict[str, Any] = {
        "llm": {"total": 0, "failed": 0, "error_rate": 0.0, "groups": []},
        "tool": {"total": 0, "failed": 0, "error_rate": 0.0, "groups": []},
    }
    try:
        factory = get_session_factory()
        async with factory() as db:
            llm_total = (
                await db.execute(
                    select(func.count(LLMCall.id)).where(*conds)
                )
            ).scalar() or 0
            llm_failed = (
                await db.execute(
                    select(func.count(LLMCall.id)).where(
                        *conds, LLMCall.ok.is_(False)
                    )
                )
            ).scalar() or 0
            llm_err_rows = (
                await db.execute(
                    select(LLMCall.error, LLMCall.created_at)
                    .where(*conds, LLMCall.ok.is_(False))
                    .order_by(LLMCall.created_at.desc())
                )
            ).all()

            tool_conds: List[Any] = []
            if agent_id:
                tool_conds.append(ToolCall.agent_id == agent_id)
            if session_id:
                tool_conds.append(ToolCall.session_id == session_id)
            if task_id:
                tool_conds.append(ToolCall.task_id == task_id)
            if since:
                tool_conds.append(ToolCall.created_at >= since)
            if until:
                tool_conds.append(ToolCall.created_at <= until)
            tool_total = (
                await db.execute(
                    select(func.count(ToolCall.id)).where(*tool_conds)
                )
            ).scalar() or 0
            tool_failed = (
                await db.execute(
                    select(func.count(ToolCall.id)).where(
                        *tool_conds, ToolCall.ok.is_(False)
                    )
                )
            ).scalar() or 0
            tool_err_rows = (
                await db.execute(
                    select(
                        ToolCall.tool, ToolCall.error, ToolCall.created_at
                    )
                    .where(*tool_conds, ToolCall.ok.is_(False))
                    .order_by(ToolCall.created_at.desc())
                )
            ).all()
    except Exception as e:  # noqa: BLE001
        logger.debug("聚合错误失败: %s", e)
        return out

    def _group(rows, key_of):
        acc: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            key = key_of(row)
            if not key:
                key = "（无错误信息）"
            item = acc.setdefault(key, {"count": 0, "last_at": None})
            item["count"] += 1
            at = row[-1]
            if at and (item["last_at"] is None or at.isoformat() > item["last_at"]):
                item["last_at"] = at.isoformat()
        groups = [
            {**{"key": k}, **v}
            for k, v in sorted(acc.items(), key=lambda kv: -kv[1]["count"])
        ]
        return groups[: max(1, min(limit, 100))]

    llm_groups = _group(
        llm_err_rows, lambda r: (r[0] or "").strip()[:120]
    )
    tool_groups = _group(
        tool_err_rows,
        lambda r: f"{(r[0] or '').strip()}: {(r[1] or '').strip()[:120]}",
    )

    out["llm"] = {
        "total": int(llm_total),
        "failed": int(llm_failed),
        "error_rate": round(llm_failed / llm_total, 4) if llm_total else 0.0,
        "groups": llm_groups,
    }
    out["tool"] = {
        "total": int(tool_total),
        "failed": int(tool_failed),
        "error_rate": round(tool_failed / tool_total, 4) if tool_total else 0.0,
        "groups": tool_groups,
    }
    return out


async def ask_rate(
    *,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> Dict[str, Any]:
    """追问率：agent 向用户提问（``kind=ask_human``）的频繁程度。

    这是**效果**指标而非成本指标——老追问说明 agent 拿到的信息不够（能力不足 /
    工具返回没用 / 上下文丢了关键信息），体验上表现为"它总来问我"。

    - ``rounds``    ：统计范围内的轮数
    - ``ask_rounds``：其中出现过追问的轮数
    - ``ask_rate``  ：ask_rounds / rounds，即多少比例的对话被打断
    - ``ask_count`` ：追问总次数（一轮可能问多次）
    """
    empty = {
        "rounds": 0,
        "ask_rounds": 0,
        "ask_count": 0,
        "ask_rate": 0.0,
    }
    try:
        from ..store import ReactStep, get_session_factory

        conds: List[Any] = [LLMCall.message_id.is_not(None)]
        if agent_id:
            conds.append(LLMCall.agent_id == agent_id)
        if session_id:
            conds.append(LLMCall.session_id == session_id)
        if since:
            conds.append(LLMCall.created_at >= since)
        if until:
            conds.append(LLMCall.created_at <= until)

        factory = get_session_factory()
        async with factory() as db:
            mids = (
                await db.execute(
                    select(func.distinct(LLMCall.message_id)).where(*conds)
                )
            ).scalars().all()
            mids = [m for m in mids if m]
            if not mids:
                return empty
            ask_rows = (
                await db.execute(
                    select(
                        ReactStep.session_message_id,
                        func.count(ReactStep.id),
                    )
                    .where(
                        ReactStep.session_message_id.in_(mids),
                        or_(
                            ReactStep.kind == "ask_human",
                            ReactStep.wait_kind.is_not(None),
                        ),
                    )
                    .group_by(ReactStep.session_message_id)
                )
            ).all()

        ask_by_msg = {mid: int(n or 0) for mid, n in ask_rows}
        rounds = len(mids)
        ask_rounds = len(ask_by_msg)
        ask_count = sum(ask_by_msg.values())
        return {
            "rounds": rounds,
            "ask_rounds": ask_rounds,
            "ask_count": ask_count,
            "ask_rate": round(ask_rounds / rounds, 4) if rounds else 0.0,
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("统计追问率失败: %s", e)
        return empty


async def duplicate_calls(
    *,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
    task_id: Optional[str] = None,
    message_id: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = 10,
) -> Dict[str, Any]:
    """重复调用 / 空转：同一轮内用**相同参数**反复调同一个工具。

    识别依据是 ``tool_calls.args_hash``（入参哈希，只存哈希不存原文）。同一轮内
    相同 ``(tool, args_hash)`` 出现 2 次以上即视为空转——模型在绕圈子，白白烧
    token。这是**效果**指标：空转率高说明模型没理解工具结果或规划能力不足。

    - ``total``           ：工具调用总数
    - ``duplicate_count`` ：多出来的调用次数（每组 count-1 之和）
    - ``duplicate_rate``  ：duplicate_count / total
    - ``groups``          ：最常重复的 Top N（含工具名、参数大小、所在轮）

    ``read`` 单独一档：它是设计内的追回动作，不计入空转；但同一块被反复展开
    说明上下文治理在抖动（取回 → 被压 → 又取回），这个必须单独可见——
    排除掉又看不见，就成了盲区。

    - ``read.calls``        ：read 调用总数
    - ``read.repeats``      ：同一 block 被多读的次数（每组 count-1 之和）
    - ``read.repeat_rate``  ：repeats / calls
    - ``read.groups``       ：最常被反复展开的块 Top N
    """
    empty = {
        "total": 0,
        "duplicate_count": 0,
        "duplicate_rate": 0.0,
        "groups": [],
        "read": {"calls": 0, "repeats": 0, "repeat_rate": 0.0, "groups": []},
    }
    try:
        # 先攒「维度过滤」，再分别拼两种口径：
        #   base      = 维度 + 排除 read（空转率）
        #   base_read = 维度 + 只要 read（追回抖动率）
        filters: List[Any] = []
        if agent_id:
            filters.append(ToolCall.agent_id == agent_id)
        if session_id:
            filters.append(ToolCall.session_id == session_id)
        if task_id:
            filters.append(ToolCall.task_id == task_id)
        if message_id:
            # 「查看详情」传进来的可能是助手消息 id，先回溯到触发本轮的用户消息
            filters.append(
                ToolCall.message_id == (await _resolve_round_anchor(message_id))
            )
        if since:
            filters.append(ToolCall.created_at >= since)
        if until:
            filters.append(ToolCall.created_at <= until)

        # read 是**设计内**的追回动作（展开被外部化/压缩的内容，见
        # doc/context-design.md），重复展开同一块不算「绕圈子」，排除掉，
        # 否则上下文压缩用得越多，空转率虚高得越厉害。
        base = filters + [ToolCall.tool != "read"]
        base_read = filters + [ToolCall.tool == "read"]

        factory = get_session_factory()
        async with factory() as db:
            total = (
                await db.execute(select(func.count(ToolCall.id)).where(*base))
            ).scalar() or 0
            rows = (
                await db.execute(
                    select(
                        ToolCall.message_id,
                        ToolCall.tool,
                        ToolCall.args_hash,
                        func.count(ToolCall.id),
                        func.max(ToolCall.args_size),
                    )
                    .where(*base, ToolCall.args_hash.is_not(None))
                    .group_by(
                        ToolCall.message_id,
                        ToolCall.tool,
                        ToolCall.args_hash,
                    )
                    .order_by(func.count(ToolCall.id).desc())
                )
            ).all()

        groups = []
        dup = 0
        for mid, tool, ah, n, a_size in rows:
            n = int(n or 0)
            if n < 2:
                continue
            dup += n - 1
            groups.append(
                {
                    "message_id": mid,
                    "tool": tool,
                    "args_hash": ah,
                    "count": n,
                    "args_size": int(a_size or 0),
                }
            )
        groups.sort(key=lambda g: -g["count"])

        # read 单独统计：它是**设计内**的追回动作（展开被外部化/压缩的内容），
        # 所以上面的空转率把它排除了。但「同一个 block_id 被 read 多次」恰恰
        # 说明上下文治理在抖动（取回 → 被压 → 又取回）——必须单独看见，
        # 否则这块是盲区：面板越干净，问题藏得越深。
        read_rows = (
            await db.execute(
                select(
                    ToolCall.message_id,
                    ToolCall.args_hash,
                    func.count(ToolCall.id),
                )
                .where(*base_read)
                .group_by(ToolCall.message_id, ToolCall.args_hash)
                .order_by(func.count(ToolCall.id).desc())
            )
        ).all()
        read_total = (
            await db.execute(
                select(func.count(ToolCall.id)).where(*base_read)
            )
        ).scalar() or 0
        read_groups = []
        read_repeats = 0
        for mid, ah, n in read_rows:
            n = int(n or 0)
            if n < 2:
                continue
            read_repeats += n - 1
            read_groups.append(
                {"message_id": mid, "block_hash": ah, "count": n}
            )
        read_groups.sort(key=lambda g: -g["count"])

        return {
            "total": int(total),
            "duplicate_count": dup,
            "duplicate_rate": round(dup / total, 4) if total else 0.0,
            "groups": groups[: max(1, min(limit, 100))],
            "read": {
                "calls": int(read_total),
                "repeats": read_repeats,
                "repeat_rate": round(read_repeats / read_total, 4) if read_total else 0.0,
                "groups": read_groups[: max(1, min(limit, 100))],
            },
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("统计重复调用失败: %s", e)
        return empty



async def tool_stats(
    *,
    session_id: Optional[str] = None,
    message_id: Optional[str] = None,
    task_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """按**工具名**聚合执行情况：定位最慢 / 最常失败 / 返回最大的工具。

    ``avg_output_size`` 大的工具要重点看——它的返回会原样进下一轮 prompt，
    是上下文膨胀的主要来源；``truncated`` 计数高说明它的返回经常超过内联上限，
    给模型的文本被换成了引用（``block_id``）或截断，该精简输出了。
    """
    try:
        stmt = select(
            ToolCall.tool,
            func.count(ToolCall.id),
            func.sum(case((ToolCall.ok.is_(False), 1), else_=0)),
            func.sum(ToolCall.duration_ms),
            func.avg(ToolCall.duration_ms),
            func.max(ToolCall.duration_ms),
            func.avg(ToolCall.output_size),
            func.max(ToolCall.output_size),
            func.sum(case((ToolCall.truncated.is_(True), 1), else_=0)),
        ).group_by(ToolCall.tool)
        if session_id:
            stmt = stmt.where(ToolCall.session_id == session_id)
        if message_id:
            stmt = stmt.where(ToolCall.message_id == message_id)
        if task_id:
            stmt = stmt.where(ToolCall.task_id == task_id)
        if agent_id:
            stmt = stmt.where(ToolCall.agent_id == agent_id)
        if since:
            stmt = stmt.where(ToolCall.created_at >= since)
        if until:
            stmt = stmt.where(ToolCall.created_at <= until)

        factory = get_session_factory()
        async with factory() as db:
            rows = (await db.execute(stmt)).all()

        out: List[Dict[str, Any]] = []
        for (
            tool,
            n,
            n_err,
            s_dur,
            a_dur,
            m_dur,
            a_out,
            m_out,
            n_trunc,
        ) in rows:
            calls = int(n or 0)
            errors = int(n_err or 0)
            out.append(
                {
                    "tool": tool,
                    "calls": calls,
                    "errors": errors,
                    "error_rate": round(errors / calls, 4) if calls else 0.0,
                    "total_duration_ms": int(s_dur or 0),
                    "avg_duration_ms": int(a_dur or 0),
                    "max_duration_ms": int(m_dur or 0),
                    "avg_output_size": int(a_out or 0),
                    "max_output_size": int(m_out or 0),
                    "truncated": int(n_trunc or 0),
                }
            )
        # 默认按总耗时降序：最该优化的排最前
        out.sort(key=lambda r: r["total_duration_ms"], reverse=True)
        return out
    except Exception as e:
        logger.debug("聚合工具统计失败: %s", e)
        return []


