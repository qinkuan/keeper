"""可观测：用量聚合查询接口（``/metrics``）。

数据全部来自 ``llm_calls`` 事实表，按 step / 消息 / 会话 / 任务 / agent 任一维度
聚合得出——各表不冗余 token 列，避免多份数据不一致。

返回结构统一见 ``observability.aggregate``：token 数、缓存命中数与命中率、
耗时、调用次数、成本；``cached_reported=False`` 表示 provider 未上报缓存，
此时别把 ``cached_tokens=0`` 当成"没命中"。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

from ..observability import aggregate, check_budget, message_timeline, tool_stats

router = APIRouter(prefix="/metrics", tags=["metrics"])

# ReAct 一轮的步数上限（与 Process 的 max_steps 默认值一致）；达到即强制总结
_MAX_STEPS = 30


@router.get("/steps/{step_id}")
async def step_metrics(step_id: str) -> Dict[str, Any]:
    """单个 ReAct 步骤的用量：token、缓存命中、耗时。"""
    return await aggregate(step_id=step_id)


@router.get("/messages/{message_id}")
async def message_metrics(message_id: str) -> Dict[str, Any]:
    """一条消息（一轮）的聚合用量。

    这里的耗时口径是「LLM 耗时之和」；用户等待的端到端墙钟在
    ``session_messages.duration_ms`` 上（前端展示时两者别混用）。
    """
    return await aggregate(message_id=message_id)


@router.get("/messages/{message_id}/timeline")
async def message_timeline_api(message_id: str) -> Dict[str, Any]:
    """一轮的**完整时间线**：总览 + 每步耗时与 token 分解（前端可视化用）。

    逐步数据来自 react_steps（类型 / 整步耗时）+ tool_calls（工具耗时 / 返回大小）
    + llm_calls（每步 token 与 LLM 耗时），按 step 序号升序，便于画瀑布图。
    """
    return await message_timeline(message_id)


@router.get("/sessions/{session_id}")
async def session_metrics(session_id: str) -> Dict[str, Any]:
    """整个会话的聚合用量。"""
    return await aggregate(session_id=session_id)


@router.get("/tasks/{task_id}")
async def task_metrics(task_id: str) -> Dict[str, Any]:
    """一个任务的聚合用量。"""
    return await aggregate(task_id=task_id)


@router.get("/tools")
async def tool_metrics(
    session_id: Optional[str] = Query(None),
    task_id: Optional[str] = Query(None),
    agent_id: Optional[str] = Query(None),
) -> List[Dict[str, Any]]:
    """**工具维度**统计：哪个工具最慢 / 最常失败 / 返回最大（默认按总耗时降序）。

    ``avg_output_size`` 大的工具要重点看——它的返回会原样进下一轮 prompt，
    是上下文膨胀的主要来源；``truncated`` 高的说明返回经常超限被砍，该精简了。
    """
    return await tool_stats(
        session_id=session_id, task_id=task_id, agent_id=agent_id
    )


@router.get("/budget")
async def budget(
    session_id: Optional[str] = Query(None),
    task_id: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """预算 / 配额：本会话（或任务）已用多少、上限多少、是否已超。"""
    return await check_budget(session_id=session_id, task_id=task_id)


@router.get("/sessions/{session_id}/steps")
async def session_steps(session_id: str) -> Dict[str, Any]:
    """会话内**每一轮用了几步**，以及是否逼近步数上限。

    一轮步数偏多通常意味着规划走了弯路；``hit_limit`` 表示曾达到上限
    （默认 30）被强制总结，那轮的答案可能是被截断拼出来的。
    """
    from sqlalchemy import func, select

    from ..store import ReactStep, SessionMessage, get_session_factory

    factory = get_session_factory()
    async with factory() as db:
        rows = (
            await db.execute(
                select(
                    ReactStep.session_message_id,
                    func.count(ReactStep.id),
                    func.max(ReactStep.step),
                )
                .join(
                    SessionMessage,
                    ReactStep.session_message_id == SessionMessage.id,
                )
                .where(SessionMessage.chat_session_id == session_id)
                .group_by(ReactStep.session_message_id)
            )
        ).all()

    per_round = [
        {
            "message_id": mid,
            "steps": int(n or 0),
            "max_step": int(mx or 0),
        }
        for mid, n, mx in rows
    ]
    counts = [r["steps"] for r in per_round] or [0]
    return {
        "rounds": per_round,
        "avg_steps": round(sum(counts) / len(counts), 2),
        "max_steps": max(counts),
        "hit_limit": max(counts) >= _MAX_STEPS,
    }


@router.get("/timeseries")
async def usage_timeseries_api(
    agent_id: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
    task_id: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None, description="起始时间（ISO 8601）"),
    until: Optional[datetime] = Query(None, description="结束时间（ISO 8601）"),
    granularity: str = Query("day", description="day 按天 / hour 按小时"),
) -> Dict[str, Any]:
    """按时间桶聚合的用量**趋势**（趋势图用）。

    返回每个桶的 token / 缓存命中 / 成本 / 调用数 / 耗时，桶按 UTC 切分。
    至少给一个归属维度（agent / session / task），否则统计的是全局。
    """
    from ..observability import usage_timeseries

    return await usage_timeseries(
        agent_id=agent_id,
        session_id=session_id,
        task_id=task_id,
        since=since,
        until=until,
        granularity=granularity,
    )


@router.get("/slowest")
async def slowest_calls_api(
    agent_id: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
    task_id: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None, description="起始时间（ISO 8601）"),
    until: Optional[datetime] = Query(None, description="结束时间（ISO 8601）"),
    limit: int = Query(10),
) -> Dict[str, Any]:
    """最慢的 N 次 LLM 调用（按耗时降序）：定位「这一轮为什么这么久」。"""
    from ..observability import slowest_calls

    items = await slowest_calls(
        agent_id=agent_id,
        session_id=session_id,
        task_id=task_id,
        since=since,
        until=until,
        limit=limit,
    )
    return {"items": items}


@router.get("/errors")
async def error_breakdown_api(
    agent_id: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
    task_id: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None, description="起始时间（ISO 8601）"),
    until: Optional[datetime] = Query(None, description="结束时间（ISO 8601）"),
    limit: int = Query(20),
) -> Dict[str, Any]:
    """错误聚合：LLM / 工具各自的失败率，以及按错误信息归类的 Top N。"""
    from ..observability import error_breakdown

    return await error_breakdown(
        agent_id=agent_id,
        session_id=session_id,
        task_id=task_id,
        since=since,
        until=until,
        limit=limit,
    )


@router.get("/ask-rate")
async def ask_rate_api(
    agent_id: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None, description="起始时间（ISO 8601）"),
    until: Optional[datetime] = Query(None, description="结束时间（ISO 8601）"),
) -> Dict[str, Any]:
    """追问率：多少比例的对话被 agent 追问打断（**效果**指标，不是成本）。

    老追问说明 agent 拿到的信息不够——能力不足、工具返回没用，或上下文丢了
    关键信息。返回 rounds / ask_rounds / ask_count / ask_rate。
    """
    from ..observability import ask_rate

    return await ask_rate(
        agent_id=agent_id,
        session_id=session_id,
        since=since,
        until=until,
    )


@router.get("/duplicates")
async def duplicate_calls_api(
    agent_id: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
    task_id: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None, description="起始时间（ISO 8601）"),
    until: Optional[datetime] = Query(None, description="结束时间（ISO 8601）"),
    limit: int = Query(10),
) -> Dict[str, Any]:
    """重复调用 / 空转：同一轮内用相同参数反复调同一工具的次数与 Top N。

    空转率高说明模型在绕圈子（没理解工具结果 / 规划能力不足），白白烧 token。
    """
    from ..observability import duplicate_calls

    return await duplicate_calls(
        agent_id=agent_id,
        session_id=session_id,
        task_id=task_id,
        since=since,
        until=until,
        limit=limit,
    )


@router.get("/messages/{message_id}/dump")
async def message_dump_api(message_id: str) -> Dict[str, Any]:
    """读取某一轮落盘的**完整 prompt**（调试用，需先在设置里开启 prompt_dump）。

    返回该轮每次 LLM 调用的 system、完整 messages、模型响应与 usage。
    未开启落盘时返回 ``enabled=false``，前端据此隐藏入口。
    """
    from ..observability import read_message_dump

    return await read_message_dump(message_id)


@router.get("/react-parse-stats")
async def react_parse_metrics(
    agent_id: Optional[str] = Query(None),
    days: int = Query(7, ge=1, le=90),
) -> Dict[str, Any]:
    """解析结构统计：模型有多想并行、丢了多少动作（P1-1 的收益依据）。"""
    from ..observability import react_parse_stats

    return await react_parse_stats(agent_id=agent_id, days=days)


@router.get("/capabilities")
async def capability_metrics(
    agent_id: Optional[str] = Query(None),
    days: int = Query(7, ge=1, le=90),
    top: int = Query(12, ge=1, le=50),
) -> Dict[str, Any]:
    """**能力加载**统计：技能正文 / 工具定义各被取进上下文多少次、来源如何。

    三个来源要一起看（``by_source``）：

    - ``preload`` 占比高 → L1 摘要写得不准，系统总在猜；
    - ``model_load`` 占比高 → 摘要够用，模型自己在取（这是好事）；
    - ``auto_disclose`` 占比高 → 工具折叠太狠或没写组摘要，模型只猜名字。

    ``redundant_loads`` 是**抖动**数：同一轮把同一个能力取两次以上，说明模型自己
    也不确定读过没有——几乎总是 L1 摘要或提示不够清楚。
    """
    from ..observability import capability_stats

    return await capability_stats(agent_id=agent_id, days=days, top=top)


@router.get("/agents/{agent_id}")
async def agent_metrics(
    agent_id: str,
    since: Optional[datetime] = Query(None, description="起始时间（ISO 8601）"),
    until: Optional[datetime] = Query(None, description="结束时间（ISO 8601）"),
) -> Dict[str, Any]:
    """某 agent 的汇总用量，可带时间范围。"""
    return await aggregate(agent_id=agent_id, since=since, until=until)
