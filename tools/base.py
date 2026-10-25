"""工具体系的底层契约：ProcessorTool 与 ToolRegistry。

放在独立模块，避免工具实现（builtin / external / collect）在互相引用时连带
拖进这张契约表而形成循环导入——它们都只需要 import 本模块。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    """工具的返回值：**文本 + 可选的多模态块**。

    为什么需要它
    ------------
    工具结果以前只能是**纯字符串**（``run`` 的签名就是 ``Awaitable[str]``），
    于是「把 PDF 页面渲染成图给 agent 看」这件事没有出口——图不是字符串，
    总不能把 PNG 的字节塞进 observation（那既撑爆上下文，模型也看不见）。

    现在工具可以返回 :class:`ToolResult`：

    - ``text``：给模型看的说明文字。**照旧**走截断 / 外部化那套既有逻辑；
    - ``blocks``：要**原样附在消息里**的多模态块。目前只有厂商文件引用
      （``{"type": "file", "file_id": ...}``）。

    为什么块**不进**文本预算
    ----------------------
    图片的体积与计费由厂商按自己的规则算（缩放后按 token 计），跟字符数
    没关系。如果把它当文本处理，就会被 3500 字符的 observation 上限砍掉、
    或者被「外部化成 block_id」——而 block_id 是给**文本**用的，模型拿它
    ``read`` 出来只会得到一堆二进制。那图就永远到不了模型眼前。

    所以：文本走文本的规则，块原样带走。
    """

    text: str
    blocks: List[Dict[str, Any]] = field(default_factory=list)

    def __str__(self) -> str:  # pragma: no cover - 只是为了日志里好看
        return self.text


@dataclass
class ProcessorTool:
    name: str
    description: str
    parameters: Dict[str, str]  # 参数名 -> 说明（用于拼 system prompt）
    #: 返回类型：``str``（绝大多数工具）或 :class:`ToolResult`（带多模态块的工具）。
    run: Callable[[dict], Awaitable[Any]]
    read_only: bool = False
    #: 能否执行命令 / 以非常规方式触达文件（shell、解释器、任意读写）。
    #: 这类工具执行前要过 :mod:`keeper.tools.guard`：只读工作区按「读命令白名单」
    #: 放行，可写区至少要挡住工作空间外的路径。内置 ``fs.*`` 工具都是 False——
    #: 它们只碰工作空间内的路径，由 ``mutating`` / ``read_only`` 那套约束即可。
    dangerous: bool = False
    #: 仅在特定会话类别下披露/可用（如浏览器自动化）。普通会话里既不进 system
    #: 工具清单，也不会被模型调用（调用时也过 chat/context 的会话类别守卫）。
    browser_only: bool = False

    async def execute(self, args: dict) -> Any:
        """执行工具。返回值可能是 ``str``，也可能是 :class:`ToolResult`。

        出错时**不抛异常**，而是回一句人话——工具失败是 agent 循环里的正常
        一环（参数写错、文件不在），让它自己看到理由并改策略，比整个
        agent 崩掉有用得多。
        """
        try:
            return await self.run(args or {})
        except Exception as e:  # 工具出错不应让整个 agent 崩溃
            logger.warning("工具 %s 执行失败: %s", self.name, e)
            return f"[工具 {self.name} 执行出错: {e}]"


class ToolRegistry:
    """agent 当前可用工具的注册表，并能生成给 LLM 的工具说明文本。"""

    def __init__(self) -> None:
        self._tools: Dict[str, ProcessorTool] = {}
        # 组名 → 一句话摘要（插件清单里写的，给折叠后的分组行用）。
        # 折叠只剩名字时，模型靠它判断「这一组跟我现在的活有没有关系」——
        # 没有这句，模型面对一坨陌生名字可能干脆不用（那就是能力退化）。
        self.group_summaries: Dict[str, str] = {}

    def set_group_summary(self, group: str, summary: str) -> None:
        """登记一个组的摘要；空值 / 纯空格不登记（宁可不显示，也别显示个空括号）。"""
        text = str(summary or "").strip()
        if text:
            self.group_summaries[group] = text

    def register(self, tool: ProcessorTool) -> None:
        """登记一个工具；同名直接覆盖（工具按名唯一，不存在「谁先来」的问题）。"""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """移除一个工具；没登记过就什么也不做——移除不存在的东西不算错误。"""
        self._tools.pop(name, None)

    def clear(self) -> None:
        """清空整张表。"""
        self._tools.clear()

    def replace_all(self, tools: Any) -> None:
        """用给定工具整体替换当前表（原子：内部字典整个换引用）。

        工具表变更的单位必须是**整张表**，不能逐个增删——否则中途会出现
        「只加未减」或「只减未加」的半成品状态，正在跑的那一轮就看到错的能力集。

        Args:
            tools: ``ToolRegistry`` 或 ``ProcessorTool`` 的可迭代对象。
        """
        items = tools.all() if hasattr(tools, "all") else list(tools or [])
        self._tools = {t.name: t for t in items}

    def get(self, name: str) -> Optional[ProcessorTool]:
        return self._tools.get(name)

    def all(self) -> List[ProcessorTool]:
        return list(self._tools.values())

    def describe(self) -> str:
        """全部工具的**完整**清单（每个都带参数说明）。

        只在确实要全量暴露时用；常规路径走 ``describe_brief``——工具一多，
        全量清单就是上下文里最大的一块死重。
        """
        if not self._tools:
            return "（当前没有可用工具）"
        tools = [t for t in self._tools.values()]
        if not tools:
            return "（当前没有可用工具）"
        return "\n".join(self._line(t) for t in tools)

    # ---- 按需展示（P1，见 doc/capability-loading.md 第四节）----
    @staticmethod
    def _line(t: ProcessorTool) -> str:
        params = ", ".join(f"{k}: {v}" for k, v in t.parameters.items()) or "（无参数）"
        # 描述里的裸换行续行必须缩进两格：否则以 `-` 开头的续行（技能的 turn:/ctx:、
        # fs.deliver 的 - 文件/- 图表）会被 markdown 解析成**同级新列表项**，把一条
        # 工具描述撕成看起来像好几个工具的碎片。缩进后它们回归所属列表项，
        # 本意的子项正好渲染成子列表。
        desc = (t.description or "").replace("\n", "\n  ")
        return f"- {t.name}: {desc}\n  参数: {params}"

    def _group_tools(self) -> "tuple[List[ProcessorTool], Dict[str, List[ProcessorTool]]]":
        """把工具分成「零散（内置）」与「按组」两堆。

        分组依据是工具名里的 ``__``：``{plugin}__{server}__{tool}`` 或
        ``{plugin}__{tool}``，**去掉最后一段**就是组名。内置工具（``fs.read_file``
        等）没有 ``__``，永远按零散处理、给完整明细。
        """
        plain: List[ProcessorTool] = []
        groups: Dict[str, List[ProcessorTool]] = {}
        for t in self._tools.values():
            key = t.name.rsplit("__", 1)[0] if "__" in t.name else ""
            if key:
                groups.setdefault(key, []).append(t)
            else:
                plain.append(t)
        return plain, groups

    def describe_brief(self, group_min: int = 3) -> str:
        """常驻的**精简**清单：内置 / 零散工具给明细，大组折叠成一行。

        折叠只折掉参数说明那一坨，**组里每个工具的短名必须全列出来**：模型是靠
        名字调用的，名字看不见就谈不上「调用时补载」——那条兜底路径要求名字可见。
        """
        if not self._tools:
            return "（当前没有可用工具）"
        plain, groups = self._group_tools()
        lines = [self._line(t) for t in plain]
        for key in sorted(groups):
            tools = groups[key]
            summary = self.group_summaries.get(key) or ""
            if len(tools) < max(1, group_min) or not summary:
                # 两种情况下**不折叠**，走老流程给完整明细：
                #   ① 组太小——折叠省不了几个 token，还白白添一层间接；
                #   ② 作者没写 summary——折叠后只剩一坨名字，模型不知道这组是干嘛的，
                #      可能干脆不用。**宁可多花 token，也不拿可发现性去换。**
                # 于是「想省这个 token 就去写 summary」，是正向激励而不是强制。
                lines.extend(self._line(t) for t in tools)
                continue
            short = [t.name.rsplit("__", 1)[-1] for t in tools]
            shown, rest = short[:20], short[20:]
            more = f" 等共 {len(tools)} 个" if rest else ""
            lines.append(
                f"- 【{key}】（{summary}）{len(tools)} 个工具：{', '.join(shown)}{more}\n"
                f"  调用时写全名 `{key}__<名字>`；"
                f"要一次拿到全部参数说明就 load_capabilities([\"{key}\"])"
            )
        return "\n".join(lines)

    def group_keys(self) -> List[str]:
        """所有组名（``plugin`` 或 ``plugin__server``）：可一次展开整组。"""
        return sorted(self._group_tools()[1])

    def disclosed_names(self, group_min: int = 3) -> List[str]:
        """``describe_brief`` 里**已经给出明细**的工具名。

        这些不用补载——它们的参数说明已在 system 里。其余工具首次被调时才补。
        """
        plain, groups = self._group_tools()
        names = [t.name for t in plain]
        for key, tools in groups.items():
            # 与 describe_brief 的判断保持一致：没折叠的组就是「已披露」的
            if len(tools) < max(1, group_min) or not (
                self.group_summaries.get(key) or ""
            ):
                names.extend(t.name for t in tools)
        return names

    def describe_full(
        self, key: str, max_items: int = 30
    ) -> "tuple[str, List[str]]":
        """展开一个工具或一整组的完整定义；返回 ``(文本, 命中的工具名)``。

        ``key`` 可以是工具全名，也可以是组名 / 组前缀（``"myplug__graph"``），
        后者一次展开整组——多工具协同时不必逐个加载。
        """
        exact = self._tools.get(key)
        if exact is not None:
            return self._line(exact), [exact.name]
        prefix = key if key.endswith("__") else key + "__"
        hits = [t for n, t in self._tools.items() if n.startswith(prefix)]
        if not hits:
            return "", []
        hits = hits[:max_items]
        head = f"【{key}】共 {len(hits)} 个工具的完整定义："
        return "\n".join([head] + [self._line(t) for t in hits]), [t.name for t in hits]
