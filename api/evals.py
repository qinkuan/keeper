"""效果评估接口（``/evals``）：读历史报告 + 触发一次评测。

**评测跑在子进程里，不在服务进程内**。SQLAlchemy 引擎是进程级全局单例，
服务进程里那个已经绑在真实库上，切不到评测要的临时副本；硬切会把评测数据
写进真实库——等于评测毁掉了它本该保护的观测数据。所以这里只用 ``subprocess``
起一个 ``python -m keeper.evals.run``，自己只读它落在磁盘上的报告。

代价是进度只能靠轮询日志展示；换来的是隔离是**结构性**的，而不是靠调用方
记得别传错参数。
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from ..evals import report as R
from ..evals.harness import RUNS_ROOT
from ..evals.spec import CASES_DIR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/evals", tags=["evals"])

# 项目根：子进程要用它当 cwd，否则 `python -m keeper.evals.run` 找不到包
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# run_id -> 运行状态。放内存而不落库：它记的是「本进程刚启动的运行」，
# 进程重启后自然作废（那时的报告由 has_report 兜底）。
_RUNNING: Dict[str, Dict[str, Any]] = {}
# 同时只允许一次：每次都是真金白银的完整 ReAct，连点两下就是双倍成本
_MAX_CONCURRENT = 1


@router.get("/suites")
async def list_suites() -> List[Dict[str, Any]]:
    """可用的用例集及其规模。"""
    out: List[Dict[str, Any]] = []
    for p in sorted(CASES_DIR.glob("*.yaml")):
        try:
            from ..evals.spec import load_cases

            cases = load_cases(p)
        except Exception as e:  # noqa: BLE001  坏用例集不该让整个列表 500
            out.append({"name": p.stem, "path": str(p), "cases": 0, "error": str(e)})
            continue
        out.append(
            {
                "name": p.stem,
                "cases": len(cases),
                "skipped": sum(1 for c in cases if c.skip),
                "tags": sorted({t for c in cases for t in c.tags}),
                "error": None,
            }
        )
    return out


@router.get("/reports")
async def reports(limit: int = Query(30, ge=1, le=200)) -> Dict[str, Any]:
    """历史基线列表（时间倒序）。"""
    items = R.list_reports()
    return {"total": len(items), "items": items[:limit]}


@router.get("/reports/{run_id}")
async def report_detail(
    run_id: str, compare_with: Optional[str] = Query("auto")
) -> Dict[str, Any]:
    """一份报告的完整详情（含逐条用例与对比结论）。

    ``compare_with``：``auto``（默认）= 自动找**早于本报告**的最近一次完整运行；给 run_id =
    跟那份指定报告比（看「离起点还差多远」）；空串 = 不对比。

    这里**总是按参数重算**，不用报告里自带的那个 compare——否则页面上切换
    「对比对象」会毫无反应：参数传到了，但被「复用已有结果」短路掉了。
    重算只是内存里的集合运算，比读文件便宜得多。
    """
    try:
        data = R.load_report(run_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    out: Dict[str, Any] = dict(data)
    baseline = R.resolve_baseline(data, compare_with)
    out["compare"] = R.compare(data, baseline)
    return out


@router.get("/reports/{run_id}/markdown")
async def report_markdown(run_id: str) -> Dict[str, Any]:
    """报告的 Markdown 原文（前端一键复制到 PR 描述用）。"""
    try:
        data = R.load_report(run_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"run_id": run_id, "markdown": R.render_markdown(data)}


@router.post("/run")
async def trigger_run(
    agent_id: Optional[str] = Query(None, description="被测 agent；不传则自动取库里第一个"),
    suite: Optional[str] = Query(
        None, description="用例集名，多个用逗号分隔；不传则全部"
    ),
    case_timeout: int = Query(300, ge=30, le=1800),
    online: bool = Query(False, description="是否重新下载插件资源（默认离线）"),
    baseline: Optional[str] = Query(
        None,
        description=(
            "对比这份历史报告（run_id）而不是上一次。"
            "默认 latest=最近一次完整运行，看「本次改动的净效果」；"
            "指定一份固定报告则看「离起点还差多远」——后者才拦得住连续小退化"
        ),
    ),
) -> Dict[str, Any]:
    """触发一次评测（异步）。

    立刻返回 run_id，前端轮询 ``/evals/run/{run_id}`` 看进度。**不阻塞请求**：
    一轮十几条用例要跑十几分钟，同步等会把 HTTP 连接和 worker 都占死。
    """
    # 逗号分隔而不是重复参数：前端那边是拼一个 query 字符串，重复参数要
    # 自己处理序列化，逗号省事得多（用例集名本身不含逗号，不会歧义）
    suites = [s.strip() for s in (suite or "").split(",") if s.strip()]
    running = [r for r, s in _RUNNING.items() if s["status"] == "running"]
    if len(running) >= _MAX_CONCURRENT:
        raise HTTPException(
            status_code=409,
            detail=f"已有评测在运行（{running[0]}），等它跑完再触发，避免双倍成本。",
        )

    run_id = f"api-{R.new_run_id()}"
    cmd: List[str] = [
        sys.executable, "-m", "keeper.evals.run",
        "--run-id", run_id,
        "--case-timeout", str(case_timeout),
    ]
    if agent_id:
        cmd += ["--agent", agent_id]
    for s in suites:
        cmd += ["--suite", s]
    if online:
        cmd.append("--online")
    if baseline:
        cmd += ["--baseline", baseline]

    log_dir = RUNS_ROOT / "api-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run_id}.log"

    _RUNNING[run_id] = {
        "status": "running",
        "log": str(log_path),
        "cmd": cmd,
        "agent_id": agent_id,
        "suites": suites,
        "started_at": R.now_iso(),
        "returncode": None,
    }
    try:
        fh = log_path.open("w", encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        _RUNNING.pop(run_id, None)
        raise HTTPException(status_code=500, detail=f"无法创建日志文件：{e}") from e

    try:
        proc = subprocess.Popen(  # noqa: S603  命令由本模块拼装，无外部输入
            cmd,
            cwd=str(_PROJECT_ROOT),
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
    except Exception as e:  # noqa: BLE001
        fh.close()
        _RUNNING.pop(run_id, None)
        raise HTTPException(status_code=500, detail=f"启动评测进程失败：{e}") from e

    _RUNNING[run_id]["pid"] = proc.pid

    def _watch() -> None:
        rc = proc.wait()
        fh.close()
        st = _RUNNING.get(run_id)
        if st is None:
            return
        st["returncode"] = rc
        # 有报告才算成功：子进程可能连 agent 都没装载成功就退出了，
        # 而那种情况退出码未必非零——真正的判断依据是报告有没有落盘。
        st["status"] = "done" if R.has_report(run_id) else "failed"
        logger.info("评测 %s 结束：status=%s rc=%s", run_id, st["status"], rc)

    threading.Thread(target=_watch, name=f"eval-{run_id}", daemon=True).start()
    return {"run_id": run_id, "status": "running", "log": str(log_path)}


@router.get("/run/{run_id}")
async def run_status(run_id: str, tail: int = Query(60, ge=1, le=2000)) -> Dict[str, Any]:
    """一次运行的当前状态 + 日志尾部（前端轮询它显示进度）。"""
    st = _RUNNING.get(run_id)
    if st is None:
        # 进程重启后内存状态丢了，但报告还在——按报告判定
        if R.has_report(run_id):
            return {
                "run_id": run_id,
                "status": "done",
                "in_memory": False,
                "log_tail": "",
            }
        raise HTTPException(status_code=404, detail=f"没有这次运行：{run_id}")

    lines: List[str] = []
    p = Path(st["log"])
    if p.exists():
        try:
            lines = p.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()[-tail:]
        except Exception as e:  # noqa: BLE001
            logger.debug("读评测日志失败: %s", e)
    return {
        "run_id": run_id,
        "status": st["status"],
        "in_memory": True,
        "agent_id": st.get("agent_id"),
        "suites": st.get("suites"),
        "started_at": st.get("started_at"),
        "returncode": st.get("returncode"),
        "has_report": R.has_report(run_id),
        "log_tail": "\n".join(lines),
    }


@router.get("/running")
async def running() -> Dict[str, Any]:
    """当前正在跑的评测（前端进页面先问一次，避免重复触发）。"""
    return {
        "items": [
            {"run_id": rid, **{k: v for k, v in st.items() if k != "cmd"}}
            for rid, st in _RUNNING.items()
            if st["status"] == "running"
        ]
    }