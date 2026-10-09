"""agent 画像配置。

本模块管两件事：

1. **配置结构**：``AgentConfig`` / ``WorkspaceConfig`` / 默认工作目录——描述
   「这个 agent 是谁、有什么能力」的数据，外加已装载的能力对象。
2. **配置来源（含全部 DB 逻辑）**：``load_agent_config`` 从数据库读出 agent 画像 +
   它的 skill / mcp / tool 绑定，解析成一份**完整的** ``AgentConfig``
   （``skill_registry`` / ``tools`` 等能力对象也一并作为 ``AgentConfig`` 的字段）。

框架不认识 MCP / skill / tool 的具体对象——它们作为 ``AgentConfig`` 的字段交给
``KeeperAgent`` 使用（见 ``keeper.agent.keeper``）。本模块**不**构造 ``KeeperAgent``、
也**不**调用 ``build()``——实例构建与 build 是 ``KeeperAgent`` 的事。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from ..mcp.locate import ExecutableNotFoundError
from ..plat.fetcher import FetchError
from ..plugin import agent_plugins_dir, find_plugin, link_plugin, linked_plugins
from ..skill import SkillRegistry
from ..store import (
    Agent,
    AgentCapabilityOverride,
    AgentLLMBinding,
    LLMProfile,
    USER_SPACE_DEFAULT_NAME,
    UserSpace,
    get_session_factory,
)

logger = logging.getLogger(__name__)

# 插件清单文件名：包解压后根目录的这一份是**真源**（库里 manifest 只是快照）
PLUGIN_MANIFEST = "keeper-plugin.json"
# MCP 组件的 server 名与工具名的分隔符（与 MCPSession.prefixed 一致）
_MCP_SEP = "__"


@dataclass
class WorkspaceConfig:
    """工作区配置：**每个 agent 都有一块自己的目录**。

    - ``path`` 给了 → 用它（绑定已有目录 / 仓库）；
    - 没给 → 自动用 ``<默认根>/<agent_name>/``。

    所以工作空间**总是存在**，工具不必判断「有没有地盘」。

    ``read_only`` 决定能不能写，**默认只读**（安全优先）；要写文件时显式打开。

    工作区只提供「根路径」这一事实，**不做** clone / pull / 索引等任何具体动作——
    那些属于具体工具的能力，由 tool / skill 自行决定怎么用这块目录。
    """

    kind: str = "none"
    path: Optional[str] = None
    read_only: bool = True
    # 历史兜底字段：过去按 agent 名给每个 agent 单独默认目录；新设计下默认统一走
    # 系统默认用户空间（workspace/user/default），此字段不再参与路径计算。
    agent_name: Optional[str] = None

    def root(self) -> Optional[Path]:
        """工作空间根目录：显式路径优先，否则落到系统默认用户空间 ``workspace/user/default``。"""
        if self.path:
            return Path(self.path).expanduser()
        return default_workspace_root() / USER_SPACE_DEFAULT_NAME


@dataclass
class AgentConfig:
    """agent 画像：这个 agent **是谁、有什么能力**。

    与框架配置相对：这里的东西用户随时会改（换 persona、勾一个 skill），
    由装配器从数据库的 agent 表读出并填充（见 ``load_agent_config``）。
    """

    name: str
    # 预设提示词（人设）：始终生效，且只有一个——定义「你是谁、怎么回答」。
    # 与 skill 的区别：skill 是可增减的具体做法，persona 是恒定基调。
    persona: str = ""
    description: str = ""
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    llm: Dict[str, Any] = field(default_factory=dict)
    # 连接器：本 agent 要连哪些 MCP server（运行时由 MCPManager 从 agent.mcp_servers 装配）
    mcp_servers: Dict[str, Any] = field(default_factory=dict)
    # 对端 agent 的 A2A 接入配置：{peer: {a2a_url, headers}}
    peers: Dict[str, Any] = field(default_factory=dict)
    # 工具组名 → 一句话摘要：折叠后的分组行靠它让模型判断「这组跟我有关吗」
    group_summaries: Dict[str, str] = field(default_factory=dict)
    # 已装载的能力对象：DB 装配时由本模块注入；直接构造时缺省为**空注册表**
    # （``default_factory`` 每次新建，不会共享可变默认）。把它们作为字段，
    # ``KeeperAgent`` 只需认一份 ``AgentConfig``。
    skill_registry: SkillRegistry = field(default_factory=SkillRegistry)
    tools: List[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        """归一化：保证任意来源的画像都是「符合要求」的配置。

        agent 侧不再做任何判空 / 默认值处理——这里把所有字段收拾齐：
        人设恒为字符串、workspace 统一成 ``WorkspaceConfig`` 且带 ``agent_name``、
        各集合字段恒为非空容器、llm 恒为 dict。这样 ``KeeperAgent.__init__``
        拿到 config 就能直接用，不必再兜底。
        """
        if not self.persona:
            self.persona = ""
        # workspace：允许用字符串路径简写，统一归一为 WorkspaceConfig
        if isinstance(self.workspace, str):
            self.workspace = WorkspaceConfig(kind="local", path=self.workspace)
        elif not isinstance(self.workspace, WorkspaceConfig):
            self.workspace = WorkspaceConfig()
        # 没给 agent_name 时，用配置名兜底（决定默认工作目录）
        if self.workspace.agent_name is None:
            self.workspace.agent_name = self.name
        if not self.mcp_servers:
            self.mcp_servers = {}
        if not self.peers:
            self.peers = {}
        if not self.llm:
            self.llm = {}


# 工作区统一根：下面分 user / agent / session 三层，分别属于用户、agent、会话。
WORKSPACE_ROOT = Path.home() / ".keeper" / "workspace"


def default_workspace_root() -> Path:
    """用户工作区（``user``）的**默认根目录**：``~/.keeper/workspace/user``。

    不指定 path 时，按 agent 名落到 ``~/.keeper/workspace/user/<agent_name>/``。
    ``~/.keeper/workspace`` 下统一分 ``user`` / ``agent`` / ``session`` 三层，分别属于
    用户、agent、会话；跟连接器缓存 ``~/.keeper/mcp`` 同级。
    """
    return WORKSPACE_ROOT / "user"


def default_agent_space_root(session_id: str, agent_id: str) -> Path:
    """会话级 agent 空间（``session``）：``~/.keeper/workspace/session/<session_id>/<agent_id>``。

    每个 (会话, agent) 一份，放 agent 私有产物（插件索引、缓存、**临时文件**等），
    纯路径推导、不落库。会话开始时由工作空间解析逻辑负责 ``mkdir``。

    模型侧用 ``@tmp/xxx`` 指代这里（见 ``tools.builtin.sandbox.resolve_path``），
    工具会把它解析到本目录——所以这个目录**必须对模型可写**，否则它写临时脚本
    时就只能往用户工作区根下堆（check.py / test_*.js 之类）。
    目录刻意留在用户工作区**之外**：放进去虽然省了改沙箱的功夫，却会在用户的
    文件浏览器里凭空多出一堆临时文件。
    """
    return WORKSPACE_ROOT / "session" / session_id / agent_id


def default_agent_space_base(agent_id: str) -> Path:
    """agent 级私有产物根（``agent``）：``~/.keeper/workspace/agent/<agent_id>``。

    与 :func:`default_agent_space_root` 的区别在于**少了 session 维度**——MCP server
    与插件 bin 工具在 ``build`` 期就启动、被同一 agent 的多个会话共享，进程内无法感知
    具体 session，因此只能用「按 agent 隔离」的稳态目录。跨会话共享索引 / 缓存反而
    更快，所以这是合理的取舍；会话级的 per-(session, agent) 空间只面向请求期内置工具。
    """
    return WORKSPACE_ROOT / "agent" / agent_id


def default_agent_resource_root(agent_id: str) -> Path:
    """本 agent 全部「平台下发资源」的下载根：``~/.keeper/agents/<agent_id>``。

    与 keeper 自带的连接器缓存 ``~/.keeper/mcp``（``default_cache_root``）完全独立——
    前者是「agent 从平台拉下来的代码」，后者是「keeper 自己带的能力」。
    默认每个 agent 一份（决策 #4 隔离），需要合并 / 复用再显式配缓存目录覆盖。
    """
    return Path.home() / ".keeper" / "agents" / str(agent_id)


def _kind_download_root(agent, kind: str) -> Path:
    """某 kind 的下载根目录（即 ``resource_dir`` 的 agent_root 参数）。

    - 显式配了 ``mcp_cache_dir`` / ``skill_cache_dir`` / ``tool_cache_dir`` → 用它；
    - 否则落到 per-agent 默认树下 ``<agent_root>/<kind>``（决策 #4）。

    注意：调用方传入的可能是运行期 ``KeeperAgent``（没有上面那些 ORM 列属性，
    也没有 ``.id``、而是 ``.agent_id``），所以用 ``getattr`` 容错，避免 AttributeError。
    """
    override = getattr(agent, f"{kind}_cache_dir", None)
    if override:
        return Path(override).expanduser()
    agent_id = getattr(agent, "id", None) or getattr(agent, "agent_id", None)
    if not agent_id:
        raise ValueError("无法确定 agent 标识，无法计算资源根目录")
    return default_agent_resource_root(agent_id) / kind


async def _ensure_plugin_links(agent: Agent) -> List[str]:
    """装载前把该 agent 目录里**断掉的**插件链接补回来。

    绑定关系由 ``~/.keeper/agents/<id>/plugins/<name>`` 这个符号链接目录本身表达，
    所以"有哪些绑定"直接列目录就知道，不需要查表。这里只处理一种情况：
    链接存在但**悬空**（插件库里那个目录被删了 / 移走了）→ 从库里重新建一次。

    库里也找不到了就留着不动并告警：删插件不该让整个 agent 起不来，而且留着
    这条链接，用户把插件放回去它还能自己恢复。

    返回补建成功的插件名（供日志）。
    """
    linked_dir = agent_plugins_dir(agent.id)
    if not linked_dir.is_dir():
        return []

    repaired: List[str] = []
    for link in sorted(linked_dir.iterdir()):
        if link.is_dir():
            continue  # is_dir() 跟随链接 → 有效链接直接过
        if not link.is_symlink():
            continue  # 不是链接（用户手建的目录等），不干预

        p = find_plugin(link.name)
        if p is None:
            logger.warning(
                "插件 %s 已不在插件库里，跳过装配（agent=%s）", link.name, agent.name
            )
            continue
        try:
            link_plugin(agent.id, p)
            repaired.append(link.name)
        except OSError as e:
            logger.warning("插件 %s 的链接建不起来（%s），跳过装配", link.name, e)
    if repaired:
        logger.info("已补建插件链接: %s", "、".join(repaired))
    return repaired


def _import_string(path: str):
    """按 ``module:attr`` 导入可调用对象——把 impl 字符串变成工具工厂函数。"""
    from importlib import import_module

    mod_name, _, attr = (path or "").partition(":")
    if not (mod_name and attr):
        raise ValueError(f"impl 应为 'module:attr' 形式，实际是 {path!r}")
    return getattr(import_module(mod_name), attr)


class AgentNotFoundError(RuntimeError):
    """数据库里找不到可用的 agent 画像。"""


def _json(raw: Optional[str]) -> Any:
    """解析库里的 JSON 字符串；空值或解析失败返回 None（不抛）。"""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception as e:
        logger.warning("配置字段 JSON 解析失败，已忽略: %s", e)
        return None


async def _load_definition(*, name: Optional[str], agent_id: Optional[str]) -> Agent:
    """按 name 或 id 读 agent 画像行；读不到 / 停用 / 废弃都抛 ``AgentNotFoundError``。

    画像读不到就抛错：没有画像就不存在这个 agent，不做「拿默认值凑一个」的降级——
    那会让配置错误悄悄变成行为异常，很难查。
    """
    if not (name or agent_id):
        raise ValueError("装配 agent 需要指定 name 或 id")

    factory = get_session_factory()
    async with factory() as db:
        if agent_id:
            row: Any = await db.get(Agent, agent_id)  # type: ignore[arg-type]
        else:
            row: Any = (
                await db.execute(select(Agent).where(Agent.name == name))
            ).scalars().first()

    if row is None:
        target = f"id={agent_id}" if agent_id else f"name={name}"
        raise AgentNotFoundError(f"数据库里没有这个 agent 画像（{target}）")
    if row.status != "active":
        why = "已废弃" if row.status == "archived" else "已停用"
        raise AgentNotFoundError(
            f"agent {why}（name={name}，status={row.status}）"
        )
    return row


def _build_skill_registry(items: List[Dict[str, Any]]) -> SkillRegistry:
    """把知识条目装进注册表——**插件的 ``skills/`` 是唯一来源**。

    技能是声明式数据（一段 system prompt 约束），不需要缓存目录也不需要下载。
    原先的 ``skill`` 表已并入插件，所以这里只负责把插件拆出来的知识组装起来。
    """
    registry = SkillRegistry().load_from_dicts(items)
    logger.debug(
        "装配知识 %d 条：%s",
        len(items), ", ".join(i["name"] for i in items) or "无",
    )
    return registry


async def _capability_overrides(agent_id: str) -> Dict[tuple[str, str], bool]:
    """读出该 agent 的全部本地开关，返回 ``{(kind, ref_name): enabled}``。

    平台同步不会碰这张表，所以写在这里的开关不会被冲掉。
    """
    from ..store import AgentCapabilityOverride, get_session_factory

    factory = get_session_factory()
    async with factory() as db:
        rows = (
            await db.execute(
                select(AgentCapabilityOverride).where(
                    AgentCapabilityOverride.agent_id == agent_id
                )
            )
        ).scalars().all()
    return {(r.kind, r.ref_name): bool(r.enabled) for r in rows}


def _enabled(
    overrides: Dict[tuple[str, str], bool],
    kind: str,
    ref_name: str,
    default: bool,
) -> bool:
    """本地开关优先：有记录就以它为准，没有就用 ``default``（平台 binding 的值）。"""
    v = overrides.get((kind, ref_name))
    return default if v is None else v


async def _resolve_workspace(row: Agent) -> tuple[Path, bool]:
    """装配期确定「工作空间根 + 它是否只读」。

    语义（唯一真源 = ``user_space.read_only``）：

    - agent **绑定了**工作区（``workspace_path`` 有值）→ 用那个空间的只读标志；
      但它必须在 ``user_space`` 表里有登记，否则**报错**——静默回落到 default 会
      变成「agent 明明绑了 A 目录，却按 B 目录的只读判」，配置与实际不符；
    - agent **没绑定**（``workspace_path`` 为空 / kind=none）→ 用 ``default`` 空间。

    为什么必须是 ``user_space.read_only``：运行期
    :func:`keeper.chat.service.resolve_workspace` 读的就是它。两个来源曾经各判各的
    （这里读 ``agent.workspace_read_only``、运行期读 user_space），后果是
    「工作区明明可写，装配期却按 agent 画像上那个从未被改过的默认值 1 把写类工具
    全丢了」——工具在界面上凭空消失，还没有任何报错。

    ``agent.workspace_read_only`` 不再参与判断（连兜底也不做：它既不权威、又会
    被平台同步冲掉，留着只会误导下一个人）。
    """
    async with get_session_factory()() as db:
        space = None
        if row.workspace_path:
            space = (
                await db.execute(
                    select(UserSpace).where(UserSpace.path == row.workspace_path)
                )
            ).scalars().first()
            if space is None:
                raise ValueError(
                    f"agent {row.id}（{row.name}）绑定的工作区"
                    f"「{row.workspace_path}」不在「用户空间」里。"
                    "工作区必须先登记才能绑定（创建 agent 的下拉框只列已登记的空间）。"
                    "请到「用户空间」补一条，或改绑其它空间。"
                )
        else:
            space = (
                await db.execute(
                    select(UserSpace).where(UserSpace.name == USER_SPACE_DEFAULT_NAME)
                )
            ).scalars().first()
            if space is None:
                raise ValueError(
                    "user_space 表里没有 default 空间，无法确定工作区。"
                    "请先到「用户空间」创建 default。"
                )

    root = Path(space.path).expanduser()
    logger.info(
        "装配 agent %s：工作空间=%s（user_space「%s」read_only=%s，来源=%s）",
        row.id, root, space.name, bool(space.read_only),
        "agent 绑定" if row.workspace_path else "未绑定，用 default",
    )
    return root, bool(space.read_only)


async def _load_builtin_tools(
    definition: Agent,
    *,
    root: Optional[Path] = None,
    read_only: Optional[bool] = None,
) -> List[Any]:
    """装配 keeper 自带的**内置**工具（fs.*）。

    内置工具的代码就在 keeper 里（见 ``tools.builtin.registry``），
    **不查 binding、不问平台**，只看本地开关（无记录 = 默认启用）——
    平台不感知也不下发它们。

    实例化：按 ``impl``（``module:attr``）拿到工厂函数，再传入工作区上下文：

        make_xxx(root=<工作空间>, read_only=<是否只读>) -> ProcessorTool

    写类工具（``mutating``）**不在装配期按只读跳过**：只读标志是「每会话动态」的
    （来自会话绑定的用户空间），改在工具**运行时**据当前工作空间拒绝写操作，
    这样同一 agent 在只读 / 可写会话里都能正确表现，也不用为换个空间重建实例。

    > 平台下发的工具能力已统一由插件提供（MCP 组件 / ``bin`` 组件），
    > 见 ``_load_plugins``。
    """
    root, read_only = await _resolve_workspace(definition) if root is None or read_only is None else (root, read_only)

    overrides = await _capability_overrides(definition.id)
    tools: List[Any] = []

    from ..tools.builtin.registry import BUILTIN_TOOLS, KIND as BUILTIN_KIND

    for t in BUILTIN_TOOLS:
        # 默认启用：没有 override 记录就是开
        if not _enabled(overrides, BUILTIN_KIND, t["name"], default=True):
            continue
        # 注：写类工具（mutating）不再在装配期按 read_only 跳过——只读标志现在
        # 是「每会话动态」的（来自绑定的用户空间），改在工具**运行时**据当前工作空间
        # 拒绝写操作，这样同一 agent 在不同只读/可写会话里能正确表现。
        try:
            make = _import_string(t["impl"])
            tools.append(make(root, read_only))
        except Exception as e:
            logger.warning(
                "内置工具 %s 加载失败（impl=%s）：%s", t["name"], t["impl"], e
            )
            continue

    logger.debug(
        "装配内置工具 %d 个：%s", len(tools), ", ".join(t.name for t in tools) or "无"
    )
    return tools


def _resolve_sourced_command(command: Optional[str], code_dir: Path) -> str:
    """定位「平台下发、已下载」资源的启动命令。

    与 keeper 自带连接器不同，这类资源代码就在 ``code_dir`` 里：

    - 绝对路径且存在 → 用它；
    - 在 PATH 里（node / python / npx 等）→ 用它；
    - 相对路径且 ``code_dir`` 下存在 → 解析成绝对路径（配合 ``cwd=code_dir`` 运行）；
    - 都没有 → 抛 ``ExecutableNotFoundError``，由上层跳过该连接器。
    """
    if not command:
        raise ExecutableNotFoundError(
            f"stdio 连接器未配置 command（code_dir={code_dir}）"
        )
    p = Path(command)
    if p.is_absolute():
        return str(p)
    if shutil.which(command):
        return command
    cand = code_dir / command
    if cand.exists():
        return str(cand.resolve())
    raise ExecutableNotFoundError(
        f"本地没有可执行文件（查过 PATH 与 {cand}）；"
        f"平台下发的 mcp 需保证命令在代码目录内或为 PATH 中命令"
    )


@dataclass
class PluginComponents:
    """插件拆出来的组件（装配器内部用）：一个包可能同时带知识与能力。

    - ``mcp_servers``：MCP 组件，key 已带插件前缀（``{plugin}__{server}``），
      交给 ``MCPManager`` 后工具全名即 ``{plugin}__{server}__{tool}``；
    - ``tools``：可执行文件组件（``bin``），已包装成 ``ProcessorTool``，
      工具全名 ``{plugin}__{tool}``；
    - ``skill_items``：知识组件（SKILL.md 正文），交给 ``SkillRegistry``。
    """

    mcp_servers: Dict[str, Any] = field(default_factory=dict)
    tools: List[Any] = field(default_factory=list)
    skill_items: List[Dict[str, Any]] = field(default_factory=list)
    # 组名 → 一句话摘要：给折叠后的工具分组行用（PluginComponents 里 key 已带前缀）
    group_summaries: Dict[str, str] = field(default_factory=dict)
    # 装配诊断：每一项 ``{"plugin","component","name","ok","reason"}``。
    # 光靠日志定位「某个工具为什么没出现」太慢——装配完把它打出来，
    # 一眼能看到是包没下载、开关关了、还是被工作区只读挡掉。
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)

    def add_diag(
        self,
        plugin: str,
        component: str,
        name: str,
        ok: bool,
        reason: str = "",
    ) -> None:
        self.diagnostics.append(
            {
                "plugin": plugin,
                "component": component,
                "name": name,
                "ok": ok,
                "reason": reason,
            }
        )

    def print_summary(self, agent_id: str, read_only: bool, root: Path) -> None:
        """装配结束打一条汇总（INFO 级），方便事后复盘。"""
        ok = [d for d in self.diagnostics if d["ok"]]
        bad = [d for d in self.diagnostics if not d["ok"]]
        logger.info(
            "装配汇总 agent=%s | 工作区=%s(read_only=%s) | skill=%d mcp=%d bin=%d | "
            "产出工具=%s",
            agent_id, root, read_only,
            len(self.skill_items), len(self.mcp_servers), len(self.tools),
            ", ".join(getattr(t, "name", "?") for t in self.tools) or "（无）",
        )
        for d in ok:
            logger.info(
                "  装配成功 [%s] %s/%s", d["component"], d["plugin"], d["name"]
            )
        for d in bad:
            logger.warning(
                "  装配跳过 [%s] %s/%s：%s",
                d["component"], d["plugin"], d["name"], d["reason"],
            )


def _expand_plugin_vars(
    value: Any, root: Path, agent_space: Optional[Path] = None
) -> Any:
    """把清单里的路径变量展开成绝对路径。

    支持的变量：

    - ``${PLUGIN_ROOT}`` —— 插件包目录（必需，绝对路径）。
    - ``${AGENT_SPACE}`` —— 本 agent 的私有产物根（见 :func:`default_agent_space_base`），
      供 MCP / bin 子进程放索引、缓存等；构建期已知，按 agent 隔离。

    递归处理 str / dict / list，这样 command、args、env、cwd 里的路径都能写变量，
    插件不必（也不该）硬编码绝对路径——安装位置由 keeper 决定。
    """
    if isinstance(value, str):
        s = value.replace("${PLUGIN_ROOT}", str(root))
        if agent_space is not None:
            s = s.replace("${AGENT_SPACE}", str(agent_space))
        return s
    if isinstance(value, dict):
        return {k: _expand_plugin_vars(v, root, agent_space) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_plugin_vars(v, root, agent_space) for v in value]
    return value


def _resolve_plugin_root(dest: Path) -> Path:
    """确定插件包的**真实根目录**：包根就有清单就直接用；否则往下探一层。

    常见打包失误：压缩时把外层文件夹也打了进去，解压后变成
    ``<dest>/<同名目录>/keeper-plugin.json``。而清单读取是按包根找的，找不到就
    返回空——表现为「插件显示已装配，但 MCP / 工具一个都没有」，失败极其隐蔽。

    所以这里宽容一层，但条件收严，避免误判：

    - 包根自己有清单 → 直接用，**不**下探；
    - 否则只看**恰好一个**子目录里是否有清单（跳过隐藏目录与 ``__MACOSX``）；
    - 一个都没有 / 有多个 → 不动（按原样处理，由后续逻辑告警）。
    """
    if (dest / PLUGIN_MANIFEST).is_file():
        return dest
    try:
        cands = [
            p
            for p in sorted(dest.iterdir())
            if p.is_dir()
            and not p.name.startswith(".")
            and p.name != "__MACOSX"
            and (p / PLUGIN_MANIFEST).is_file()
        ]
    except OSError:  # noqa: BLE001
        return dest
    if len(cands) == 1:
        logger.info(
            "插件包多套了一层目录（%s），已按 %s 装配（建议重新打包：让清单位于包根）",
            cands[0].name,
            cands[0],
        )
        return cands[0]
    return dest


def _read_plugin_manifest(dest: Path) -> Dict[str, Any]:
    """读包内清单（真源）。

    清单**可选**：只有 SKILL.md 的纯知识包可以不写，按空清单处理。
    解析失败抛 ``FetchError``——装载期就暴露，而不是等到用的时候才发现。
    """
    path = dest / PLUGIN_MANIFEST
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"插件清单解析失败（{path}）：{e}") from e
    if not isinstance(data, dict):
        raise FetchError(f"插件清单必须是 JSON 对象（{path}）")
    return data


_FRONTMATTER_RE = re.compile(
    r"^\uFEFF?---[ \t]*\n(.*?)\n---[ \t]*(?:\n(.*))?$", re.S
)


def _parse_frontmatter_list(fm_text: str, key: str) -> List[str]:
    """从 frontmatter 解析列表字段；支持行内 ``[a, b]`` 与块列表 ``- a``。"""
    out: List[str] = []
    # 1) 行内形式：key: [a, b, c]
    m = re.search(rf"(?im)^\s*{re.escape(key)}\s*:\s*\[(.*?)\]\s*$", fm_text)
    if m:
        for item in m.group(1).split(","):
            item = item.strip().strip("'\"")
            if item:
                out.append(item)
        return list(dict.fromkeys(out))
    # 2) 块形式：key:\n  - a\n  - b
    m = re.search(
        rf"(?im)^\s*{re.escape(key)}\s*:\s*$\n((?:[ \t]*-[ \t]*[^\n]*\n?)*)", fm_text
    )
    if m:
        for line in m.group(1).splitlines():
            line = line.strip()
            if line.startswith("-"):
                item = line[1:].strip().strip("'\"")
                if item:
                    out.append(item)
    return list(dict.fromkeys(out))


def _parse_skill_tools(fm_text: str) -> List[str]:
    """解析 ``tools``：技能软标注自己擅长哪些工具（不控制工具可用性）。"""
    return _parse_frontmatter_list(fm_text, "tools")


def _fallback_summary(body: str) -> str:
    """没写 description 时用正文首个标题行当概要——**只用于清单展示**。

    作者没写概要，清单里总不能显示「未写用途说明」，模型看不出这技能是干嘛的。
    取正文第一行标题（去掉 `#`）足够表意；这**不改变**它按常驻处理的决定。
    """
    for line in (body or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:80]
    return ""


def _parse_frontmatter_scalar(fm_text: str, key: str) -> str:
    """取 frontmatter 里的单值字段（``key: value``），取不到返回空串。

    只支持**单行值**：``description`` 本来就该是一句话；写成 YAML 块语法的按取不到
    处理——宁可让它退化为常驻全文，也好过把半截内容当成概要给模型看。
    """
    m = re.search(rf"(?im)^\s*{re.escape(key)}\s*:\s*(.*?)\s*$", fm_text)
    return m.group(1).strip().strip("'\"") if m else ""


def _empty_skill_meta() -> "Dict[str, Any]":
    """一份「什么都没声明」的 meta。

    所有分支都必须走这里**生成**，不要在调用点手写字典字面量——上次加 ``keywords``
    时就在早退分支漏了一个键，装载期直接 ``KeyError``。**新增字段只改这一处。**
    """
    return {"tools": [], "keywords": [], "description": "", "always": False}


def _split_skill_frontmatter(raw: str) -> "tuple[str, Dict[str, Any]]":
    """把 SKILL.md 拆成 ``(正文, meta)``。

    支持顶部 YAML frontmatter（``--- ... ---``）；没有或没闭合时返回 ``(原文, 空 meta)``。

    ``meta`` 四样：

    - ``tools``      ：软标注本技能擅长的工具（不控制实际工具可用性）；
    - ``keywords``   ：系统侧预加载的匹配词（可选，不写则从 description 抽）；
    - ``description``：**什么时候用**——常驻上下文的 L1 概要，按需加载的判据；
    - ``always``     ：作者声明「这是行为规矩」，正文常驻。

    见 ``doc/capability-loading-design.md`` 第二节（L1 / L2 拆分）。
    """
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return raw, _empty_skill_meta()
    fm_text = m.group(1) or ""
    body = m.group(2) or ""
    meta = _empty_skill_meta()
    meta.update(
        {
            "tools": _parse_skill_tools(fm_text),
            "keywords": _parse_frontmatter_list(fm_text, "keywords"),
            "description": _parse_frontmatter_scalar(fm_text, "description"),
            "always": _parse_frontmatter_scalar(fm_text, "always").lower()
            in ("true", "1", "yes", "on"),
        }
    )
    return body, meta


def _discover_skills(
    plugin_name: str, dest: Path, manifest: Dict[str, Any]
) -> List[tuple[str, Path]]:
    """收集插件的知识组件：**目录自动发现** + 清单声明（两者按路径去重）。

    与 Claude Code 一致——``skills/`` 是默认目录，放进去就生效，清单只用来
    指向**非默认位置**的文件。所以下面两种布局都支持：

    - ``skills/SKILL.md``        → 插件的主知识，名字就用插件名
    - ``skills/<name>/SKILL.md`` → 多个知识，名字为 ``{plugin}__{name}``
    - ``skills/<name>.md``       → 上一种的简写

    清单里的 ``skill``（单个）与 ``skills``（列表）是补充，指向目录外的文件。
    """
    found: List[tuple[str, Path]] = []
    seen: set[Path] = set()
    skills_dir = dest / "skills"

    def add(path: Path) -> None:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            return
        seen.add(key)
        found.append((_skill_name(plugin_name, path, skills_dir), path))

    # 1) 清单声明的（可指向任意位置，含目录外）
    declared = manifest.get("skills")
    if isinstance(declared, str):
        declared = [declared]
    for item in declared or []:
        if isinstance(item, str) and item.strip():
            add(dest / item.strip())
    single = manifest.get("skill")
    if isinstance(single, str) and single.strip():
        add(dest / single.strip())

    # 2) skills/ 目录自动发现（即使清单没写也生效）
    if skills_dir.is_dir():
        for entry in sorted(skills_dir.iterdir()):
            if entry.is_dir():
                for cand in ("SKILL.md", "skill.md"):
                    p = entry / cand
                    if p.is_file():
                        add(p)
                        break
            elif entry.is_file() and entry.suffix.lower() == ".md":
                add(entry)

    return found


def _skill_name(plugin_name: str, path: Path, skills_dir: Path) -> str:
    """知识条目的名字（必须唯一，否则注册表会互相覆盖）。

    - ``skills/SKILL.md``（直接在 ``skills/`` 根下且就叫 SKILL）→ 插件主知识，
      名字用插件名；
    - ``skills/<name>/SKILL.md`` → ``{plugin}__{name}``，取目录名；
    - ``skills/<name>.md`` → ``{plugin}__{name}``，取文件名。
    """
    if path.parent == skills_dir and path.stem.upper() == "SKILL":
        return plugin_name
    if path.parent.parent == skills_dir:
        return f"{plugin_name}{_MCP_SEP}{path.parent.name}"
    return f"{plugin_name}{_MCP_SEP}{path.stem}"


def _command_problem(command: str) -> Optional[str]:
    """检查可执行文件能不能用：不存在 / 目录 / 没执行权限，返回原因，否则 None。

    插件命令经 ``${PLUGIN_ROOT}`` 展开后必然是绝对路径，而
    ``_resolve_sourced_command`` 对绝对路径**不校验**就返回，所以二进制缺失或
    没有 +x 位（解压丢权限那种）要在这里补查——装载期就报，别等连接失败。
    """
    p = Path(command)
    if p.is_absolute():
        if not p.exists():
            return f"可执行文件不存在：{p}"
        if p.is_dir():
            return f"command 指向的是目录，不是可执行文件：{p}"
        if not os.access(p, os.X_OK):
            return f"可执行文件没有可执行权限（+x）：{p}"
    return None



def _build_plugin_components(
    *,
    name: str,
    dest: Path,
    settings: Dict[str, Any],
    out: "PluginComponents",
    overrides: Dict[tuple, bool],
    root: Path,
    read_only: bool,
    agent_space: Path,
) -> None:
    """从 ``dest`` 的清单装配插件三大组件（DB 插件与内置插件共用）。

    与 ``_load_plugins`` 里原本内联的装配逻辑一致；抽出来让内置插件复用同一套逻辑，
    保证两种来源的行为完全对齐。
    """
    from ..tools.subprocess_tool import (
        DEFAULT_TIMEOUT,
        SubprocessToolError,
        make_subprocess_tool,
    )

    manifest = _read_plugin_manifest(dest)

    # 目录与清单一致性
    for dirname, key in (("mcp", "mcpServers"), ("bin", "bin")):
        d = dest / dirname
        if d.is_dir() and any(d.iterdir()) and not manifest.get(key):
            logger.warning(
                "插件 %s 有 %s/ 目录但清单未声明 %s，该目录下的内容不会被加载",
                name, dirname, key,
            )

    # 1) 知识组件：skills
    for skill_name, skill_path in _discover_skills(name, dest, manifest):
        if not skill_path.is_file():
            logger.warning("插件 %s 声明的 skill 文件不存在：%s", name, skill_path)
            out.add_diag(name, "skill", skill_name, False, f"文件不存在 {skill_path}")
            continue
        try:
            raw = skill_path.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.warning("插件 %s 读取 skill 文件失败：%s", name, skill_path, e)
            continue
        # 拆分 frontmatter（tools 软标注 + description/always）与正文。tools 不再
        # 写死为空，此前占位导致「可用技能」段永远显示「工具：无」。
        body, skill_meta = _split_skill_frontmatter(raw)
        # 兼容规则：**没写 description 的旧技能一律按常驻全文处理**。
        # 省 token 的前提是模型能靠概要判断要不要用；概要都没有就摘掉正文，
        # 等于让能力凭空消失——宁可不省，也不能让它退化（设计文档第九节）。
        description = skill_meta["description"] or _fallback_summary(body)
        always = bool(skill_meta["always"]) or not skill_meta["description"]
        out.skill_items.append(
            {
                "name": skill_name,
                "system_prompt": body,
                "tools": skill_meta["tools"],
                "keywords": skill_meta["keywords"],
                "description": description,
                "always": always,
                "optional_subgraph": None,
            }
        )
        out.add_diag(
            name,
            "skill",
            skill_name,
            True,
            f"常驻={always} 声明工具={skill_meta['tools'] or '无'}",
        )

    # 2) 能力组件：mcpServers
    for srv_name, srv_cfg in (manifest.get("mcpServers") or {}).items():
        if not isinstance(srv_cfg, dict):
            logger.warning("插件 %s 的 mcpServers.%s 不是对象，跳过", name, srv_name)
            out.add_diag(name, "mcp", srv_name, False, "清单里这一项不是对象")
            continue
        cfg = _expand_plugin_vars(srv_cfg, dest, agent_space)
        cfg = {**cfg, **settings}
        cfg.setdefault("cwd", str(dest))
        if cfg.get("transport", "stdio") == "stdio":
            try:
                command = _resolve_sourced_command(cfg.get("command"), Path(cfg["cwd"]))
            except ExecutableNotFoundError as e:
                logger.warning("跳过插件 %s 的 MCP server %s：%s", name, srv_name, e)
                out.add_diag(name, "mcp", srv_name, False, str(e))
                continue
            problem = _command_problem(str(command))
            if problem:
                logger.warning("跳过插件 %s 的 MCP server %s：%s", name, srv_name, problem)
                out.add_diag(name, "mcp", srv_name, False, str(problem))
                continue
            cfg["command"] = command
        out.mcp_servers[f"{name}{_MCP_SEP}{srv_name}"] = cfg
        out.add_diag(name, "mcp", srv_name, True, f"command={cfg.get('command')}")
        # 组摘要：折叠后这一组只剩工具名，靠这句话让模型判断「跟我现在的活有关吗」。
        # 不写的组就只有名字（英文命名通常还能自解释，但中文/缩写场景会很难受）。
        if str(srv_cfg.get("summary") or "").strip():
            out.group_summaries[f"{name}{_MCP_SEP}{srv_name}"] = str(
                srv_cfg["summary"]
            ).strip()

    # 3) 能力组件：bin 可执行文件（子进程，语言无关）
    for entry in (manifest.get("bin") or []):
        if not isinstance(entry, dict):
            logger.warning("插件 %s 的 bin 条目不是对象，跳过", name)
            continue
        # 组摘要（可选）：bin 工具的组名就是插件名，一句说明覆盖这一组
        if str(entry.get("summary") or "").strip():
            out.group_summaries.setdefault(name, str(entry["summary"]).strip())
        command = _expand_plugin_vars(entry.get("command"), dest, agent_space)
        if not command:
            logger.warning("插件 %s 的 bin 条目缺少 command，跳过", name)
            continue
        problem = _command_problem(str(command))
        if problem:
            logger.warning("插件 %s 的 %s 跳过：%s", name, entry.get("name"), problem)
            continue
        entry_cwd = str(_expand_plugin_vars(entry.get("cwd") or dest, dest, agent_space))
        extra_argv = _expand_plugin_vars(entry.get("args") or [], dest, agent_space)
        timeout = int(entry.get("timeout") or DEFAULT_TIMEOUT)

        for t in (entry.get("tools") or []):
            if not isinstance(t, dict) or not t.get("name"):
                logger.warning("插件 %s 的 bin 工具缺少 name，跳过：%s", name, t)
                out.add_diag(name, "bin", "?", False, "清单里这一项缺少 name")
                continue
            mutating = bool(t.get("mutating", False))
            needs_ws = bool(t.get("requires_workspace", False))
            # read_only 与 mutating 独立：能否并发跑由作者用 read_only 显式声明，
            # 没声明一律串行（拿不准就慢，安全侧）。
            tool_read_only = bool(t.get("read_only", False))
            if mutating and read_only:
                # 与内置工具对齐：**不再**在装配期丢弃写类工具。
                # 只读是「每会话动态」的（来自会话绑定的用户空间），装配期丢掉它
                # 造成的最坏后果是「工作区明明可写，工具却在界面上凭空消失」——
                # 静默失败比晚一点拒绝难查得多。真正的拒绝放在运行期：
                #   1) keeper 侧 guard.check_call(read_only=...) 放行读命令白名单；
                #   2) 子进程收到的 context.read_only 也是这个值，插件自己可以再兜一层。
                logger.info(
                    "插件 %s 的写类工具 %s：工作区当前只读，但**仍然装配**"
                    "（运行期按当前会话的工作区判定，而不是装配期一刀切）",
                    name, t["name"],
                )
            if needs_ws and not root:
                logger.info("无工作区，跳过插件 %s 的依赖工作区工具 %s", name, t["name"])
                out.add_diag(
                    name,
                    "bin",
                    t["name"],
                    False,
                    "agent 没有可用工作区，而工具声明 requires_workspace=true",
                )
                continue
            reg_name = f"{name}{_MCP_SEP}{t['name']}"
            try:
                out.tools.append(
                    make_subprocess_tool(
                        name=reg_name,
                        tool=t["name"],  # 传给可执行文件的是短名
                        description=t.get("description") or "",
                        schema=t.get("parameters") or {},
                        command=str(command),
                        extra_argv=extra_argv,
                        cwd=entry_cwd,
                        timeout=timeout,
                        workspace=root,
                        read_only=read_only,
                        tool_read_only=tool_read_only,
                    )
                )
                logger.info(
                    "装配 bin 工具：插件=%s 工具=%s command=%s cwd=%s timeout=%ds",
                    name, reg_name, command, entry_cwd, timeout,
                )
                out.add_diag(name, "bin", t["name"], True, f"注册名={reg_name}")
            except SubprocessToolError as e:
                logger.warning("插件 %s 的工具 %s 装配失败：%s", name, t["name"], e)
                out.add_diag(name, "bin", t["name"], False, str(e))


async def _load_plugins(
    agent: Agent,
    *,
    root: Optional[Path] = None,
    read_only: Optional[bool] = None,
) -> PluginComponents:
    """装配本 agent 的插件：读包内清单 → 拆成 skill / mcpServers 两类组件。

    插件是「一个包 + 一份清单」的统一分发单元（合并了原 mcp / skill / tool）：

    - 包已由 ``_fetch_agent_resources`` 下载到 ``<agent_root>/plugin/<name>``；
    - 清单真源是**包内** ``keeper-plugin.json``，不读库里的快照（避免库包漂移）；
    - ``loaded=False`` 的是被卸载过的，直接跳过（与 agent 卸载同一套语义）。

    可执行文件组件沿用内置工具的权限规则：``mutating`` 且工作区只读 → 不装配；
    ``requires_workspace`` 却没有工作区 → 不装配。

    单个插件出问题只跳过它并告警：插件是附加能力，缺哪个就少哪个。
    """
    from ..tools.subprocess_tool import (
        DEFAULT_TIMEOUT,
        SubprocessToolError,
        make_subprocess_tool,
    )

    overrides = await _capability_overrides(agent.id)
    # 工作空间：可执行文件组件的权限判断与上下文注入都要用。
    # 只读取**工作空间自己的**标志（user_space.read_only），不再读 agent 画像上
    # 那个同名字段——两者曾经各判各的，详见 _resolve_workspace 的注释。
    if root is None or read_only is None:
        root, read_only = await _resolve_workspace(agent)
    # agent 私有产物根：常驻子进程（MCP / bin）的索引、缓存等落这里（按 agent 隔离）。
    # 构建期已知，提前 mkdir，避免子进程启动后才发现目录不存在。
    agent_space = default_agent_space_base(agent.id)
    agent_space.mkdir(parents=True, exist_ok=True)
    out = PluginComponents()
    # 绑定 = ~/.keeper/agents/<id>/plugins/<name> 这个符号链接。列目录就是读绑定，
    # 链接有效就等于"插件还在"——不需要查任何表。
    bound = sorted(agent_plugins_dir(agent.id).iterdir()) if agent_plugins_dir(
        agent.id
    ).is_dir() else []
    logger.info(
        "开始装配插件 agent=%s | 绑定目录=%s | 目录内容=%s | 工作区=%s(read_only=%s)",
        agent.id,
        agent_plugins_dir(agent.id),
        [p.name + ("/链接" if p.is_symlink() else "/实体") for p in bound] or "（空）",
        root, read_only,
    )
    for name in linked_plugins(agent.id):
        if not _enabled(overrides, "plugin", name, default=True):
            logger.info("插件 %s 已被本地开关关闭，跳过装配", name)
            out.add_diag(name, "plugin", name, False, "本地开关已关闭（agent_capability_overrides）")
            continue
        # 兼容「打包时多套一层目录」：清单不在包根就往下探唯一的那层
        dest = _resolve_plugin_root(agent_plugins_dir(agent.id) / name)
        if not (dest / PLUGIN_MANIFEST).is_file():
            logger.warning(
                "插件 %s 的包根没有 %s（包根=%s）", name, PLUGIN_MANIFEST, dest
            )
            out.add_diag(
                name, "plugin", name, False,
                f"包根缺少 {PLUGIN_MANIFEST}（{dest}）",
            )
            continue
        logger.info("装配插件 %s（包根=%s）", name, dest)
        _build_plugin_components(
            name=name,
            dest=dest,
            settings={},
            out=out,
            overrides=overrides,
            root=root,
            read_only=read_only,
            agent_space=agent_space,
        )

    out.print_summary(agent.id, read_only, root)
    return out


def _build_config(
    row: Agent,
    mcp_servers: Dict[str, Any],
    skill_registry: SkillRegistry,
    tools: List[Any],
    llm: Optional[Dict[str, Any]] = None,
    group_summaries: Optional[Dict[str, str]] = None,
    workspace_read_only: bool = True,
) -> AgentConfig:
    """把一行画像 + 已聚合的连接器 / 技能 / 工具，组装成完整的 ``AgentConfig``。

    skill_registry / tools 也是 DB 解析出的能力对象，一并作为 ``AgentConfig`` 的
    字段——这样 ``KeeperAgent`` 只认一份 ``AgentConfig``，不再有额外的注入参数。

    ``workspace_read_only`` 必须由调用方用 :func:`_resolve_workspace` 从
    ``user_space`` 记录解析后传入（只读的真源在那里，agent 表上已不存这一列）。
    缺省 True 是「没有工作区上下文时按最保守处理」，不是给调用方偷懒的默认值。
    """
    return AgentConfig(
        name=row.name,
        persona=row.persona or "",
        description=row.description or "",
        workspace=WorkspaceConfig(
            kind=row.workspace_kind or "none",
            path=row.workspace_path,
            read_only=bool(workspace_read_only),
            agent_name=row.name,
        ),
        llm=llm if llm is not None else (_json(row.llm) or {}),
        mcp_servers=mcp_servers or {},
        skill_registry=skill_registry,
        tools=tools,
        group_summaries=group_summaries or {},
    )


async def _resolve_llm(row: Agent) -> Dict[str, Any]:
    """解析本 agent 实际使用的 LLM 配置。

    优先级：agent 内联 llm（row.llm，兼容现有用法）> 绑定的模型预设
    （agent_llm_binding → llm_profiles）> 否则返回内联（可能为空，保持现状）。
    全局 config.yaml 的 llm 段不在本处处理，避免改变既有行为。
    """
    inline = _json(row.llm) or {}
    if inline.get("provider"):
        return inline
    factory = get_session_factory()
    async with factory() as session:
        binding = await session.get(AgentLLMBinding, row.id)
        if binding and binding.llm_profile_id:
            profile = await session.get(LLMProfile, binding.llm_profile_id)
            if profile and profile.provider:
                return profile.to_llm_dict()
    return inline


async def load_agent_config(
    *, name: Optional[str] = None, agent_id: Optional[str] = None
) -> AgentConfig:
    """从数据库读出 agent 画像 + 它的 skill / mcp / tool 绑定，解析成一份完整的
    ``AgentConfig``（含已装载的 ``skill_registry`` / ``tools`` 能力对象）。

    这一步**只**负责「把数据库数据变成配置」——所有 DB 逻辑都在本模块完成，
    返回的是**已经解析好的** ``AgentConfig``；构造实例与 build 是 ``KeeperAgent``
    的事（见 ``keeper.agent.keeper`` 的 ``build_agent``）。

    Raises:
        AgentNotFoundError: 数据库里没有这条画像，或它已停用 / 废弃。
        ValueError: name 与 id 都没给。
    """
    row = await _load_definition(name=name, agent_id=agent_id)
    # 工作空间与它的只读标志**只解析一次**，装配各组件与构造 AgentConfig 共用同一个
    # 结论。否则三处各查一次，既多余又可能查到不同的值。
    ws_root, ws_read_only = await _resolve_workspace(row)
    # 第 3 步：确保插件链接可用（缺失/悬空的自愈在 _load_plugins 里做）
    await _ensure_plugin_links(row)
    # 能力现在只有两个来源：keeper 自带的内置工具 + 平台下发的插件
    plugins = await _load_plugins(row, root=ws_root, read_only=ws_read_only)
    tools = await _load_builtin_tools(row, root=ws_root, read_only=ws_read_only)
    tools.extend(plugins.tools)          # 插件的可执行文件（bin/）组件
    llm_dict = await _resolve_llm(row)
    return _build_config(
        row,
        plugins.mcp_servers,             # 插件的 MCP 组件
        _build_skill_registry(plugins.skill_items),   # 插件的 skills/
        tools,
        group_summaries=plugins.group_summaries,
        llm=llm_dict,
        workspace_read_only=ws_read_only,
    )
