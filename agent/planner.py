"""ReAct 规划器：负责把「工具清单 + 问题 + 历史」喂给 LLM，并解析其决策。

采用模型无关的 ReAct 协议（不依赖原生 tool-calling），任意 chat 模型都可用。
协议标签的**唯一定义**在 :mod:`keeper.agent.protocol`——本模块只负责「决策」，
不负责「这段文本长什么样」：

- ``<THOUGHT>思考过程</THOUGHT>``
- ``<ACTION>工具名</ACTION>`` + ``<ACTION_INPUT>{"参数": "值"}</ACTION_INPUT>``
- ``<FINAL>最终回答</FINAL>``

协议本体是 **XML 标签**；文本形态（``FINAL: ...``）仍被解析，作为从旧协议
切过来这段过渡期的兜底——混写、漏闭合都不会整轮作废。

解析对中文标签（最终回答 / 提问）和缺失标签都做了容错。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import protocol

logger = logging.getLogger(__name__)

# 「段尾可能混入的其它标签」，用于把宽松匹配到的正文切干净。
#
# 协议本体已由 protocol 统一处理（段落天然截到下一个标签），这里只服务于
# 两条**宽松兜底**路径：模型把标签写在行中间（不换行）、以及旧文本协议残留。
_TAIL_LABEL = (
    r"\n\s*(?:THOUGHT|ACTION_INPUT|ACTION|OBSERVATION|FINAL|最终回答"
    r"|ANSWER|ASK|提问|OPTIONS|TASK|TASK_INPUT|TASK_CONTEXT)\s*[:：]"
)

# ── 原生 tool-calling（DSML）兼容 ────────────────────────────────────────
#
# 现象：模型不按文本协议走，直接吐原生工具调用标记::
#
#     <｜｜DSML｜｜ calls>
#     <｜｜DSML｜｜ invoke name="bash__run">
#     <｜｜DSML｜｜ parameter name="command" string="true">node x.js</｜｜DSML｜｜ parameter>
#     </｜｜DSML｜｜ invoke>
#     </｜｜DSML｜｜ calls>
#
# 危害：文本协议解析认不出来 → 落进兜底 → 整段当成**最终回答**结束这一轮。工具一步
# 都没执行，用户看到的却是一段乱码（线上实测：第 12/30 步直接终止）。
#
# 为什么会出现：请求里我们**没传** tools（keeper 全程走文本协议，见下面两处 _DSML_）。
# 通常是模型端 / 中转层自行决定用原生格式；也可能上一轮的历史里已经出现过这种文本，
# 被模型当成"输出范例"模仿，形成自我强化——所以必须能解析，光在 prompt 里禁止不够。
#
# 分隔符故意写宽松（用 ``[^>]*`` 而不是写死 ｜）：不同版本标记里的分隔符不一样。
_DSML_CALLS_BLOCK = r"<[^>]*calls[^>]*>.*?</[^>]*calls[^>]*>"
_DSML_INVOKE = re.compile(r'invoke\s+name="([^"]+)"[^>]*>(.*?)</[^>]*invoke\s*>', re.S)
_DSML_PARAM = re.compile(
    r'parameter\s+name="([^"]+)"[^>]*>(.*?)</[^>]*parameter\s*>', re.S
)

# XML 风格的调用壳（``<action> read {"block_id":"x"} </action>``）现在由
# :mod:`keeper.agent.protocol` 统一处理：``<ACTION>`` 本就是协议标签，壳内的
# 「工具名 + JSON」会被拆成工具名与参数（见 ``_split_tool_and_inline_args``）。
# ``<tool_call>`` / ``<function_call>`` 两种旧壳名在 protocol 里归一到 ACTION。

def detect_hallucination(text: str) -> str:
    """检出「模型自己在扮演引擎」。正常返回空串，命中返回可读的原因。

    两条判据都是**结构性**的，不是概率型启发式——所以敢据此整轮作废：

    1. **输出里出现 OBSERVATION**。它是**引擎注入**的标签，进 ``role="tool"``
       消息，从来不属于 assistant 消息。模型输出里出现它属于不可能事件，
       **命中即幻觉，零误报**。

    2. **一轮里写了多个 THOUGHT**。一轮只有一个 THOUGHT；写第二个就说明它在
       替引擎编排后续轮次——实测它会一路写到 FINAL，把整条轨迹自己演完，
       还顺手编出「文件已写入 / 已发布」这种假回执。

    为什么**不看 ACTION 的个数**：连写多个 ACTION 是「想一次做两件事」，
    属急躁而非伪造，执行第一个是对的（见 ``process._dropped_actions_note``）。

    真实案例（deepseek-flash）：一步里输出 THOUGHT → ACTION(bash: cat > index.html)
    → **自编 OBSERVATION** → THOUGHT → ACTION(fs.deliver) → **自编 OBSERVATION**
    → FINAL。因为 FINAL 在 parse 里优先判定，两个 ACTION 一个都没执行，
    而用户已经收到「已在右侧挂成产物卡片，点开就能跑」。

    实现说明：正文里的字面标签**不算**——协议分段（``protocol.segments``）按
    标签切段，``ACTION_INPUT`` 段里的内容自成一体，不会被当成独立标签。
    """
    if not text:
        return ""
    segs = protocol.segments(text)
    if any(s.name == "OBSERVATION" for s in segs):
        return "你自己写了 OBSERVATION"
    n_thought = len([s for s in segs if s.name == "THOUGHT"])
    if n_thought > 1:
        return f"你一轮里写了 {n_thought} 个 THOUGHT（一轮只能有一个）"
    return ""


def _tool_parallel_mode() -> int:
    """工具并行档位：0=关闭（默认）/ 1=只读 / 2=只读 + 显式声明。

    与 :meth:`ReactProcess` 里那份同名函数读的是同一个配置项（``context.tool_parallel``）。
    提示词必须跟着它走，否则会出现"提示词让模型只写一个、引擎却在等多个"的错位。
    """
    try:
        from .context_store import ctx_cfg

        v = int(getattr(ctx_cfg(), "tool_parallel", 0) or 0)
    except Exception:  # noqa: BLE001 配置读不到就按最保守的"关闭"处理
        v = 0
    return v if v in (0, 1, 2) else 0


def _action_rule() -> str:
    """生成「一次写几个 ACTION」那条准则的**正文**（编号由调用方统一加）。

    为什么必须动态生成
    ------------------
    这条准则原本写死成"每次响应只写一个 ACTION"，而引擎在 ``tool_parallel > 0``
    时会真的执行同一 THOUGHT 里的多个 ACTION。写死 = 开着并行却禁止模型并行，
    功能等于没开（实测里模型的"想连查两次"就被这条规则劝退了）。反过来关着
    并行时更不能让模型连写——第二个会被直接丢弃，等于白烧一次输出。
    """
    mode = _tool_parallel_mode()
    if mode <= 0:
        return (
            "**每次响应只写一个 `<ACTION>`**：写了第二个也**不会被执行**（系统只取第一个），"
            "想再查一次，等看到回执后的下一轮再写。"
        )
    limit = 5
    try:
        from .context_store import ctx_cfg

        limit = max(1, int(getattr(ctx_cfg(), "tool_parallel_max", 0) or 0) or 5)
    except Exception:  # noqa: BLE001
        pass
    return (
        f"**互相独立的只读查询可以合并成一轮，写最多 {limit} 个 `<ACTION>`**，"
        "每个 `<ACTION>` / `<ACTION_INPUT>` 各占一对标签；有依赖的（要先看 A 的结果再决定 B）"
        "必须拆成多轮，等看到回执后的下一轮再写。"
    )


@dataclass
class Decision:
    kind: str  # "think" | "act" | "final" | "ask"
    thought: str = ""
    tool: Optional[str] = None
    args: Optional[dict] = None
    answer: Optional[str] = None
    # kind == "ask" 时有效：向用户提问的内容与可选项
    ask: Optional[str] = None
    options: Optional[list] = None
    # 这一段响应里出现了几个 ACTION。prompt 要求「每次只写一个」，现状是第二个
    # 会被丢弃；这个计数用来量化「模型想并行」的比例（P1-1 的收益依据）。
    # 只在 kind == "act" 时有意义，其余场景留 0。
    action_count: int = 0
    # 非空 = 模型在「扮演引擎」，本轮整轮作废。值是幻觉原因，会原样念回给它。
    # 见 detect_hallucination()：两条判据都是结构性的，不是概率型启发式。
    hallucination: str = ""
    # 非空 = 输出了本接口不接受的格式（当前只有原生 tool-calling）。与幻觉分开
    # 记：成因不同（一个是自编轨迹、一个是格式跑偏），处置也不同——幻觉点破的是
    # 「你以为你做过」，格式点破的是「这样写没用的」。同样走「点破 + 重试」。
    format_error: str = ""
    # kind == "delegate" 时有效：TASK 动作委托给的子 agent 名 / 任务要求 / 已知信息
    capability: Optional[str] = None
    requirement: Optional[str] = None
    context: Optional[str] = None


def _clean_tool_name(raw: str) -> str:
    """清洗工具名。

    模型偶尔会把工具名写成 ```fs.list_dir``` 或把多个标签挤在同一行，这里逐一处理：
    ① 截到下一个已知标签之前；② 去掉 Markdown 反引号 / 引号包裹；③ 去首尾空白。
    """
    line = raw or ""
    # ① 同一行若又出现其它标签（如 ACTION: fs.list_dir ACTION_INPUT: {...}），
    #    或后面跟着 XML 标签，截到标签前
    line = re.split(
        r"(?i)\s+(?:" + "|".join(protocol.LABELS) + r")\s*[:：]"
        r"|<\s*/?\s*(?:" + "|".join(protocol.LABELS) + r")\b",
        line,
    )[0]
    return line.strip().strip("`\"'，,;；。").strip()


def _split_tool_and_inline_args(content: str) -> Tuple[str, dict]:
    """拆「工具名 + 可能内联在后面的 JSON」。

    - ``bash`` → ``("bash", {})``
    - ``read {"block_id":"x"}`` → ``("read", {"block_id": "x"})``（旧调用壳的形状）

    只有**以 ``{`` 开头**的剩余部分才当参数。否则那不是参数，是工具名被写坏
    之后剩下的字（``bash 命令：ls``）——把它当参数会得到一个语义错误但
    「看起来合法」的参数集，比直接报「没有这个工具」更难查。
    """
    c = (content or "").strip()
    if not c:
        return "", {}
    m = re.match(r"^([^\s{}<>]+)\s*(.*)$", c, re.S)
    if not m:
        return "", {}
    name = m.group(1)
    rest = m.group(2).strip()
    if rest.startswith("{"):
        return name, ReActPlanner._parse_args(rest)
    # 不是内联 JSON：工具名取整行（与旧实现一致：写坏的名字原样报错更好查）
    return c.splitlines()[0].strip(), {}


def _action_pairs(text: str) -> List[Tuple[str, dict]]:
    """把输出里的调用解析成 ``(工具名, 参数)`` 列表（按文档顺序）。

    协议本体：``<ACTION>工具名</ACTION>`` + ``<ACTION_INPUT>{JSON}</ACTION_INPUT>``。
    兼容两种旧写法：文本协议（``ACTION: 工具名``）与合并式（工具名后直接跟 JSON）。

    配对规则：每个 ACTION 配**它自己区间内**（到下一个 ACTION 之前）的第一个
    ACTION_INPUT。这样「两个 ACTION 各带一个参数」不会串味；区间内没有就回退
    到全局第一个——与旧实现一致（旧实现取的就是全局第一个）。

    兜底：ACTION_INPUT 段**一个都没认出来**时（标签形态怪、没顶行首、正文被
    截断……），从 ACTION 之后用 JSON 解码器直接捞第一个对象。理由：参数丢了的
    代价太高——引擎会拿空参数去调工具，模型收到的是「缺少 command 参数」这种
    没法据此纠正的报错，于是原样重试、连烧十几轮（实测一次烧掉 11 轮
    ``bash__run`` 全废，最后 agent 放弃并开始编）。宁可多捞一次，也不要把
    明明写对了的 JSON 丢掉。
    """
    t = text or ""
    segs = protocol.segments(t)
    actions = [s for s in segs if s.name == "ACTION"]
    inputs = [s for s in segs if s.name == "ACTION_INPUT"]
    out: List[Tuple[str, dict]] = []
    for i, a in enumerate(actions):
        end = actions[i + 1].start if i + 1 < len(actions) else len(t)
        name, inline = _split_tool_and_inline_args(a.content)
        args = inline
        if not args:
            for s in inputs:
                if a.start < s.start < end:
                    args = ReActPlanner._parse_args(s.content)
                    break
            else:
                if inputs:
                    args = ReActPlanner._parse_args(inputs[0].content)
        if not args:
            args = _json_after(t, a.end)
        out.append((name, args))
    return out


def _json_after(text: str, pos: int) -> dict:
    """从 ``pos`` 之后捞出第一个 JSON 对象；捞不到返回 ``{}``。

    用 ``json.JSONDecoder().raw_decode`` 而不是正则：它认得字符串里的 ``{``/
    ``}``/转义引号，正则会在 ``{"a": "}{"}`` 这种内容上截错。
    """
    if not text:
        return {}
    tail = text[pos:].lstrip()
    if not tail.startswith("{"):
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(tail)
    except Exception:  # noqa: BLE001 捞不到就算了，不该让解析崩
        return {}
    return obj if isinstance(obj, dict) else {}


def _delivery_rules() -> List[str]:
    """「交付物挂载 + 上下文被折叠时怎么办」几条准则的**正文**（编号由调用方统一加）。

    这两条都是**实测踩出来的**，而且都属于「模型无从推断、只能靠试错」的类别：

    ① **交付物认显式清单**。实测一整轮把大文件分段写进 ②、再用 ``cat`` 合并成
       ① 里的 ``index.html``，结果文件是好的，但**会话里没有产物卡片**——因为
       产物优先认 ``fs.deliver([...])`` 交付清单（没调时退回认本轮用 ``bash``
       写出的文件），而 ``cat`` 创建的文件两者都不是，必须合并后显式交付。模型
       不知道这件事，就只能说出「文件已生成」这种用户点不开的废话。

    ② **工具输出会被折叠**。超阈值的输出在上下文里只剩「首尾预览 + ``ctx:<id>``」。
       模型看不到全文时**正确反应是展开，而不是重发命令**——但它不知道有
       ``ctx:<id>`` 这条路，于是选了重发。这会直接变成死循环：重发 → 更多输出 →
       更多折叠 → 更想重发。实测因此把一个简单任务烧到 30 步上限。
    """
    return [
        "成规模数据优先用 `fs.deliver` 交成可视化块（对比 / 趋势 / 占比 → `chart`；"
        "多行多列 → `table`；流程 / 架构 → `mermaid`；地理 → `map`）。"
        "数据点 ≥ 5 类、含时间序列、或用户明确要求时再上图；零散两三个数字文字说清即可。",
        "生成的文件不会自动上卡片：写完必须用 `fs.deliver` 显式交付（写法见上方），且**只交最终成品**。",
        "超长输出变「首尾预览 + `ctx:<id>`」：看到 `ctx:` 说明已执行过，用 `read` 取全文，"
        "别重发（重发白等一轮还多一份重复文件）。",
        "`<THOUGHT>` 里的计划不会自动执行——只有本轮真正发出的 `<ACTION>` 才算数。",
        "同一动作连败两次就换思路：换路径 / 换工具 / 拆更小步；"
        "`returncode: 1` 反复时先 `bash`（`ls` / `test -f`）确认依赖真在，再继续。",
        "写完代码 / 改动先跑通验证再交付（pytest / node / 直接运行），"
        "别凭「应该没问题」就声称完成；验证最多 2 轮，仍不通过就交付并写明「已知限制」。"
        "不要自加用户没要求的验收标准。",
    ]


def _max_output_tokens() -> int:
    """单次回复的输出上限（token）；0 / 读不到 = 不限制。"""
    try:
        from .context_store import ctx_cfg

        return int(getattr(ctx_cfg(), "max_output_tokens", 0) or 0)
    except Exception:  # noqa: BLE001 配置读不到就按"不限制"处理，与运行行为一致
        return 0


def _output_budget_rules() -> List[str]:
    """「单次输出有上限，超了要自己分段」这条准则的**正文**（编号由调用方统一加）。

    为什么必须**告诉模型**数字，而不只是给引擎配个上限
    --------------------------------------------------
    撞上输出上限时，模型那边看到的是「输出正常结束」——它不知道被截断了。
    于是它以为整个 ``ACTION_INPUT: {...}`` 已经写完，实际上是个半截 JSON：
    参数解析失败、工具报一些看不懂的错，它多半会换个写法再试一遍，往往又超。
    实测就是这么把一个任务烧到步数上限的。

    所以这里的重点不是"限制模型"，而是**让它按预算分配输出**：短的写参数，
    长的分段写。要点写具体（给字符数、给分段套路），不然等于没提醒。

    没配置上限（0）时返回空列表——那时按 provider 默认走，提示词不提数字，
    免得给出一个与实际不符的上限。
    """
    n = _max_output_tokens()
    if n <= 0:
        return []
    # token→字符用 2.0 粗估（中文更接近 1.5，代码接近 3），取中间值且**宁可
    # 偏小**：这条是让模型少写，不是让它写满，写少了顶多多一轮，写多了被截。
    per = max(200, int(n * 2 * 0.5))
    return [
        f"**单次回复的输出上限是 {n} token**\n"
        f"   折算：中文约 1 字 ≈ 1 token（安全线约 {n} 字）；英文 / 代码约 4 字符 ≈ 1 token。"
        f"你这边**看不到自己被截断**，只会觉得「输出结束了」——但参数 JSON 会半截、工具必然失败。\n"
        f"   所以要主动按预算分配：\n"
        f"   - 单个 `<ACTION_INPUT>` 控制在 **{per} 字符以内**；一次写不完就"
        f"**分多次写**（文件用 `bash` 分段 `cat >>`，再 `cat` 合并，最后 `fs.deliver` 交付），"
        f"别一次写完整份文件 / 脚本。\n"
        f"   - `<THOUGHT>` 只写决定和理由，别复述代码、文档正文或工具返回的原文。\n"
        f"   - 要引用的大段内容用 `read` / 工具参数**分次**取，别整段抄进参数。"
    ]


class ReActPlanner:
    def __init__(self, max_steps: int = 6) -> None:
        self.max_steps = max_steps

    def system_prompt(
        self,
        tools_desc: str,
        skill_desc: str = "",
        agent_name: str = "",
        persona: str = "",
        system_extra: str = "",
        memory_hint: str = "",
        delegatees_desc: str = "",
        dynamic_extra: str = "",
    ) -> str:
        """拼出 system 提示词。

        顺序是有讲究的——**越靠后权重越高**：

        ① 身份（名字 + persona）→ ② 可用工具 → ③ 可用技能 → ④ 工作准则
        → ⑤ skill 的额外要求 → ⑥ 输出格式

        「输出格式」必须排在最后：它是引擎契约，不能被 persona 或 skill 的内容
        覆盖掉，否则模型不再输出 `<THOUGHT>` / `<ACTION>`，ReAct 循环直接断。
        """
        ident = f"（你的名称：{agent_name}）" if agent_name else ""
        base = (
            "你是一个遵循 ReAct（推理-行动）模式工作的 AI 助手" + ident + "。\n"
            "你可以调用工具来获取真实信息，再综合给出最终回答；"
            "不要编造未经工具确认的事实。\n\n"
        )
        # 章节标题统一用 H1：注入的 persona（soul.md）与 always 技能正文都以 H1
        # 起头，模板若用 H2 就会出现「H2 里套 H1」的层级倒挂；升为 H1 后注入内容
        # 的 H1 成为同级、其内部 H2/H3 层级完整保留，且不动任何用户文件。
        if persona:
            base += "# 你的角色设定\n" + persona.strip() + "\n\n"
        base += "# 可用工具（通过 <ACTION> 调用）\n" + tools_desc + "\n\n"
        if delegatees_desc:
            base += "# 可用子 agent\n" + delegatees_desc + "\n\n"
        if skill_desc:
            base += "# 可用的技能（Skill）\n" + skill_desc + "\n\n"
        # 准则编号只在这里生成一次：各节只提供正文（_action_rule / _delivery_rules /
        # _output_budget_rules），避免号分散硬编码在三个字符串里、插一条就全部撞号。
        rules = [
            "先思考（`<THOUGHT>`），再决定要不要调工具（`<ACTION>`）。",
            "需要信息就调工具；工具回执是**系统注入**的，你**绝对不要自己写** "
            "`<OBSERVATION>`——一轮只写你自己的几行就停，等回执。"
            "自己编回执再写下一轮，等于捏造工具结果，整段推理作废。",
            "信息足够才给 `<FINAL>`，且必须基于工具真实返回。",
            _action_rule(),
            "只引用工具实际返回过的内容；没返回的禁止凭空列。",
            "需要用户决定时一律 `<ASK>` + `<OPTIONS>`，别把候选写进 `<FINAL>` 正文。",
            "**不需要工具就别调**：问候、常识直接 `<FINAL>`；recall / read 别为走流程乱调。",
        ]
        # 交付 / 验证几条放最后，输出预算紧跟其末（与「分段写文件」那条是同一件事的延续）
        rules += _delivery_rules()
        rules += _output_budget_rules()
        base += "# 工作准则\n" + "\n".join(
            f"{i}. {r}" for i, r in enumerate(rules, 1)
        ) + "\n\n"
        if system_extra:
            base += "# 当前技能的额外要求\n" + system_extra.strip() + "\n\n"
        # `/` 动态激活插件的正文：用户显式点开的 = 高优先级，紧跟常驻技能之后、
        # 输出格式之前（格式是引擎契约，必须压在最末）
        if dynamic_extra:
            base += dynamic_extra.strip() + "\n\n"
        base += (
            "# 输出格式（严格遵守）\n"
            "用 **XML 标签**表达每一步；每个标签**成对出现**（有开就有闭），标签顶行首。\n"
            "<THOUGHT>你要做什么、为什么</THOUGHT>\n"
            "<ACTION>工具名</ACTION>\n"
            "<ACTION_INPUT>{\"参数名\": \"值\"}</ACTION_INPUT>\n"
            "<TASK>子 agent 名（如 core / writer）</TASK>\n"
            "<TASK_INPUT>自然语言任务要求（可多行）</TASK_INPUT>\n"
            "<FINAL>最终回答正文（可多行；之后不再写任何标签）</FINAL>\n"
            "<ASK>需要用户回答的问题</ASK>\n"
            "<OPTIONS>[\"候选1\", \"候选2\"]</OPTIONS>\n\n"
            "- 一轮里 `<THOUGHT>` **只写一个**；然后要么给一组 `<ACTION>`/`<ACTION_INPUT>`，"
            "要么给 `<FINAL>`，要么给 `<ASK>`/`<OPTIONS>`。\n"
            "- **`<ACTION>` 与 `<ACTION_INPUT>` 必须成对**：`<ACTION>` 里**只写工具名**，"
            "参数一律放 `<ACTION_INPUT>`（JSON）。\n"
            "- **`<ACTION>` 与 `<TASK>` 二选一，一次只发一个**。有现成工具走 `<ACTION>`；"
            "需写代码 / 生成视频等多步能力且无现成工具走 `<TASK>`（后跟子 agent 名 + "
            "`<TASK_INPUT>` 自然语言要求）。\n"
            "- 工具名必须一字不差用「可用工具」清单里的；清单外"
            "（如 `fs.write_file` / `fs.list_dir` / `fs.find`）**已废弃**，请改用 `bash`（cat / ls / find / grep）；"
            "`fs.publish` 不存在，交付用 `fs.deliver`。\n"
            "- 想让图片 / 图表等多模态产物**插在 `<FINAL>` 文字中间**（而不是堆在末尾）："
            "① 先真正把它做出来；② 交给 `fs.deliver` 时给块加 `\"ref\": \"名字\"`；"
            "③ 在 `<FINAL>` 对应位置写 `[[art:名字]]`（ref 字面量两边必须完全一致）。"
            "**这条只对「你已经生成并交付」的产物有效**——没调 `fs.deliver` 就写 `[[art:...]]` 只会显示成失效占位，等于没图。\n"
            "  - 流程图 / 架构图 / 时序图：写 `x.mmd` 再 `fs.deliver([{\"kind\":\"mermaid\",\"file\":\"x.mmd\",\"ref\":\"seq\"}])`（时序图用 `sequenceDiagram` 语法）。\n"
            "  - 真实生成图 / 视频等无现成单步工具的能力：走 `<TASK>` 派给对应子 agent，由其交付，再用它的 ref 引用。\n"
            "  - 没被 `[[art:...]]` 引用的产物仍按原样显示在底部。\n"
            "- 标签要闭合（`</FINAL>` 这样的收尾也要写）。万一漏了闭合，引擎会尽量兜底，"
            "但正文可能被截短——所以别漏。正文里若必须出现 `</FINAL>` 这类字样，"
            "把斜杠写成反斜杠：`<\\/FINAL>`。\n"
            "- **绝对不要输出原生工具调用标记**（`<function_calls>`、`<｜｜DSML｜｜ invoke>`、"
            "`<tool>` 这类）——本接口没注册任何工具，那些标记不会执行也不会解析，"
            "只会让本轮直接结束。要调工具就用上面那几个标签。\n"
        )
        # 历史记忆是「随每轮变化」的内容，必须放在 system 最末尾：
        # DeepSeek 前缀缓存要求前缀从头逐 token 一致，变化内容放中间会切断
        # 跨轮稳定前缀，导致整段系统提示词都吃不到缓存。放在末尾后，
        # 上面的身份/工具/技能/准则/输出格式全部成为可跨轮命中的稳定前缀。
        if memory_hint:
            base += (
                "\n# 历史记忆摘要\n"
                "（系统按当前问题自动注入，可能相关；某块需要完整内容时，"
                "用 read 工具并传入其 block_id）\n"
                "包含「最近 N 轮摘要」与「更早的相关记忆（按当前问题召回）」：\n"
                + memory_hint.strip()
                + "\n"
            )
        return base

    def parse_actions(self, text: str) -> List[Tuple[str, Dict[str, Any]]]:
        """解析出**全部** ``(工具名, 参数)``，而不是只取第一个。

        为什么需要：prompt 要求「每次响应只写一个 ACTION」，但模型经常连写多个
        （见 :meth:`parse` 里的注释）。原先第二个会被直接丢弃——想并行 / 不想并行
        都是它，所以想省掉这个浪费，就得先把它们**解析出来**（工具并行化 P1-1）。

        切分规则：每个 ``ACTION:`` 到**下一个** ``ACTION:``（或下一个结构标签）之间
        找 ``ACTION_INPUT:``，与 :meth:`parse` 的单动作切分保持一致。
        """
        out: List[Tuple[str, Dict[str, Any]]] = []
        for name, args in _action_pairs(text):
            tool = _clean_tool_name(name)
            if not tool:
                continue
            out.append((tool, args))
        return out

    def parse(self, text: str, step: int) -> Decision:
        # -1) 最先判幻觉：模型在扮演引擎时，整段响应都不可信——包括里面的
        # ACTION（它后面那串「已写入 / 已发布」全是自己编的）。
        #
        # 必须放在 ASK / FINAL **之前**：伪造的轨迹里往往还带着一个 FINAL，
        # 而 FINAL 分支会直接 return，导致本轮被当成正常回答、工具一个都不执行。
        # 真实案例：贪吃蛇那次，用户收到「已在右侧挂成产物卡片，点开就能跑」，
        # 实际文件没写、产物没挂。
        hallu = detect_hallucination(text or "")
        if hallu:
            return Decision(kind="hallucination", thought=self._thought(text), hallucination=hallu)

        # 0) 再看 ASK：需要用户补充信息，本轮到此暂停（优先于 FINAL）
        ask_seg = protocol.first_segment(text, "ASK")
        if ask_seg is not None:
            return Decision(
                kind="ask",
                ask=ask_seg.content.strip(),
                options=self._options(text),
                thought=self._thought(text),
            )

        # 1) 优先 FINAL（XML 优先，其次文本形态）
        #
        # 为什么 XML 优先：文本形态的 FINAL 允许写在行中间（旧协议容错），模型
        # 在正文里随口写一句 "FINAL: xxx" 也会被匹配上——若正文里那句排在后面，
        # 就会抢走真正的答案。XML 形态没有这个歧义（标签必须成对）。
        finals = protocol.sections_of(text, "FINAL", form="xml") or protocol.sections_of(
            text, "FINAL"
        )
        if finals:
            answer = finals[-1].strip()
            return Decision(kind="final", answer=answer, thought=self._thought(text))

        # 2.4) TASK：把子任务委托给子 agent（与 ACTION 平级）。携带自然语言要求，
        # 子 agent 自带内部循环（如 code：写代码→跑→看输出→迭代），结果当 OBSERVATION
        # 回喂主循环。要求原样捕获（到下一个标签为止），不做 JSON 转义。
        task_seg = protocol.first_segment(text, "TASK")
        if task_seg is not None:
            cap = _clean_tool_name(task_seg.content.splitlines()[0] if task_seg.content else "")
            req = protocol.section(text, "TASK_INPUT")
            ctx = protocol.section(text, "TASK_CONTEXT") or None
            if cap and req:
                return Decision(
                    kind="delegate",
                    capability=cap,
                    requirement=req,
                    context=ctx,
                    thought=self._thought(text),
                    action_count=1,
                )

        # 2) 否则解析 ACTION / ACTION_INPUT
        #
        # 协议本体是 ``<ACTION>工具名</ACTION>`` + ``<ACTION_INPUT>{JSON}</ACTION_INPUT>``；
        # 文本协议（``ACTION: 工具名``）与合并式（``<ACTION>工具名 {JSON}</ACTION>``）
        # 也照样认——混写、漏闭合都不会整轮作废。
        #
        # 工具名允许各种分隔符（内置点号名 fs.list_dir、MCP 双下划线名），不能
        # 用 \w+ 截取，否则会报「没有名为 fs 的工具」。
        pairs = _action_pairs(text)
        if pairs:
            tool, args = pairs[0]
            tool = _clean_tool_name(tool)
            # 统计这一段里出现了几个 ACTION —— prompt 要求「每次只写一个」，但模型
            # 经常连写多个（第二个会被丢弃）。这个计数是 P1-1 并行化的收益依据：
            # 没有它就只能靠感觉决定要不要做。
            return Decision(
                kind="act",
                tool=tool,
                args=args,
                thought=self._thought(text),
                action_count=max(1, len(pairs)),
            )

        # 2.5) 原生 tool-calling（DSML 等）：本接口的协议认不出。
        #
        # 为什么不「解析出来直接执行」而要整轮作废：那是**默认这套格式合法**了。
        # 模型一旦发现吐 DSML 能推动流程，下一轮就继续吐；而且这段文本会作为
        # assistant 消息进入历史，后面的轮次会拿它当范例模仿——实测线上第 12/30
        # 步整轮报废。正确做法和幻觉一样：**点破它 + 令其重来**。
        #
        # 连续点破仍不改时，process 侧再降级执行它那个调用（参数其实是完整的），
        # 见 ``native_as_act``。所以这一轮不会白白丢掉。
        native = self._parse_native_calls(text)
        if native is not None:
            return Decision(
                kind="format_error",
                thought=self._thought(text),
                format_error=(
                    "你输出的是原生工具调用标记"
                    "（invoke name=… 这种 XML/DSML 块），不是本接口要求的协议"
                ),
            )

        # 3) 都没出现：模型可能直接回答但未加标签，按最终回答处理
        #
        # 但**不能整段返回**：模型漏写 FINAL 时的典型输出是
        # 「THOUGHT: <思考>\n\n<正文>」，原样返回会让用户直接看到
        # 「THOUGHT: 用户只是打了个招呼…」这种正文（实测一句「你好」就能触发）。
        # 交给 _split_bare_answer 划界，只把真正的正文留给用户。
        answer, thought = self._split_bare_answer(text)
        return Decision(kind="final", answer=answer, thought=thought)

    @staticmethod
    def extract_final(text: str) -> Optional[str]:
        """提取 ``<FINAL>``（或 ``FINAL:``）之后的正文；没有就返回 ``None``。

        与 :meth:`parse` 的 final 分支同一套容错（含「不换行也认得出」）。单独
        暴露是因为**强制总结**那条路径必须在 parse 之外提取：模型在「已达上限、
        请给最终回答」的提示下仍经常顺手写出 THOUGHT + ACTION（它正处在「要调
        工具」的惯性里），这时 ``dec.answer`` 是 None，而 ``answer = resp`` 会把
        整段思考——含 ACTION / ACTION_INPUT——当成答案发给用户。
        """
        finals = protocol.sections_of(text, "FINAL", form="xml") or protocol.sections_of(
            text, "FINAL"
        )
        if not finals:
            return None
        ans = finals[-1].strip()
        return ans or None

    @staticmethod
    def _options(text: str) -> Optional[list]:
        """解析 OPTIONS：优先按 JSON 数组；解析不出时从原文抠出第一个 [...] 列表
        （容忍段尾跟着的示例模板 / 说明文字，避免把杂质按逗号切成垃圾）；再不行
        才按逗号/顿号切分。始终返回 None 或非空列表。"""
        raw = protocol.section(text, "OPTIONS").strip()
        if not raw:
            return None

        # 1) 整段就是合法 JSON 数组（最常见、最干净）
        try:
            obj = json.loads(raw)
            if isinstance(obj, list):
                return [str(x) for x in obj]
        except Exception:
            pass

        # 2) 段尾混入了多余文字（模型常把「输出格式」里的示例模板顺手复述出来）：
        #    从第一个 [ 起用 JSONDecoder 抠出第一个合法列表，忽略其后杂质
        start = raw.find("[")
        if start != -1:
            try:
                obj, _ = json.JSONDecoder().raw_decode(raw[start:])
                if isinstance(obj, list):
                    return [str(x) for x in obj]
            except Exception:
                pass

        # 3) 非 JSON（如「方式A，方式B」「a、b」）：按逗号/顿号切分兜底
        parts = [p.strip().strip("\"'") for p in re.split(r"[,，、]", raw) if p.strip()]
        return parts or None

    @staticmethod
    def _parse_native_calls(text: str) -> Optional[Tuple[str, dict, int]]:
        """解析原生 tool-calling（DSML）格式，返回 ``(工具名, 参数, 调用个数)``。

        认不出来返回 None。只取**第一个** invoke——ReAct 一轮只执行一个动作（见
        prompt 第 4 条），其余的仅计数后丢弃，与多个 ACTION 的处理保持一致。

        参数值可能是 JSON（结构化参数），也可能就是裸文本（bash 命令）；解析不了
        就按原文用，绝不因为猜不透格式而把整个调用丢掉。
        """
        invokes = _DSML_INVOKE.findall(text or "")
        if not invokes:
            return None
        name, body = invokes[0]
        args: dict = {}
        for k, v in _DSML_PARAM.findall(body):
            val = (v or "").strip()
            if val[:1] in "{[":
                try:
                    args[k] = json.loads(val)
                    continue
                except Exception:  # noqa: BLE001 解析不了就按原文用
                    pass
            args[k] = val
        return _clean_tool_name(name), args, len(invokes)

    def native_as_act(self, text: str, thought: str = "") -> Optional[Decision]:
        """把原生 tool-calling 响应转成可执行的 act 决策——**降级路径**用。

        连续点破若干次仍不改格式时走这里：模型那个调用的**参数往往是完整的**
        （``<parameter name=...>ls -la</parameter>`` 一目了然），直接丢掉太可惜，
        于是最后还是让它跑，但用我们的方式执行。

        解析不出来返回 None（理论上不会：刚被 ``_parse_native_calls`` 检出过）。
        """
        parsed = self._parse_native_calls(text)
        if parsed is None:
            return None
        tool, args, n_calls = parsed
        return Decision(
            kind="act",
            tool=tool,
            args=args,
            thought=thought,
            action_count=max(1, n_calls),
        )

    @staticmethod
    def _split_bare_answer(text: str) -> Tuple[str, str]:
        """拆「没写 FINAL、只写了 THOUGHT + 正文」的输出 → ``(正文, 思考)``。

        模型不按格式走时的典型输出（实测：一句「你好」就会触发）::

            <THOUGHT>用户只是打了个招呼，没有具体任务。直接回应即可。</THOUGHT>

            你好！我是这个 agent，可以帮你…

        之前这里把**整段**当答案返回，于是用户直接看到「THOUGHT: …」开头的正文；
        而 ``_thought`` 用 DOTALL 贪婪匹配到末尾，会把正文也当成思考，于是那段文字
        在「Agent自主规划过程」里再出现一次。两边都错，根因是没给 THOUGHT 与正文
        划界——XML 形态按标签划界，文本形态以**空行**为界（THOUGHT 段是思考，
        空行之后是正文）。

        只有 THOUGHT、后面什么都没有时，返回它去掉标签前缀的内容：总比把
        「THOUGHT:」这个标签暴露给用户强。
        """
        t = text or ""
        # XML 形态：THOUGHT 段之后、下一段之前的那段文字就是正文
        seg = protocol.first_segment(t, "THOUGHT", form="xml")
        if seg is not None:
            thought = seg.content.strip()
            nxt = protocol.next_segment_start(t, seg.end)
            body = t[seg.end : nxt].strip()
            return (body or thought), thought
        # 文本形态：行首标签 + 内容，一直到下一个空行（或文本结束）
        m = re.search(
            r"(?ms)^[ \t]*(?:THOUGHT|思考)[ \t]*[:：][ \t]*\n?(.+?)(?:\n[ \t]*\n|\Z)",
            t,
        )
        if not m:
            return t.strip(), ""
        thought = m.group(1).strip()
        body = re.split(_TAIL_LABEL, t[m.end():].lstrip())[0].strip()
        return (body or thought), thought

    @staticmethod
    def _thought(text: str) -> str:
        """取思考内容（协议段优先，宽松匹配兜底）。

        协议段天然截到下一个标签——模型伪造 OBSERVATION 时 THOUGHT 会跟着延伸到
        下一轮，这一段正好在那里被切断。
        """
        seg = protocol.first_segment(text, "THOUGHT")
        if seg is not None:
            t = seg.content
        else:
            # 兜底：标签写在行中间（模型不换行）
            m = re.search(r"(?ims)THOUGHT\s*[:：]\s*(.+)", text, re.S)
            if not m:
                return ""
            t = re.split(_TAIL_LABEL, m.group(1))[0]
        # 模型改用原生 tool-calling 时，THOUGHT 之后的 DSML 块是**调用**不是思考，
        # 不剥掉就会整块显示在「Agent自主规划过程」里。
        t = re.sub(_DSML_CALLS_BLOCK, "", t, flags=re.S)
        return t.strip()

    @staticmethod
    def _parse_args(raw: str) -> dict:
        """把 ACTION_INPUT 的内容解析成参数字典，尽量兜住模型的不规范写法。

        常见问题：
        - JSON 后面又追加了说明文字、甚至第二个 ACTION（会被吞成一整坨）；
        - 用了单引号 / 尾逗号等非法 JSON；
        - 干脆给裸文本。

        实在解析不出 JSON 时返回 ``{"_raw": ...}``，由工具明确报错让模型重试，
        **不要**让它退化成「没有参数」——那会被误读成「检索结果为空」。
        """
        raw = (raw or "").strip()
        if not raw or raw.lower() in ("{}", "none", "null"):
            return {}

        # 1) 整段本身就是合法 JSON
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

        # 2) 从头扫出第一个合法 JSON 对象，丢掉其后多余的说明 / 重复的 ACTION
        dec = json.JSONDecoder()
        for i, ch in enumerate(raw):
            if ch != "{":
                continue
            try:
                obj, _ = dec.raw_decode(raw[i:])
            except Exception:
                continue
            if isinstance(obj, dict):
                return obj

        # 3) 非 JSON：原样返回，交由 agent 反馈给模型重试
        return {"_raw": raw}
