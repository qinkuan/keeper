"""协议分段器的回归脚本（直接 `python keeper/tests/test_protocol.py` 跑）。

为什么单独一个文件
------------------
协议是**引擎契约**：模型写什么、引擎怎么切，两边必须对得上。这类东西最容易
在改动中悄悄坏掉（症状是「模型明明写了却没被执行」），所以给它一份能随手
跑、结果可读的验证——和 ``test_guard.py`` / ``test_planner_parse.py`` 同风格。

覆盖
----
① XML 形态：闭合 / 漏闭合 / 大小写 / 前缀不误认（``<ACTION_INPUT>`` 不是 ``<ACTION>``）
② 文本形态（兼容期兜底）：行首标签、正文截到下一个标签
③ 混写：两种形态按**文档顺序**合并
④ 回放转义：正文里的 ``</FINAL>`` 不误闭合
"""
from __future__ import annotations

import os
import sys

# tests/ → keeper/ → 项目根（keeper 包的父目录）
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from keeper.agent import protocol  # noqa: E402

FAILED: list = []


def check(ok: bool, label: str, got=None) -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  got={got!r}"))
    if not ok:
        FAILED.append(label)


# ── ① XML 形态 ─────────────────────────────────────────────────────────────
segs = protocol.segments("<THOUGHT>先看看目录</THOUGHT>\n<ACTION>bash</ACTION>")
check([s.name for s in segs] == ["THOUGHT", "ACTION"], "XML 两个标签按序切出", segs)
check(segs[0].content == "先看看目录", "XML THOUGHT 正文", segs[0].content)
check(segs[1].content == "bash", "XML ACTION 正文", segs[1].content)

check(protocol.section("<FINAL>答案</FINAL>", "FINAL") == "答案", "XML FINAL 闭合")
check(
    protocol.section("<FINAL>答案", "FINAL") == "答案",
    "XML 漏闭合：取到文末",
    protocol.section("<FINAL>答案", "FINAL"),
)
check(
    protocol.section("<FINAL>答案\n<ACTION>bash</ACTION>", "FINAL") == "答案",
    "XML 漏闭合：取到下一个标签为止",
    protocol.section("<FINAL>答案\n<ACTION>bash</ACTION>", "FINAL"),
)
check(
    protocol.section("<final>答案</final>", "FINAL") == "答案",
    "XML 标签名大小写不敏感",
)
check(
    protocol.section("<ACTION_INPUT>{\"a\": 1}</ACTION_INPUT>", "ACTION") == "",
    "<ACTION_INPUT> 不被当成 <ACTION>",
    protocol.section("<ACTION_INPUT>{\"a\": 1}</ACTION_INPUT>", "ACTION"),
)
check(
    protocol.section("<ACTION_INPUT>{\"a\": 1}</ACTION_INPUT>", "ACTION_INPUT")
    == '{"a": 1}',
    "<ACTION_INPUT> 正文完整",
)
# JSON 字符串里出现字面标签 + 闭合完整 → 不能被截断
check(
    protocol.section('<ACTION_INPUT>{"c": "<FINAL>"}</ACTION_INPUT>', "ACTION_INPUT")
    == '{"c": "<FINAL>"}',
    "闭合完整时 JSON 里的字面标签不截断",
    protocol.section('<ACTION_INPUT>{"c": "<FINAL>"}</ACTION_INPUT>', "ACTION_INPUT"),
)

# ── ② 文本形态（兼容期）───────────────────────────────────────────────────
check(protocol.section("FINAL: 答案", "FINAL") == "答案", "文本 FINAL")
check(
    protocol.section("THOUGHT: 想一下\nACTION: bash\nACTION_INPUT: {\"a\": 1}", "THOUGHT")
    == "想一下",
    "文本 THOUGHT 截到下一个标签",
    protocol.section("THOUGHT: 想一下\nACTION: bash", "THOUGHT"),
)
check(
    protocol.section("ACTION: bash\nACTION_INPUT: {\"a\": 1}", "ACTION") == "bash",
    "文本 ACTION 不吞下一行",
)
check(
    protocol.section("FINAL: 答案\nOBSERVATION: 编的", "FINAL") == "答案",
    "文本 FINAL 截到 OBSERVATION 行",
    protocol.section("FINAL: 答案\nOBSERVATION: 编的", "FINAL"),
)
# 文本段遇到 XML 标签也要截断（混写时别把 XML 吞进正文）
check(
    protocol.section("THOUGHT: 想一下\n<ACTION>bash</ACTION>", "THOUGHT") == "想一下",
    "文本段遇到 XML 标签即截断",
    protocol.section("THOUGHT: 想一下\n<ACTION>bash</ACTION>", "THOUGHT"),
)

# ── ③ 混写：按文档顺序合并 ────────────────────────────────────────────────
mixed = "THOUGHT: 想一下\n<ACTION>bash</ACTION>\nACTION_INPUT: {\"a\": 1}"
segs = protocol.segments(mixed)
check(
    [s.name for s in segs] == ["THOUGHT", "ACTION", "ACTION_INPUT"],
    "混写按文档顺序合并",
    [(s.name, s.form) for s in segs],
)
check(
    protocol.sections_of(mixed, "ACTION_INPUT") == ['{"a": 1}'],
    "混写时参数照样取得到",
)

# 多个同名标签：按出现顺序
multi = "<ACTION>bash</ACTION>\n<ACTION>read</ACTION>"
check(
    protocol.sections_of(multi, "ACTION") == ["bash", "read"],
    "同名标签按出现顺序",
    protocol.sections_of(multi, "ACTION"),
)

# ── ④ 回放转义 ─────────────────────────────────────────────────────────────
check(
    protocol.escape_body("这里有 </FINAL> 和 </div>") == "这里有 <\\/FINAL> 和 </div>",
    "只转协议标签的闭合形式",
    protocol.escape_body("这里有 </FINAL> 和 </div>"),
)
esc = protocol.render("THOUGHT", "见到 </FINAL> 就结束")
check(esc == "<THOUGHT>见到 <\\/FINAL> 就结束</THOUGHT>", "render 转义内容", esc)
check(
    protocol.section(esc, "THOUGHT") == "见到 <\\/FINAL> 就结束",
    "转义后回放不会提前闭合",
    protocol.section(esc, "THOUGHT"),
)

# ── 其它小工具 ─────────────────────────────────────────────────────────────
check(
    protocol.has_label("<FINAL>x</FINAL>", "FINAL")
    and not protocol.has_label("<FINAL>x</FINAL>", "ACTION"),
    "has_label 判存在",
)
check(
    protocol.strip_protocol("前面的话\n<THOUGHT>思考</THOUGHT>\n后面的话") == "前面的话\n\n后面的话",
    "strip_protocol 只留协议外文字",
    repr(protocol.strip_protocol("前面的话\n<THOUGHT>思考</THOUGHT>\n后面的话")),
)
check(
    protocol.next_segment_start("<THOUGHT>x</THOUGHT><ACTION>y</ACTION>", 0) == 0,
    "next_segment_start 找得到段起点",
)
check(
    protocol.next_segment_start("<THOUGHT>x</THOUGHT>正文", 17) == len("<THOUGHT>x</THOUGHT>正文"),
    "next_segment_start 没有段时给全文长度",
)


print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("全部通过")
