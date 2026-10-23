"""ReAct 文本协议解析的回归脚本（直接 `python keeper/tests/test_planner_parse.py` 跑）。

为什么是脚本而不是 pytest 用例
----------------------------
跟 ``keeper/tools/test_guard.py`` 保持一致：本仓的"测试"就是能直接 ``python``
跑完就出结论的脚本，不引测试框架依赖。

覆盖的三个坑（都是实测踩出来的，不是假想）
------------------------------------------
① **模型自己伪造 OBSERVATION**。OBSERVATION 由引擎注入，模型不该写，但它经常
   自己把一整轮 ReAct（THOUGHT → ACTION → ACTION_INPUT → OBSERVATION → 再一轮）
   一次吐完，像在扮演引擎。不在 OBSERVATION 处切断，伪造内容会被当成
   ACTION_INPUT 的续行吞进 JSON，表现为"参数解析失败"——排查方向会被带偏成
   "模型写错了参数"。
② **一次连写多个 ACTION**。``parse_actions`` 必须把它们都取出来，且各自的
   ACTION_INPUT 不能互相吞。``parse``（单动作路径）也用同一套切分。
③ **准则 4 必须跟着 tool_parallel 走**。写死成"只写一个"会让开了并行的引擎
   永远收不到多个 ACTION，等于功能没开。
"""
from __future__ import annotations

import os
import sys

# tests/ → keeper/ → 项目根（keeper 包的父目录）
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from keeper.agent.planner import ReActPlanner  # noqa: E402

FAILED: list = []


def check(ok: bool, label: str, got=None) -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  got={got!r}"))
    if not ok:
        FAILED.append(label)


# ── ① 伪造 OBSERVATION ────────────────────────────────────────────────────
FAKE = """THOUGHT: 我先写文件再校验。
ACTION: fs.write_file
ACTION_INPUT: {"path": "index.html", "content": "<html></html>"}
OBSERVATION: 已写入 index.html，共 18 字节
THOUGHT: 现在跑校验脚本。
ACTION: bash
ACTION_INPUT: {"command": "python check.py"}
OBSERVATION: exit 0
"""

p = ReActPlanner()

d = p.parse(FAKE, 1)
# 策略（见 detect_hallucination）：模型自编 OBSERVATION / 连写多个 THOUGHT =
# 它在扮演引擎，**整轮作废**，不再截断后执行第一个动作。
# 原因：它后面那句「已写入 index.html」说明它已默认前一个 ACTION 成功，
# 顺着执行只会让它在错误的世界观上继续推。
check(d.kind == "hallucination", "伪造 OBSERVATION 时整轮作废", d.kind)
check(bool(d.hallucination), "幻觉原因非空", d.hallucination)
check(d.tool is None, "作废时不带工具", d.tool)
check(
    d.thought == "我先写文件再校验。",
    "THOUGHT 不延伸到伪造的下一轮",
    d.thought,
)
# 作废不意味着「解析能力」退化：连续跑飞到上限时会退化成截断后执行，
# 那时仍必须能从截断片段里还原出第一个动作与参数。
_cut = FAKE[: FAKE.index("OBSERVATION")]
d_cut = p.parse(_cut, 1)
check(d_cut.kind == "act", "截断后仍解析为 act", d_cut.kind)
check(d_cut.tool == "fs.write_file", "截断后工具名不被伪造段污染", d_cut.tool)
check(
    d_cut.args == {"path": "index.html", "content": "<html></html>"},
    "ACTION_INPUT 在 OBSERVATION 处被切断",
    d_cut.args,
)

acts = p.parse_actions(FAKE)
check(
    acts == [
        ("fs.write_file", {"path": "index.html", "content": "<html></html>"}),
        ("bash", {"command": "python check.py"}),
    ],
    "parse_actions 拆出两个动作且参数各自干净",
    acts,
)

# 单行内塞标签（模型不换行时）
d2 = p.parse("THOUGHT: 查一下\nACTION: fs.list_dir\nACTION_INPUT: {\"path\": \".\"}\n", 1)
check(d2.tool == "fs.list_dir", "单动作不受影响", d2.tool)
check(d2.args == {"path": "."}, "单动作参数正确", d2.args)

# ── ② FINAL / ASK 段尾切干净 ──────────────────────────────────────────────
# FINAL 之后又自编 OBSERVATION：同样整轮作废（FINAL 优先于 ACTION 的老洞
# 会让这种响应被当成正常回答，工具一个都不执行）
d3 = p.parse("THOUGHT: 想清楚了\nFINAL: 建议这样安排\nOBSERVATION: 编的\n", 1)
check(d3.kind == "hallucination", "FINAL 后自编 OBSERVATION 也整轮作废", d3.kind)

# 没有伪造的 FINAL 才正常：正文要能干净抠出来。
# 尾标签用 ACTION（ASK/FINAL/ANSWER 优先级更高，会先被别的分支截走；
# OBSERVATION / 第二个 THOUGHT 现在判幻觉）——正好验证 _TAIL_LABEL 的切断。
d3b = p.parse("THOUGHT: 想清楚了\nFINAL: 建议这样安排\nACTION: fs.list_dir\n", 1)
check(
    d3b.kind == "final" and d3b.answer == "建议这样安排",
    "FINAL 正文不被后续标签污染",
    d3b.answer,
)

d4 = p.parse("THOUGHT: 需要你定\nASK: 你想用哪个？\nOPTIONS: [\"A\", \"B\"]\n", 1)
check(d4.kind == "ask" and d4.ask == "你想用哪个？", "ASK 正文正确", d4.ask)
check(d4.options == ["A", "B"], "OPTIONS 正确", d4.options)

