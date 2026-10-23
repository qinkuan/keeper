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
check(
    "只写一个" in off and "<ACTION>" in off,
    "默认（并行关闭）要求单动作（XML 标签形态）",
    off,
)

import keeper.agent.planner as planner_mod  # noqa: E402

_orig = planner_mod._tool_parallel_mode
planner_mod._tool_parallel_mode = lambda: 1
try:
    on = _action_rule()
finally:
    planner_mod._tool_parallel_mode = _orig
check("最多" in on and "只写一个" not in on, "并行开启时允许多动作", on)


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

# 合并式（工具名后直接跟 JSON）仍认：``<action> read {"block_id":"x"} </action>``
XML_MERGED = f"{_TAG} read " + '{"block_id":"ctx:1"} ' + _END
dxm = p.parse(XML_MERGED, 1)
check(
    dxm.kind == "act"
    and dxm.tool == "read"
    and dxm.args == {"block_id": "ctx:1"},
    "合并式调用壳：工具名 + 内联 JSON",
    (dxm.tool, dxm.args),
)

# 工具名后面跟的不是 JSON（既不是纯工具名、也不是参数）→ 当成写坏的工具名，
# 原样报错比「猜成参数」更好查：模型会照着错误把参数挪进 <ACTION_INPUT>。
XML_BAD = f"{_TAG} bash " + "ls -la " + _END
dxb = p.parse(XML_BAD, 1)
check(dxb.kind == "act", "工具名写坏时仍可执行（报工具不存在）", dxb.kind)
check(dxb.tool == "bash ls -la", "工具名写坏时原样保留", dxb.tool)
check(dxb.args == {}, "工具名写坏时不猜参数", dxb.args)

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


# ── ⑤ XML 协议形态（现行协议本体）────────────────────────────────────────
# 协议从文本（``THOUGHT: …``）迁到 XML 标签（``<THOUGHT>…</THOUGHT>``）后，
# 这里是**新协议**的行为基准：文本形态那几段是兼容期兜底，这一段才是正路。
_L, _R = chr(60), chr(62)


def _tag(name: str, body: str) -> str:
    return f"{_L}{name}{_R}{body}{_L}/{name}{_R}"


# 正常一轮：思考 → 调工具
dxa = p.parse(
    _tag("THOUGHT", "先看看目录里有什么")
    + "\n"
    + _tag("ACTION", "bash")
    + "\n"
    + _tag("ACTION_INPUT", '{"command": "ls"}'),
    1,
)
check(dxa.kind == "act", "XML 协议：正常 act", dxa.kind)
check(dxa.tool == "bash", "XML 协议：工具名", dxa.tool)
check(dxa.args == {"command": "ls"}, "XML 协议：参数", dxa.args)
check(dxa.thought == "先看看目录里有什么", "XML 协议：思考", dxa.thought)

# 正常一轮：思考 → 回答
dxf = p.parse(
    _tag("THOUGHT", "想清楚了") + "\n" + _tag("FINAL", "已经做完了"), 1
)
check(
    dxf.kind == "final" and dxf.answer == "已经做完了",
    "XML 协议：final",
    (dxf.kind, dxf.answer),
)

# 提问 + 选项
dxq = p.parse(
    _tag("THOUGHT", "信息不足")
    + "\n"
    + _tag("ASK", "用哪个目录？")
    + "\n"
    + _tag("OPTIONS", '["A", "B"]'),
    1,
)
check(dxq.kind == "ask" and dxq.ask == "用哪个目录？", "XML 协议：ask", dxq.ask)
check(dxq.options == ["A", "B"], "XML 协议：options", dxq.options)

# 委托子 agent
dxd = p.parse(
    _tag("TASK", "core")
    + "\n"
    + _tag("TASK_INPUT", "写一个贪吃蛇并跑起来"),
    1,
)
check(dxd.kind == "delegate", "XML 协议：delegate", dxd.kind)
check(dxd.capability == "core", "XML 协议：子 agent 名", dxd.capability)
check(dxd.requirement == "写一个贪吃蛇并跑起来", "XML 协议：任务要求", dxd.requirement)

