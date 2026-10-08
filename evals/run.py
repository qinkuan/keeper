"""评测命令行入口。

    # 跑默认 smoke 用例集（自动选库里第一个 active agent）
    python -m keeper.evals.run

    # 指定 agent / 用例集
    python -m keeper.evals.run --agent <id> --suite smoke

    # 只看某个历史报告
    python -m keeper.evals.run --show latest

    # 只比不跑（对最近两次报告做 diff）
    python -m keeper.evals.run --compare-only 20261004-101500

**必须在独立进程里跑**（这就是它是 CLI 而不是 API 的原因）：SQLAlchemy 引擎
是进程级全局单例，评测需要把库切到临时副本；服务进程里那个引擎已经绑在真实库
上，切不过去，也不该切。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# 支持直接运行本文件（``python keeper/evals/run.py``）：脚本所在目录
# ``keeper/evals`` 会进入 sys.path，先摘掉再把项目根插到最前。
_HERE = str(Path(__file__).resolve().parent)
_ROOT = str(Path(__file__).resolve().parents[2])
sys.path[:] = [p for p in sys.path if p not in (_HERE, str(Path(_HERE).parent))]
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from keeper.evals import report as R  # noqa: E402
from keeper.evals.runner import DEFAULT_CASE_TIMEOUT, run_eval  # noqa: E402


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # 评测的进度输出在 stdout 上逐条打印；LLM 用量明细、每次 HTTP 请求、
    # chat.service 的 [dbg] 追踪会把它冲得看不见——评测时这些全是噪音，
    # 真要看细节就加 -v。
    for noisy in (
        "httpx", "httpx2", "httpcore", "mcp", "urllib3",
        "keeper.llm.client", "keeper.chat.service", "keeper.tools.collect",
        "keeper.agent.process",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _make_printer() -> "callable":
    """把进度打成一串行，扫一眼就知道跑到哪了。"""
    def _p(phase: str, payload: dict) -> None:
        if phase == "fingerprint":
            fp = payload["fingerprint"]
            print(
                f"被测配置：模型={fp.get('model')} 工具={fp.get('tool_count')}"
                f" 技能={fp.get('skill_count')} persona={fp.get('persona_chars')}字",
                flush=True,
            )
        elif phase == "case_start":
            print(
                f"\n[{payload['index']}/{payload['total']}] {payload['case_id']}"
                f"　{payload.get('title') or ''}",
                flush=True,
            )
        elif phase == "case_done":
            m = payload.get("metrics") or {}
            mark = "✓" if payload.get("passed") else "✗"
            print(
                f"  {mark} {m.get('steps', '?')} 步 / {m.get('total_tokens', 0)} token"
                f" / {int(m.get('duration_ms') or 0) // 1000}s"
                f" / 工具 {m.get('tool_calls', 0)} 次"
                f" / 能力加载 {m.get('capability_loads', 0)} 次",
                flush=True,
            )
            for f in payload.get("failures") or []:
                print(f"      · {f.get('assert')}：{f.get('detail')}", flush=True)
        elif phase == "done":
            s = payload.get("summary") or {}
            print(
                f"\n完成：{s.get('passed')}/{s.get('total')} 通过，"
                f"报告已写入 {R.BASELINES_DIR}",
                flush=True,
            )

    return _p


async def _show(name: str) -> None:
    data = R.load_report(name)
    print(R.render_markdown(data))


async def _compare_only(run_id: str) -> None:
    """拿某份历史报告跟上一次 latest 比——用于回看某次改动到底带来了什么。"""
    cur = R.load_report(run_id)
    if not R.has_report("latest"):
        print("没有可比的 latest 基线。")
        return
    baseline = R.load_report("latest")
    if baseline.get("run_id") == cur.get("run_id"):
        print(f"{run_id} 就是当前 latest，没有可比的其它基线。")
        return
    print(R.compare(cur, baseline)["verdict"])


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="python -m keeper.evals.run",
        description="跑一轮效果评估，产出可 diff 的基线报告",
    )
    ap.add_argument("--agent", help="被测 agent id；不传则自动取库里第一个 active 的")
    ap.add_argument(
        "--suite", action="append", help="用例集名（可重复），默认 cases/ 下全部"
    )
    ap.add_argument(
        "--case",
        action="append",
        help="只跑指定用例 id（可重复）。调试单条用，省得每次烧十几轮 token",
    )
    ap.add_argument("--run-id", help="本次运行 id，默认用时间戳")
    ap.add_argument(
        "--case-timeout",
        type=int,
        default=DEFAULT_CASE_TIMEOUT,
        help=f"单条用例墙钟上限（秒），默认 {DEFAULT_CASE_TIMEOUT}",
    )
    ap.add_argument(
        "--baseline",
        help=(
            "对比这份历史报告（run_id）而不是上一次。"
            "默认对比 latest=最近一次完整运行，用于评估「本次改动的净效果」；"
            "想看「离起点还差多远」就指定一份固定报告——"
            "它是唯一能发现连续小退化的手段（每次对比上次都 ±0，"
            "其实已经比起点掉了两条)"
        ),
    )
    ap.add_argument(
        "--no-compare", action="store_true", help="不与任何基线对比"
    )
    ap.add_argument(
        "--online",
        action="store_true",
        help="装载时重新下载插件资源（默认离线，避免评测受网络影响）",
    )
    ap.add_argument("--source-db", help="被复制的源库路径，默认 keeper/data/keeper.db")
    ap.add_argument("--show", help="只打印某份历史报告（run_id 或 latest）后退出")
    ap.add_argument("--compare-only", help="把该历史报告与 latest 对比后退出")
    ap.add_argument("-v", "--verbose", action="store_true", help="打开 debug 日志")
    args = ap.parse_args()

    _setup_logging(args.verbose)

    if args.show:
        asyncio.run(_show(args.show))
        return
    if args.compare_only:
        asyncio.run(_compare_only(args.compare_only))
        return

    async def _run() -> int:
        # --baseline 显式指定 > 默认 latest（= 上一次完整运行）。
        # 两者回答不同问题：默认看「本次改动的净效果」，
        # 指定基线看「离那个被认可的起点还差多远」。
        compare_with = None if args.no_compare else (args.baseline or "auto")
        report = await run_eval(
            agent_id=args.agent,
            suites=args.suite,
            case_ids=args.case,
            run_id=args.run_id,
            offline=not args.online,
            case_timeout=args.case_timeout,
            compare_with=compare_with,
            source_db=Path(args.source_db) if args.source_db else None,
            on_progress=_make_printer(),
        )
        cmp_ = report.get("compare") or {}
        if cmp_.get("has_baseline"):
            label = "上次" if not args.baseline else "基线"
            print(
                f"\n对比{label} {cmp_.get('baseline_run_id')}："
                f"{cmp_.get('verdict')}"
            )
        else:
            print(f"\n{cmp_.get('verdict')}")
        s = report.get("summary") or {}
        # 有回归就以非零退出：这样以后真接 CI 时不用改判定逻辑
        broken = [
            c["case_id"]
            for c in (cmp_.get("cases") or [])
            if c.get("status") == "broken"
        ]
        if broken:
            print(f"存在回归用例：{'、'.join(broken)}")
            return 1
        if s.get("failed"):
            print(f"有 {s['failed']} 条用例未通过（详见报告）")
        return 0

    code = asyncio.run(_run())
    # 注意：这里**不** dispose_engine 之外的清理——harness.teardown 已释放。
    # 退出码保留给 CI 用；os._exit 会跳过日志 flush，用正常的 return 即可。
    sys.exit(code)


if __name__ == "__main__":
    main()