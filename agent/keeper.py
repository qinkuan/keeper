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




        # 可执行体（懒建：prepare() 时装配，随实例复用）；工具表归它自己持有
        self._process: Any = None
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
    async def prepare(self) -> Any:
        """装配当轮能力集，返回可执行体。

        用「全量重建 + 整体替换」而不是增量追加：配置里拿掉的 MCP server / 对端，
        它的工具必须随之消失——否则页面上取消勾选了，LLM 还能继续调它。

        幂等：一轮开始前反复调用都安全。执行侧（``executor``）开跑前调它。
        """
        tools = await self._collect_tools()
        # 组摘要跟着工具表一起重建：折叠后的分组行靠它告诉模型「这一组是干嘛的」
        for group, summary in (self.config.group_summaries or {}).items():
            tools.set_group_summary(group, summary)
        if self._process is None:
            from .process import Process

            # Process 自己知道该从 agent 上取什么（llm / persona / skill 注入）
            self._process = Process.from_agent(self, tools)
        else:
            # 工具表归 Process 所有：就地整体替换即可，不必重建实例
            self._process.replace_tools(tools)
        return self._process

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
        peer 工具）后替换 ``Process`` 持有的工具表，不动 llm / MCP 连接。
        """
        tools = await self._collect_tools()
        if self._process is not None:
            self._process.replace_tools(tools)


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


def list_keepers() -> list["KeeperAgent"]:
    """列出所有已装载的 agent（供 landing 页 / 管理接口枚举）。"""
    return list(_KEEPERS.values())


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
    """
    from sqlalchemy import select

    from ..store import Agent as AgentRow
    from ..store import AgentCapabilityOverride, get_session_factory

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