# 漏闭合：引擎兜底取到下一个标签
dxu = p.parse(
    _L + "FINAL" + _R + "答案没闭合\n" + _tag("ACTION", "bash"), 1
)
check(dxu.kind == "final", "XML 协议：漏闭合仍能取到答案", dxu.kind)
check(dxu.answer == "答案没闭合", "XML 协议：漏闭合的答案内容", dxu.answer)

# 一轮多个动作（并行路径）
xa2 = p.parse_actions(
    _tag("ACTION", "bash")
    + "\n"
    + _tag("ACTION_INPUT", '{"command": "ls"}')
    + "\n"
    + _tag("ACTION", "read")
    + "\n"
    + _tag("ACTION_INPUT", '{"block_id": "ctx:1"}')
)
check(
    xa2 == [("bash", {"command": "ls"}), ("read", {"block_id": "ctx:1"})],
    "XML 协议：parse_actions 拆出两个动作且参数各自干净",
    xa2,
)

# 新旧混写：一半 XML、一半文本，仍然能执行
dxm2 = p.parse(
    _tag("THOUGHT", "混着写")
    + "\nACTION: bash\n"
    + _tag("ACTION_INPUT", '{"command": "ls"}'),
    1,
)
check(dxm2.kind == "act" and dxm2.tool == "bash", "XML 协议：混写仍可执行", dxm2.tool)
check(dxm2.args == {"command": "ls"}, "XML 协议：混写参数正确", dxm2.args)

# FINAL 正文里出现字面标签（闭合完整时不该截断）
dxl = p.parse(_tag("FINAL", "看这段：<FINAL> 不是标签"), 1)
check(
    dxl.kind == "final" and dxl.answer == "看这段：<FINAL> 不是标签",
    "XML 协议：FINAL 正文里的字面标签不截断",
    dxl.answer,
)

# ── ⑥ 参数没送达时的兜底 ────────────────────────────────────────────
# 实测踩过的坑：模型偶尔把 <ACTION_INPUT> 写成空体、或形态怪到段切不出来，
# 引擎就拿空参数去调工具，工具只能回一句「缺少 command 参数」——这句话里
# 没有任何模型能据此纠正的信息（它不知道自己漏了什么），于是原样重试。
# 实测一条用例因此连烧 11 轮 bash__run，最后 agent 放弃、直接开始编答案。
# 所以解析器要多捞一次：ACTION 之后第一个 JSON 对象就是参数。
MISSING_INPUT = _tag("ACTION", "bash__run") + "\n" + '{"command": "cat todo.md"}'
dxr = p.parse(MISSING_INPUT, 1)
check(
    dxr.kind == "act" and dxr.tool == "bash__run",
    "认不出 ACTION_INPUT 段时仍认得工具名",
    dxr.tool,
)
check(
    dxr.args == {"command": "cat todo.md"},
    "从 ACTION 之后兜底捞回参数",
    dxr.args,
)

# 空体：没有 JSON 可捞，参数仍为空（这一档由引擎侧的点破兜住）
EMPTY_INPUT = _tag("ACTION", "bash__run") + "\n" + _tag("ACTION_INPUT", "")
dxe = p.parse(EMPTY_INPUT, 1)
check(
    dxe.kind == "act" and dxe.args == {},
    "空 ACTION_INPUT 时参数为空（交引擎点破）",
    dxe.args,
)

# 兜底不能瞎捞：ACTION 之后没有 JSON 就不要编参数
NO_JSON = _tag("ACTION", "bash__run") + "\n" + _tag("FINAL", "好了")
dxn = p.parse(NO_JSON, 1)
check(dxn.kind == "final", "没有 JSON 时不乱捞（走 final 分支）", dxn.kind)

# JSON 字符串里带花括号（awk 的 {print $1}）：正则会截错，JSON 解码器不会
BRACE_STR = '{"command": "awk \'{print $1}\' f"}'
dxb2 = p.parse(_tag("ACTION", "bash__run") + "\n" + BRACE_STR, 1)
check(
    dxb2.args == {"command": "awk '{print $1}' f"},
    "兜底捞取认得 JSON 字符串里的花括号",
    dxb2.args,
)


print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("全部通过")
