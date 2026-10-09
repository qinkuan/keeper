"""对话核心逻辑：ReAct 循环（思考 → 调工具 → 观测 → FINAL）。

这一层只负责「怎么想」：拿到一个问题，多步推理直到给出答案。

分工：**工具怎么收集**归 keeper（``keeper.py`` 的 ``_collect_tools``），
**怎么把自己装起来**归本模块——``Process.from_agent`` 从 agent 实例吸取
llm / persona / skills，产出一份可直接 ``run`` 的可执行体。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from .context_store import Compactor, clip_observation, ctx_cfg, externalize
from .planner import ReActPlanner

if TYPE_CHECKING:  # 仅类型检查时引入，避免运行时循环导入
    from .keeper import KeeperAgent

logger = logging.getLogger(__name__)

# 「最终回答」标签：与 planner.parse 的 FINAL 判定保持一致。
# 流式时用它判断从哪儿开始才是给用户的回答（之前是 THOUGHT / ACTION）。
_FINAL_RE = re.compile(r"(?ims)(?:^|\n)\s*(?:FINAL|最终回答|ANSWER)\s*[:：]\s*")

# 工具「失败」的 observation 标记。多数失败是工具**正常返回**一段错误文本而不是
# 抛异常，所以只能靠这些前缀判断——它们是 fs.py / guard.py 约定的稳定前缀。
_ERR_OBS_MARKERS = (
    "[缺少参数", "[拒绝]", "[文件不存在]", "[不是目录]", "[工作空间为只读",
    "[工具 ", "returncode: 1", "Traceback", "error:",
)


def _is_error_obs(obs: str) -> bool:
    """observation 是否表示这次调用失败了（供重复重试检测使用）。"""
    if not obs:
        return False
    head = obs.lstrip()[:200]
    return any(m in head for m in _ERR_OBS_MARKERS)


def _fit_observation(tool: str, raw: str, limit: int) -> tuple[str, int]:
    """把单条工具输出压到 ``limit`` 以内：**先外部化，再硬截断**。

    两种降级的信息损失完全不同：

    - 外部化：原文落盘 + 给 ``block_id``，模型能 ``read(block_id)`` 展开全文。
      信息**不丢**，只是要多一步。
    - 硬截断：只留 70% 头 + 30% 尾，中间那段既没落盘也没有 id，模型永远拿不
      回来——它只看到「省略了多少字符」，不知道该干什么，往往只能原样重调
      同一个工具（内置工具还没有分页参数），于是打转。

    为什么必须是这个顺序：以前是「先截断、再让滚动外部化判断」，可 messages 里
    的内容已经 ≤ limit（3500），永远够不到 ``externalize_min_chars``（5000），
    外部化这条兜底路径实际**一次都没触发过**——超长结果只有截断，没有出路。

    外部化不触发的情况（总开关关 / 在 ``never_externalize`` 名单里 / 写盘失败）
    一律退回硬截断，此时它仍是唯一的即时保护。

    返回 ``(给模型的文本, 工具原始输出长度)``。
    """
    raw_len = len(raw)
    if limit > 0 and raw_len > limit:
        # 阈值用 limit（不是 externalize_min_chars）：超过硬截断线就必须给
        # block_id，否则这段内容既没落盘也没 id，谁都拿不回来。
        kept = externalize(tool, raw, min_chars=limit + 1)
        if kept != raw:
            logger.info(
                "[context] 单条超长输出已外部化：%s %d 字符 → 预览 + block_id",
                tool,
                raw_len,
            )
            return kept, raw_len
    return clip_observation(raw, limit), raw_len


def _call_key(name: str, args: Any) -> str:
    """工具名 + 参数的稳定指纹，用来识别「完全相同」的调用。

    参数可能含不可序列化对象，所以走 ``json.dumps(default=str)``；``sort_keys``
    保证键序不同但内容相同的 dict 落到同一个 key。
    """
    try:
        payload = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        payload = str(args)
    return f"{name}::{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:12]}"

# 按需加载：统一的加载器工具名（设计文档第六节——只给一个，不要 load_skill/load_tool 两个）
_CAP_LOADER = "load_capabilities"
# 一轮里最多同时保留几份 L2 正文。再多就挤上下文了，按加载顺序 FIFO 换出。
_CAP_MAX = 3

# 「小规模」阈值（正文 token 粗估合计）：低于它直接全量预加载，不做判读
_CAP_SMALL_LIMIT = 6000
# 判读/关键词最多预加载几个
_CAP_PRELOAD_TOP = 2

# 工具组折叠门槛（设置读不到时的兜底）：组内工具数达到它就只列名字、不列参数
_TOOL_GROUP_MIN = 3
# 单轮最多补载几次工具定义：防模型乱调名字把上下文刷满
_TOOL_DISCLOSE_MAX = 8


def _tool_parallel_mode() -> int:
    """工具并行档位：0=关闭（默认，行为与从前一致）/ 1=只读 / 2=只读+显式声明。"""
    try:
        v = int(getattr(ctx_cfg(), "tool_parallel", 0) or 0)
    except Exception:  # noqa: BLE001
        v = 0
    return v if v in (0, 1, 2) else 0


def _tool_parallel_max() -> int:
    """单轮最多并发几个：防止模型一次写出几十个把 MCP server / 连接池打爆。"""
    try:
        v = int(getattr(ctx_cfg(), "tool_parallel_max", 0) or 0)
    except Exception:  # noqa: BLE001
        v = 0
    return v if v > 0 else 5


def _cap_preload_policy() -> str:
    """预加载策略：**只认设置页**（`context.capability_preload`），非法值回落 auto。

    配置入口只有一处——这也是刻意的选择：它跟旁边的「最近保留步数」「保活条数」
    是同一类旋钮，都归设置页管；再开一个环境变量口子，只会让人不知道该改哪个。
    """
    try:
        v = str(getattr(ctx_cfg(), "capability_preload", "") or "").strip().lower()
    except Exception:  # noqa: BLE001
        v = ""
    return v if v in ("auto", "off", "keyword", "llm") else "auto"


def _cap_small_limit() -> int:
    """「技能少」的判定线：读设置，读不到就用内置默认。"""
    try:
        v = int(getattr(ctx_cfg(), "cap_small_limit", 0) or 0)
    except Exception:  # noqa: BLE001
        v = 0
    return v if v > 0 else _CAP_SMALL_LIMIT


def _tool_group_min() -> int:
    """工具组的折叠门槛：组里达到几个工具就折成一行。"""
    try:
        v = int(getattr(ctx_cfg(), "tool_group_min", 0) or 0)
    except Exception:  # noqa: BLE001
        v = 0
    return v if v > 0 else _TOOL_GROUP_MIN


def _chars_per_token() -> float:
    """一个 token 大约几个字符（沿用上下文设置里的粗估系数）。"""
    try:
        v = float(getattr(ctx_cfg(), "chars_per_token", 0) or 0)
        return v if v > 0 else 2.0
    except Exception:  # noqa: BLE001
        return 2.0


@dataclass
class Step:
    # thought 允许为空：并非每步都有「想法」，比如 human_answer（用户的回答）只是
    # 一条输入，硬要求它填 thought 只会逼调用方塞占位符
    thought: str = ""
    tool: Optional[str] = None
    args: Optional[dict] = None
    observation: Optional[str] = None
    # "ask" = 向用户提问（此时 observation 是问题内容）；其余为工具调用或纯思考
    kind: Optional[str] = None


@dataclass
class ProcessResult:
    answer: str
    steps: List[Step] = field(default_factory=list)
    used_tools: bool = False
    # 需要向用户提问：本轮挂起，等该会话的下一条消息来恢复
    ask: Optional[str] = None
    options: Optional[list] = None
    # 用户点了「暂停」：停在某一步之后——那一步已实时落库，未开始的下一步直接不跑。
    # 下次执行时把已落库的步骤重放给模型，从断点继续（不重复已完成的工作）。
    paused: bool = False
    # 用户点了「停止生成」：本轮在生成途中被中断，已输出的内容保留
    canceled: bool = False
    # 预算触顶被强制收尾：本轮**没跑完**，是「干到预算线就收尾」（不是正常 FINAL）。
    # 触顶后不再发起新的 LLM 调用——否则「拦截」本身又烧一笔，实际消耗会超过上限。
    budget_stopped: bool = False
    # 触发收尾的那次预算快照（scope / limit / used / rate），给上层写提示文案用
    budget: Optional[Dict[str, Any]] = None


class Process:
    """一次问答的执行过程（ReAct 多步循环）。

    llm / tools 由 keeper 注入；skill 以 skill_desc + system_extra 两段文本注入，
    本类不理解 skill 的结构。
    """

    def __init__(
        self,
        llm,
        tools,
        *,
        agent_name: str = "",
        persona: str = "",
        skill_desc: str = "",
        system_extra: str = "",
        # 技能本体（Skill 对象列表）：按需加载时按名取正文用。skill_desc /
        # system_extra 只是**已经渲染好**的文本，加载时要的是原始正文。
        skills: Optional[List[Any]] = None,
        # 一轮 ReAct 最多跑多少步（一步 = 一次「思考 + 调工具 + 看结果」）。
        # 原来 10 对多步骤任务偏紧，容易被超步数强制总结；放宽到 30。
        # **不传就每次 run 现读配置**（context.max_steps / observation_limit），
        # 这样在设置页改完立即生效，不必重新装载 agent。
        max_steps: Optional[int] = None,
        observation_limit: Optional[int] = None,
    ) -> None:
        self.llm = llm
        self.registry = tools
        # 工具名 + 参数指纹 -> 连续失败次数。用于打断「同参数反复重试」的死循环
        #（实测见过一条 `cat a b > out` 被重发十几次，模型始终没意识到该换思路）。
        # 成功一次就清零：问题可能已被别的动作解决了。
        self._recent_failures: Dict[str, int] = {}
        self._max_steps_arg = max_steps
        self._observation_limit_arg = observation_limit
        self.max_steps = max_steps or 30
        self.planner = ReActPlanner(max_steps=self.max_steps)
        self.agent_name = agent_name
        self.persona = persona
        self.skill_desc = skill_desc
        self.system_extra = system_extra
        self.observation_limit = observation_limit or 3500
        # 预算守卫的作用域（会话 / 任务）：由 run / resume 传入。两个都没有就
        # 不做预算检查——没配上限时查库纯属白跑。
        self._budget_scope: Tuple[Optional[str], Optional[str]] = (None, None)
        # 用掉这么多（比例）就开始让模型收尾：留一点余量给「结论」这一次调用，
        # 免得刚提示就触顶，模型连总结的机会都没有。
        self.budget_warn_rate = 0.9

        # ---- 能力按需加载（L1 概要常驻 / L2 正文按需，见 capability-loading-design.md）
        self.skills: List[Any] = list(skills or [])
        self._cap_loaded: Dict[str, str] = {}  # key -> 已注入正文（判重用）
        self._cap_order: List[str] = []  # 注入顺序，FIFO 换出用
        self._cap_msgs: Dict[str, Dict[str, Any]] = {}  # key -> 对应的那条消息
        self._cap_just_loaded: List[str] = []  # 本次 load 实际新加载的 key
        # key -> 正文现在在哪（"system" 预加载 / "messages" 模型半路加载）。
        # 判重时要告诉模型去哪儿找，否则它会以为「说是有，可我没看见」。
        self._cap_source: Dict[str, str] = {}
        # 完整定义**已在上下文里**的工具（内置工具、没被折叠的小组）。
        # 不在这里的：只列了名字，调用时要先补定义（P1 的调用触发补载）。
        self._tool_disclosed: set = set()
        self._tool_disclosed_n = 0

    def _refresh_limits(self) -> None:
        """每轮开始时刷新「步数上限 / 单条截断」——改设置立即生效。"""
        try:
            cfg = ctx_cfg()
            if cfg is None or not getattr(cfg, "enabled", True):
                return
            if self._max_steps_arg is None:
                self.max_steps = int(getattr(cfg, "max_steps", 30) or 30)
            if self._observation_limit_arg is None:
                self.observation_limit = int(
                    getattr(cfg, "observation_limit", 3500) or 3500
                )
            self.planner.max_steps = self.max_steps
        except Exception as e:  # noqa: BLE001
            logger.debug("刷新上下文上限失败（沿用旧值）: %s", e)

    async def _check_budget(self) -> Optional[Dict[str, Any]]:
        """本轮累计用量离预算上限还有多远；上限未配置时返回 None（不限制）。

        只在**每步之后**调一次：一轮里的消耗是逐步累加的，只在会话开始查一次
        的话，长任务能在单轮内把额度烧穿（这就是「预算没真正拦截」的漏洞）。
        查库失败一律放行——配额不该把正常对话挡在门外。
        """
        sid, tid = self._budget_scope
        if not sid and not tid:
            return None
        try:
            from ..observability import check_budget

            b = await check_budget(session_id=sid, task_id=tid)
        except Exception as e:  # noqa: BLE001
            logger.debug("预算检查失败（放行）: %s", e)
            return None
        # limit == 0 = 没配上限；scope 为空说明两个维度都没查
        if not b or not b.get("limit") or not b.get("scope"):
            return None
        return b

    def _budget_stop_answer(
        self, steps: List[Step], budget: Dict[str, Any]
    ) -> str:
        """触顶后的收尾答案：**不再调 LLM**，用已完成的步骤拼一份可交付的结论。

        再发起一次「总结调用」看似更好，但那笔钱本身就是超支——拦截的意义是
        「不超过上限」，所以这里只做纯文本整理。
        """
        scope = "任务" if budget.get("scope") == "task" else "会话"
        used = budget.get("used") or 0
        limit = budget.get("limit") or 0
        head = (
            f"（已达{scope} token 预算上限：已用 {used} / {limit}，"
            f"本轮到此提前结束，未继续调用工具。）"
        )
        if not steps:
            return head
        # 只列最近若干步：早期步骤早被压缩/外部化，全列反而淹掉有效信息
        tail = steps[-10:]
        lines = [head, "", "已完成的工作："]
        omitted = len(steps) - len(tail)
        if omitted > 0:
            lines.append(f"（前面 {omitted} 步已略）")
        for i, s in enumerate(tail, len(steps) - len(tail) + 1):
            title = f"#{i} {s.tool}" if s.tool else f"#{i} 思考"
            thought = (s.thought or "").strip().replace("\n", " ")
            if thought:
                title += f"：{thought[:120]}"
            lines.append(title)
        lines.append("")
        lines.append("如需继续，请新开会话，或调高 KEEPER_SESSION_TOKEN_LIMIT。")
        return "\n".join(lines)

    @classmethod
    def from_agent(cls, agent: "KeeperAgent", tools) -> "Process":
        """从 agent 实例装配一份可执行体：替代外界的 ``build_process``。

        llm / persona / skill 在这一刻从 agent 现取并固化到本实例——此后
        ``replace_tools`` 只换工具表，不再回头问 agent 要东西，保证一轮执行
        期间所见的能力集是稳定的。skill 以两段文本注入（清单 + system 合集），
        本类不理解 skill 的结构，只认这两段字符串。
        """
        return cls(
            agent.llm,
            tools,
            agent_name=agent.name,
            persona=agent.persona,
            skill_desc=cls._skill_desc(agent.skills),
            system_extra=cls._skill_system(agent.skills),
            skills=agent.skills,
        )

    @staticmethod
    def _skill_desc(skills) -> str:
        """「可用技能」段：常驻的只有 L1 概要——**什么时候用**。

        正文（**怎么用**）不在这里，那是按需加载的内容。清单末尾必须写明加载方式：
        否则模型不知道正文要去哪儿取，会拿着一句概要硬干，表现就是「知道有这个技能、
        但用得不对」。
        """
        lines = []
        for s in skills:
            desc = (getattr(s, "description", "") or "").strip()
            always = bool(getattr(s, "always", False))
            tools = ", ".join(s.tools) if getattr(s, "tools", None) else "无"
            lines.append(
                f"- {s.name}{'（已常驻）' if always else ''}："
                f"{desc or '（未写用途说明）'}（工具：{tools}）"
            )
        if not lines:
            return ""
        lines.append("")
        lines.append(
            "上面只给了「什么时候用」。确定要用某个技能时，先调用 "
            f"{_CAP_LOADER}（keys 里给它名字）把完整做法读进来再动手；"
            "不确定可以先读，读错了没有代价。标「已常驻」的已在上下文里，不用再读。"
        )
        return "\n".join(lines)

    @staticmethod
    def _skill_system(skills) -> str:
        """**行为型**技能的正文合集（追加到 system 末尾）——只有 ``always`` 的常驻。

        任务型技能的正文走按需加载：它们讲的是「某件事怎么做」，只在真要做那件事时
        才需要，常驻等于为每个八成用不上的技能预先付 token。

        没写 description 的旧技能在装配层已被归一成 ``always``（config._load_plugins），
        所以这里对存量技能的行为与改造前一致——不会静默丢能力。
        """
        return "\n".join(
            s.system_prompt
            for s in skills
            if s.system_prompt and bool(getattr(s, "always", False))
        )

    # ---- 能力按需加载：L2 正文（见 doc/capability-loading-design.md）----
    def install_capability_loader(self) -> None:
        """把 ``load_capabilities`` 装进本轮工具表。

        每轮开头调一次：工具表可能被 ``replace_tools`` 整表换过，但 register 是同名
        覆盖，幂等。没有技能时**不装**——白给模型一个工具只会增加它的选择负担。
        """
        if not self.skills:
            return
        try:
            from ..tools.base import ProcessorTool
        except Exception as e:  # noqa: BLE001
            logger.debug("装载能力加载器失败：%s", e)
            return
        self.registry.register(
            ProcessorTool(
                name=_CAP_LOADER,
                description=(
                    "按名字读取某个技能的完整做法（参数怎么填、按什么顺序、有什么禁区）。"
                    "「可用技能」清单里只写了什么时候用；确定要用某个技能时，"
                    "先用这个把它的正文读进来再动手。一次可以传多个名字。"
                ),
                parameters={
                    "keys": "技能名列表，如 [\"codebase-memory\"]；名字取自「可用技能」清单"
                },
                run=self._load_capabilities,
            )
        )

    def _reset_capabilities(self) -> None:
        """每轮开始清空加载状态：messages 是新的一批，旧的消息引用不能留。"""
        self._cap_loaded.clear()
        self._cap_order.clear()
        self._cap_msgs.clear()
        self._cap_just_loaded.clear()
        self._cap_source.clear()
        # 精简清单里给出明细的工具就是「已披露」的，不需要补载
        self._tool_disclosed = set(self.registry.disclosed_names(_tool_group_min()))
        self._tool_disclosed_n = 0

    async def _load_capabilities(self, args: dict) -> str:
        """``load_capabilities`` 的执行体：返回 keys 对应的 L2 正文。

        三条硬规则（设计文档第六节）：

        - **部分成功**：未知 key 单列 missing，不整请求失败（否则模型得重发全部）；
        - **幂等**：已加载的只回 already，不重复塞进上下文；
        - **模糊建议**：missing 带最接近的名字，让模型一次改对。
        """
        raw_keys = args.get("keys")
        if isinstance(raw_keys, str):
            keys = [k.strip() for k in raw_keys.replace(",", " ").split() if k.strip()]
        elif isinstance(raw_keys, list):
            keys = [str(k).strip() for k in raw_keys if str(k).strip()]
        else:
            keys = []
        if not keys:
            return "[没给要加载的能力名：keys 里传技能名列表]"

        by_name = {s.name: s for s in self.skills}
        loaded: List[str] = []
        already: List[str] = []
        missing: List[str] = []
        blocks: List[str] = []

        for k in keys:
            if k in self._cap_loaded:
                already.append(k)
                continue
            s = by_name.get(k)
            if s is None:
                # 不是技能名 → 当**工具名 / 工具组前缀**处理（P1：工具按需展开）。
                # 组前缀一次展开整组，多工具协同时不用逐个加载。
                text, names = self.registry.describe_full(k)
                if names:
                    self._cap_loaded[k] = text
                    self._cap_source[k] = "messages"
                    self._tool_disclosed.update(names)
                    loaded.append(k)
                    blocks.append(text)
                    await self._record_cap_load(
                        k,
                        kind="tool_group" if len(names) > 1 else "tool",
                        source="model_load",
                        chars=len(text),
                    )
                    continue
                missing.append(
                    self._missing_hint(k, list(by_name) + self._tool_candidates())
                )
                continue
            body = (getattr(s, "system_prompt", "") or "").strip()
            if not body:
                missing.append(f"{k}（这个技能没有正文）")
                continue
            self._cap_loaded[k] = body
            self._cap_source[k] = "messages"
            loaded.append(k)
            await self._record_cap_load(
                k, kind="skill", source="model_load", chars=len(body)
            )
            block = f"## 技能：{k}\n{body}"
            tips = self._tool_tips(s)
            if tips:
                block += "\n" + tips
            blocks.append(block)

        # 交给外层：把这些 key 与即将落进 messages 的那条消息绑定起来（可能多个共享一条）
        self._cap_just_loaded = list(loaded)

        out: List[str] = []
        if loaded:
            out.append(f"[已加载 {len(loaded)} 个技能：{', '.join(loaded)}]")
            out.append("")
            out.extend(blocks)
        if already:
            # 带上「在哪」：system（系统预加载的）还是 messages（之前自己读的）——
            # 否则模型会疑惑「说是有，可我没看见」。
            detail = "、".join(
                f"{k}（{self._cap_source.get(k) or '上下文'} 里）" for k in already
            )
            out.append("")
            out.append(f"[{detail} 已在上下文里，未重复加载]")
        if missing:
            out.append("")
            out.append("[以下没有找到：" + "；".join(missing) + "]")
        if not loaded and not already and not missing:
            # 连 key 都没解析出来才需要这句；有 missing 时它已经说清楚原因了，
            # 再补一句「一个都没加载」纯属重复噪音。
            out.insert(0, "[没认出要加载的名字：keys 传数组，如 [\"技能名\"]]")
        return "\n".join(out).strip()

    async def _exec_raw(
        self, name: str, args: Optional[dict]
    ) -> "tuple[str, bool, int, int, bool]":
        """执行一个工具，返回 ``(裁剪后的 obs, 是否成功, 耗时ms, 原始长度, 是否被截断)``。

        **不记账、不抛异常**：记账由调用方按「串行 / 并行」分别决定（并行时各工具
        的耗时会重叠，sum 会高估），异常则转成错误文本——批量执行里**一个失败不该
        拖垮整批**，让模型自己看到「哪个没成」比直接终止更好。
        """
        t0 = time.perf_counter()
        tool = self.registry.get(name)
        if tool is None:
            obs = (
                f"[没有名为 {name} 的工具，可用："
                f"{', '.join(t.name for t in self.registry.all())}]"
            )
            return obs, False, int((time.perf_counter() - t0) * 1000), len(obs), False
        # 重复动作检测放在这里（执行前）：连续两次**完全相同**的调用都失败时，
        # 第三次直接拦下并换策略提示。实测过一次 30 步打转——模型把同一条
        # `cat a b > out` 重发了十几次，每次失败的理由还略有不同，它始终没
        # 意识到「重发没用，得换思路」。让它继续撞南墙是纯浪费。
        blocked = self._repeat_guard(name, args or {})
        if blocked:
            return blocked, False, int((time.perf_counter() - t0) * 1000), len(blocked), False
        # 危险工具的守卫放在这里：这是所有工具唯一的必经之路，且在真正执行
        # **之前**——所以拦下来时副作用为零，模型看到的是一段可读的拒绝理由，
        # 可以改用别的方式达成目标，而不是被硬碰断。
        blocked = self._guard_check(name, tool, args or {})
        if blocked:
            return blocked, False, int((time.perf_counter() - t0) * 1000), len(blocked), False
        try:
            raw = await tool.execute(args or {})
        except Exception as e:  # noqa: BLE001
            obs = f"[工具 {name} 执行出错: {e}]"
            return obs, False, int((time.perf_counter() - t0) * 1000), len(obs), False
        obs, raw_len = _fit_observation(
            name, raw if isinstance(raw, str) else str(raw), self.observation_limit
        )
        return (
            obs,
            True,
            int((time.perf_counter() - t0) * 1000),
            raw_len,
            raw_len > len(obs),
        )

    def _dropped_actions_note(self, dec: Any) -> str:
        """并行关闭却写了多个 ACTION 时的回执说明（空串 = 不用提示）。

        为什么必须说出来
        ----------------
        以前是**静默丢弃**：模型写了 2 个 ACTION，只回来 1 个结果，而它那边
        完全不知道第二个没跑。实测后果是它把「文件已写入」直接写进下一个
        THOUGHT（当成既定事实），然后去 ``cp`` 一个其实还没落盘的文件。
        世界观的偏差一旦形成，后面每步都在错的基础上继续推。
        """
        if _tool_parallel_mode() > 0:
            return ""
        n = int(getattr(dec, "action_count", 0) or 1)
        if n <= 1:
            return ""
        extra = n - 1
        return (
            f"\n\n[系统] 这一轮你写了 {n} 个 ACTION，但当前**没有开启**多动作并行，"
            f"只有第一个（{dec.tool}）被执行，后面 {extra} 个**没有执行**。"
            f"「做过什么」只以回执为准：没收到回执的动作就是没做。"
            f"请在下一轮把剩下的单独再发一次。"
        )

    def _progress_note(self, step_no: int) -> str:
        """回执上附一条进度（`[步 12/30]`），临近上限时催交付。

        为什么需要
        ----------
        模型**全程不知道自己在第几步**，也没有"快到上限了"的概念。实测写一个
        HTML 游戏的会话里，它第 28 步还在开新一轮调试脚本，最后撞上上限被强制
        总结——产物既没 ``fs.publish``（前端拿不到卡片），总结也是仓促拼的。
        给它看到进度，并在最后几步明确要求「先交付再继续」，是最便宜的止损。
        """
        total = int(self.max_steps or 0)
        if total <= 0:
            return ""
        left = total - step_no
        head = f"\n[步 {step_no}/{total}"
        if left <= 0:
            return head + "：这是最后一步，必须直接给 FINAL]"
        if left <= 3:
            return (
                head + f"，只剩 {left} 步]"
                "\n请立刻收尾：先用 `fs.publish` 挂载产物，再给 FINAL；"
                "不要开启新的验证 / 调试（写了也跑不完）。"
            )
        return head + "]"

    def _note_repeat(self, name: str, args_hash: str, *, ok: bool) -> None:
        """记录一次调用的成败，供 :meth:`_repeat_guard` 判断是否该打断。

        成功就清零——问题可能已经被别的动作解决了，再执行一次是合理的。
        """
        if not args_hash:
            return
        key = f"{name}::{args_hash}"
        if ok:
            self._recent_failures.pop(key, None)
            return
        self._recent_failures[key] = self._recent_failures.get(key, 0) + 1

    def _repeat_guard(self, name: str, args: Dict[str, Any]) -> Optional[str]:
        """同一工具 + 完全相同参数连续失败 2 次后，第三次拦下并要求换思路。

        只统计**失败**，且只认**参数完全一致**的调用——参数变了就是换了思路，
        不该拦（模型换路径重试是正当的）。

        拦下来给的提示必须能**指出下一步做什么**，而不是只说「重复了」：
        失败原因和「换什么」写清楚，模型才有路可走。
        """
        try:
            key = _call_key(name, args)
        except Exception:  # noqa: BLE001
            return None
        fails = self._recent_failures.get(key, 0)
        if fails < 2:
            return None
        return (
            f"[{name} 用完全相同的参数已经连续失败 {fails} 次，本次不再执行] "
            f"原样重发不会有不同结果，请换思路：换一个路径/工具，"
            f"或把这一步拆成更小的步骤。"
            f"如果连着失败是因为依赖的东西还没准备好，"
            f"先用 fs.list_dir / fs.read_file 确认它真的存在，再继续。"
        )

    def _guard_check(
        self, name: str, tool: Any, args: Dict[str, Any]
    ) -> Optional[str]:
        """危险工具执行前的静态拦截；放行返回 ``None``，拦下则返回给模型看的说明。

        刻意放在**执行之前**而不是之后：拦下来时副作用为零，模型也拿到了可读的
        理由能改用别的方式，而不是被硬碰断。

        只读工作区按「读命令白名单」放行（``ls`` / ``grep`` / ``git status`` 这类），
        可写工作区不拦命令（否则正常的 git / npm 流程会全废），只查工作空间外路径。
        详见 :mod:`keeper.tools.guard`——它同时诚实列出了自己的漏判。
        """
        if not getattr(tool, "dangerous", False):
            return None
        from ..chat.context import current_workspace
        from ..tools import guard as _guard

        ws = current_workspace()
        if ws is None:
            # 无工作空间上下文（构建期 / 任务执行等）。没有边界可比，此时
            # 「读不到根目录」不等于「可以随便跑」，所以按最保守处理。
            return (
                f"[工具 {name} 已被安全策略拒绝：当前没有工作空间上下文，"
                f"无法确认它不会越界。带dangerous 的工具（可执行命令）"
                f"只能在会话内的工作空间中使用。]"
            )
        try:
            reason = _guard.check_call(
                tool_name=name,
                args=args,
                dangerous=True,
                read_only=bool(ws.read_only),
                workspace=ws.root,
                # 会话私有临时目录在工作区之外，但它是 agent 自己的草稿区，
                # 明确放行——否则 bash 里带临时文件路径会被当越界拦掉，agent
                # 只好把测试脚本堆回用户工作区。
                extra_roots=[ws.agent_space],
            )
        except Exception as e:  # noqa: BLE001
            # 守卫本身出错不能变成「放行」——但也不该让整轮对话挂掉。
            # 记一条日志并放行：守卫是护栏，不是围栏（它的漏判是已知且被记录的）。
            logger.warning("危险工具守卫异常，本次放行：%s", e)
            return None
        if not reason:
            return None
        logger.info(
            "危险工具被守卫拦截：%s（只读=%s，根=%s）", name, ws.read_only, ws.root
        )
        return f"[工具 {name} 已被安全策略拒绝] {reason}"

    async def _run_action_batch(
        self,
        dec,
        actions: List[tuple],
        messages: List[dict],
        steps: List[Step],
        on_step,
        mode: int,
    ) -> None:
        """一轮里执行多个工具（工具并行化 P1-1）。

        **只有全部是只读工具才并发**。只要掺进一个写类或未知工具，就整体串行：
        并行的收益是「少等几个慢工具」，代价是「失去顺序保证」——写类工具一旦
        顺序错了，模型会拿着错乱的结果继续推理，那比慢更糟。

        每个动作各生成一个 Step（thought 只挂在第一个上），落库 / 时间线因此
        天然对齐，无需改动既有结构。
        """
        import asyncio as _asyncio

        def _can(name: str) -> bool:
            t = self.registry.get(name)
            if t is None or not t.read_only:
                return False
            # 开关 1 只放**内置**只读工具（名字里没有 __ 的那些）；开关 2 才
            # 放插件清单里显式声明 read_only 的工具。
            return True if mode >= 2 else ("__" not in name)

        parallel = all(_can(n) for n, _ in actions)
        limit = max(1, _tool_parallel_max())
        wall_ms = 0
        results: List[tuple] = []

        if parallel and len(actions) > 1:
            batch = list(actions[:limit])
            rest = list(actions[limit:])
            t0 = time.perf_counter()
            got = await _asyncio.gather(
                *[self._exec_raw(n, a) for n, a in batch], return_exceptions=True
            )
            wall_ms = int((time.perf_counter() - t0) * 1000)
            for (name, _), r in zip(batch, got):
                # gather 只会在我们 CancelledError 时抛异常；单个工具的异常已被
                # _exec_raw 吞掉，这里再兜一层，保证批处理永不被单个工具打断。
                results.append(
                    r
                    if isinstance(r, tuple)
                    else (f"[工具 {name} 执行出错: {r}]", False, 0, 0, False)
                )
            for n, a in rest:
                results.append(await self._exec_raw(n, a))
        else:
            for n, a in actions:
                results.append(await self._exec_raw(n, a))

        # 记账按**每个工具**各记一条（耗时口径见下）；但步骤只占**一个** step：
        # step 对应的是「一次思考」，不是「一次工具调用」。否则 3 个工具就吃掉
        # 3 步配额，max_steps 会被工具数而不是思考轮数消耗光。
        segs: List[str] = []
        for (name, args), (obs, ok, _ms, raw_len, trunc) in zip(actions, results):
            try:
                args_json = json.dumps(args or {}, ensure_ascii=False, default=str)
            except Exception:  # noqa: BLE001
                args_json = str(args or {})
            await self._record_tool_call(
                name,
                # 并行时各工具的耗时会重叠，sum 会高估成 N 倍 → 只记墙钟跨度，
                # 由下面那条 observation 说明，不在这里重复计时。
                duration_ms=0 if parallel else _ms,
                ok=ok,
                error=None if ok else "工具执行失败或不存在",
                args_size=len(args_json),
                args_hash=hashlib.sha1(args_json.encode("utf-8")).hexdigest()[:12],
                output_size=len(obs),
                raw_output_size=raw_len,
                truncated=trunc,
            )
            head = f"[{name}] " if len(actions) > 1 else ""
            segs.append(head + obs)
            messages.append({"role": "tool", "name": name, "content": obs})

        if len(actions) > 1:
            names = ", ".join(n for n, _ in actions)
            logger.info("[tools] 一步内执行 %d 个工具：%s", len(actions), names)
        step = Step(
            thought=dec.thought,
            tool=actions[0][0],
            args=actions[0][1],
            # 多个工具的结果按原顺序分段拼接，模型读到的因果顺序不会乱
            observation="\n\n".join(segs),
        )
        steps.append(step)
        if on_step is not None:
            await on_step(len(steps), step)

        if parallel and len(actions) > 1:
            logger.info(
                "工具并行：%d 个只读工具并发完成，墙钟 %dms（串行需 %dms）",
                min(len(actions), limit),
                wall_ms,
                sum(r[2] for r in results),
            )
            step.observation = str(step.observation) + (
                f"\n（本批 {min(len(actions), limit)} 个只读工具并发执行，"
                f"墙钟 {wall_ms}ms）"
            )

    async def _ensure_tool_disclosed(self, name: str, messages: List[dict]) -> None:
        """把「只见过名字」的工具的完整定义补进上下文，然后才执行它。

        插在 assistant 的 ACTION 之后、工具结果之前：模型下一步就能看到
        「我调了它 → 它的参数是这样 → 结果是这样」，错了也知道怎么改。
        """
        if name in self._tool_disclosed:
            return
        if self._tool_disclosed_n >= _TOOL_DISCLOSE_MAX:
            # 一轮补到上限就不再补：要么是模型在乱调名字，要么是工具确实分散，
            # 再补下去就变成拿上下文给工具清单做备份了。
            return
        text, names = self.registry.describe_full(name)
        if not names:
            return
        self._tool_disclosed.update(names)
        self._tool_disclosed_n += 1
        messages.append(
            {
                "role": "user",
                "content": (
                    f"（系统补充）你要用的工具 {name} 在清单里只给了名字，"
                    f"完整定义如下，按这里的参数调用：\n{text}"
                ),
                "_cap": True,
            }
        )
        # 观测：这类占比高 = 工具折叠得太狠 / 组摘要没写，模型只能靠猜名字
        await self._record_cap_load(
            name, kind="tool", source="auto_disclose", chars=len(text)
        )

    async def _record_parse_stat(self, dec) -> None:
        """记一条解析结构统计（P1-1 阶段 0）。失败不影响主流程。"""
        try:
            from ..observability import record_react_parse_stat

            await record_react_parse_stat(dec.kind, dec.action_count or 0)
        except Exception as e:  # noqa: BLE001
            logger.debug("记录解析统计失败（忽略）: %s", e)

    async def _record_cap_load(
        self, key: str, *, kind: str = "skill", source: str, chars: int = 0
    ) -> None:
        """记一条能力加载事件（观测用）。失败不影响主流程。

        做成 async 而不是 fire-and-forget 的 ``create_task``：这样写入顺序确定、
        冒烟测试能直接断言，不留「任务被丢弃」的暗角。
        """
        try:
            from ..observability import record_capability_load

            await record_capability_load(
                key, kind=kind, source=source, chars=chars or None
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("能力加载记账失败（忽略）: %s", e)

    def _tool_candidates(self) -> List[str]:
        """拼错时给模型看的候选：工具全名 + 组名（组名可一次展开整组）。"""
        return [t.name for t in self.registry.all()] + self.registry.group_keys()

    @staticmethod
    def _missing_hint(key: str, names: List[str]) -> str:
        """给拼错的名字一个最接近的建议：让模型一次改对，而不是反复猜。"""
        try:
            import difflib

            near = difflib.get_close_matches(key, names, n=1, cutoff=0.6)
        except Exception:  # noqa: BLE001
            near = []
        return f"{key}（最接近：{near[0]}）" if near else key

    def _tool_tips(self, s) -> str:
        """技能声明的常用工具：连带附上用法。

        没有这一步会出现「加载了做法却没有工具」的死局——这是按需加载特有的新故障，
        全量注入时不存在（做法和工具总是一起出现）。
        """
        names = list(getattr(s, "tools", None) or [])
        if not names:
            return ""
        lines = ["本技能常用工具："]
        for n in names:
            t = self.registry.get(n)
            if t is None:
                lines.append(f"- {n}（当前不可用）")
                continue
            params = ", ".join(f"{k}: {v}" for k, v in (t.parameters or {}).items())
            lines.append(
                f"- {n}：{t.description}" + (f"（参数：{params}）" if params else "")
            )
        return "\n".join(lines)

    def _trim_capabilities(self, messages: List[dict]) -> str:
        """超出容量上限就按加载顺序换出最早的；返回要追加给模型的告知文案。

        换出**必须告诉模型**：它看不见上下文被改动，不知道内容已经不在那儿了，
        会照着已经消失的步骤继续调工具。
        """
        note = ""
        while len(self._cap_order) > _CAP_MAX:
            oldest = self._cap_order[0]
            msg = self._cap_msgs.get(oldest)
            if msg is None:
                self._cap_order.pop(0)
                continue
            dropped = self._drop_cap_msg(messages, msg)
            if dropped:
                note += f"（{', '.join(dropped)} 的完整做法已移出上下文；还要用请重新加载）"
        return note

    def _drop_cap_msg(self, messages: List[dict], msg: Dict[str, Any]) -> List[str]:
        """移除一条加载消息；返回随之失效的全部 key（多个 key 可能共享同一条消息）。"""
        dropped = [k for k, m in self._cap_msgs.items() if m is msg]
        for k in dropped:
            self._cap_msgs.pop(k, None)
            self._cap_loaded.pop(k, None)
        try:
            messages.remove(msg)
        except ValueError:
            pass
        self._cap_order = [k for k in self._cap_order if k in self._cap_msgs]
        return dropped

    # -- 系统侧预加载：模型判不出来的那部分，由系统替它挑 --
    async def _preload_capabilities(self, question: str) -> str:
        """轮开始时挑几个技能，把**正文**拼进 system；返回要追加的那段文本。

        这一层不能少：**skill 不是可调用对象**，模型没法「先调用了再说」——tool 那条
        「调用触发补载」的兜底对 skill 根本不成立（它没有调用动作）。所以判读必须由
        系统发起，否则模型判不出来就等于这个能力不存在。

        小规模时干脆全量：按需省下的那点 token，抵不过「多一次判读 + 行为不再可复现」
        的代价。
        """
        policy = _cap_preload_policy()
        if policy == "off":
            return ""
        cands = [
            s
            for s in self.skills
            if not getattr(s, "always", False)
            and (getattr(s, "system_prompt", "") or "").strip()
        ]
        if not cands:
            return ""

        picked: List[Any] = []
        # 记下是「全给」还是「按相关性挑的」——两者注入的引导语必须不同，
        # 否则会告诉模型「这是按你的问题挑的」，而全量分支根本没看问题。
        all_included = False
        if policy == "auto" and self._caps_token_estimate(cands) <= _cap_small_limit():
            picked = cands
            all_included = True
        elif policy == "llm":
            picked = await self._pick_by_llm(question, cands) or self._pick_by_keyword(
                question, cands
            )
        else:
            # keyword（含 auto 的大规模分支）：**先零成本捞一轮**，捞不到再花钱判读。
            # 顺序不能反过来——大部分问题用关键词就够定了，没必要每轮都付一次判读钱。
            picked = self._pick_by_keyword(question, cands)
            if not picked and policy == "auto":
                picked = await self._pick_by_llm(question, cands)

        blocks: List[str] = []
        names: List[str] = []
        for s in picked:
            body = (getattr(s, "system_prompt", "") or "").strip()
            if not body:
                continue
            # 记进已加载：模型后面再 load 同一技能时回「在 system 里」，别重复塞一份
            self._cap_loaded[s.name] = body
            self._cap_source[s.name] = "system"
            await self._record_cap_load(
                s.name, kind="skill", source="preload", chars=len(body)
            )
            tips = self._tool_tips(s)
            blocks.append(
                f"## 技能做法：{s.name}\n{body}" + (f"\n{tips}" if tips else "")
            )
            names.append(s.name)
        if not blocks:
            return ""
        logger.info("[cap-preload] 本轮预加载技能：%s", ", ".join(names))
        # 引导语按分支写：全量分支**没有**看用户问题，不能说成「按你的问题取来的」，
        # 否则模型会以为这些技能与当前问题相关，在不相关的场景里硬套。
        if all_included:
            head = (
                "\n\n## 本轮可用的全部技能做法\n"
                "技能数量不多，已全部取来（**没有**按问题筛选）：下面这些是各技能的完整做法，"
                f"按需取用即可，不必再调用 {_CAP_LOADER}：\n\n"
            )
        else:
            head = (
                "\n\n## 本轮已取来的技能做法\n"
                "下面这些是系统按你的问题预先取来的完整做法，直接用就行，"
                f"不必再调用 {_CAP_LOADER} 读取它们：\n\n"
            )
        return head + "\n\n".join(blocks)

    def _caps_token_estimate(self, skills: List[Any]) -> int:
        """粗估一批技能正文的 token 总量：只用来判断「算不算小规模」。"""
        chars = sum(len(getattr(s, "system_prompt", "") or "") for s in skills)
        return int(chars / _chars_per_token())

    async def _pick_by_llm(self, question: str, cands: List[Any]) -> List[Any]:
        """一次便宜判读：只让模型挑名字，不让它干活。

        失败一律返回空——外层会退到关键词法；挑不出来最多是没预加载，
        模型仍可以自己 load，不能因为挑选项挂掉整轮。
        """
        if self.llm is None:
            return []
        listing = "\n".join(
            f"- {s.name}：{(getattr(s, 'description', '') or '')[:120]}" for s in cands
        )
        prompt = (
            f"用户问题：{question}\n\n可用技能：\n{listing}\n\n"
            "只输出**需要加载**的技能名，多个用英文逗号分隔；都不需要就只输出「无」。"
            "不要解释，不要输出其它文字。"
        )
        try:
            # 判读也是一次 LLM 调用：不归属任何 step，单独标 kind，
            # 免得被算成某个 react_step 的用量（同 Compactor 的做法）。
            from ..chat.context import set_llm_call_kind, set_step_seq

            set_step_seq(None)
            set_llm_call_kind("capability_preload")
            text = await self.llm.chat([{"role": "user", "content": prompt}])
        except Exception as e:  # noqa: BLE001
            logger.debug("预加载判读失败（回退关键词）: %s", e)
            return []

        names = [s.name for s in cands]
        hits: List[Any] = []
        for tok in re.split(r"[,，;；\s]+", str(text or "")):
            tok = tok.strip().strip("`\"'（）()")
            if not tok or tok in ("无", "none", "None", "-"):
                continue
            match = next((s for s in cands if s.name == tok), None)
            if match is None:  # 模型可能写错一点：模糊捞一次
                try:
                    import difflib

                    near = difflib.get_close_matches(tok, names, n=1, cutoff=0.8)
                except Exception:  # noqa: BLE001
                    near = []
                match = next((s for s in cands if s.name == near[0]), None) if near else None
            if match is not None and match not in hits:
                hits.append(match)
        return hits[:_CAP_PRELOAD_TOP]

    def _pick_by_keyword(self, question: str, cands: List[Any]) -> List[Any]:
        """关键词打分：零 LLM 成本，命中即用（LLM 不可用时的兜底）。"""
        q = (question or "").lower()
        scored: List[tuple] = []
        for s in cands:
            score = 0
            for t in self._cap_terms(s):
                tl = str(t).lower()
                if not tl or tl not in q:
                    continue
                score += 3 if tl == str(getattr(s, "name", "")).lower() else 1
            if score:
                scored.append((score, s))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s for _n, s in scored[:_CAP_PRELOAD_TOP]]

    @staticmethod
    def _cap_terms(s) -> List[str]:
        """匹配用词：作者写了 ``keywords`` 听作者的，否则从 description 里抽。

        抽取故意粗糙（英文词 + 2–6 字中文片段）：它只用来「先捞一批候选」，漏了还有
        模型主动 load 兜着，不值得为它写一个分词器。
        """
        kws = [str(k).strip() for k in (getattr(s, "keywords", None) or [])]
        if kws:
            return kws + [str(getattr(s, "name", ""))]
        desc = getattr(s, "description", "") or ""
        words = re.findall(r"[A-Za-z_][A-Za-z0-9_\-.]{1,}|[\u4e00-\u9fa5]{2,6}", desc)
        return [w for w in words if len(w) >= 2][:12] + [str(getattr(s, "name", ""))]

    def replace_tools(self, tools) -> None:
        """整体替换本轮可用的工具表：registry 对象不变，只换内容。

        工具变更必须以**整张表**为单位（而不是逐个增删），否则中途会露出
        「只加未减 / 只减未加」的半成品能力集；``ToolRegistry.replace_all``
        正是原子换表。换完后无需重建 Process——它读的就是这个 registry。

        Args:
            tools: ``ToolRegistry`` 或 ``ProcessorTool`` 的可迭代对象。
        """
        self.registry.replace_all(tools)

    async def _record_tool_call(self, tool: str, **kw) -> None:
        """记录一次工具调用（可观测：工具维度统计）。

        只记账，失败一律吞掉——统计绝不能把工具执行搞挂。
        """
        try:
            from ..observability import record_tool_call

            await record_tool_call(tool, **kw)
        except Exception:  # noqa: BLE001
            pass

    def _log_usage(self, before) -> None:
        """打印 token 用量：本轮增量 + 本次进程累计。

        provider 不上报用量时（usage.reported == 0）明确说明，只给调用次数，
        避免把「没统计到」误读成「没消耗」。
        """
        u = getattr(self.llm, "usage", None)
        if u is None:
            return
        cur = u - before if before is not None else u.copy()
        note = "" if u.reported else "（provider 未返回 token 用量，仅统计调用次数）"
        logger.info(
            "【Token 用量】本轮：LLM 调用 %d 次，prompt %d + completion %d = %d；"
            "累计：调用 %d 次，prompt %d + completion %d = %d%s",
            cur.calls, cur.prompt_tokens, cur.completion_tokens, cur.total_tokens,
            u.calls, u.prompt_tokens, u.completion_tokens, u.total_tokens,
            note,
        )

    async def run(
        self,
        question: str,
        *,
        on_step=None,
        on_delta=None,
        history=None,
        memory_hint=None,
        should_stop=None,
        session_id=None,
        task_id=None,
    ) -> ProcessResult:
        """跑一轮 ReAct；无论怎么结束（FINAL / ASK / 暂停 / 调用失败 / 超步数）都打印用量。

        session_id / task_id：预算守卫的作用域，每步之后据此查一次累计用量
        （不传就不做预算检查，行为同以前）。


        on_step: 可选异步回调 (seq, step)，每执行完一步就调用一次，用于实时落库。
        on_delta: 可选异步回调 (text)，最终回答按块推送，用于前端打字机效果。
        history: 可选历史消息列表（user/assistant/tool），拼在当前问题之前注入，
            用于「最近 N 轮」直接注入上下文（见 doc/memory.md R0）。
        memory_hint: 可选「相关历史记忆」摘要文本（系统自动召回，L1），注入 system prompt。
        should_stop: 可选异步回调 () -> bool，每执行完一步就问一次「该停了吗」，
            用于按 step 暂停（已落库的步骤保留，未开始的下一步不跑）。
        """
        u = getattr(self.llm, "usage", None)
        before = u.copy() if u is not None else None
        self._budget_scope = (session_id, task_id)
        try:
            return await self._react(
                question,
                on_step=on_step,
                on_delta=on_delta,
                history=history,
                memory_hint=memory_hint,
                should_stop=should_stop,
            )
        finally:
            self._log_usage(before)

    async def resume(
        self,
        question: str,
        prior_steps: List[Step],
        *,
        on_step=None,
        on_delta=None,
        should_stop=None,
        session_id=None,
        task_id=None,
    ) -> ProcessResult:
        """从挂起点继续：把已有步骤重建成消息历史，接着跑。

        session_id / task_id：同 ``run``——续跑同样是新一轮消耗，也要被预算拦。


        prior_steps 需包含挂起的那条 ask 步骤，以及紧随其后的 human_answer
        步骤（用户对本轮提问的回答）——两者都已占序号，因此新步骤自然排在
        其后，不会与已有记录撞号。

        用户回答不再单独传入：它已作为 human_answer 步骤落在 prior_steps 里，
        由重放统一还原。这样本轮若再次挂起，先前每一次回答都能被还原。
        """
        u = getattr(self.llm, "usage", None)
        before = u.copy() if u is not None else None
        self._budget_scope = (session_id, task_id)
        try:
            return await self._react(
                question,
                on_step=on_step,
                on_delta=on_delta,
                prior_steps=prior_steps,
                should_stop=should_stop,
            )
        finally:
            self._log_usage(before)

    async def _llm_call(
        self, messages: list, system: str, on_delta=None, should_stop=None
    ) -> Tuple[str, bool]:
        """一次 LLM 调用；给了 on_delta 且模型支持流式时，把「最终回答」逐块推给它。

        ReAct 协议下这一次输出可能是 THOUGHT/ACTION（要调工具），也可能是 FINAL（回答）。
        只有出现 FINAL（或「最终回答」/ANSWER）标签之后的内容才是给用户的回答，
        所以流式时先累积，识别到 FINAL 之后才开始推送——否则会把思考过程当回答显示。

        should_stop：可选异步回调，流式时每收到一块就问一次「该停了吗」——
        用户点「停止生成」时尽快收尾，已生成的内容保留。

        返回 ``(文本, 是否因停止而中断)``。
        """
        stream = getattr(self.llm, "astream_chat", None)
        if on_delta is None or stream is None:
            # 一次性调用没法中途打断，只能整段返回
            return await self.llm.chat(messages, system=system), False

        buf = ""
        final_at: Optional[int] = None
        stopped = False
        async for chunk in stream(messages, system=system):
            buf += chunk
            if final_at is None:
                m = _FINAL_RE.search(buf)
                if m:
                    final_at = m.end()  # 标记之后才是回答正文
                    tail = buf[final_at:]
                    if tail:
                        await on_delta(tail)
            else:
                await on_delta(chunk)
            if should_stop is not None and await should_stop():
                stopped = True
                break
        return buf, stopped

    async def _react(
        self,
        question: str,
        *,
        on_step=None,
        on_delta=None,
        prior_steps: Optional[List[Step]] = None,
        history: Optional[List[dict]] = None,
        memory_hint: Optional[str] = None,
        should_stop=None,
    ) -> ProcessResult:
        self._refresh_limits()  # 步数上限 / 单条截断：每轮现读配置
        # 每轮重置加载状态：messages 是新建的一批，上轮的「已加载」不再成立。
        # loader 工具也要重装——工具表可能已被 replace_tools 整表换掉。
        self._reset_capabilities()
        self.install_capability_loader()
        steps: List[Step] = list(prior_steps or [])
        messages: List[dict] = list(history or []) + [
            {"role": "user", "content": "用户问题：" + question}
        ]
        # 上下文治理：外部化 + 水位压缩（见 doc/context-design.md）
        compactor = Compactor(self.llm)

        # 重放挂起前的步骤，让模型看到之前做过什么、问过什么、用户答过什么
        for s in prior_steps or []:
            if s.kind == "ask":
                messages.append(
                    {
                        "role": "assistant",
                        "content": f"THOUGHT: {s.thought}\nASK: {s.observation or ''}",
                    }
                )
                continue
            if s.kind == "human_answer":
                messages.append(
                    {"role": "user", "content": "用户补充：" + (s.observation or "")}
                )
                continue
            if s.tool:
                messages.append(
                    {
                        "role": "assistant",
                        "content": (
                            f"THOUGHT: {s.thought}\nACTION: {s.tool}\n"
                            f"ACTION_INPUT: {json.dumps(s.args or {}, ensure_ascii=False)}"
                        ),
                    }
                )
                messages.append(
                    {"role": "tool", "name": s.tool, "content": s.observation or ""}
                )

        # system_extra 交给 planner 排在「输出格式」之前：格式要求是引擎契约，
        # 不能被 skill 的内容覆盖，否则模型不再输出 THOUGHT / ACTION
        system = self.planner.system_prompt(
            # 精简清单：内置/零散工具给明细，大组折成一行只列名字（用到时补定义）
            tools_desc=self.registry.describe_brief(_tool_group_min()),
            skill_desc=self.skill_desc,
            agent_name=self.agent_name,
            persona=self.persona,
            system_extra=self.system_extra,
            memory_hint=memory_hint or "",
        )
        # 系统侧预加载拼进 **system**，不是 messages：history 每轮都在长，放 messages
        # 里前缀会被冲掉；system 才是那段稳定的前缀（多轮也能命中缓存）。
        # 模型半路自己 load 的才进 messages 尾部——两者位置不同，别合并处理。
        preload = await self._preload_capabilities(question)
        if preload:
            system = system + preload

        # 预算告警只催一次：反复塞同一句提示只会挤上下文，且模型已看到要求
        budget_warned = False

        for step_i in range(self.max_steps):
            try:
                # 可观测：把「本步序号」注入上下文，LLM 打点时据此归属到具体 step。
                # 用 len(steps)+1 而不是 step_i+1——executor 落库用的序号是
                # len(steps)（append 之后取），两者必须对齐，回填 step_id 才不会错位。
                from ..chat.context import set_llm_call_kind, set_step_seq

                set_step_seq(len(steps) + 1)
                set_llm_call_kind("react_step")
                resp, stopped = await self._llm_call(
                    messages, system, on_delta, should_stop
                )
            except Exception as e:
                logger.warning("LLM 调用失败，终止规划: %s", e)
                # 可观测：**失败调用也要记账**，否则错误率永远是 0（只有成功调用
                # 会走 _record_usage）。记账失败只记日志，不能影响主流程。
                try:
                    from ..observability import record_llm_call

                    await record_llm_call(
                        self.llm,
                        prompt_tokens=0,
                        completion_tokens=0,
                        ok=False,
                        error=str(e)[:500],
                    )
                except Exception:  # noqa: BLE001
                    pass
                return ProcessResult(
                    answer=f"（LLM 调用失败：{e}）",
                    steps=steps,
                    used_tools=bool(steps),
                )
            if stopped:
                # 用户在生成途中点了「停止生成」：已吐出的内容保留，不再跑下一步。
                # 只取 FINAL 之后的正文——之前的是思考，不该显示成回答。
                partial = ""
                m = _FINAL_RE.search(resp)
                if m:
                    partial = resp[m.end() :].strip()
                return ProcessResult(
                    answer=partial,
                    steps=steps,
                    used_tools=bool(steps),
                    canceled=True,
                )

            messages.append({"role": "assistant", "content": resp})
            dec = self.planner.parse(resp, step_i)
            # 量化「模型想并行」：prompt 要求每次只写一个 ACTION，但模型经常连写。
            # 现状第二个会被丢弃，这里只记数不改行为（P1-1 的收益依据）。
            await self._record_parse_stat(dec)

            if dec.kind == "final":
                # 没调工具的纯思考轮：也记一步，让思考内容落在「自主规划过程」里，
                # 而不是混进正文（正文只放 FINAL 之后的回答）。
                if dec.thought and not steps:
                    step = Step(thought=dec.thought)
                    steps.append(step)
                    if on_step is not None:
                        await on_step(len(steps), step)
                return ProcessResult(
                    answer=dec.answer or resp, steps=steps, used_tools=bool(steps)
                )

            if dec.kind == "ask":
                # 信息不足，需要用户补充：本轮到此为止，由上层写挂起步骤
                return ProcessResult(
                    answer=dec.ask or "",
                    steps=steps,
                    used_tools=bool(steps),
                    ask=dec.ask,
                    options=dec.options,
                )

            # act：执行工具。开关关着时行为与从前完全一致；开着且模型连写了多个
            # ACTION 时才走批量路径（顺带止损：以前第二个以后是被直接丢弃的）。
            if _tool_parallel_mode() > 0 and (dec.action_count or 1) > 1:
                parsed = self.planner.parse_actions(resp)
                if len(parsed) > 1:
                    await self._run_action_batch(
                        dec, parsed, messages, steps, on_step, _tool_parallel_mode()
                    )
                    if should_stop is not None and await should_stop():
                        return ProcessResult(
                            answer="", steps=steps, used_tools=True, paused=True
                        )
                    continue

            tool = self.registry.get(dec.tool)
            if tool is not None:
                # 调用触发补载：这工具在清单里只有名字（被折叠了），先把完整定义
                # 补进上下文再执行。**这条兜底只对 tool 成立**——tool 有调用动作，
                # skill 没有，所以 skill 必须靠系统侧预加载（设计文档第七节）。
                await self._ensure_tool_disclosed(dec.tool, messages)
            step = Step(thought=dec.thought, tool=dec.tool, args=dec.args)
            t_tool = time.perf_counter()
            # 入参哈希：用于识别「同样的参数又调了一遍」（空转 / 绕圈子）。
            # 注意：这里**绝不能抛异常**——它跑在流式响应的生成器里，一抛 SSE 流
            # 就会异常中断，表现为「请求一直不返回」。所以不用 sort_keys（遇到
            # 混合类型的 key 会 TypeError），并对不可序列化的入参做兜底。
            try:
                args_json = json.dumps(dec.args or {}, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                args_json = str(dec.args or {})
            args_size = len(args_json)
            args_hash = hashlib.sha1(args_json.encode("utf-8")).hexdigest()[:12]
            if tool is None:
                obs = (
                    f"[没有名为 {dec.tool} 的工具，可用："
                    f"{', '.join(t.name for t in self.registry.all())}]"
                )
                clipped = obs
                # 工具不存在也算一次失败调用（错误率要能看到）
                await self._record_tool_call(
                    dec.tool,
                    duration_ms=int((time.perf_counter() - t_tool) * 1000),
                    ok=False,
                    error=f"工具不存在：{dec.tool}",
                    args_size=args_size,
                    args_hash=args_hash,
                    output_size=len(obs),
                    raw_output_size=len(obs),
                )
            else:
                # 守卫放在执行**之前**。两条执行路径（单动作 / 批量）都要过，
                # 漏一条就等于没装——单动作这条才是主路径。
                #
                # 拦下后**不 continue**：两条分支汇合到下面的
                # `step.observation = clipped`，直接让 clipped 装拒绝理由即可，
                # 这样拒绝也会正常落库、进时间线、进消息历史（模型看得见、
                # 用户也查得到），而不是静默消失。
                blocked = self._guard_check(dec.tool, tool, dec.args or {})
                if blocked:
                    clipped = blocked
                    obs = blocked
                    await self._record_tool_call(
                        dec.tool,
                        duration_ms=int((time.perf_counter() - t_tool) * 1000),
                        ok=False,
                        error="被安全策略拒绝",
                        args_size=args_size,
                        args_hash=args_hash,
                        output_size=len(clipped),
                        raw_output_size=len(clipped),
                    )
                else:
                    try:
                        raw = await tool.execute(dec.args or {})
                        raw = raw if isinstance(raw, str) else str(raw)
                        # 先外部化（超长时给「预览 + block_id」，模型能 read 回
                        # 全文），再退到硬截断。clipped 仍只用于落库/展示，
                        # obs 才是喂给模型的——见 _fit_observation 的注释。
                        obs, raw_len = _fit_observation(
                            dec.tool, raw, self.observation_limit
                        )
                        clipped = clip_observation(raw, self.observation_limit)
                        # 失败计数：靠 observation 里的错误标记判断，不靠异常
                        #（大部分「失败」是工具正常返回一段错误文本，不是抛异常）
                        self._note_repeat(dec.tool, args_hash, ok=not _is_error_obs(obs))
                        await self._record_tool_call(
                            dec.tool,
                            duration_ms=int((time.perf_counter() - t_tool) * 1000),
                            ok=True,
                            args_size=args_size,
                            args_hash=args_hash,
                            # output_size 记**真正进上下文**的长度——影响 token 的是它；
                            # raw_output_size 是工具原本返回的长度，不等即说明给模型的
                            # 文本被缩减过（超内联上限 → 换引用；外部化不可用 → 硬截断）
                            output_size=len(obs),
                            raw_output_size=raw_len,
                            truncated=raw_len > len(clipped),
                        )
                    except Exception as e:
                        # 只记账，不改变原有行为：异常照常向上抛（由外层终止规划）
                        await self._record_tool_call(
                            dec.tool,
                            duration_ms=int((time.perf_counter() - t_tool) * 1000),
                            ok=False,
                            error=str(e)[:500],
                            args_size=args_size,
                            args_hash=args_hash,
                        )
                        raise
            # 两件「模型看不见、但会直接影响它下一步判断」的事，都挂在回执上：
            # ① 并行关闭时它写了多个 ACTION，第二个以后是被**丢弃**的（以前静默丢弃，
            #    它以为都执行了，于是下一轮直接假定"文件已写入"去 cp 一个还没落盘的文件）；
            # ② 还剩几步——它完全没有进度概念，实测第 28 步还在开新一轮调试。
            obs = obs + self._dropped_actions_note(dec) + self._progress_note(len(steps) + 1)

            # 落库/展示用 clipped（完整保留这次工具结果，UI 不受外部化影响）；
            # 喂给模型的是 obs——超长时只是「引用 + 预览」。
            step.observation = clipped
            steps.append(step)
            if on_step is not None:
                await on_step(len(steps), step)  # 实时落库：中途挂起/异常也不丢
            msg = {"role": "tool", "name": dec.tool, "content": obs}
            if dec.tool == "read":
                # 追回的内容：标成 pin，滚动外部化时会保住**最近 N 条**，
                # 免得出现「刚读回来 → 走出窗口被压 → 又读一遍」的抖动。
                msg["_pinned"] = True
            if dec.tool == _CAP_LOADER:
                # 按需加载的正文：打独立标记，滚动外部化会**整条跳过**它。
                # 不能用 _pinned——那个池子只保最近 N 条，且被 read 追回共用，
                # 加载内容是「接下来几步都要照着做」的说明书，不能被顺手换掉。
                msg["_cap"] = True
            messages.append(msg)
            if dec.tool == _CAP_LOADER:
                for k in self._cap_just_loaded:
                    self._cap_msgs[k] = msg
                    if k not in self._cap_order:
                        self._cap_order.append(k)
                self._cap_just_loaded = []
                note = self._trim_capabilities(messages)
                if note:
                    # 换出必须留话：模型看不见上下文被改，否则会照着已消失的步骤继续调工具
                    msg["content"] = str(msg.get("content") or "") + "\n" + note

            # ① 滚动外部化：把**退出保活窗口**的旧工具输出换成引用（最近 K 步
            #    保持原文——模型正在用的结果必须看得全，否则会漏文件/漏条目）。
            messages = compactor.rolling_externalize(messages)
            # ② 水位到了再压缩：只压最早的一批步骤，头部与最近 K 步不动
            # （前缀缓存命中的正是头部，动它等于每步都按全价重算）。
            if compactor.should_compact(messages, len(steps)):
                compacted = await compactor.compact(messages, len(steps))
                if compacted is not None:
                    messages = compacted

            # 按 step 暂停：每跑完一步就问一次「该停了吗」。此刻这一步已实时落库，
            # 所以命中就停在这里——已完成的步骤都留着，未开始的下一步不跑，
            # 下次执行时重放这些步骤即可从断点继续（不重复已完成的工作）。
            if should_stop is not None and await should_stop():
                return ProcessResult(
                    answer="", steps=steps, used_tools=True, paused=True
                )

            # ③ 预算守卫：每步查一次。只在会话开始查的话，长任务能在单轮内把
            #    额度烧穿——那正是「预算没真正拦截」的漏洞。
            budget = await self._check_budget()
            if budget:
                if budget.get("over"):
                    # 触顶：**不再发起新的 LLM 调用**，直接用已有步骤收尾，
                    # 否则「拦截」自己又烧一笔，实际消耗会超过上限。
                    logger.info(
                        "预算触顶，本轮提前结束：%s 已用 %s / %s",
                        budget.get("scope"),
                        budget.get("used"),
                        budget.get("limit"),
                    )
                    return ProcessResult(
                        answer=self._budget_stop_answer(steps, budget),
                        steps=steps,
                        used_tools=True,
                        budget_stopped=True,
                        budget=budget,
                    )
                if not budget_warned and (budget.get("rate") or 0) >= self.budget_warn_rate:
                    # 逼近上限：要求模型停止开新工具、就地给结论，留余量给这一次总结
                    budget_warned = True
                    pct = int((budget.get("rate") or 0) * 100)
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"（系统提示）本轮 token 预算已用 {pct}%"
                                f"（{budget.get('used')} / {budget.get('limit')}）："
                                "不要再调用新的工具，直接基于已有信息给出最终回答，"
                                "以 FINAL: 开头。"
                            ),
                        }
                    )

        # 超过最大步数：强制基于已有信息总结
        try:
            messages.append(
                {
                    "role": "user",
                    "content": "你已收集到上述信息，请基于已有信息给出最终回答（用 FINAL: 开头）。",
                }
            )
            # 超步数后的「强制总结」是一次额外调用，不对应任何 step：
            # 清掉 step_seq 并显式标 kind，免得被误归到最后一步。
            from ..chat.context import set_llm_call_kind, set_step_seq

            set_step_seq(None)
            set_llm_call_kind("final_summary")
            resp, _stopped = await self._llm_call(
                messages, system, on_delta, should_stop
            )
            dec = self.planner.parse(resp, self.max_steps)
            # 兜底一：模型在这个提示下仍经常顺手写 THOUGHT + ACTION，此时
            # dec.answer 是 None——而 `answer = resp` 会把整段思考（含 ACTION /
            # ACTION_INPUT）当成答案发给用户。所以要么从原文里把 FINAL 抠出来，
            # 要么明确承认"没总结出来"，绝不能把思考当答案。
            answer = (
                dec.answer or self.planner.extract_final(resp) or ""
            ).strip()
            if not answer:
                # 兜底二：把要求说死再试一次（只给正文，别再调工具）。
                # 不走 on_delta：第一次已经推过流，避免同一段内容出现两次。
                messages.append(
                    {
                        "role": "user",
                        "content": "不要再调用任何工具，也不要写 THOUGHT / ACTION，"
                        "只输出 FINAL: 之后的最终回答正文。",
                    }
                )
                resp2, _ = await self._llm_call(
                    messages, system, None, should_stop
                )
                answer = (self.planner.extract_final(resp2) or "").strip()
        except Exception as e:
            answer = f"（达到最大步数且最终总结失败：{e}）"
        if not answer:
            answer = self._max_steps_fallback(steps)
        return ProcessResult(answer=answer, steps=steps, used_tools=True)

    @staticmethod
    def _max_steps_fallback(steps: List[Step]) -> str:
        """两次总结都没给出正文时的结构化兜底。

        宁可如实说「没产出最终回答 + 已经做了什么」，也不把模型的思考过程当答案
        发出去——那看起来像答非所问，实际更糟：用户会以为这就是它的结论。
        """
        lines = [
            f"（已达到最大步数 {len(steps)} 步，本轮未产出最终回答。）",
            "",
            "已完成的工作：",
        ]
        tail = steps[-8:]
        if len(steps) > len(tail):
            lines.append(f"（前面 {len(steps) - len(tail)} 步已略）")
        for i, st in enumerate(tail, len(steps) - len(tail) + 1):
            head = f"{i}. " + (st.tool or "思考")
            thought = (st.thought or "").strip().replace("\n", " ")
            if thought:
                head += f"：{thought[:100]}"
            lines.append(head)
        lines.append("")
        lines.append("可以让我针对其中某一步继续，或把问题范围缩小后重来。")
        return "\n".join(lines)
