"""记账层：把一次 LLM 调用 / 一次工具调用 / 一次能力加载写进库。

本文件是 keeper.observability 包的一部分，从原来的单体
keeper/observability.py（1856 行）拆分而来。设计见
doc/observability-design.md。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

from sqlalchemy import select

from ..store import (
    CapabilityLoad,
    LLMCall,
    ReactParseStat,
    ToolCall,
    get_session_factory,
    new_id,
)
from ._context import _ctx
from ._guard import note_if_bug

logger = logging.getLogger(__name__)


async def _price_this_call(
    llm: Any,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: Optional[int],
) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    """给**这一次**调用定价：返回 ``(成本, 币种, 价格快照 JSON)``。

    为什么在落库时算，而不是展示时再算
    ----------------------------------
    成本是「这次调用的属性」，和 token 一样是既成事实。落库之后：

    - 换模型、解绑 agent、删掉模型预设 —— 历史金额**纹丝不动**；
    - 改价 / 删价段 —— 也不再回头改写已经发生过的账。

    而"展示时现算"会有两个真实的坑：删预设会连带删掉价段，历史调用匹配不到
    单价 → 金额从有值变成「—」；改价段则会让同一段历史悄悄变成另一个数。

    单价按**调用发生时刻**匹配（错峰时段、星期、生效时间窗都按那一刻算），
    与 ``observability/pricing.py`` 的取价规则完全一致——那里怎么算，这里就
    怎么算，两条路径不会给出不同的数。

    取不到有效单价时返回 ``None``（不是 0）：0 意味着"这次调用免费"，而事实是
    "当时没配价"，两者不能混。
    """
    from .pricing import _cost_of, _price_for

    try:
        at = datetime.now(timezone.utc)
        price = await _price_for(
            getattr(llm, "profile_id", None),
            getattr(llm, "model_name", None),
            getattr(llm, "provider", None),
            at=at,
        )
        cost = _cost_of(prompt_tokens, completion_tokens, cached_tokens, price)
        currency = price[3] if price else None
        snapshot = None
        if price is not None:
            snapshot = json.dumps(
                {
                    "input": price[0],
                    "cached": price[1],
                    "output": price[2],
                    "currency": price[3],
                    "priced_at": at.isoformat(),
                },
                ensure_ascii=False,
            )
        return cost, currency, snapshot
    except Exception as e:  # noqa: BLE001 —— 定价失败不该影响记账
        note_if_bug(e, "计算调用成本")
        logger.debug("计算调用成本失败: %s", e)
        return None, None, None


async def record_llm_call(
    llm: Any,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: Optional[int] = None,
    cache_write_tokens: Optional[int] = None,
    reasoning_tokens: Optional[int] = None,
    duration_ms: Optional[int] = None,
    ttft_ms: Optional[int] = None,
    is_stream: bool = False,
    ok: bool = True,
    error: Optional[str] = None,
    kind: str = "other",
) -> None:
    """落一条 LLM 调用记录。失败只记日志，绝不影响主流程。

    ``llm`` 是 ``LangChainLLM`` 实例，从中取 model / provider / profile_id
    （后两者用于成本换算时去 ``llm_profiles`` 匹配单价）。
    """
    try:
        ctx = _ctx()
        # kind 优先级：调用方显式标记 > 带 step_seq 即 ReAct 的一步 > 传入的默认值。
        # 显式标记用于把「超步数强制总结」这类不对应任何 step 的额外调用区分出来。
        kind = ctx.get("llm_kind") or (
            "react_step" if ctx.get("step_seq") is not None else kind
        )

        # 先定价（独立连接，别和下面那次写事务嵌套），再和 token 一起落库
        cost, currency, snapshot = await _price_this_call(
            llm,
            prompt_tokens or 0,
            completion_tokens or 0,
            cached_tokens,
        )

        factory = get_session_factory()
        async with factory() as db:
            db.add(
                LLMCall(
                    id=new_id(),
                    trace_id=ctx.get("trace_id"),
                    agent_id=ctx.get("agent_id"),
                    session_id=ctx.get("session_id"),
                    message_id=ctx.get("message_id"),
                    step_seq=ctx.get("step_seq"),
                    task_id=ctx.get("task_id"),
                    task_item_id=ctx.get("task_item_id"),
                    kind=kind,
                    model=getattr(llm, "model_name", None),
                    provider=getattr(llm, "provider", None),
                    profile_id=getattr(llm, "profile_id", None),
                    prompt_tokens=prompt_tokens or 0,
                    completion_tokens=completion_tokens or 0,
                    cached_tokens=cached_tokens,
                    cache_write_tokens=cache_write_tokens,
                    reasoning_tokens=reasoning_tokens,
                    duration_ms=duration_ms,
                    ttft_ms=ttft_ms,
                    is_stream=is_stream,
                    ok=ok,
                    error=error,
                    cost=cost,
                    currency=currency,
                    price_snapshot=snapshot,
                )
            )
            await db.commit()
    except Exception as e:  # 用量记账绝不能把对话搞挂
        note_if_bug(e, "记录 LLM 调用用量")
        logger.debug("记录 LLM 调用用量失败: %s", e)


async def record_tool_call(
    tool: str,
    *,
    duration_ms: Optional[int] = None,
    ok: bool = True,
    error: Optional[str] = None,
    args_size: Optional[int] = None,
    args_hash: Optional[str] = None,
    output_size: Optional[int] = None,
    raw_output_size: Optional[int] = None,
    truncated: bool = False,
) -> None:
    """落一条**工具调用**记录。失败只记日志，绝不影响主流程。

    归属与 LLM 调用同源（``_ctx``），因此工具记录能与它所在的 step / 消息 /
    任务对上，便于回答「这一步慢，是慢在 LLM 还是慢在工具」。
    """
    try:
        ctx = _ctx()
        factory = get_session_factory()
        async with factory() as db:
            db.add(
                ToolCall(
                    id=new_id(),
                    trace_id=ctx.get("trace_id"),
                    agent_id=ctx.get("agent_id"),
                    session_id=ctx.get("session_id"),
                    message_id=ctx.get("message_id"),
                    step_seq=ctx.get("step_seq"),
                    task_id=ctx.get("task_id"),
                    task_item_id=ctx.get("task_item_id"),
                    tool=tool,
                    ok=ok,
                    error=error,
                    duration_ms=duration_ms,
                    args_size=args_size,
                    args_hash=args_hash,
                    output_size=output_size,
                    raw_output_size=raw_output_size,
                    truncated=truncated,
                )
            )
            await db.commit()
    except Exception as e:  # 记账失败绝不能把工具执行搞挂
        note_if_bug(e, "记录工具调用")
        logger.debug("记录工具调用失败: %s", e)


async def record_capability_load(
    key: str,
    *,
    kind: str = "skill",
    source: str = "model_load",
    chars: Optional[int] = None,
) -> None:
    """落一条**能力加载**记录（技能正文 / 工具定义进入上下文）。失败只记日志。

    归属同样取自 ``_ctx``（trace / agent / session / message / task），因此能精确
    回答「这一轮预加载了什么」——``message_id`` 就是一轮的粒度，抖动检测靠它。
    """
    try:
        ctx = _ctx()
        factory = get_session_factory()
        async with factory() as db:
            db.add(
                CapabilityLoad(
                    id=new_id(),
                    trace_id=ctx.get("trace_id"),
                    agent_id=ctx.get("agent_id"),
                    session_id=ctx.get("session_id"),
                    message_id=ctx.get("message_id"),
                    task_id=ctx.get("task_id"),
                    key=key[:128],
                    kind=kind,
                    source=source,
                    chars=chars,
                )
            )
            await db.commit()
    except Exception as e:  # 观测失败绝不能影响对话
        note_if_bug(e, "记录能力加载")
        logger.debug("记录能力加载失败: %s", e)


async def record_react_parse_stat(
    kind: str, action_count: int = 0, model: Optional[str] = None
) -> None:
    """落一条「本次响应的结构」统计；失败只记日志，绝不影响解析。

    ``kind`` 取值：``final`` / ``ask`` / ``act`` / ``think`` /
    ``hallucination``（模型自编轨迹，已检出并重试）/
    ``hallucination_forced``（点破若干次仍跑飞，退化为执行第一个 ACTION）。

    ``model`` 是 LLM 模型名：幻觉率必须按模型看，聚合在一起会抹平
    「换模型能根治」这个结论。
    """
    try:
        ctx = _ctx()
        factory = get_session_factory()
        async with factory() as db:
            db.add(
                ReactParseStat(
                    id=new_id(),
                    trace_id=ctx.get("trace_id"),
                    agent_id=ctx.get("agent_id"),
                    session_id=ctx.get("session_id"),
                    message_id=ctx.get("message_id"),
                    task_id=ctx.get("task_id"),
                    kind=kind,
                    action_count=action_count,
                    # 现状：解析器只认第一个 ACTION，其余全丢
                    dropped_count=max(0, action_count - 1) if kind == "act" else 0,
                    model=(model or None),
                )
            )
            await db.commit()
    except Exception as e:  # noqa: BLE001
        note_if_bug(e, "记录解析统计")
        logger.debug("记录解析统计失败: %s", e)



async def backfill_step_id(
    message_id: Optional[str], step_seq: Optional[int], step_id: Optional[str]
) -> None:
    """step 落库后回填它的 id。

    打点时 step 还没写库（现有流程是「先执行、后落库」），只能先记 ``step_seq``；
    等 step 落库拿到 id 后，按 ``(message_id, step_seq)`` 回填——与
    ``PeerService._set_message_task`` 的「补登」是同一套思路。
    """
    if not message_id or step_seq is None or not step_id:
        return
    try:
        factory = get_session_factory()
        async with factory() as db:
            rows = (
                await db.execute(
                    select(LLMCall).where(
                        LLMCall.message_id == message_id,
                        LLMCall.step_seq == step_seq,
                        LLMCall.step_id.is_(None),
                    )
                )
            ).scalars().all()
            for r in rows:
                r.step_id = step_id
            # 该步的工具调用同样要回填（打点时 step 也还没落库）
            trows = (
                await db.execute(
                    select(ToolCall).where(
                        ToolCall.message_id == message_id,
                        ToolCall.step_seq == step_seq,
                        ToolCall.step_id.is_(None),
                    )
                )
            ).scalars().all()
            for r in trows:
                r.step_id = step_id
            if rows or trows:
                await db.commit()
    except Exception as e:
        note_if_bug(e, "回填 step_id")
        logger.debug("回填 step_id 失败: %s", e)


