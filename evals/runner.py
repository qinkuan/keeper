"""评测执行：装载 → 逐条跑 → 判定 → 出报告。

三条编排上的取舍：

1. **串行跑用例**。评测烧的是真钱（每条一次完整 ReAct），并发只会让失败率
   和 token 一起上去，还得处理同一 agent 实例的并发共享。真要提速应该去
   优化单条，而不是并发。
2. **单条用例有超时**。某条卡死（比如 MCP 对端不响应）不该让整轮评测永远挂着；
   超时按「运行级异常」记，同样进报告——「它卡住了」也是有价值的结论。
3. **判定与执行分离**（``harness`` 采集 / ``asserts`` 判定）。这样以后加新断言
   不用碰执行逻辑，而执行逻辑的不确定性不会渗进判定。
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import report as R
from .asserts import evaluate
from .harness import EvalHarness
from .spec import EvalCase, load_suite

logger = logging.getLogger(__name__)

# 单条用例的墙钟上限（秒）。默认 5 分钟：正常用例几十秒就完，超过基本是卡住了。
DEFAULT_CASE_TIMEOUT = 300


async def resolve_agent_id(explicit: Optional[str] = None) -> str:
    """确定被测 agent。

    没显式指定时从库里取**第一个**已启用的 agent。当前项目通常只有一个
    agent 在用，这样省掉每次都去翻 id；但绝不写死某个 id——换 agent 时命令
    不用改。
    """
    if explicit:
        return explicit
    from sqlalchemy import select

    from ..store import Agent, get_session_factory

    factory = get_session_factory()
    async with factory() as db:
        row = (
            await db.execute(
                select(Agent)
                .where(Agent.status == "active")
                .order_by(Agent.created_at)
                .limit(1)
            )
        ).scalars().first()
    if row is None:
        raise ValueError(
            "库里没有可评测的 agent（status=active）。请先在智能体页面装载一个，"
            "或用 --agent <id> 显式指定。"
        )
    logger.info("未指定 --agent，自动选用：%s（%s）", row.name, row.id)
    return row.id


async def run_eval(
    *,
    agent_id: Optional[str] = None,
    suites: Optional[List[str]] = None,
    case_ids: Optional[List[str]] = None,
    cases: Optional[List[EvalCase]] = None,
    run_id: Optional[str] = None,
    offline: bool = True,
    case_timeout: int = DEFAULT_CASE_TIMEOUT,
    compare_with: Optional[str] = "auto",
    source_db: Optional[Path] = None,
    on_progress: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """跑一轮评测，返回完整报告（含与基线的对比）。

    ``on_progress(phase, payload)`` 供调用方输出进度（CLI 打日志、API 层
    转发给前端）。它**只用于展示**，报告的正确性不依赖它。
    """
    started_at = R.now_iso()
    t0 = time.perf_counter()

    case_list = cases if cases is not None else load_suite(suites)
    if case_ids:
        wanted = set(case_ids)
        by_id = {c.id: c for c in case_list}
        missing = sorted(wanted - set(by_id))
        if missing:
            # 静默忽略不存在的 id 会让人以为「跑了但没输出」——直接报错更省事
            raise ValueError(f"用例集里没有这些用例：{missing}")
        case_list = [by_id[i] for i in case_ids]
    if not case_list:
        raise ValueError("用例集为空，没有可跑的东西")

    run_id = run_id or R.new_run_id()
    harness = EvalHarness(
        run_id=run_id, offline=offline, source_db=source_db
    )
    # 顺序：先建环境（临时库就绪）→ 再定 agent（要查库）→ 最后装 agent。
    # 反过来就会「用还没建好的库去查 agent」。
    await harness.setup()
    try:
        agent_id = agent_id or await resolve_agent_id()
        await harness.load_agent(agent_id)
        # 基线要在本次报告落盘**之前**取好。本报告此刻还不存在，所以磁盘上
        # 每一份都早于它——直接取「最新的完整运行」即可，天然不会拿到自己。
        baseline: Optional[Dict[str, Any]] = None
        if compare_with:
            try:
                if compare_with == "auto":
                    baseline = R.latest_full_run()
                elif R.has_report(compare_with):
                    baseline = R.load_report(compare_with)
            except Exception as e:  # noqa: BLE001  坏基线不该让本次评测失败
                logger.warning("读取基线报告失败（本次不做对比）: %s", e)

        fp = await harness.fingerprint()
        _notify(on_progress, "fingerprint", {"fingerprint": fp})

        results: List[Dict[str, Any]] = []
        for i, case in enumerate(case_list, 1):
            if case.skip:
                # 跳过的用例也进报告：否则「少测了 N 条」在对比里会显示成
                # 「这些用例消失了」，容易误读成回归。
                results.append(
                    R.case_result(
                        case,
                        _empty_outcome(case),
                        True,
                        [],
                    )
                )
                _notify(
                    on_progress,
                    "case",
                    {"index": i, "total": len(case_list), "case_id": case.id, "skipped": True},
                )
                continue

            _notify(
                on_progress,
                "case_start",
                {"index": i, "total": len(case_list), "case_id": case.id, "title": case.display_name},
            )
            try:
                out = await asyncio.wait_for(
                    harness.run_case(case), timeout=case_timeout
                )
            except asyncio.TimeoutError:
                out = _empty_outcome(case)
                out.error = f"用例超时（超过 {case_timeout}s），按卡死处理"

            passed, failures = evaluate(case, out)
            results.append(R.case_result(case, out, passed, failures))
            _notify(
                on_progress,
                "case_done",
                {
                    "index": i,
                    "total": len(case_list),
                    "case_id": case.id,
                    "passed": passed,
                    "failures": failures,
                    "metrics": out.metrics,
                },
            )

        duration_ms = int((time.perf_counter() - t0) * 1000)
        report = R.build_report(
            run_id=run_id,
            agent_id=agent_id,
            fingerprint=fp,
            results=results,
            started_at=started_at,
            finished_at=R.now_iso(),
            duration_ms=duration_ms,
            suites=suites or [],
            # 记下这次跑的范围。**选集运行（--case/--suite）必须能被识别出来**：
            # 否则对比时会把「这次没跑」当成「用例被删了」，报出
            # 「用例集删掉 N 条」这种完全误导的结论。
            selection={
                "mode": "subset" if (case_ids or suites) else "all",
                "case_ids": list(case_ids or []),
                "suites": list(suites or []),
                "total": len(case_list),
            },
        )
        report["compare"] = R.compare(report, baseline)
        R.write_report(report)
        _notify(on_progress, "done", {"run_id": run_id, "summary": report["summary"]})
        return report
    finally:
        await harness.teardown()


def _empty_outcome(case: EvalCase) -> Any:
    from .harness import CaseOutcome

    return CaseOutcome(
        case_id=case.id, title=case.display_name, tags=list(case.tags), input=case.input
    )


def _notify(
    cb: Optional[Callable[[str, Dict[str, Any]], None]],
    phase: str,
    payload: Dict[str, Any],
) -> None:
    if cb is None:
        return
    try:
        cb(phase, payload)
    except Exception as e:  # noqa: BLE001  进度回调失败绝不能影响评测
        logger.debug("进度回调异常（忽略）: %s", e)