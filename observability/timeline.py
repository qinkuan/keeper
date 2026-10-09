"""时间线：一条消息的 step 明细、每步耗时、单条消息的完整时间线。

本文件是 keeper.observability 包的一部分，从原来的单体
keeper/observability.py（1856 行）拆分而来。设计见
doc/observability-design.md。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select

from ..store import LLMCall, ToolCall, get_session_factory
from ._context import _resolve_round_anchor
from ._guard import note_if_bug, raise_if_bug
from .budget import aggregate
from .stats import duplicate_calls

logger = logging.getLogger(__name__)


async def step_metrics_for_message(message_id: Optional[str]) -> Dict[int, Any]:
    """按轮取**每个 step** 的用量，key 是 step 序号（1 起，与 react_steps.step 对齐）。

    一次查两张表（react_steps 取 step id 与耗时，llm_calls 按 step_id 聚合用量）
    后在内存里合并——避免前端为每一步各发一次请求。

    返回每项含：token 数、缓存命中数与是否上报、LLM 耗时、整步耗时（含工具）。
    """
    out: Dict[int, Any] = {}
    if not message_id:
        return out
    try:
        from ..store import ReactStep, get_session_factory

        factory = get_session_factory()
        async with factory() as db:
            rows = (
                await db.execute(
                    select(ReactStep).where(
                        ReactStep.session_message_id == message_id
                    )
                )
            ).scalars().all()
        seq_by_id = {r.id: r.step for r in rows}
        dur_by_id = {r.id: r.duration_ms for r in rows}

        stmt = (
            select(
                LLMCall.step_id,
                func.sum(LLMCall.prompt_tokens),
                func.sum(LLMCall.completion_tokens),
                func.sum(LLMCall.cached_tokens),
                func.sum(LLMCall.reasoning_tokens),
                func.sum(LLMCall.duration_ms),
                func.count(LLMCall.id),
                func.count(LLMCall.cached_tokens),
            )
            .where(
                LLMCall.message_id == message_id,
                LLMCall.step_id.is_not(None),
            )
            .group_by(LLMCall.step_id)
        )
        async with factory() as db:
            call_rows = (await db.execute(stmt)).all()

        for step_id, p, c, cached, reasoning, dur, n, n_cached in call_rows:
            seq = seq_by_id.get(step_id)
            if seq is None:
                continue
            prompt = int(p or 0)
            completion = int(c or 0)
            out[seq] = {
                "step_id": step_id,
                "calls": int(n or 0),
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
                "cached_tokens": int(cached or 0),
                "reasoning_tokens": int(reasoning or 0),
                "cached_reported": bool(n_cached),
                "llm_duration_ms": int(dur or 0),
                "duration_ms": dur_by_id.get(step_id),
                "cache_hit_rate": (
                    round(int(cached or 0) / prompt, 4) if prompt else 0.0
                ),
            }
    except Exception as e:
        raise_if_bug(e, "按轮聚合 step 用量")
        logger.debug("按轮聚合 step 用量失败: %s", e)
    return out


def _clip(text: Optional[str], limit: int = 4000) -> Optional[str]:
    """截断长文本：详情要带上具体内容，但不能把工具返回的几十 KB 全塞进响应。"""
    if not text:
        return None
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…（已截断，共 {len(text)} 字符）"


async def message_timeline(message_id: str) -> Dict[str, Any]:
    """一轮的**完整时间线**：总览 + 每个 step 的耗时与 token 分解（前端可视化用）。

    四路数据合并：
    - ``aggregate``                 : 本轮总用量（token / 缓存 / 成本 / 耗时）
    - ``step_metrics_for_message``  : 每步的 LLM 用量（token / LLM 耗时）
    - ``react_steps`` + ``tool_calls``：每步的类型、整步耗时、工具耗时与返回大小
    - ``duplicate_calls``           : 本轮的空转（重复调用）与追回抖动，
      排查「这一轮为什么慢/贵」时和 token 曲线放一起看才有效

    按 step 序号升序返回，便于前端画时间线 / 瀑布图。任一路查不到都降级为
    「只有总览」，绝不抛给调用方——可观测不能影响主流程。
    """
    anchor = await _resolve_round_anchor(message_id)
    summary = await aggregate(message_id=anchor)
    usage_by_seq = await step_metrics_for_message(anchor)
    steps: List[Dict[str, Any]] = []
    try:
        from ..store import ReactStep, get_session_factory

        factory = get_session_factory()
        async with factory() as db:
            rows = (
                await db.execute(
                    select(ReactStep)
                    .where(ReactStep.session_message_id == anchor)
                    .order_by(ReactStep.step)
                )
            ).scalars().all()
            step_ids = [r.id for r in rows]
            tool_rows: List[Any] = []
            if step_ids:
                tool_rows = (
                    await db.execute(
                        select(
                            ToolCall.step_id,
                            func.sum(ToolCall.duration_ms),
                            func.sum(ToolCall.output_size),
                            func.sum(ToolCall.raw_output_size),
                            func.count(ToolCall.id),
                        )
                        .where(ToolCall.step_id.in_(step_ids))
                        .group_by(ToolCall.step_id)
                    )
                ).all()
                # 逐条调用明细：用来把「空转」定位到**具体哪一步**
                call_rows = (
                    await db.execute(
                        select(
                            ToolCall.step_id,
                            ToolCall.tool,
                            ToolCall.args_hash,
                        )
                        .where(ToolCall.step_id.in_(step_ids))
                        .order_by(ToolCall.created_at, ToolCall.id)
                    )
                ).all()
            else:
                call_rows = []
            # 压缩调用：kind=context_compact、step_id 为空。它们是**独立的一次
            # LLM 调用**，不属于任何步骤，不查出来就永远不在时间线上出现——
            # 排查时只能靠 dump 文件名猜「哪一步压了」。
            compact_rows = (
                await db.execute(
                    select(
                        LLMCall.id,
                        LLMCall.duration_ms,
                        LLMCall.prompt_tokens,
                        LLMCall.completion_tokens,
                        LLMCall.created_at,
                    )
                    .where(
                        LLMCall.message_id == anchor,
                        LLMCall.kind == "context_compact",
                    )
                    .order_by(LLMCall.created_at)
                )
            ).all()
            # 每步的起始时间：用来决定压缩节点插在哪两步之间
            step_start_rows = (
                await db.execute(
                    select(LLMCall.step_id, func.min(LLMCall.created_at))
                    .where(
                        LLMCall.message_id == anchor,
                        LLMCall.step_id.is_not(None),
                    )
                    .group_by(LLMCall.step_id)
                )
            ).all()

        step_start = {sid: ts for sid, ts in step_start_rows}
        tool_by_step = {
            sid: {
                "tool_duration_ms": int(d or 0),
                "output_size": int(o or 0),
                "raw_output_size": int(ro or 0),
                "tool_calls": int(n or 0),
            }
            for sid, d, o, ro, n in tool_rows
        }
        # 重复调用**定位到步骤**：同一轮内「同工具 + 同入参」的第 2 次及以后才是
        # 空转（第一次是正常调用）。read 单独口径——它是追回，重复算「抖动」。
        dup_by_step: Dict[str, Dict[str, Any]] = {}
        read_repeat_steps: set = set()
        _seen: Dict[tuple, int] = {}
        _first_step: Dict[tuple, str] = {}
        for sid, tname, ah in call_rows:
            key = (tname, ah)
            n = _seen.get(key, 0) + 1
            _seen[key] = n
            if n == 1:
                _first_step[key] = sid
                continue
            if tname == "read":
                read_repeat_steps.add(sid)
            else:
                dup_by_step[sid] = {"tool": tname, "nth": n, "first_step_id": _first_step.get(key)}

        # kind 落库时就是「工具名」（executor：kind or (step.tool if has_tool else
        # "think")），无工具时是 think / ask / pause 等，故用白名单反推是否工具步。
        non_tool = {"think", "ask", "pause", "final", "final_summary"}
        seq_by_id = {r.id: r.step for r in rows}

        def _compact_step(c: Dict[str, Any]) -> Dict[str, Any]:
            """压缩节点：不是任何一步，但确实花了一次 LLM 调用的钱与时间。"""
            ms = int(c["duration_ms"] or 0)
            return {
                "seq": None,  # 不属于任何步骤
                "step_id": None,
                "kind": "context_compact",
                "is_tool": False,
                "virtual": "compact",
                "status": "done",
                "duration_ms": ms,
                "llm_duration_ms": ms,
                "tool_duration_ms": 0,
                "tool_calls": 0,
                "output_size": 0,
                "raw_output_size": 0,
                "truncated": False,
                "created_at": c["created_at"].isoformat() if c["created_at"] else None,
                "input_text": "",
                "output_text": "",
                "input_chars": 0,
                "output_chars": 0,
                "dup": None,
                "read_repeat": False,
                "usage": {
                    "prompt_tokens": int(c["prompt_tokens"] or 0),
                    "completion_tokens": int(c["completion_tokens"] or 0),
                    # 前端按 total_tokens 画 token 条，缺了它压缩节点的条长恒为 0
                    "total_tokens": int(c["prompt_tokens"] or 0)
                    + int(c["completion_tokens"] or 0),
                    "cached_tokens": 0,
                    "reasoning_tokens": 0,
                    "call_count": 1,
                    "llm_duration_ms": ms,
                },
            }

        compacts = [
            {
                "duration_ms": dms,
                "prompt_tokens": p,
                "completion_tokens": ct,
                "created_at": ts,
            }
            for _cid, dms, p, ct, ts in compact_rows
        ]
        ci = 0  # 下一个待插入的压缩节点

        for r in rows:
            # 把发生在这步之前的压缩节点插进来（按时间顺序还原真实过程）
            st = step_start.get(r.id)
            while ci < len(compacts) and st is not None:
                ct = compacts[ci]["created_at"]
                if ct is None or ct >= st:
                    break
                steps.append(_compact_step(compacts[ci]))
                ci += 1
            u = usage_by_seq.get(r.step) or {}
            t = tool_by_step.get(r.id) or {}
            llm_ms = int(u.get("llm_duration_ms") or 0)
            total_ms = r.duration_ms
            raw_size = int(t.get("raw_output_size") or 0)
            out_size = int(t.get("output_size") or 0)
            dup = dup_by_step.get(r.id)
            if dup:  # first_step_id → 步号，前端直接显示「同 #N 步」
                dup = {
                    "tool": dup["tool"],
                    "nth": dup["nth"],
                    "first_seq": seq_by_id.get(dup["first_step_id"]),
                }
            steps.append(
                {
                    "seq": r.step,
                    "step_id": r.id,
                    "kind": r.kind,
                    "is_tool": r.kind not in non_tool,
                    "status": r.status,
                    "duration_ms": total_ms,
                    "llm_duration_ms": llm_ms,
                    # 工具耗时优先取 tool_calls 实测；没记录就用「整步 - LLM」倒推
                    "tool_duration_ms": t.get("tool_duration_ms")
                    or (max(total_ms - llm_ms, 0) if total_ms else 0),
                    "tool_calls": int(t.get("tool_calls") or 0),
                    "output_size": out_size,
                    "raw_output_size": raw_size,
                    # 原始返回 > 实际进上下文：超内联上限，被换引用或截断了
                    "truncated": bool(raw_size and out_size and raw_size > out_size),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    # 具体内容：input = 本步的思考（+ 工具入参），output = 工具返回
                    # （观测，会原样进下一轮 prompt）。真正的完整 prompt（system +
                    # 历史消息）没有落库——只统计了 token，故这里给的是步骤级内容。
                    "input_text": _clip(r.input),
                    "output_text": _clip(r.output),
                    "input_chars": len(r.input or ""),
                    "output_chars": len(r.output or ""),
                    # 空转定位：本步是同工具+同入参的第 nth 次调用（首次在 #first_seq）
                    "dup": dup,
                    # 追回抖动：本步 read 的块之前已展开过
                    "read_repeat": r.id in read_repeat_steps,
                    "usage": u or None,
                }
            )
        # 末尾剩下的压缩节点（发生在最后一步之后 / 没有步骤时间可比）
        while ci < len(compacts):
            steps.append(_compact_step(compacts[ci]))
            ci += 1
    except Exception as e:  # noqa: BLE001
        raise_if_bug(e, "构建消息时间线")
        logger.debug("构建消息时间线失败: %s", e)

    # 本轮的空转 / 追回抖动：排查「为什么这轮这么慢/这么贵」时，光看 token 曲线
    # 看不出模型在绕圈子，必须把重复调用摆到同一屏上。
    try:
        duplicates = await duplicate_calls(message_id=anchor, limit=10)
    except Exception as e:  # noqa: BLE001
        raise_if_bug(e, "统计本轮重复调用")
        logger.debug("统计本轮重复调用失败: %s", e)
        duplicates = None

    return {
        "message_id": message_id,
        "anchor_message_id": anchor,
        "summary": summary,
        "steps": steps,
        "duplicates": duplicates,
    }



async def set_message_duration(message_id: Optional[str], duration_ms: int) -> None:
    """写一条消息的端到端耗时（一轮：从提问到答完的墙钟毫秒）。

    注意与 ``llm_calls`` 的 ``SUM(duration_ms)`` 是两个口径：后者只是「算力花了
    多久」，不含工具执行、等待与挂起恢复；本值是用户真正等待的时间。
    """
    if not message_id:
        return
    try:
        from ..store import SessionMessage, get_session_factory

        factory = get_session_factory()
        async with factory() as db:
            row = await db.get(SessionMessage, message_id)
            if row is not None:
                row.duration_ms = duration_ms
                await db.commit()
    except Exception as e:
        note_if_bug(e, "写入消息耗时")
        logger.debug("写入消息耗时失败: %s", e)


