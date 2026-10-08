"""ReAct 规划器：负责把「工具清单 + 问题 + 历史」喂给 LLM，并解析其决策。

采用模型无关的 ReAct 文本协议（不依赖原生 tool-calling），任意 chat 模型都可用：
- THOUGHT: 思考过程
- ACTION: 工具名
- ACTION_INPUT: JSON 参数
- FINAL: 最终回答

解析对中文标签（最终回答）与缺失标签都做了容错。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 「下一个结构性标签」的正则片段：任何一段（THOUGHT / ACTION_INPUT / FINAL / ASK …）
# 到这里就该结束，段尾 ``\Z`` 保证永远能匹配（等价于"取到文本末尾"）。
#
# 为什么 OBSERVATION 也在列
# -------------------------
# OBSERVATION 是**引擎注入**的，模型永远不该自己写。但实测它会自己伪造一整轮
# ReAct（THOUGHT → ACTION → ACTION_INPUT → OBSERVATION → 再来一轮），好像自己在
# 扮演引擎。若这里不在 OBSERVATION 处切断，伪造的 OBSERVATION 会被当成
# ACTION_INPUT 的续行一起吞进 JSON，参数直接解析失败——而且看起来像"模型把参数
# 写错了"，排查方向会被带偏。
_NEXT_LABEL = (
    r"(?=\n\s*(?:THOUGHT|ACTION_INPUT|ACTION|OBSERVATION|FINAL|最终回答"
    r"|ANSWER|ASK|提问|OPTIONS)\s*[:：]|\Z)"
)

# 「段尾可能混入的其它标签」，用于把 FINAL / THOUGHT / ASK 的正文切干净
_TAIL_LABEL = (
    r"\n\s*(?:THOUGHT|ACTION_INPUT|ACTION|OBSERVATION|FINAL|最终回答"
    r"|ANSWER|ASK|提问|OPTIONS)\s*[:：]"
)


def _tool_parallel_mode() -> int:
    """工具并行档位：0=关闭（默认）/ 1=只读 / 2=只读 + 显式声明。

    与 :meth:`Process` 里那份同名函数读的是同一个配置项（``context.tool_parallel``）。
    提示词必须跟着它走，否则会出现"提示词让模型只写一个、引擎却在等多个"的错位。
    """
    try:
        from .context_store import ctx_cfg

        v = int(getattr(ctx_cfg(), "tool_parallel", 0) or 0)
    except Exception:  # noqa: BLE001 配置读不到就按最保守的"关闭"处理
        v = 0
    return v if v in (0, 1, 2) else 0


def _action_rule() -> str:
    """按并行开关生成准则 4（一次写几个 ACTION）。

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
            "4. **每次响应只写一个 ACTION**：写了第二个也**不会被执行**（系统只取第一个），"
            "想再查一次，等看到 OBSERVATION 后的下一轮再写。\n"
        )
    limit = 5
    try:
        from .context_store import ctx_cfg

        limit = max(1, int(getattr(ctx_cfg(), "tool_parallel_max", 0) or 0) or 5)
    except Exception:  # noqa: BLE001
        pass
    return (
        f"4. **互相独立的只读查询可以合并成一轮，写最多 {limit} 个 ACTION**，"
        "每个 ACTION / ACTION_INPUT 各占一对行；有依赖的（要先看 A 的结果再决定 B）"
        "必须拆成多轮，等看到 OBSERVATION 后的下一轮再写。\n"
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


def _clean_tool_name(raw: str) -> str:
    """清洗 ACTION 行里的工具名。

    模型偶尔会把工具名写成 ```fs.list_dir``` 或把多个标签挤在同一行，这里逐一处理：
    ① 截到下一个已知标签之前；② 去掉 Markdown 反引号 / 引号包裹；③ 去首尾空白。
    """
    line = raw or ""
    # ① 同一行若又出现其它标签（如 ACTION: fs.list_dir ACTION_INPUT: {...}），截到标签前
    line = re.split(
        r"(?i)\s+(?:THOUGHT|ACTION_INPUT|ACTION|OBSERVATION|FINAL|最终回答"
        r"|ANSWER|ASK|提问|OPTIONS)\s*[:：]",
        line,
    )[0]
    return line.strip().strip("`\"'，,;；。").strip()


def _workspace_section() -> str:
    """拼出「工作区」说明：**两个**工作区、各自的路径 / 职责 / 权限。

    为什么必须把这三个属性都写死
    ----------------------------
    之前这里只有一句「临时文件用 `@tmp/` 前缀」，模型实际拿不到三样东西，于是
    只能靠猜（实测踩坑：它写对了 `@tmp/x.js`，紧接着用 `find <父目录>` 和 `..`
    去"找"这个文件，两次被沙箱拒绝就放弃 `@tmp/`，改把脚本写进用户工作区，
    临时脚本反过来变成了展示给用户的产物）：

    ① **路径**。提示词只说"本会话私有的临时目录"，是个概念不是路径。真实路径
       里含两个随机 ULID，不主动给的话模型无从推断结构。
    ② **拓扑**。两个工作区是 ``workspace/user/<x>`` 与 ``workspace/session/<sid>``
       这两棵**平级**的树，``..`` 从user 只能走到 ``workspace/user/``，永远到不了
       session。模型默认按"临时目录是工作区的子目录"来推理，必然试错。
    ③ **哪个工具认前缀**。``fs.*`` 认 ``@tmp/``，``bash`` 不认。模型不知道这个
       分歧，就会拿前缀去写 bash 命令。

    所以这里直接给两条绝对路径 + 职责 + 权限 + 前缀作用域，并明确「平级、别用
    ``..``」。两个工作区用 ``① ②`` 编号而不是各起一段，也是为了让模型把它们当成
    两个并列的具名实体，而不是"工作区"和"某个附属目录"。

    无工作空间上下文时（构建期 / 单测）退化成只讲约定，不编路径。
    """
    from ..chat.context import current_workspace
    from ..tools.builtin.sandbox import TMP_PREFIX

    ws = current_workspace()
    ro_hint = (
        "（当前这个用户工作区是**只读**的：你能读、能运行，但**不能写**——"
        "要交付文件请写到 ② 临时工作区，并在最终回答里说明文件放在那里。）"
        if (ws is not None and ws.read_only)
        else "可读、可写。写在这里的文件会作为产物展示给用户。"
    )
    out = (
        "7. **工作区：有两个，职责与权限不同**\n"
        "   它们是**平级的两棵目录树**，不是父子关系——"
        "不要用 `..`、也不要拿任何一个的父目录去定位另一个。\n\n"
        f"   **① 用户工作区**\n"
        f"   - 路径：`{ws.root if ws else '（当前不可用）'}`\n"
        "   - 职责：放**要交付给用户**的东西（最终产物、文档、报告）。\n"
        f"   - 权限：{ro_hint}\n\n"
        f"   **② 会话临时工作区**\n"
        f"   - 路径：`{ws.agent_space if ws else '（当前不可用）'}`\n"
        "   - 职责：放**过程文件**——测试脚本、调试脚本、抽取脚本、中间产物、"
        "一次性数据。\n"
        "   - 权限：可读、可写。**这里的内容不会展示给用户**，会话结束后也不再保留。\n"
        f"   - 简写：在 `fs.*` 工具里可以用 `{TMP_PREFIX}/` 前缀代替上面的路径"
        f"（`{TMP_PREFIX}/a.js` 就是该目录下的 `a.js`）。\n\n"
        "   **该往哪写**：要给用户看的 → ①，用相对路径（`index.html`、`docs/report.md`）；"
        f"只是自己验证 / 中间步骤 → ②，用 `{TMP_PREFIX}/` 前缀。\n\n"
        f"   **在命令里怎么引用 ②**：`bash` 这类工具**不认** `{TMP_PREFIX}/` 前缀，"
        "只认绝对路径。两个办法：\n"
        "   - 写完之后，`fs.write_file` 的回执会回显该文件的真实绝对路径，"
        "跑命令时直接用那个路径；\n"
        f"   - 想先摸清 ② 里有什么，用 `fs.list_dir` / `fs.find` 配 `{TMP_PREFIX}/` 前缀"
        "（这两个工具认前缀）。\n\n"
    )
    return out


def _delivery_section() -> str:
    """拼出「交付物挂载 + 上下文被折叠时怎么办」。

    这两条都是**实测踩出来的**，而且都属于「模型无从推断、只能靠试错」的类别：

    ① **产物只认两种动作**。实测一整轮把大文件分段写进 ②、再用 ``cat`` 合并成
       ① 里的 ``index.html``，结果文件是好的，但**会话里没有产物卡片**——因为
       产物只登记 ``fs.write_file`` 与 ``fs.publish`` 两个工具的动作，而 ``cat``
       创建的文件两者都不是。模型不知道这件事，就只能说出「文件在工作区」这种
       没法点开的废话。

    ② **工具输出会被折叠**。超阈值的输出在上下文里只剩「首尾预览 + ``ctx:<id>``」。
       模型看不到全文时**正确反应是展开，而不是重发命令**——但它不知道有
       ``ctx:<id>`` 这条路，于是选了重发。这会直接变成死循环：重发 → 更多输出 →
       更多折叠 → 更想重发。实测因此把一个简单任务烧到 30 步上限。
    """
    return (
        "8. **交付物要显式挂载，用户才看得到**\n"
        "   产物卡片只登记两种动作：`fs.write_file` 写到 ①，"
        "或 `fs.publish <路径>` 把已在 ① 里的文件登记上来。\n"
        "   用 `bash` 生成的文件（`cat a > b`、`npm run build` 之类）**不会**被"
        "自动挂载——必须补一次 `fs.publish`，否则用户只听到「文件在工作区」，"
        "拿不到可点开的卡片。\n"
        "   文件大到一次写不下时的标准做法：分段写进 ② → 用 `cat` 合并到 ① → "
        "`fs.publish` 挂载。\n\n"
        "9. **工具输出被折叠时，要展开而不是重发**\n"
        "   超长的工具输出在上下文里会变成「首尾预览 + `ctx:<id>`」。"
        "看到 `ctx:` 开头的引用说明**命令早已执行过**，全文用 `read` 工具配 "
        "`block_id=\"ctx:...\"` 取回。\n"
        "   重新发一遍同样的命令不会让它重新出现，只会白等一轮，"
        "在写文件的场景下还会多出一份重复文件。\n\n"
        "10. **THOUGHT 里的计划不会自动执行**\n"
        "   「我先写 A 再写 B」只是**打算**；只有本轮真正发出的 ACTION 才被执行。"
        "没收到工具回执 = 那一步没做过。判断「做没做」只看回执，不看自己想没想过。\n\n"
        "11. **同一个动作失败两次就换思路**\n"
        "   同一条命令、同一个写法连续失败（哪怕失败理由看着不一样），"
        "不要再原样发第三遍——换个路径、换个工具、或者拆成更小的步骤。\n"
        "   `returncode: 1` 反复出现时，先确认依赖的文件是不是真的都在"
        "（用 `fs.list_dir` 看一眼），而不是继续重跑合并命令。\n\n"
        "12. **先交付，再验证；验证最多两轮就收**\n"
        "   产物一写出来就先 `fs.publish` 挂上（用户立刻拿得到卡片），再回头验证。\n"
        "   验证的目的是确认「能不能跑」，不是把它调优到完美：同一类验证"
        "**最多做 2 轮**，还不通过就交付，并在 FINAL 里写清「已知限制」，"
        "不要继续修下去。\n"
        "   尤其**不要给自己加用户没要求的验收标准**——比如用户只要一个能玩的"
        "HTML 文件，你却要求「AI 试玩必须下到第 N 层」：这类自设指标没有终点，"
        "会一路烧到步数上限，最后连产物都没挂上。\n\n"
    )


def _max_output_tokens() -> int:
    """单次回复的输出上限（token）；0 / 读不到 = 不限制。"""
    try:
        from .context_store import ctx_cfg

        return int(getattr(ctx_cfg(), "max_output_tokens", 0) or 0)
    except Exception:  # noqa: BLE001 配置读不到就按"不限制"处理，与运行行为一致
        return 0


def _output_budget_section() -> str:
    """拼出「单次输出有上限，超了要自己分段」这一条。

    为什么必须**告诉模型**数字，而不只是给引擎配个上限
    --------------------------------------------------
    撞上输出上限时，模型那边看到的是「输出正常结束」——它不知道被截断了。
    于是它以为整个 ``ACTION_INPUT: {...}`` 已经写完，实际上是个半截 JSON：
    参数解析失败、工具报一些看不懂的错，它多半会换个写法再试一遍，往往又超。
    实测就是这么把一个任务烧到步数上限的。

    所以这里的重点不是"限制模型"，而是**让它按预算分配输出**：短的写参数，
    长的分段写。要点写具体（给字符数、给分段套路），不然等于没提醒。

    没配置上限（0）时整段返回空串——那时按 provider 默认走，提示词不提数字，
    免得给出一个与实际不符的上限。
    """
    n = _max_output_tokens()
    if n <= 0:
        return ""
    # token→字符用 2.0 粗估（中文更接近 1.5，代码接近 3），取中间值且**宁可
    # 偏小**：这条是让模型少写，不是让它写满，写少了顶多多一轮，写多了被截。
    per = max(200, int(n * 2 * 0.5))
    return (
        f"13. **单次回复的输出上限是 {n} token（约 {n * 2} 字符），超出会被截断**\n"
        f"   截断发生在句子中间，参数 JSON 会不完整、工具必然失败，**而你这边看不出"
        f"自己被截了**（只会觉得「输出结束了」）。所以要主动按预算分配：\n"
        f"   - 单个 `ACTION_INPUT` 的内容控制在 **{per} 字符以内**；一次写不完就"
        f"**分多次调用**（文件用 `fs.write_file` 分段落盘，再 `bash` 合并，"
        f"最后 `fs.publish` 挂载），不要试图一次写完整份文件 / 脚本。\n"
        f"   - `THOUGHT` 只写决定和理由，不要复述代码、文档正文或工具返回的原文。\n"
        f"   - 要引用的大段内容用 `read` / 工具参数**分次**取，不要整段抄进参数。\n\n"
    )


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
    ) -> str:
        """拼出 system 提示词。

        顺序是有讲究的——**越靠后权重越高**：

        ① 身份（名字 + persona）→ ② 可用工具 → ③ 可用技能 → ④ 工作准则
        → ⑤ skill 的额外要求 → ⑥ 输出格式

        「输出格式」必须排在最后：它是引擎契约，不能被 persona 或 skill 的内容
        覆盖掉，否则模型不再输出 THOUGHT / ACTION，ReAct 循环直接断。
        """
        ident = f"（你的名称：{agent_name}）" if agent_name else ""
        base = (
            "你是一个遵循 ReAct（推理-行动）模式工作的 AI 助手" + ident + "。\n"
            "你可以调用工具来获取真实信息，再综合给出最终回答；"
            "不要编造未经工具确认的事实。\n\n"
        )
        if persona:
            base += "## 你的角色设定\n" + persona.strip() + "\n\n"
        base += "## 可用工具\n" + tools_desc + "\n\n"
        if skill_desc:
            base += "## 可用的技能（Skill）\n" + skill_desc + "\n\n"
        base += (
            "## 工作准则\n"
            "1. 先思考（THOUGHT），再决定是否需要调用工具（ACTION）。\n"
            "2. 需要信息时调用工具；工具返回结果（OBSERVATION）后据此继续推理。\n"
            "   **OBSERVATION 是系统注入的标签，你绝对不要自己写。**"
            "一轮里只写你自己的那几行就停下等回执——自己接着写 OBSERVATION、"
            "再自己写下一轮 THOUGHT / ACTION，等于凭空捏造工具结果，"
            "会让整段推理作废。\n"
            "3. 信息足够时给出最终回答（FINAL），回答要基于工具返回的真实信息。\n"
            + _action_rule()
            + "5. 只引用工具实际返回过的内容；工具没返回过的，禁止凭空列出。\n"
            "6. 需要用户做决定时**一律用 ASK + OPTIONS**，不要把候选项写进 FINAL 正文。\n"
            "   触发场景：信息不足；有多种可能你无法判断；或你要让用户在若干候选\n"
            "   （工具 / 接口 / 方案 / 字段 / 参数值）里挑一个。\n"
            "   特别注意「1. xxx  2. yyy，请告诉我你想了解哪一个」这类——看着像陈述，\n"
            "   实际是在提问，必须走 ASK + OPTIONS；写成 FINAL 的话对端只拿到一段\n"
            "   文字，拿不到可点的选项。拿到回答后再继续，不要凭猜测硬答。\n\n"
        )
        base += _workspace_section()
        base += _delivery_section()
        # 放在工作准则最后（编号 13，紧跟交付/验证那几条）：它是「输出预算」，
        # 与紧邻的分段写文件套路是同一件事的延续，读起来连得上。
        base += _output_budget_section()
        if system_extra:
            base += "## 当前技能的额外要求\n" + system_extra.strip() + "\n\n"
        base += (
            "## 输出格式（严格遵守）\n"
            "**每个标签必须单独占一行，且标签名顶在该行最前面。**\n"
            "不要把标签夹在句子里（错误示例：「…那我就回答了。FINAL: 具体建议是…」），\n"
            "这种写法解析不到你的回答，会把整段思考当成答案发给用户。\n"
            "正确写法（每行一个标签）：\n"
            "THOUGHT: 你的思考过程\n"
            "ACTION: 工具名\n"
            "ACTION_INPUT: {\"参数名\": \"值\"}\n"
            "（信息足够、直接给出最终回答时）\n"
            "FINAL: 最终回答内容（可多行；FINAL 之后不要再写任何标签）\n"
            "（需要用户补充信息时）\n"
            "ASK: 需要用户回答的问题\n"
            "OPTIONS: [\"候选1\", \"候选2\"]\n\n"
            "示例 1（调工具）：\n"
            "THOUGHT: 这个问题需要外部信息，我先查一下再回答。\n"
            "ACTION: 上面「可用工具」清单里的某个工具名\n"
            "ACTION_INPUT: {\"参数名\": \"值\"}\n\n"
            "示例 2（问用户）：\n"
            "THOUGHT: 有几种做法会影响结果，用户没说倾向哪一个，需要他定。\n"
            "ASK: 你希望采用哪种方式？\n"
            "OPTIONS: [\"方式A\", \"方式B\"]\n\n"
            "示例 3（让用户在若干候选里挑一个，别写成 FINAL 列举）：\n"
            "THOUGHT: 查到多个候选，用户没指定是哪个，需要他选择。\n"
            "ASK: 你想了解哪一个？\n"
            "OPTIONS: [\"候选1\", \"候选2\", \"候选3\"]\n\n"
            "示例 4（标签必须顶行首，FINAL 尤其注意）：\n"
            "错误：我想清楚了可以直接回答。FINAL: 建议你这样安排…\n"
            "正确：\n"
            "THOUGHT: 想清楚了，可以直接回答。\n"
            "FINAL: 建议你这样安排…\n"
        )
        # 历史记忆是「随每轮变化」的内容，必须放在 system 最末尾：
        # DeepSeek 前缀缓存要求前缀从头逐 token 一致，变化内容放中间会切断
        # 跨轮稳定前缀，导致整段系统提示词都吃不到缓存。放在末尾后，
        # 上面的身份/工具/技能/准则/输出格式全部成为可跨轮命中的稳定前缀。
        if memory_hint:
            base += (
                "\n## 历史记忆摘要（系统自动注入，可能相关；"
                "如需某块的完整内容，用 read 工具并传入其 block_id）\n"
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
        matches = list(
            re.finditer(r"(?im)(?:^|\n)\s*ACTION\s*[:：]\s*([^\n]+)", text)
        )
        for i, m in enumerate(matches):
            tool = _clean_tool_name(m.group(1))
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            seg = text[m.end() : end]
            im = re.search(
                r"(?is)ACTION_INPUT\s*[:：]\s*(.*?)" + _NEXT_LABEL,
                seg,
            )
            out.append((tool, self._parse_args(im.group(1).strip() if im else "")))
        return out

    def parse(self, text: str, step: int) -> Decision:
        # 0) 最先看 ASK：需要用户补充信息，本轮到此暂停（优先于 FINAL）
        m = re.search(r"(?ims)(?:^|\n)\s*(?:ASK|提问)\s*[:：]\s*(.+)", text)
        if m:
            ask = re.split(
                r"\n\s*(?:OPTIONS|THOUGHT|ACTION_INPUT|ACTION|OBSERVATION"
                r"|FINAL|最终回答|ANSWER)\s*[:：]",
                m.group(1),
            )[0].strip()
            return Decision(
                kind="ask",
                ask=ask,
                options=self._options(text),
                thought=self._thought(text),
            )

        # 1) 优先 FINAL / 最终回答 / ANSWER（按格式应另起一行）
        answer_text = None
        m = re.search(
            r"(?ims)(?:^|\n)\s*(?:FINAL|最终回答|ANSWER)\s*[:：]\s*(.+)", text
        )
        if m:
            answer_text = m.group(1)
        else:
            # 容错：模型有时不换行，写成「…给出 FINAL。FINAL: 正文」。
            # 这时严格的行首匹配会失败，进而把整段 THOUGHT 当成回答显示给用户；
            # 退一步取最后一次出现的 FINAL 标记，保证回答里只有正文。
            found = re.findall(
                r"(?is)(?:FINAL|最终回答|ANSWER)\s*[:：]\s*(.+)", text
            )
            if found:
                answer_text = found[-1]

        if answer_text is not None:
            answer = answer_text.strip()
            # 去掉末尾可能混入的其它标签
            answer = re.split(_TAIL_LABEL, answer)[0].strip()
            return Decision(kind="final", answer=answer, thought=self._thought(text))

        # 2) 否则解析 ACTION / ACTION_INPUT
        am = re.search(
            # 工具名允许各种分隔符，不能再用 \w+：\w 不匹配 '.' 和 '-'，会把工具名截短
            #  → 内置点号名 fs.list_dir 截成 fs、git.status 截成 git、
            #    MCP 名 codebase-memory__graph__list_projects 截成 codebase，
            #    最终报「没有名为 xxx 的工具」。
            #  这里直接取 ACTION 的整行内容（因此天然支持中文 / MCP 双下划线 / 点号 / 连字符），
            #  再用 _clean_tool_name 做清洗。
            r"(?ims)(?:^|\n)\s*ACTION\s*[:：]\s*([^\n]+)", text
        )
        if am:
            tool = _clean_tool_name(am.group(1))
            # 注意：这里必须非贪婪到「下一个标签」为止。模型经常一次吐出两个
            # ACTION（想连查两次），若用 \{.*\} 配 DOTALL，会把第二个 ACTION
            # 一起吞进来，导致 json 解析失败 -> 参数变成 {"_raw": ...} -> query 为空。
            im = re.search(
                r"(?ims)(?:^|\n)\s*ACTION_INPUT\s*[:：]\s*(.*?)" + _NEXT_LABEL,
                text,
            )
            raw = im.group(1).strip() if im else ""
            args = self._parse_args(raw)
            # 统计这一段里出现了几个 ACTION —— prompt 要求「每次只写一个」，但模型
            # 经常连写多个（第二个会被丢弃）。这个计数是 P1-1 并行化的收益依据：
            # 没有它就只能靠感觉决定要不要做。
            try:
                action_count = len(
                    re.findall(r"(?im)(?:^|\n)\s*ACTION\s*[:：]", text)
                )
            except Exception:  # noqa: BLE001 统计失败不该影响解析
                action_count = 1
            return Decision(
                kind="act",
                tool=tool,
                args=args,
                thought=self._thought(text),
                action_count=max(1, action_count),
            )

        # 3) 都没出现：模型可能直接回答但未加标签，按最终回答处理
        return Decision(kind="final", answer=text.strip(), thought=self._thought(text))

    @staticmethod
    def extract_final(text: str) -> Optional[str]:
        """提取 ``FINAL:`` 之后的正文；没有就返回 ``None``。

        与 :meth:`parse` 的 final 分支同一套容错（含「不换行也认得出」）。单独
        暴露是因为**强制总结**那条路径必须在 parse 之外提取：模型在「已达上限、
        请给最终回答」的提示下仍经常顺手写出 THOUGHT + ACTION（它正处在「要调
        工具」的惯性里），这时 ``dec.answer`` 是 None，而 ``answer = resp`` 会把
        整段思考——含 ACTION / ACTION_INPUT——当成答案发给用户。
        """
        found = re.findall(
            r"(?is)(?:FINAL|最终回答|ANSWER)\s*[:：]\s*(.+)", text or ""
        )
        if not found:
            return None
        ans = found[-1].strip()
        # 去掉末尾可能混入的其它标签
        ans = re.split(_TAIL_LABEL, ans)[0].strip()
        return ans or None

    @staticmethod
    def _options(text: str) -> Optional[list]:
        """解析 OPTIONS：优先按 JSON 数组；解析不出时从原文抠出第一个 [...] 列表
        （容忍段尾跟着的示例模板 / 说明文字，避免把杂质按逗号切成垃圾）；再不行
        才按逗号/顿号切分。始终返回 None 或非空列表。"""
        m = re.search(r"(?ims)(?:^|\n)\s*OPTIONS\s*[:：]\s*(.+)", text)
        if not m:
            return None
        raw = m.group(1).strip()

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
    def _thought(text: str) -> str:
        m = re.search(r"(?ims)THOUGHT\s*[:：]\s*(.+)", text, re.S)
        if not m:
            return ""
        # 模型伪造 OBSERVATION 时 THOUGHT 会跟着延伸到下一轮，同样要在标签处切断
        return re.split(_TAIL_LABEL, m.group(1))[0].strip()

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
