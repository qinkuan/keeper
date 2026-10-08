"""报告：可 diff 的 JSON + 人看的 Markdown + 与上次基线的对比。

两层产物，职责不同：

- ``baselines/<run_id>.json``：**唯一真相源**，机器读、前端读、对比读。
- ``baselines/<run_id>.md``：给人看（能贴进 commit message / PR 描述）。
- ``baselines/latest.json``：给「跟上次比」用。不设软链是因为跨平台软链处理麻烦，
  且前端要展示的是历史列表，不是某个软链的目标。

报告里**必须**带配置指纹（模型 / 工具清单 / 技能清单）。少了它，两次运行的
数字不可比——而不可比的数字比没有数字更危险，它会让人得出错误结论。
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .asserts import summarize
from .harness import BASELINES_DIR, CaseOutcome
from .spec import EvalCase

LATEST = "latest.json"


def new_run_id() -> str:
    """运行 id：本地时间戳到秒。人类可读且天然按时间排序。"""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- 构建


def case_result(
    case: EvalCase, out: CaseOutcome, passed: bool, failures: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """单条用例的最终结果结构（JSON / Markdown / 前端共用这一份）。"""
    return {
        "case_id": case.id,
        "title": case.display_name,
        "tags": list(case.tags),
        "summary": summarize(case),
        "input": case.input,
        "skipped": case.skip,
        "skip_reason": case.skip_reason,
        "passed": passed,
        "failures": failures,
        "error": out.error,
        "metrics": out.metrics,
        "tool_calls": out.tool_calls,
        "answer_preview": out.answer_preview,
    }


def build_report(
    *,
    run_id: str,
    agent_id: str,
    fingerprint: Dict[str, Any],
    results: List[Dict[str, Any]],
    started_at: str,
    finished_at: str,
    duration_ms: int,
    suites: Optional[List[str]] = None,
    selection: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把逐条结果汇总成一份完整报告（含 summary 聚合）。"""
    cases = [r for r in results if not r.get("skipped")]
    total = len(cases)
    passed = sum(1 for r in cases if r.get("passed"))

    def avg(key: str) -> float:
        vals = [float((r.get("metrics") or {}).get(key) or 0) for r in cases]
        return round(sum(vals) / len(vals), 1) if vals else 0.0

    def ssum(key: str) -> int:
        return int(sum(float((r.get("metrics") or {}).get(key) or 0) for r in cases))

    costs = [
        (r.get("metrics") or {}).get("cost")
        for r in cases
        if (r.get("metrics") or {}).get("cost") is not None
    ]
    # 只有**每条都算出成本**才给合计：少一条还报合计会让人以为总额可信
    total_cost = (
        round(sum(float(c) for c in costs), 6)
        if costs and len(costs) == total
        else None
    )

    return {
        "schema": 1,
        "run_id": run_id,
        "agent_id": agent_id,
        "suites": suites or [],
        # 本次**跑的范围**。必须有它，否则对比分不清「用例被删了」和
        # 「这次只跑了一条」——两者在报告里长得一模一样（都不在 cases 里）。
        # 混淆的后果很实在：会让人以为用例集被改过，或者以为漏跑了 15 条。
        "selection": selection or {"mode": "all", "case_ids": [], "suites": []},
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "fingerprint": fingerprint,
        "summary": {
            "total": total,
            "skipped": sum(1 for r in results if r.get("skipped")),
            "passed": passed,
            "failed": total - passed,
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "avg_steps": avg("steps"),
            "avg_tokens": avg("total_tokens"),
            "avg_duration_ms": avg("duration_ms"),
            "total_tokens": ssum("total_tokens"),
            "total_cost": total_cost,
            # 「跑挂了」要和「答错了」分开统计：两者的修法完全不同
            "error_cases": sum(1 for r in cases if r.get("error")),
            "asked_cases": sum(
                1 for r in cases if (r.get("metrics") or {}).get("asked")
            ),
            "budget_stopped_cases": sum(
                1 for r in cases if (r.get("metrics") or {}).get("budget_stopped")
            ),
            "capability_loads": ssum("capability_loads"),
            "tool_calls": ssum("tool_calls"),
        },
        "cases": results,
    }


# ---------------------------------------------------------------- 落盘


def write_report(
    report: Dict[str, Any],
    directory: Optional[Path] = None,
    update_latest: Optional[bool] = None,
) -> Path:
    """写 JSON + Markdown，并按需更新 ``latest.json``。返回 JSON 路径。

    ``latest`` 的语义是「最近一次**完整**基线」，所以选集运行（``--case`` /
    ``--suite``）默认**不**动它。否则调试跑一条就会把 latest 换成残缺的那份，
    页面上默认展示的东西也跟着变残缺——而那正是「latest 看起来只有一条」的由来。
    报告文件照样落盘，随时能从历史下拉框里选出来看。
    """
    d = Path(directory or BASELINES_DIR)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{report['run_id']}.json"
    p.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (d / f"{report['run_id']}.md").write_text(
        render_markdown(report), encoding="utf-8"
    )
    if update_latest is None:
        update_latest = (report.get("selection") or {}).get("mode") == "all"
    if update_latest:
        shutil.copy2(p, d / LATEST)
    return p


