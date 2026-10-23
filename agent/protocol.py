"""ReAct 协议的**唯一真源**：标签集 + 分段器。

为什么要有这个模块
------------------
协议从「文本协议」（``THOUGHT: ...``）迁到「XML 标签」（``<THOUGHT>...</THOUGHT>``）
之后，同一套规则会散落在好几处：planner 解析、process 的流式切分、幻觉判定、
历史回放……任何一处漏改，症状都是「模型明明写了却没被执行」，而且极难定位
（提示词是新的、模型也照写了，唯独某一处正则没跟上）。

所以这里只做一件事：**把一段模型输出切成「标签 → 内容」的段落**。
「哪个标签优先」「参数怎么配对」「幻觉怎么判」这些决策留在 planner 里，
但它们只面向段落字典，不再各自去认标签。

两种形态同时认
--------------
- **XML**（新协议，提示词只教这个）：``<FINAL>答案</FINAL>``
- **文本**（旧协议，兼容期兜底）：``FINAL: 答案``

两条都解析，并按**文档顺序**合并成同一份段落表。这样即使模型只学会一半
（新旧混写、漏闭合、标签名大小写乱写），也不会整轮作废——这是从旧协议切到
新协议期间最重要的保险。摘掉文本形态只是把这里的开关关掉，不必再动别处。

漏闭合怎么办
------------
模型经常忘记写 ``</FINAL>``。若要求严格成对，一次手抖就丢掉整轮回答——
所以每个标签都按「**闭合优先、漏闭合兜底**」两段式解析：

1. 找同名闭合 ``</FINAL>``——找到就用它（内容就是两个标签之间的东西）；
2. 找不到才退而求其次：取到**下一个协议标签**或文末为止。

兜底这一档有个已知的代价：正文里若恰好出现字面标签（``<FINAL>``）会被截断。
这是「漏闭合 + 正文含字面标签」的组合，概率很低；而严格成对的代价是
「漏一个闭合就丢整轮」——那个更高。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

# 协议标签全集。新增标签只改这里一处，别处都靠它推导。
LABELS: tuple = (
    "THOUGHT",
    "ACTION",
    "ACTION_INPUT",
    "TASK",
    "TASK_INPUT",
    "TASK_CONTEXT",
    "FINAL",
    "ASK",
    "OPTIONS",
    "OBSERVATION",
)

# 文本协议的**别名**（旧协议遗留，兼容期保留）：``最终回答:`` 与 ``FINAL:`` 同义。
# 解析时归一到规范名，所以下游只认 ``FINAL``。
TEXT_ALIASES: Dict[str, str] = {
    "提问": "ASK",
    "最终回答": "FINAL",
    "ANSWER": "FINAL",
}

# XML 旧壳名 → 规范标签：模型偶尔把调用包成 ``<tool_call>`` / ``<function_call>``。
# 归一到 ACTION，与 ``<ACTION>`` 走同一套解析（工具名 + JSON 参数）。
XML_ALIASES: Dict[str, str] = {
    "TOOL_CALL": "ACTION",
    "FUNCTION_CALL": "ACTION",
}

# OBSERVATION 是**引擎注入**的标签（进 role="tool" 消息），模型自己写它 = 幻觉。
ENGINE_LABELS: tuple = ("OBSERVATION",)

# 「下一个协议标签」的候选名：规范名 + 文本别名 + XML 旧壳名。截断时全都要认，
# 否则正文会把下一个标签一起吞进来。
_ALL_NAMES = tuple(LABELS) + tuple(TEXT_ALIASES) + tuple(XML_ALIASES)
_ALL = "|".join(_ALL_NAMES)
_ALL_GROUP = f"(?:{_ALL})"

# 这些标签的**文本形态**允许写在行中间（模型不换行时很常见）。
# FINAL 尤其需要：旧协议里「…给出 FINAL。FINAL: 正文」是实测出现过的写法，
# 严格要求行首会把回答整段漏掉，最终让用户看到思考。
_LOOSE_TEXT: frozenset = frozenset({"FINAL", "ASK"})


@dataclass
class Segment:
    """输出里的一个协议段。

    - ``name``：标签名（大写常量，不受模型大小写影响）
    - ``content``：两个标签之间的正文
    - ``start`` / ``end``：整段在原文中的区间（含标签本身）
    - ``form``：``"xml"`` 或 ``"text"``——哪种形态解析出来的
    """

    name: str
    content: str
    start: int
    end: int
    form: str


# ── 正则（按标签名缓存编译）────────────────────────────────────────────────

_XML_CLOSED: Dict[str, re.Pattern] = {}
_XML_OPEN: Dict[str, re.Pattern] = {}
_TEXT: Dict[tuple, re.Pattern] = {}


def _xml_closed_re(name: str) -> re.Pattern:
    """同名闭合优先：``<FINAL>…</FINAL>``。

    非贪婪取**最近**的同名闭合，所以一段里出现两个 ``</FINAL>`` 也不会串。
    """
    rx = _XML_CLOSED.get(name)
    if rx is None:
        rx = re.compile(rf"<{name}\b[^>]*>(.*?)</\s*{name}\s*>", re.S | re.I)
        _XML_CLOSED[name] = rx
    return rx


def _xml_open_re(name: str) -> re.Pattern:
    """漏闭合兜底：``<FINAL>…`` 一直吃到下一个协议标签或文末。

    结束条件认开标签与闭标签两种（``</FINAL>`` 也要能截断），
    但**不认**正文里普通的 ``</div>`` 之类——那不是协议标签。
    """
    rx = _XML_OPEN.get(name)
    if rx is None:
        rx = re.compile(
            rf"<{name}\b[^>]*>(.*?)(?=<\s*/?\s*{_ALL_GROUP}\b|\Z)",
            re.S | re.I,
        )
        _XML_OPEN[name] = rx
    return rx


def _text_re(name: str, loose: bool = False) -> re.Pattern:
    """文本协议段：``FINAL: 答案``（到下一个标签为止）。

    ``loose=True`` 时**不要求行首**——模型不换行写成「…给出 FINAL。FINAL: 正文」
    是实测出现过的写法，严格要求行首会把整段回答漏掉。

    结束条件同时认「下一个文本标签行」与「下一个 XML 标签」——混写时
    文本段不会把后面的 XML 标签一起吞进正文。
    """
    key = (name, loose)
    rx = _TEXT.get(key)
    if rx is None:
        # 行首锚定用局部 ``(?m:^)``：宽松形态的**标签本身**可以写在行中间，
        # 但「下一个标签行」的截断条件仍然只认行首（否则正文里的 "x: y" 会被误切）。
        head = (
            rf"{name}[ \t]*[:：]"
            if loose
            else rf"(?m:^[ \t]*{name}[ \t]*[:：])"
        )
        rx = re.compile(
            rf"(?si){head}[ \t]*(.*?)"
            rf"(?=(?m:^[ \t]*{_ALL_GROUP}[ \t]*[:：])|<\s*/?\s*{_ALL_GROUP}\b|\Z)",
        )
        _TEXT[key] = rx
    return rx


# ── 分段 ──────────────────────────────────────────────────────────────────


def _xml_segments(text: str) -> List[Segment]:
    out: List[Segment] = []
    for name in tuple(LABELS) + tuple(XML_ALIASES):
        canon = XML_ALIASES.get(name, name)
        closed = [
            (m.start(), m.end(), m.group(1))
            for m in _xml_closed_re(name).finditer(text)
        ]
        for s, e, content in closed:
            out.append(Segment(canon, (content or "").strip(), s, e, "xml"))
        # 漏闭合的兜底段：与已闭合的段重叠就跳过（否则同一段会被算两次）
        for m in _xml_open_re(name).finditer(text):
            if any(not (m.end() <= s or m.start() >= e) for s, e, _ in closed):
                continue
            out.append(
                Segment(canon, (m.group(1) or "").strip(), m.start(), m.end(), "xml")
            )
    return out


def _text_segments(text: str) -> List[Segment]:
    out: List[Segment] = []
    for name in LABELS:
        # 宽松标签只跑宽松正则（否则同一行会被算成两段，内容还不一样）
        loose = name in _LOOSE_TEXT
        for m in _text_re(name, loose).finditer(text):
            out.append(
                Segment(name, (m.group(1) or "").strip(), m.start(), m.end(), "text")
            )
    # 别名（提问 / 最终回答 / ANSWER）：归一到规范标签
    for alias, canon in TEXT_ALIASES.items():
        for m in _text_re(alias, True).finditer(text):
            out.append(
                Segment(canon, (m.group(1) or "").strip(), m.start(), m.end(), "text")
            )
    return out


def segments(text: str) -> List[Segment]:
    """把输出切成协议段，按**文档顺序**返回（XML 与文本形态混在一起）。

    顺序很重要：``<ACTION>`` 与 ``<ACTION_INPUT>`` 按出现顺序配对，
    靠的就是这个顺序而不是「先找所有 ACTION 再找所有 INPUT」。
    """
    t = text or ""
    if not t:
        return []
    out = _xml_segments(t) + _text_segments(t)
    out.sort(key=lambda s: (s.start, s.end))
    return out


def sections(text: str) -> Dict[str, List[str]]:
    """``{标签名: [正文, ...]}``；同名标签按文档顺序排列。"""
    out: Dict[str, List[str]] = {}
    for seg in segments(text):
        out.setdefault(seg.name, []).append(seg.content)
    return out


def sections_of(text: str, name: str, form: Optional[str] = None) -> List[str]:
    """某个标签的所有出现（正文，按文档顺序）。

    ``form`` 可限定 ``"xml"`` / ``"text"``。混合输出时常用「先看 XML，没有
    再看文本」的顺序取，避免正文里出现的字面标签抢走真正的答案。
    """
    return [
        seg.content
        for seg in segments(text)
        if seg.name == name and (form is None or seg.form == form)
    ]


def first_segment(text: str, name: str, form: Optional[str] = None) -> Optional[Segment]:
    """第一个匹配的段；``form`` 可限定 ``"xml"`` / ``"text"``。"""
    for seg in segments(text):
        if seg.name == name and (form is None or seg.form == form):
            return seg
    return None


def section(text: str, name: str) -> str:
    """第一个匹配的段的正文；没有则空串。"""
    seg = first_segment(text, name)
    return seg.content if seg is not None else ""


def has_label(text: str, name: str) -> bool:
    """输出里有没有这个标签。"""
    return first_segment(text, name) is not None


def next_segment_start(text: str, pos: int) -> int:
    """``pos`` 之后第一个协议段的起点；没有则返回全文长度。

    用于「THOUGHT 段之后、下一段之前的那段文字 = 正文」这类划界。
    """
    for seg in segments(text):
        if seg.start >= pos:
            return seg.start
    return len(text or "")


def strip_protocol(text: str) -> str:
    """去掉全部协议段（含标签与内容），只留「协议之外」的文字。

    用来判断「模型是不是漏写 FINAL、把正文直接写了出来」。
    """
    segs = sorted(segments(text), key=lambda s: s.start, reverse=True)
    out = text or ""
    for seg in segs:
        out = out[: seg.start] + out[seg.end :]
    return out.strip()


# ── 回放时的转义 ──────────────────────────────────────────────────────────

_ESCAPE_RE = re.compile(rf"</(?={_ALL_GROUP}\b)", re.I)


def escape_body(text: str) -> str:
    """把正文里**协议标签的闭合形式**转义掉：``</FINAL>`` → ``<\\/FINAL>``。

    为什么需要
    ----------
    回放历史时，assistant 消息里的 THOUGHT / ACTION_INPUT 正文是**模型自己写的
    任意文本**——可能是一段 HTML、一段代码，里面恰好出现 ``</FINAL>``。
    不转义的话，它会把 ``<FINAL>`` 段提前闭合，解析出的正文少一截。

    只转协议标签的闭合形式（``</div>`` 这类不转），范围最小。

    ``<\\/`` 里的反斜杠不是笔误：对 JSON 字符串来说 ``\\/`` 是合法转义
    （解码后还是 ``/``）；对 XML 解析器来说 ``<\\/FINAL>`` 不是闭合标签，
    于是这段文字既能被原样保留，又不会误闭合。
    """
    return _ESCAPE_RE.sub(r"<\\/", text or "")


def render(name: str, content: str) -> str:
    """按 XML 形态渲染一个协议段（回放历史时用）。

    标签名大写、内容做闭合转义——内容里的 ``</FINAL>`` 不会误闭合本段。
    """
    return f"<{name}>{escape_body(content)}</{name}>"


def join_blocks(blocks: Iterable[str]) -> str:
    """把若干协议块拼成一条 assistant 消息正文。"""
    return "\n".join(b for b in blocks if b)