# ── ③ 准则 4 跟随 tool_parallel ───────────────────────────────────────────
from keeper.agent.planner import _action_rule  # noqa: E402

off = _action_rule()
check("只写一个 ACTION" in off, "默认（并行关闭）要求单动作", off)

import keeper.agent.planner as planner_mod  # noqa: E402

_orig = planner_mod._tool_parallel_mode
planner_mod._tool_parallel_mode = lambda: 1
try:
    on = _action_rule()
finally:
    planner_mod._tool_parallel_mode = _orig
check("最多" in on and "只写一个 ACTION" not in on, "并行开启时允许多动作", on)


# ── ④ XML 调用壳兼容 ────────────────────────────────────────────────────────
# 模型偶尔把工具调用包成 ``<action> 工具名 {JSON} </action>``（实测出现过）。
# 这类输出必须当正常 ACTION 执行，而不是当最终回答漏给用户、也不是整轮作废。
# 注意：这里用拼接构造标签字面量，避免测试脚本自身的文本被当成被测输入的一部分。
_LT, _GT = chr(60), chr(62)
_TAG = _LT + "action" + _GT
_END = _LT + "/action" + _GT
_TAG_TC = _LT + "tool_call" + _GT
_END_TC = _LT + "/tool_call" + _GT

XML1 = f"{_TAG} read " + '{"block_id":"ctx:8130f1bf6d"} ' + _END
dx = p.parse(XML1, 1)
check(dx.kind == "act", "XML 调用壳解析为 act", dx.kind)
check(dx.tool == "read", "XML 调用壳工具名正确", dx.tool)
check(
    dx.args == {"block_id": "ctx:8130f1bf6d"},
    "XML 调用壳参数正确",
    dx.args,
)

# 多行变体：壳里换行也要认
XML2 = (
    f"{_TAG}\n  fs.write_file\n  "
    + '{"path": "a.txt", "content": "hi"}'
    + f"\n{_END}"
)
dx2 = p.parse(XML2, 1)
check(dx2.kind == "act", "多行 XML 壳解析为 act", dx2.kind)
check(dx2.tool == "fs.write_file", "多行 XML 壳工具名正确", dx2.tool)
check(
    dx2.args == {"path": "a.txt", "content": "hi"},
    "多行 XML 壳参数正确",
    dx2.args,
)

# 无参数：工具名后面没 JSON，参数应为空 dict（不是 {"_raw": ...}）
XML3 = f"{_TAG} fs.list_dir {_END}"
dx3 = p.parse(XML3, 1)
check(dx3.kind == "act" and dx3.tool == "fs.list_dir", "无参 XML 壳可执行", dx3.tool)
check(dx3.args == {}, "无参 XML 壳参数为空", dx3.args)

# tool_call 壳名同样认
XML4 = f"{_TAG_TC} read " + '{"block_id":"ctx:x"} ' + _END_TC
dx4 = p.parse(XML4, 1)
check(
    dx4.kind == "act" and dx4.tool == "read" and dx4.args == {"block_id": "ctx:x"},
    "tool_call 壳同样解析为 act",
    (dx4.tool, dx4.args),
)

# 文本协议优先：同时出现时仍以 ACTION: 为准（XML 兼容是兜底，不是抢优先级）
XML5 = (
    "THOUGHT: 查一下\nACTION: bash\nACTION_INPUT: "
    + '{"command": "ls"}'
    + f"\n{_TAG} read "
    + '{"block_id":"ctx:y"} '
    + _END
)
dx5 = p.parse(XML5, 1)
check(dx5.tool == "bash", "文本协议优先于 XML 壳", dx5.tool)
check(dx5.args == {"command": "ls"}, "文本协议参数不被 XML 壳污染", dx5.args)

# 非 JSON 参数走兜底（{"_raw": ...}），不丢整个调用
XML6 = f"{_TAG} bash " + "ls -la " + _END
dx6 = p.parse(XML6, 1)
check(dx6.kind == "act" and dx6.tool == "bash", "非 JSON 参数的 XML 壳仍可执行", dx6.tool)
check("_raw" in dx6.args, "非 JSON 参数走 _raw 兜底", dx6.args)

# parse_actions 在没有 ACTION: 时回退到 XML 壳
xa = p.parse_actions(XML1)
check(
    xa == [("read", {"block_id": "ctx:8130f1bf6d"})],
    "parse_actions 回退解析 XML 壳",
    xa,
)

# 壳里套着多个调用：action_count 记数，第一个先执行
XML7 = (
    f"{_TAG} read " + '{"block_id":"ctx:1"} ' + _END
    + f"\n{_TAG} read " + '{"block_id":"ctx:2"} ' + _END
)
dx7 = p.parse(XML7, 1)
check(dx7.action_count == 2, "多个 XML 壳被计数", dx7.action_count)

# DSML 原生调用仍然整轮作废（作废的是「格式非法」，不是内容不全）
DSML = (
    _LT + "invoke name=" + chr(34) + "read" + chr(34) + _GT
    + _LT + "parameter name=" + chr(34) + "block_id" + chr(34) + _GT
    + "ctx:z"
    + _LT + "/parameter" + _GT
    + _LT + "/invoke" + _GT
)
dd = p.parse(DSML, 1)
check(dd.kind == "format_error", "DSML 仍整轮作废（点破格式）", dd.kind)


print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("全部通过")
