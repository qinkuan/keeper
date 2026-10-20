"""Agent 装配：从配置到一个可运行的 agent 实例。

本文件只负责两件事：

1. **装配**：把配置里的 mcp / skill / workspace 拼成一个可执行体（Process）；
2. **生命周期与构建**：资源的连接（MCP）与释放，以及 ``build_agent`` 从配置构建实例。

「一轮对话怎么跑、步骤怎么落库 / 恢复」在 ``executor.py``——那是执行侧。

命名约定：内部加载步骤统一 ``_load_`` 前缀（``_load_skills`` / ``_load_mcp``），
对外只暴露一个 ``build()``——调用方不必知道内部有几步、什么顺序。

这里不知道代码仓库、不认识任何具体 MCP server：所有能力都按名从配置动态装载。
任何业务语义都应落在 tool / skill / mcp server 里，不要回到这里。

配置的**来源**（从数据库读 agent 画像 + 绑定、解析成完整的 ``AgentConfig``）在
``keeper.agent.config`` 的 ``load_agent_config`` 里；本文件的 ``build_agent`` 负责
编排三步：拿到 config → 构造实例 → 调 ``build()``——即「把 AgentConfig 交给 keeper，
然后开始 build」。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import AgentConfig, load_agent_config
from ..llm import get_llm
from ..mcp import MCPSession, MCPError
from ..mcp.launcher import MCPManager

logger = logging.getLogger(__name__)

# ---- 就绪性 ----
# 每个请求进来先过这一层：就绪 → 正常跑；不就绪 → 立即回「稍后重试」，
# 重连丢到后台异步做，不占用户等待时间。
# 只有「跑不完一轮 ReAct」的东西才算必需（目前只有 LLM）；MCP 这类可选能力
# 即便没连上也照常回答——只是这一轮用不到那些工具而已。

# 建议用户的重试间隔（秒）：重连在后台跑，用户稍后再来即可
RETRY_AFTER = 30

# 缺失项的标识（同时用于 message 展示与 _recover 判断）
MISSING_LLM = "LLM"


@dataclass
class Readiness:
    """就绪检查结果。"""

    ok: bool
    missing: List[str] = field(default_factory=list)
    retry_after: int = RETRY_AFTER

    def message(self) -> str:
        return (
            f"（agent 尚未就绪，缺少：{'、'.join(self.missing)}；"
            f"已开始后台重连，请约 {self.retry_after} 秒后重试）"
        )


class KeeperAgent:
    """agent 实例：装配产物 + 生命周期。

    四类能力来源彼此独立、全部可配：
    - ``mcp_servers``：mcp.json 声明的标准 MCP server，其工具运行时动态装载；
    - ``skills``      ：声明式技能，注入 system prompt；
    - ``workspace``   ：可选的一块本地目录（kind=none 时不碰文件系统）；
    - ``peers``       ：经 A2A 接入的对端 agent，其 AgentCard.skills 变成工具。
    """

    def __init__(
        self,
        *,
        config: AgentConfig,
        agent_id: Optional[str] = None,
    ):
        """构造 agent 实例：只做「取值派生」，不建立任何外部连接。

        ``config`` 是唯一真相源——workspace / mcp_servers / llm / skills / peers /
        persona / skill_registry / tools 全部由它派生（后两者是 config 里已装载好的
        能力对象）。连接 MCP 是 ``build()`` 的事，可执行体是 ``prepare()`` 懒装配的
        事——构造完还不能直接对话。

        ``agent_id`` 是数据库里 agent 的主键，用于在单进程多 agent 场景下按 URL
        路由到本实例；纯配置直接运行时为 None。
        """
        # config 是唯一真相源；下文所有能力字段都从它派生。
        self.config = config
        self.name = config.name
        self.agent_id = agent_id

        # 人设：始终生效的身份基调（区别于可随时增减的 skill）。
        # config 已保证 persona 为字符串（见 AgentConfig.__post_init__）。
        self.persona = config.persona
        # workspace 根目录一次性算出存好；本类不做 IO，具体用法由 tool / skill 决定。
        self.workspace_root = config.workspace.root()


        ##############################装配属性#######################
        # llm：config.llm 恒为 dict；无 provider 时 get_llm 返回 None（纯索引模式）
        self.llm : Any = None
        self.mcp: Optional[MCPSession] = None
        # skill 注册表直接用 config.skill_registry（见 _load_skills），不另存字段；
        # skills 属性标注为 List[Any]，避免下游把元素推断成 Optional；实际内容由 build() 装载
        self.skills: List[Any] = []
        # 运行时对端层：{name: {a2a_url, headers}}。**优先于** config.peers 被
        # 工具收集读取（见 tools.collect.peer_tools_from_config），因此运行时
        # 增删对端只改这里 + 热刷新工具表即可，不动 config（那是平台同步的静态真相源）。
        # 启动时由 load_managed_peers() 把持久化的 agent_peers 叠加进来。
        self.peers: Dict[str, Any] = dict(config.peers or {})




        # 可执行体：按运行模式各建一份、缓存复用（normal→ReactProcess /
        # simple→SimpleProcess / ptc→PtcProcess）。工具表归各 Process 自己持有。
        self._processes: dict = {}
        # 后台重连的占位标记：同一时刻只跑一个重连任务，避免每个请求都起一个
        self._recovering = False
        self._recovering_peers = False


    # ---- 生命周期：加载与释放 ----
    async def build(self) -> None:
        """构建全部能力：先装载 skill，再连 MCP（对外唯一的构建入口）。

        调用方不需要知道内部有几步、什么顺序，将来要加第三种能力（比如自定义
        tool）也只改这里。MCP 连不上不会抛错：进降级模式，由执行侧的后台重连兜底补连。
        """
        # 工作空间总是要有：文件 / git 类工具都依赖这个根目录（不存在就建出来）
        ws_root = self.workspace_root
        if ws_root:
            ws_root.mkdir(parents=True, exist_ok=True)

        self.llm = get_llm(self.config.llm)
        self.skills = self._load_skills()
        self.mcp = await self._load_mcp()

    async def load_managed_peers(self) -> None:
        """从持久化表读出本 agent 的对端，叠加进运行时 ``self.peers``。

        启动时由 ``build_agent`` 调用，让「运行时添加过的 peer」重启后仍生效。
        ``config.peers``（平台同步来的静态真相）保持不变，本表只承载运行时增量。
        """
        import json

        from sqlalchemy import select

        from ..store import AgentPeer, get_session_factory

        if self.agent_id is None:
            return
        factory = get_session_factory()
        async with factory() as db:
            rows = (
                await db.execute(
                    select(AgentPeer).where(
                        AgentPeer.agent_id == self.agent_id,
                        AgentPeer.enabled.is_(True),
                    )
                )
            ).scalars().all()
        names: List[str] = []
        for r in rows:
            try:
                headers = json.loads(r.headers) if r.headers else {}
            except Exception:  # noqa: BLE001
                headers = {}
            self.peers[r.name] = {
                "a2a_url": r.a2a_url,
                "headers": headers,
                "source": r.source,
                "trust_env": r.trust_env,
            }
            names.append(r.name)
        if names:
            logger.info("已加载持久化的对端(%d): %s", len(names), ", ".join(names))

    def _load_skills(self) -> List[Any]:
        """装载 skill：直接把注册表里的全部 skill 加载进来。

        注册表的数据源在 ``skill`` 模块（默认目录 / 数据库 / 接口都行），
        这里只决定「本实例装上哪些」，不碰数据源。注册表已是本 agent 按
        binding 装配好的范围，故直接 ``all()`` 全量加载，不再区分「注册了哪些 /
        选用了哪些」。

        幂等：需要热更新 skill 时重新调用即可。
        """
        skills = self.config.skill_registry.all()
        if skills:
            logger.info("已启用 skills: %s", ", ".join(s.name for s in skills))
        else:
            logger.info("未启用任何 skill（注册表里没有可用的技能）")
        return skills

    async def _load_mcp(self) -> Optional[MCPSession]:
        """按 mcp.json 标准注册表连接 MCP。

        连接成功只是把工具「连上」，真正登记给 agent 是在 ``prepare()`` 里——
        两者分开的好处：连不上可以先跑无工具的对话，连上了下一轮自动可用。

        任一 server 失败只会被跳过（降级），不拖垮其它 server；连不上时
        停掉旧会话、返回 None 进入降级模式，由执行侧的后台重连兜底补连。
        """
        mcp: Optional[MCPSession] = None
        try:
            if not self.config.mcp_servers:
                raise MCPError("mcp.json 中没有任何 server 配置")
            manager = MCPManager(self.config.mcp_servers)
            conn = manager.connection_config()
            if not conn:
                raise MCPError("mcp.json 中没有可连接的 server 配置")
            session = MCPSession(servers=conn)
            await session.start()
            tools = await session.health()  # 健康检查（list_tools）
            mcp = session
            logger.info(
                "MCP 已连接: server=%s, 已加载 MCP 工具(%d): %s",
                list(conn), len(tools), tools,
            )
        except MCPError as e:
            logger.warning("MCP 暂不可用，进入降级模式并后台重试: %s", e)
            if self.mcp is not None:
                await self.mcp.stop()
        return mcp

    async def shutdown(self) -> None:
        """释放 MCP 会话等资源，实例随之不可用。

        由服务的退出流程调用（见 ``main.serve`` 的 finally）。重复调用安全。
        """
        if self.mcp is not None:
            await self.mcp.stop()
            self.mcp = None

    # ---- 就绪性：检查与后台重连 ----
    def check_readiness(self) -> Readiness:
        """检查运行所必需的依赖是否都就绪。

        MCP 不参与判定：配了却未连上时本轮照跑（工具少几个而已），
        重连由 ``recover_peers_in_background`` 在后台做，不挡请求。
        """
        missing: List[str] = []
        if self.llm is None:
            missing.append(MISSING_LLM)
        # 注意是 config.mcp_servers：实例上没有这个属性，写错会在"没配 MCP 的 agent"上抛 AttributeError
        if self.mcp is None and self.config.mcp_servers:
            logger.info("MCP 尚未连接，本轮按无 MCP 工具执行")
        return Readiness(ok=not missing, missing=missing)

    def recover_in_background(self, missing: List[str]) -> None:
        """后台按缺失项重连；已有重连在跑则跳过，避免每个请求都起一个任务。"""
        if self._recovering:
            return
        self._recovering = True

        async def _run() -> None:
            try:
                await self._recover(missing)
            finally:
                self._recovering = False

        def _on_done(t: "asyncio.Task") -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.warning("后台重连异常: %s", exc)

        task = asyncio.create_task(_run())
        task.add_done_callback(_on_done)

    def recover_peers_in_background(self) -> None:
        """后台重连还没连上的 MCP 对端。

        与 ``recover_in_background`` 的区别：对端连不上**不影响本次请求**——
        这一轮可能根本用不到那个对端。所以这里不阻塞、也不把请求挡回去，
        只是顺手重试；连上了，下次提问 agent 就能用它的工具。
        """
        mcp = self.mcp
        if mcp is None or not mcp.missing_names():
            return
        if self._recovering_peers:
            return
        self._recovering_peers = True

        async def _run() -> None:
            try:
                await mcp.reconnect_missing()
            except Exception as e:
                logger.warning("MCP 对端重连异常: %s", e)
            finally:
                self._recovering_peers = False

        def _on_done(t: "asyncio.Task") -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.warning("MCP 对端重连任务异常: %s", exc)

        task = asyncio.create_task(_run())
        task.add_done_callback(_on_done)

    async def _recover(self, missing: List[str]) -> None:
        """按缺失项逐个重连。"""
        if MISSING_LLM in missing:
            try:
                if self.config.llm:
                    self.llm = get_llm(self.config.llm)
                    logger.info(
                        "LLM 重连%s", "成功" if self.llm is not None else "失败"
                    )
            except Exception as e:
                logger.warning("LLM 重连失败: %s", e)

    # ---- 装配：能力来源 -> 可执行体 ----
    async def prepare(self, mode: str = "normal") -> Any:
        """装配当轮能力集，返回该**运行模式**对应的可执行体。

        不同模式是不同的 Process 类（普通 ReactProcess / 快捷 SimpleProcess /
        程序化 PtcProcess），按 mode 各建一份、缓存复用——与原来单例同理：工具表
        整体替换，不重建实例。mode 缺省 ``normal``，行为与改造前完全一致。

        用「全量重建 + 整体替换」而不是增量追加：配置里拿掉的 MCP server / 对端，
        它的工具必须随之消失——否则页面上取消勾选了，LLM 还能继续调它。

        幂等：一轮开始前反复调用都安全。执行侧（``executor``）开跑前调它。
        """
        tools = await self._collect_tools()
        # 组摘要跟着工具表一起重建：折叠后的分组行靠它告诉模型「这一组是干嘛的」
        for group, summary in (self.config.group_summaries or {}).items():
            tools.set_group_summary(group, summary)
        from .process import ReactProcess
        from .simple_process import SimpleProcess

        cls = {"normal": ReactProcess, "simple": SimpleProcess}.get(mode, ReactProcess)
        p = self._processes.get(mode)
        if p is None:
            # Process 自己知道该从 agent 上取什么（llm / persona / skill 注入）
            p = self._processes[mode] = cls.from_agent(self, tools)
        elif hasattr(p, "replace_tools"):
            # 工具表归 Process 所有：就地整体替换即可，不必重建实例。
            # 快捷模式不持有工具（无 replace_tools），跳过即可。
            p.replace_tools(tools)
        # 普通模式挂子 agent（Delegatee）：TASK 动作触达，独立于工具注册表。
        # 仅 normal——simple 不调工具；ptc 模式本身即代码循环，无需再委托。
        # 每个子 agent 按 delegatee_configs 表造「专属 LLM（独立模型）+ 工具子集」，
        # 实现「子 agent 作为独立子 agent」；无配置行 / 被禁用则跳过或回退主 agent llm。
        if mode == "normal":
            from .delegatee import DelegateeRegistry
            from .delegatee_types import (
                DELEGATEE_TYPES,
                build_delegatee,
            )
            from keeper.store import get_session_factory
            from keeper.store.models import DelegateeConfig, LLMProfile
            from sqlalchemy import select
            from keeper.llm.client import get_llm

            reg = DelegateeRegistry()
            async with get_session_factory()() as db:
                cfgs = (await db.execute(select(DelegateeConfig))).scalars().all()
                cfg_map = {c.delegatee: c for c in cfgs}
                profile_ids = [c.llm_profile_id for c in cfgs if c.llm_profile_id]
                profiles = (
                    (await db.execute(
                        select(LLMProfile).where(LLMProfile.id.in_(profile_ids))
                    )).scalars().all()
                    if profile_ids else []
                )
                prof_map = {p.id: p for p in profiles}

            for name, cls in DELEGATEE_TYPES.items():
                cfg = cfg_map.get(name)
                if cfg is not None and not cfg.enabled:
                    continue
                llm = self.llm
                if cfg is not None and cfg.llm_profile_id:
                    prof = prof_map.get(cfg.llm_profile_id)
                    if prof is not None:
                        # get_llm 造出的 LangChainLLM 自带用量记账，子 agent 与主 agent
                        # 共用同一套成本/费用统计（按 profile_id 归属）。构造失败则回退
                        # 主 agent 的 llm，避免一个坏预设拖垮整轮。
                        try:
                            built = get_llm(prof.to_llm_dict())
                        except Exception:  # noqa: BLE001
                            built = None
                        if built is not None:
                            llm = built
                dtools = None
                if cfg is not None and (cfg.plugins or cfg.tools):
                    from .delegatee_types import _parse_list, filter_tool_registry

                    dtools = filter_tool_registry(
                        tools, _parse_list(cfg.tools), _parse_list(cfg.plugins)
                    )
                inst = build_delegatee(name, llm=llm, tools=dtools)
                if inst is not None:
                    reg.register(inst)
            p.delegatees = reg
        return p

    async def _collect_tools(self) -> Any:
        """按**当前**配置收集全部可用工具，返回一张全新的工具表。

        三个来源：本地 / 自定义工具、已连上的 MCP、config.peers 里的 A2A 对端。
        任一来源出问题只影响它自己（降级），不拖垮整张表。
        """
        from ..tools.collect import (
            keeper_tools_from_agent,
            mcp_tools_from_session,
            peer_tools_from_config,
        )

        reg = keeper_tools_from_agent(self)          # 本地 / 自定义
        for t in (self.config.tools or []):           # 装配进来的内置 / 外部工具
            reg.register(t)
        if self.mcp is not None:
            # 插件清单里显式声明 read_only 的 MCP 工具才允许参与并发（MCP 协议
            # 本身不声明副作用，所以默认不可并发）。
            declared = set()
            for srv_cfg in (self.config.mcp_servers or {}).values():
                for tname, tmeta in ((srv_cfg or {}).get("tools") or {}).items():
                    if isinstance(tmeta, dict) and tmeta.get("read_only"):
                        declared.add(tname)
            for t in mcp_tools_from_session(self.mcp, declared).all():
                reg.register(t)
        for t in (await peer_tools_from_config(self)).all():
            reg.register(t)
        return reg

    async def refresh_tools(self) -> None:
        """热刷新工具表（增删对端后调用）：只重新收集 + 整体替换。

        与 ``build()`` 的区别：``build()`` 会重建 llm 并**重连 MCP**（重且有副作用:
        新建 session、可能停掉旧的），这里只按当前配置重收工具（含新加入的
        peer 工具）后替换 ``ReactProcess`` 持有的工具表，不动 llm / MCP 连接。
        """
        tools = await self._collect_tools()
        for p in self._processes.values():
            if hasattr(p, "replace_tools"):
                p.replace_tools(tools)


# ---- 当前进程内已装载的 agent 实例：agent_id -> KeeperAgent（供 HTTP 接口按 id 取用）----
_KEEPERS: dict[str, "KeeperAgent"] = {}


def register_keeper(agent_id: str, agent: "KeeperAgent") -> None:
    """注册一个已装载的 agent 实例（按 agent_id 索引，单进程多 agent）。"""
    global _KEEPERS
    _KEEPERS[agent_id] = agent


def unregister_keeper(agent_id: str) -> None:
    """移除一个已装载的 agent 实例（热卸载 / 重载时用）。"""
    _KEEPERS.pop(agent_id, None)


def get_keeper(agent_id: Optional[str] = None) -> Optional["KeeperAgent"]:
    """按 agent_id 取 agent 实例；未指定或不存在返回 None。

    单进程装载多个 agent 后，HTTP 接口据此按 URL 里的 agent_id 路由到对应实例。
    """
    if agent_id is None:
        return None
    return _KEEPERS.get(agent_id)


# ---- 核心 agent：独立槽位 ----
#
# 它**不进 _KEEPERS**：那本按 agent_id 索引，而核心 agent 没有 id（它不是数据库里的
# 任何一行）。塞进去要么编一个假 id（于是它能被 unload、能被 A2A 路由到，恰好是
# 我们要避免的），要么把 dict 的键类型改成 Optional（会让所有调用点的类型标注失真）。
# 单独一个槽位最直白，也把「它没有 id、不对外暴露」这件事写在了代码结构里。
_CORE_AGENT: Optional["KeeperAgent"] = None


def get_core_keeper() -> Optional["KeeperAgent"]:
    """核心 agent 实例；未装配（比如目录里还没有定义文件）时返回 None。"""
    return _CORE_AGENT


def resolve_for_chat(agent_id: Optional[str] = None) -> Optional["KeeperAgent"]:
    """对话侧解析 agent 实例。

    - ``agent_id`` 给了 → 就用它。取不到就是取不到，**不回退**到主脑：
      用户点名要跟某个 agent 说话，悄悄换成另一个是错的（而且不可排查）。
    - ``agent_id`` 为空 → 用核心 agent。这正是"会话是一等公民"这条设计的落点：
      不指定 agent 就是默认值，而默认那个是主脑——不是"没有 agent"。

    ``get_keeper`` 保持"纯按 id 查"的语义不变，因为 A2A 路由（``api.py``）依赖它：
    那里按 URL 里的 agent_id 找实例，找不到就该404，不能悄悄换人。
    """
    if agent_id:
        hit = _KEEPERS.get(agent_id)
        if hit is not None:
            return hit
        # 也可能是核心 agent 的 id：它**不在 _KEEPERS 里**（那本放的是数据库
        # agent），所以这里要单独认一次。少了这一步，点「开始对话」建出来的
        # 会话（agent_id 是核心 agent 的 id）会解析不到任何实例——表现为
        # "进了对话页但发消息没反应"，而日志里只有一句 agent 为 None。
        core = get_core_keeper()
        if core is not None and core.agent_id == agent_id:
            return core
        return None
    return resolve_core_agent()


def set_core_keeper(agent: Optional["KeeperAgent"]) -> None:
    """登记 / 清空核心 agent 实例（装配与热重载用）。"""
    global _CORE_AGENT
    _CORE_AGENT = agent


async def reload_core_agent() -> Optional["KeeperAgent"]:
    """重新装配核心 agent（改完 agent.yaml / soul.md 后调用）。

    **先建新的、成功后才停旧的**：装配失败时旧实例原样保留，主脑不会整体消失。
    反过来做（先停后建）的话，一次 soul.md 写错就会让核心 agent 彻底不可用，
    而用户看到的只是"对话框没反应了"。

    ``build_core_agent`` 内部会 ``set_core_keeper(new)``，所以这里只需要在成功后
    把旧实例关掉——它占着的 MCP 连接、HTTP 客户端不主动放就会漏。
    """
    old = get_core_keeper()
    new = await build_core_agent()
    if new is None:
        # 没建成（目录没定义 / 读不出内容）：槽位没被动过，旧的还在
        logger.warning("核心 agent 重建失败，保留现有实例")
        return old
    if old is not None and old is not new:
        try:
            await old.shutdown()
        except Exception as e:  # noqa: BLE001 关闭失败不影响新实例已生效
            logger.warning("旧的核心 agent 关闭异常（新的已生效）: %s", e)
    return new


def list_keepers() -> list["KeeperAgent"]:
    """列出所有已装载的 agent（供 landing 页 / 管理接口枚举）。"""
    return list(_KEEPERS.values())


def resolve_core_agent() -> Optional["KeeperAgent"]:
    """选出**核心 agent**（会话的默认主脑）。

    解析顺序（逐级回落，保证"配了就能用、不配也能用"）：

    1. **本地目录装配的核心 agent**（``~/.keeper/core-agent/``）——首选，也是正常情况。
       它是应用内生的，不依赖数据库、不依赖平台，也不可能被卸载掉。
    2. **第一个已装载的 agent** —— 只有目录定义缺失/装配失败时才用到这里，
       走按 id 排序保证结果稳定（dict 顺序取决于装载完成先后，多进程/重启后
       可能变，所以不能直接用 ``list(_KEEPERS)[0]``，否则"哪个是主脑"会随机跳动）。

    没有"配置里指定某个 agent 当主脑"这一层：核心 agent 就是那个目录构建出来的
    唯一一个实例，不存在"选谁"的问题。加一层选择只会让用户以为核心 agent 可以
    在数据库里的 agent 之间挑，而那其实是普通 agent 的编排问题。

    返回 None = 既没有目录定义、也没装载任何 agent。调用方据此给出可读的提示，
    而不是 500。
    """
    # 1) 本地目录装配的核心 agent
    if _CORE_AGENT is not None:
        return _CORE_AGENT

    # 2) 兜底：第一个已装载的 agent
    if not _KEEPERS:
        logger.warning("没有核心 agent 目录定义，也没有已装载的 agent")
        return None
    fallback = _KEEPERS[sorted(_KEEPERS)[0]]
    logger.warning(
        "核心 agent 不可用（目录缺失或装配失败），暂时用已装载的 %s 顶上",
        fallback.name,
    )
    return fallback


def resolve_core_agent_id() -> Optional[str]:
    """核心 agent 的 id（前端「新建会话」调用时用它，省得前端自己判断用谁）。"""
    agent = resolve_core_agent()
    return agent.agent_id if agent is not None else None


async def build_all_agents() -> None:
    """启动期装载：把「active + 已装载 + 本地未停用」的 agent 逐个 build 并注册。

    三个条件各自管一件事，别混：

    - ``status == 'active'``：**配置**状态。平台 agent 归平台管（同步会写它），
      本地 agent 由创建表单定。
    - ``loaded IS NOT False``：**装载**状态，即"已经从远程拿到本地了"。平台 agent
      卸载后是 False；本地 agent 天然就是 True（它没有远程可拿）。
    - 没有被本地停用：**运行时**意图（``AgentCapabilityOverride(kind='agent')``）。
      刻意和 status 分开——它是"我现在不想跑它"，会被下一次平台同步冲掉，
      所以不能写进 status。

    只有三条件都满足的 agent 才会出现在概览里。

    单个 agent 装配失败只降级跳过（继续装载其余），避免一个坏配置拖垮整服务。

    核心 agent 走独立路径（``build_core_agent``），**先于**数据库里的 agent 装配：
    它是主脑，失败了也不该影响其它 agent 的装载。
    """
    from sqlalchemy import select

    from ..store import Agent as AgentRow
    from ..store import AgentCapabilityOverride, get_session_factory

    # 先铺目录：首次启动时生成一份带注释的定义与灵魂模板。
    # 放在这里而不是模块导入时，是为了让"没写过配置"这件事在日志里可见，
    # 且不拖慢 import。
    try:
        from ..config import load_config
        from . import core as core_def

        core_dir = core_def.ensure_core_dir(load_config())
        logger.info("核心 agent 目录: %s", core_dir)
    except Exception as e:  # noqa: BLE001 目录建不出来不该挡住其它 agent 装载
        logger.warning("准备核心 agent 目录失败: %s", e)

    # 核心 agent 装配失败（模型名写错、soul.md 编码坏了……）只降级：主脑缺席时
    # resolve_core_agent() 会回落到数据库里的 agent，应用仍可用。
    try:
        await build_core_agent()
    except Exception as e:  # noqa: BLE001
        logger.warning("核心 agent 装配失败，将回落到数据库中的 agent: %s", e)

    factory = get_session_factory()
    async with factory() as db:
        rows = (
            await db.execute(
                select(AgentRow).where(
                    # 用「非显式 False」而非「等于 True」：存量行即便 loaded 为
                    # NULL 也照旧装载，绝不会因为加了这一列就让 agent 消失。
                    AgentRow.loaded.is_not(False),
                )
            )
        ).scalars().all()
        # 本地停用清单：ref_name 存的就是 agent_id
        off = set(
            (
                await db.execute(
                    select(AgentCapabilityOverride.ref_name).where(
                        AgentCapabilityOverride.kind == "agent",
                        AgentCapabilityOverride.enabled.is_(False),
                    )
                )
            ).scalars().all()
        )
    skipped = 0
    for row in rows:
        if row.id in off:
            skipped += 1
            continue
        try:
            agent = await build_agent(agent_id=row.id)
            register_keeper(row.id, agent)
            logger.info("已装载 agent: id=%s name=%s", row.id, row.name)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "agent 装载失败，已跳过: id=%s name=%s (%s)", row.id, row.name, e
            )
    if skipped:
        logger.info("有 %s 个 agent 被本地停用，未装载", skipped)


async def shutdown_all() -> None:
    """释放所有 agent 资源（服务退出时调用）。"""
    # 核心 agent 先关：它是主脑，可能被其它 agent 的 peer 调用依赖
    global _CORE_AGENT
    if _CORE_AGENT is not None:
        try:
            await _CORE_AGENT.shutdown()
        except Exception as e:  # noqa: BLE001
            logger.warning("核心 agent shutdown 异常: %s", e)
        _CORE_AGENT = None

    for agent in list(_KEEPERS.values()):
        try:
            await agent.shutdown()
        except Exception as e:  # noqa: BLE001
            logger.warning("agent shutdown 异常: %s", e)
    _KEEPERS.clear()


async def build_agent(
    *, name: Optional[str] = None, agent_id: Optional[str] = None
) -> KeeperAgent:
    """从数据库装配并构建 agent：拿到 config → 构造实例 → build。

    配置的**来源**（读 agent 画像 + 绑定、解析成完整的 ``AgentConfig``）在
    ``keeper.agent.config`` 的 ``load_agent_config`` 里；本函数只负责编排三步：

    1. ``load_agent_config`` 完成全部 DB 逻辑，返回解析好的 ``AgentConfig``；
    2. 用该 config 构造 ``KeeperAgent`` 实例（注入）；
    3. 调 ``agent.build()`` 完成 build（连 MCP / 装载 skill）。

    返回的实例即可直接对话。

    Args:
        name / agent_id: 定位数据库里的 agent 画像（二选一）。
    """
    config = await load_agent_config(name=name, agent_id=agent_id)
    agent = KeeperAgent(config=config, agent_id=agent_id)
    await agent.build()
    # 叠加运行时持久化过的对端（agent_peers 表），使重启后它们仍然生效
    await agent.load_managed_peers()
    return agent


class _CoreAgentRef:
    """给 ``_load_plugins`` 用的轻量代理：核心 agent 不在 ``agents`` 表里，但插件
    装配只读 ``id`` / ``name`` 两个字段，用这个替身即可，不必造一条假 DB 行。"""

    def __init__(self, id: str, name: str):
        self.id = id
        self.name = name


async def build_core_agent() -> Optional[KeeperAgent]:
    """从本地目录装配核心 agent，并登记到核心槽位。

    与 ``build_agent`` 的三步编排一致（构造 config → 建实例 → build），差别只在
    **配置的来源**：这里读 ``~/.keeper/core-agent/agent.yaml`` + ``soul.md``，
    不查数据库。所以它可以在"一个 agent 都没装载""平台连不上"的情况下照样成功——
    这正是把核心 agent 独立出来的目的。

    返回 None 表示"没配核心 agent"（目录里没有定义文件），此时不清空已有槽位：
    调用方通常是启动期，此时本来也没有。

    关于能力：核心 agent **只带内置工具**，不扫插件、不读 agent_binding。
    插件与对端是"往某个会话里加入"的东西，不属于它的出厂配置——
    这也让定义文件保持足够小，不引入第二套能力模型。
    """
    from ..config import load_config
    from . import core as core_def
    from .config import (
        AgentConfig,
        WorkspaceConfig,
        _build_skill_registry,
        _load_plugins,
        load_builtin_tools,
    )

    cfg = load_config()

    if not core_def.exists(cfg):
        logger.info("未发现核心 agent 定义（%s），跳过装配", core_def.def_path(cfg))
        return None

    d = core_def.load_core_def(cfg)
    if d is None:  # exists() 为真但读取失败：按"没配"处理，不让启动挂掉
        return None

    from ..config import core_agent_id

    core_id = core_agent_id(cfg)



    # 模型走**标准的 agent 绑定**（agent_llm_binding），和普通 agent 同一套：
    # 核心 agent 现在也有 agent_id 了，所以能在这张表里占一行。这样"换模型"这件
    # 事对所有 agent 都是同一个动作、同一份存储，不会出现两种模型来源。
    #
    # 刻意**不**回落到 config.yaml 的全局 llm 段：那种看不见的兜底正是 _resolve_llm
    # 注释里记的历史教训——"改了配置却不生效、又查不出原因"。没绑就是没绑，
    # 由界面明确提示用户去选一个。
    llm: Dict[str, Any] = await _resolve_llm_by_binding(core_id)
    if not llm:
        logger.warning(
            "核心 agent 未绑定模型预设，将以纯索引模式运行（不调用模型，只做本地检索）。"
            "在对话输入框下方选择模型即可绑定。"
        )

    # 工作区：**不钉路径**。会话各自绑定 user_space（见 chat/service.py 的
    # _resolve_workspace），那才是这个角色当下工作的地方。这里给 kind="none"，
    # 让会话的绑定生效——一个陪伴的角色不该被锁死在某个固定目录里。
    # read_only=False：陪用户做事的角色需要能写文件（其它 agent 默认只读）。
    workspace = WorkspaceConfig(
        kind="none",
        path=None,
        read_only=False,
        agent_name=d.name,
    )

    # 画像 = soul.md 全文，**唯一来源**。刻意不再叠加 settings.yaml 里的
    # persona / persona_file 覆盖层：两个入口用户就得猜哪个生效，而"改了却不生效"
    # 是最难排查的一类问题。人格只有一个地方说了算，就是 soul.md。
    persona = d.persona

    # 内置工具：核心 agent 的**出厂能力**（fs.* 与 task.mark_item_done），也是它
    # 区别于「只会检索」的唯一来源。
    #
    # 这里以前是漏的：核心 agent 不走 ``load_agent_config``（它从本地目录构建、
    # 不查 agent 表），而内置工具正是在那条路径上装配的——于是 ``config.tools``
    # 恒为空，界面上「生效工具」只剩记忆检索的 recall / read，读写文件的能力
    # 整块消失，且没有任何报错。docstring 早就写着「只带内置工具」，实现没跟上。
    #
    # 兜底工作区（root / read_only）缺省按 default 用户空间解析，与普通 agent
    # 同一套；真正的生效值仍由每轮对话注入的会话工作空间决定。
    try:
        builtin_tools = await load_builtin_tools(core_id)
    except Exception as e:  # noqa: BLE001 缺 default 空间等不该让主脑起不来
        logger.warning("核心 agent 装配内置工具失败，仅影响本地文件能力: %s", e)
        builtin_tools = []

    # 插件：核心 agent 的插件同样是 ``~/.keeper/agents/<id>/plugins/<name>`` 的符号链接，
    # 与普通 agent 同一套机制——只是它不进 ``agents`` 数据库表、从本地目录构建。这里
    # 显式装载，界面上「绑定本地插件到核心 agent」才真正生效。
    plugin_tools: List[Any] = []
    plugin_mcp: Dict[str, Any] = {}
    plugin_skills: List[Dict[str, Any]] = []
    try:
        _comps = await _load_plugins(_CoreAgentRef(core_id, d.name))
        plugin_tools = _comps.tools
        plugin_mcp = _comps.mcp_servers
        plugin_skills = _comps.skill_items
    except Exception as e:  # noqa: BLE001 插件坏不该让主脑起不来
        logger.warning("核心 agent 装载插件失败，仅影响插件能力: %s", e)

    agent_config = AgentConfig(
        name=d.name,
        persona=persona,
        description=d.description,
        workspace=workspace,
        llm=llm,
        tools=builtin_tools + plugin_tools,
        # 插件带来的 MCP server / 工具 / 技能：核心 agent 同样支持本地插件。
        mcp_servers=plugin_mcp,
        skill_registry=_build_skill_registry(plugin_skills),
        peers={},
    )

    # agent_id 取配置里的核心 agent id（默认 "core"）：会话用它做归属。
    # 注意它**不是**数据库 agent 表里的主键——核心 agent 不在那张表里，这里只是
    # 一个让"这个会话归谁"有答案的稳定标识。
    agent = KeeperAgent(config=agent_config, agent_id=core_id)
    await agent.build()
    # 对端仍然从 agent_peers 表加载：核心 agent 不做"会话级 peer"，
    # 但它可以沿用上一版配置里已挂在某条 agent 记录上的对端。
    # TODO: 会话级 peer 落地后，这里应改为只读会话内的 peer 集合。
    try:
        await agent.load_managed_peers()
    except Exception as e:  # noqa: BLE001 peer 加载失败不该让主脑起不来
        logger.warning("核心 agent 加载对端失败，仅影响对端能力: %s", e)

    set_core_keeper(agent)
    logger.info(
        "核心 agent 已装配：name=%s 模型=%s 目录=%s 内置工具=%d（%s）",
        d.name,
        "（未绑定模型，纯索引模式）" if not llm else "来自 agent_llm_binding",
        core_def.core_agent_dir(cfg),
        len(builtin_tools),
        ", ".join(getattr(t, "name", "?") for t in builtin_tools) or "无",
    )
    return agent


async def _resolve_llm_by_binding(agent_id: str) -> Dict[str, Any]:
    """按 agent_id 解析**绑定的模型**（``agent_llm_binding`` → ``llm_profiles``）。

    核心 agent 没有数据库行，所以用不了普通 agent 那条 ``_resolve_llm(row)``。
    但绑定本身就是按 agent_id 存的，所以这里直接按 id 查——**语义与它完全一致**：
    只认绑定，没绑就返回空 dict（纯索引模式，不调模型），不做任何兜底。
    """
    try:
        # 相对导入：这里在 keeper.agent 包内，写成 ``from store.db import`` 会
        # 找不到模块——而这个 except 会把它吞掉、安静地返回 {}，症状是"绑定了
        # 模型却仍然不调模型"，极难定位。所以这类导入失败要看得见。
        from ..store import AgentLLMBinding, LLMProfile, get_session_factory
    except Exception as e:  # noqa: BLE001
        logger.warning("导入模型绑定相关模块失败: %s", e)
        return {}

    try:
        factory = get_session_factory()
        async with factory() as db:
            binding = await db.get(AgentLLMBinding, agent_id)
            if binding is None or not binding.llm_profile_id:
                return {}
            profile = await db.get(LLMProfile, binding.llm_profile_id)
            if profile is None:
                logger.warning(
                    "agent %s 绑定的模型预设 %s 已不存在，按未绑定处理",
                    agent_id,
                    binding.llm_profile_id,
                )
                return {}
            return profile.to_llm_dict()
    except Exception as e:  # noqa: BLE001 查不到不该让装配整体失败
        logger.warning("解析 agent %s 的模型绑定失败: %s", agent_id, e)
        return {}