def load_report(name: str, directory: Optional[Path] = None) -> Dict[str, Any]:
    """按 run_id（或 ``latest``）读一份报告。"""
    d = Path(directory or BASELINES_DIR)
    fn = LATEST if name in ("latest", "latest.json") else name
    if not fn.endswith(".json"):
        fn = f"{fn}.json"
    p = d / fn
    if not p.exists():
        raise FileNotFoundError(f"评测报告不存在：{p}")
    return json.loads(p.read_text(encoding="utf-8"))


def has_report(name: str, directory: Optional[Path] = None) -> bool:
    d = Path(directory or BASELINES_DIR)
    fn = name if name.endswith(".json") else f"{name}.json"
    return (d / fn).exists()


def latest_full_run(
    earlier_than: Optional[str] = None, directory: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """最近的**完整**运行报告（时间倒序第一个）。

    ``earlier_than`` 传 ISO 时间戳时，只考虑**更早于**它的那些。

    跳过选集运行（``--case`` 跑出来的只有一两条）：拿它当基线会得出
    「少测了十几条」这种结论。老报告没有 ``selection`` 字段，按全量处理。
    """
    d = Path(directory or BASELINES_DIR)
    cur_at = str(earlier_than or "")
    for item in list_reports(d):
        if item.get("mode") == "subset":
            continue
        if cur_at and str(item.get("finished_at") or "") >= cur_at:
            continue
        try:
            return load_report(str(item["run_id"]), d)
        except FileNotFoundError:
            continue
    return None


def previous_full_run(
    current: Dict[str, Any], directory: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """「上一次」= **时间上早于** current 的、最近一次**完整**运行。

    为什么不能直接读 ``latest.json``：刚跑完那刻，latest 就是这份报告自己，
    于是「对比上一次」变成自己跟自己比——看着像有基线，其实毫无意义。

    为什么条件必须是「早于」而不能只是「不等于自己」：查看一份三天前的老
    报告时，按时间倒序遇到的第一个「不是自己」是**更新的**那份，结论方向
    整个反过来（明明是现在更好，却报成「回归 N 条」）。
    """
    d = Path(directory or BASELINES_DIR)
    got = latest_full_run(str(current.get("finished_at") or ""), d)
    if got is not None and got.get("run_id") == current.get("run_id"):
        return None
    return got


def resolve_baseline(
    current: Dict[str, Any],
    compare_with: Optional[str],
    directory: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """把「跟谁比」这个参数解析成一份具体的基线报告；None 表示不对比。

    ``compare_with`` 的取值：

    - ``None`` / ``""``        不对比
    - ``"auto"``（**默认**）    自动找「上一次完整运行」（见 previous_full_run）
    - ``"latest"``             显式读 ``latest.json``（几乎总是自己，仅兼容）
    - 其它字符串                 当作 run_id 精确指定
    """
    if not compare_with:
        return None
    if compare_with == "auto":
        return previous_full_run(current, directory)
    if compare_with == "latest":
        if not has_report("latest", directory):
            return None
        data = load_report("latest", directory)
        # latest 指向自己时降级为「自动往前找」，否则自己跟自己比没意义
        if data.get("run_id") == current.get("run_id"):
            return previous_full_run(current, directory)
        return data
    try:
        data = load_report(compare_with, directory)
    except FileNotFoundError:
        return None
    if data.get("run_id") == current.get("run_id"):
        return None
    return data


def list_reports(directory: Optional[Path] = None) -> List[Dict[str, Any]]:
    """列出历史报告的元信息（时间倒序），供前端渲染历史列表。"""
    d = Path(directory or BASELINES_DIR)
    if not d.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    # 先按 mtime 粗排（只是为了少读几个文件）；最后统一按 finished_at 精排——
    # mtime 会被复制 / 迁移改写，finished_at 才是权威时间。
    for p in sorted(
        d.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True
    ):
        if p.name == LATEST:
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001  坏文件不该让整个列表打不开
            continue
        s = data.get("summary") or {}
        sel = data.get("selection") or {}
        out.append(
            {
                "run_id": data.get("run_id"),
                "agent_id": data.get("agent_id"),
                "finished_at": data.get("finished_at"),
                # 老报告没有 selection 字段，一律当全量（向后兼容）
                "mode": sel.get("mode") or "all",
                "total": s.get("total"),
                "passed": s.get("passed"),
                "failed": s.get("failed"),
                "pass_rate": s.get("pass_rate"),
                "avg_steps": s.get("avg_steps"),
                "avg_tokens": s.get("avg_tokens"),
                "avg_duration_ms": s.get("avg_duration_ms"),
                "model": (data.get("fingerprint") or {}).get("model"),
            }
        )
    # 必须按 **finished_at** 排，不能按 run_id：run_id 有三种前缀
    # （``baseline-`` / ``api-`` / 纯时间戳），字典序和时间序完全对不上
    # （'b' > 'a' > '2'），按它排会得到一份时间乱序的列表——而
    # 「找上一次完整运行」正是靠这个顺序，会直接跳过中间那次运行。
    out.sort(key=lambda r: str(r.get("finished_at") or ""), reverse=True)
    return out


# ---------------------------------------------------------------- 对比

# 参与对比的汇总指标：都是「越小越好」，delta 为负 = 变好
_COMPARE_KEYS = ("avg_steps", "avg_tokens", "avg_duration_ms")


def compare(
    current: Dict[str, Any], baseline: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """与一份基线报告对比，输出**可执行的**结论。

    最关键的一条是 ``fingerprint_changed``：配置变了（比如换了模型）时，
    指标 delta 完全不能归因给代码改动。必须先说清这一点，否则会拿着
    「换了模型所以 token 降了」的假结论去庆祝代码优化。
    """
    if not baseline:
        return {
            "has_baseline": False,
            "baseline_run_id": None,
            "fingerprint_changed": [],
            "summary": {},
            "cases": [],
            "removed": [],
            "verdict": "首次运行，没有可比基线。",
        }

    fp_changes = _fingerprint_diff(
        baseline.get("fingerprint") or {}, current.get("fingerprint") or {}
    )
    cs = baseline.get("summary") or {}
    ns = current.get("summary") or {}
    summary: Dict[str, Any] = {
        "pass_rate": _delta(cs.get("pass_rate"), ns.get("pass_rate")),
        "passed": _delta(cs.get("passed"), ns.get("passed")),
    }
    for k in _COMPARE_KEYS:
        summary[k] = _delta(cs.get(k), ns.get(k))

    base_cases = {c["case_id"]: c for c in (baseline.get("cases") or [])}
    cur_cases = {c["case_id"]: c for c in (current.get("cases") or [])}
    rows: List[Dict[str, Any]] = []
    for cid, cur in cur_cases.items():
        base = base_cases.get(cid)
        if cur.get("skipped"):
            status = "skipped"
        elif base is None:
            status = "new"
        elif base.get("passed") and not cur.get("passed"):
            status = "broken"   # 回归：原来能过，现在过不了
        elif not base.get("passed") and cur.get("passed"):
            status = "fixed"    # 修好了
        else:
            status = "kept"
        tk = (cur.get("metrics") or {}).get("total_tokens")
        td = None
        if base and base.get("metrics") and not cur.get("skipped"):
            td = _delta(base["metrics"].get("total_tokens"), tk)
        rows.append(
            {
                "case_id": cid,
                "title": cur.get("title"),
                "status": status,
                "failures": cur.get("failures") or [],
                "tokens": tk,
                "tokens_delta": td,
                "steps": (cur.get("metrics") or {}).get("steps"),
            }
        )
    # 已从用例集里删掉的用例也要报出来，否则「少测了 3 条」会被当成「都过了」
    removed = [cid for cid in base_cases if cid not in cur_cases]
    # 选集运行（--case / --suite）时，上面这些「消失」的用例只是**没跑**，
    # 不是被删了。这两种情况必须分开说，否则一次单条调试就会让人以为
    # 用例集被改过、或以为自己漏跑了十几条。
    subset = (current.get("selection") or {}).get("mode") == "subset"

    broken = [r for r in rows if r["status"] == "broken"]
    fixed = [r for r in rows if r["status"] == "fixed"]
    added = [r for r in rows if r["status"] == "new"]
    parts: List[str] = []
    if subset:
        parts.append(f"选集运行，本次只跑了 {len(cur_cases)} 条")
    if broken:
        parts.append(
            f"回归 {len(broken)} 条：" + "、".join(r["case_id"] for r in broken)
        )
    if fixed:
        parts.append(
            f"修复 {len(fixed)} 条：" + "、".join(r["case_id"] for r in fixed)
        )
    # 「新增」只有全量运行时才有意义：选集运行时那只是「这次刚好跑了它」
    if added and not subset:
        parts.append(f"用例集新增 {len(added)} 条：" + "、".join(r["case_id"] for r in added))
    if removed:
        if subset:
            parts.append(f"另有 {len(removed)} 条未参与本次对比")
        else:
            parts.append(
                f"用例集删掉 {len(removed)} 条：" + "、".join(removed)
            )
    if not parts:
        parts.append("相对基线无状态变化")
    if fp_changes:
        parts.append(
            "注意：被测配置变了（"
            + "；".join(fp_changes)
            + "），指标 delta 不能直接归因给代码改动"
        )

    return {
        "has_baseline": True,
        "baseline_run_id": baseline.get("run_id"),
        "fingerprint_changed": fp_changes,
        "summary": summary,
        "cases": rows,
        "removed": removed,
        "verdict": "；".join(parts),
    }


def _delta(before: Any, after: Any) -> Optional[Dict[str, Any]]:
    if before is None or after is None:
        return None
    try:
        b, a = float(before), float(after)
    except (TypeError, ValueError):
        return None
    return {"from": b, "to": a, "delta": round(a - b, 4)}


def _fingerprint_diff(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    """列出指纹里发生变化的项。

    只比「人看得懂」的聚合字段加上工具/技能的名单变化，不做全字段 diff——
    后者会产生一堆噪音，把真正该看的变化淹没掉。
    """
    out: List[str] = []
    for key in (
        "model", "provider", "profile_id",
        "tool_count", "skill_count", "persona_chars",
    ):
        b, a = before.get(key), after.get(key)
        if b is None and a is None:
            continue
        if b != a:
            out.append(f"{key}: {b} → {a}")
    for key, label in (("tools", "工具"), ("skills", "技能")):
        bs = set(before.get(key) or [])
        as_ = set(after.get(key) or [])
        if bs and as_ and bs != as_:
            bits = []
            if as_ - bs:
                bits.append("+" + ",".join(sorted(as_ - bs)[:5]))
            if bs - as_:
                bits.append("-" + ",".join(sorted(bs - as_)[:5]))
            out.append(f"{label}变化：{' '.join(bits)}")
    return out


# ---------------------------------------------------------------- Markdown


def render_markdown(report: Dict[str, Any]) -> str:
    """人读报告。刻意保持短——没人愿意读三页 Markdown。"""
    s = report.get("summary") or {}
    fp = report.get("fingerprint") or {}
    L: List[str] = []
    L.append(f"# 评测报告 {report.get('run_id')}")
    L.append("")
    L.append(
        f"- 被测 agent：`{report.get('agent_id')}`"
        f"　模型：`{fp.get('model') or '—'}`"
        f"　工具 {fp.get('tool_count', 0)} 个 / 技能 {fp.get('skill_count', 0)} 个"
    )
    L.append(
        f"- 结果：**{s.get('passed')}/{s.get('total')} 通过**"
        f"（{_pct(s.get('pass_rate'))}）"
        f"　平均 {_num(s.get('avg_steps'))} 步 /"
        f" {_int(s.get('avg_tokens'))} token /"
        f" {int(s.get('avg_duration_ms') or 0) // 1000}s"
    )
    if s.get("total_cost") is not None:
        L.append(f"- 成本合计：{s.get('total_cost')}")
    if s.get("error_cases"):
        L.append(f"- 有 {s['error_cases']} 条运行级异常（不是答错，是跑挂了）")
    L.append("")

    failed = [
        c for c in (report.get("cases") or [])
        if not c.get("passed") and not c.get("skipped")
    ]
    if failed:
        L.append("## 失败明细")
        L.append("")
        for c in failed:
            L.append(f"### ✗ {c['case_id']}　{c.get('title')}")
            L.append(f"- 输入：{_oneline(c.get('input'))}")
            for f in c.get("failures") or []:
                L.append(f"- **{f.get('assert')}**：{_oneline(f.get('detail'), 200)}")
            L.append("")

    L.append("## 全部用例")
    L.append("")
    L.append("| 状态 | 用例 | 判定依据 | 步 | token | 耗时 |")
    L.append("|---|---|---|---|---|---|")
    for c in report.get("cases") or []:
        st = "跳过" if c.get("skipped") else ("✓" if c.get("passed") else "✗")
        m = c.get("metrics") or {}
        if c.get("error"):
            why = "运行异常"
        elif not c.get("passed") and c.get("failures"):
            why = (c["failures"][0] or {}).get("detail", "")
        else:
            why = c.get("summary") or ""
        L.append(
            f"| {st} | `{c.get('case_id')}` | {_oneline(why)} | "
            f"{m.get('steps', '—')} | {m.get('total_tokens', '—')} | "
            f"{int(m.get('duration_ms') or 0) // 1000}s |"
        )
    L.append("")
    return "\n".join(L)


def _oneline(s: Any, limit: int = 80) -> str:
    t = str(s or "").replace("|", "\\|").replace("\n", " ")
    return t if len(t) <= limit else t[:limit] + "…"


def _pct(v: Any) -> str:
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _num(v: Any) -> str:
    try:
        return f"{float(v):.1f}"
    except (TypeError, ValueError):
        return "—"


def _int(v: Any) -> str:
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "—"