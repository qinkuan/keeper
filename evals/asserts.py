"""断言判定：纯程序化，不引入第二个模型。

为什么不用 LLM judge：judge 换个模型 / 换个提示词，评分口径就变了，历史基线
全部作废；而且成本翻倍。评测最怕的就是**口径会漂**——漂了就无法比较，
无法比较的评测等于没有评测。所以判定只用两种依据：

- **答案内容**：该出现的字样出现了吗（行为型，稳）。
- **阈值**：步数 / token / 耗时有没有超出（回归护栏）。

每条失败都要给出**可执行的**说明（缺了什么、实际是什么、超了多少倍），
只报「不通过」的评测没人能拿来修。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .harness import CaseOutcome
from .spec import Asserts, EvalCase

# 单条断言失败信息的最大长度：报告要能给人看，太长会把 Markdown 撑爆
_DETAIL_LIMIT = 300


def _clip(s: Any, limit: int = _DETAIL_LIMIT) -> str:
    t = str(s)
    return t if len(t) <= limit else f"{t[:limit]}…（共 {len(t)} 字符）"


# 归一化时要剥掉的字符：markdown 强调符、引号的各种写法、中英文标点差异。
# 完全精确匹配会制造大量**假红**——模型写成「端口是 8080。」或「**8080**」
# 本该算命中，所以判定前先把这些纯排版差异抹平。
_NOISE = ("*", "`", "_", '"', "“", "”", "'", "「", "」", "（", "）", "(", ")")
_PUNCT = ("，", "。", "：", "；", "！", "？", ",", ".", ":", ";", "!", "?")


def _normalize(s: str) -> str:
    """归一化：剥掉排版噪声与标点，统一空白。"""
    for ch in _NOISE:
        s = s.replace(ch, "")
    for ch in _PUNCT:
        s = s.replace(ch, " ")
    return " ".join(s.split())


def _has(text: str, needle: str) -> bool:
    """包含判定，对排版差异留一点容差。"""
    hay = text.lower()
    nd = needle.lower()
    if nd in hay:
        return True
    nnd = _normalize(nd)
    return bool(nnd) and nnd in _normalize(hay)


def evaluate(
    case: EvalCase, out: CaseOutcome
) -> Tuple[bool, List[Dict[str, Any]]]:
    """对一条用例的产出做判定，返回 ``(是否通过, 失败明细)``。

    运行级异常（LLM 报错 / agent 未就绪）直接判失败，且**跳过其余断言**——
    基础设施故障时逐条报「答案不含 X」只会淹没真正的原因。
    """
    if case.skip:
        return True, []

    failures: List[Dict[str, Any]] = []
    a = case.asserts

    if out.error:
        return False, [{"assert": "run", "detail": _clip(f"运行异常：{out.error}")}]

    if not out.answer.strip():
        return False, [{"assert": "answer", "detail": "最终答案为空"}]

    _check_contains(a, out, failures)
    _check_tools(a, out, failures)
    _check_files(a, out, failures)
    _check_suspend(a, out, failures)
    _check_limits(a, out, failures)
    _check_isolation(case, out, failures)
    return (not failures), failures


def _check_files(
    a: Asserts, out: CaseOutcome, failures: List[Dict[str, Any]]
) -> None:
    """验证文件**真的**被写出来了，而不是只看 agent 自称写好了。

    自述与事实不一致正是要测的东西，所以这里查文件系统而不是查答案文本。
    """
    if not a.expect_files_created:
        return
    root = Path(out.workspace) if out.workspace else None
    if root is None:
        failures.append(
            {
                "assert": "expect_files_created",
                "detail": "用例没拿到工作空间路径，无法校验产出文件",
            }
        )
        return
    for rel in a.expect_files_created:
        p = (root / rel).resolve()
        # 路径越界的产出等于评测往任意目录写文件，必须挡住
        if not str(p).startswith(str(root.resolve())):
            failures.append(
                {"assert": "expect_files_created", "detail": f"产出文件路径越界：{rel}"}
            )
            continue
        if not p.is_file():
            failures.append(
                {
                    "assert": "expect_files_created",
                    "detail": f"工作空间里没有 {rel}（agent 自称写了，实际没有）",
                }
            )


def _check_isolation(
    case: EvalCase, out: CaseOutcome, failures: List[Dict[str, Any]]
) -> None:
    """只读用例的工作空间被改动 = 隔离失效，直接判失败。

    这是**基础设施的断言**，和被测能力无关：只读工作区是评测不污染真实文件的
    唯一保障，一旦被绕过，后面所有断言的可信度都归零。
    """
    if not case.read_only:
        return
    if out.metrics.get("workspace_mutated"):
        changed = out.metrics.get("changed_files") or []
        failures.append(
            {
                "assert": "read_only",
                "detail": (
                    "只读用例改动了工作空间："
                    + "、".join(str(c) for c in changed[:5])
                ),
            }
        )


def _check_contains(
    a: Asserts, out: CaseOutcome, failures: List[Dict[str, Any]]
) -> None:
    for pat in a.answer_matches:
        try:
            hit = re.search(pat, out.answer, re.IGNORECASE | re.DOTALL)
        except re.error as e:
            # 断言本身写错了要立刻暴露：静默跳过会让人以为「没匹配上」是模型的问题
            failures.append(
                {
                    "assert": "answer_matches",
                    "detail": f"用例里这个正则不合法：{pat}（{e}）",
                }
            )
            continue
        if not hit:
            failures.append(
                {
                    "assert": "answer_matches",
                    "detail": _clip(
                        f"答案里没有匹配 /{pat}/；实际答案：{out.answer_preview}"
                    ),
                }
            )
    for needle in a.answer_contains:
        if not _has(out.answer, needle):
            failures.append(
                {
                    "assert": "answer_contains",
                    "detail": _clip(
                        f"答案里找不到「{needle}」；实际答案：{out.answer_preview}"
                    ),
                }
            )
    for needle in a.answer_not_contains:
        if _has(out.answer, needle):
            failures.append(
                {
                    "assert": "answer_not_contains",
                    "detail": f"答案里不该出现「{needle}」",
                }
            )


def _check_tools(
    a: Asserts, out: CaseOutcome, failures: List[Dict[str, Any]]
) -> None:
    # 前缀匹配：MCP 工具名带 ``server__`` 前缀，插件改名也会改前缀，
    # 断言写前缀（如 ``github``）比写全名稳定。
    for want in a.must_call_tools:
        hit = any(
            t == want or t.startswith(want) for t in out.tool_calls
        )
        if not hit:
            failures.append(
                {
                    "assert": "must_call_tools",
                    "detail": _clip(
                        f"应当调用过工具「{want}」，实际调用："
                        f"{out.tool_calls or '（一次都没调）'}"
                    ),
                }
            )
    for bad in a.must_not_call:
        if any(t == bad or t.startswith(f"{bad}__") for t in out.tool_calls):
            failures.append(
                {
                    "assert": "must_not_call",
                    "detail": _clip(
                        f"不该调用工具「{bad}」，实际调用：{out.tool_calls}"
                    ),
                }
            )
    # 工具失败也算挂：「调了但没看结果」是真实的能力退化，只看答案会漏掉。
    #
    # 但**被安全策略拒绝的不算失败**——那正是期望行为（agent 尝试越界，
    # 守卫拦住了）。否则「拦住 = 工具报错 = 用例失败」，越界用例永远红着，
    # 而红的原因恰恰是防线在起作用。用 report 里的 error 文案区分这两者。
    terr = 0
    for c in out.metrics.get("tool_failures") or []:
        if "安全策略" in str(c):
            continue
        terr += 1
    if terr:
        failures.append(
            {"assert": "tool_ok", "detail": f"本轮有 {terr} 次工具调用失败"}
        )


def _check_suspend(
    a: Asserts, out: CaseOutcome, failures: List[Dict[str, Any]]
) -> None:
    if a.expect_suspend is None:
        return
    asked = bool(out.metrics.get("asked"))
    if a.expect_suspend and not asked:
        failures.append(
            {
                "assert": "expect_suspend",
                "detail": _clip(
                    "信息不足时应当向用户追问，实际直接给了答案：" + out.answer_preview
                ),
            }
        )
    elif not a.expect_suspend and asked:
        failures.append(
            {
                "assert": "expect_suspend",
                "detail": "本不该追问，却把问题抛回给用户了",
            }
        )


def _check_limits(
    a: Asserts, out: CaseOutcome, failures: List[Dict[str, Any]]
) -> None:
    m = out.metrics
    checks = (
        (a.max_steps, "steps", "步数"),
        (a.max_tokens, "total_tokens", "token"),
        (a.max_duration_ms, "duration_ms", "耗时"),
    )
    for limit, key, label in checks:
        if limit is None:
            continue
        actual = int(m.get(key) or 0)
        if actual > limit:
            # 给倍数而不只是绝对差：超 1.2 倍和超 5 倍是完全不同的两件事
            ratio = (actual / limit) if limit else 0
            failures.append(
                {
                    "assert": f"max_{key}",
                    "detail": (
                        f"{label}超预算：实际 {actual} / 上限 {limit}"
                        f"（{ratio:.1f} 倍）"
                    ),
                }
            )


def summarize(case: EvalCase) -> str:
    """人读的一句话：这条用例在测什么（报告与前端列表用）。"""
    bits: List[str] = []
    a = case.asserts
    if a.answer_contains:
        bits.append("答案含 " + "/".join(a.answer_contains[:3]))
    if a.answer_matches:
        bits.append("答案匹配 /" + "/".join(a.answer_matches[:2]) + "/")
    if a.must_call_tools:
        bits.append("调用 " + "/".join(a.must_call_tools))
    if a.expect_suspend:
        bits.append("应追问")
    if a.max_steps:
        bits.append(f"≤{a.max_steps} 步")
    return "；".join(bits)