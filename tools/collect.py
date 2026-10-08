"""从各类来源收集工具，包装成统一的 ``ProcessorTool``。

两类工具：
1. MCP server 的真实工具：运行时从「已连接的 MCPSession」动态拉取，
   不手写镜像封装（server 加了新工具，agent 自动可用）。
2. 本地 / 自定义工具：keeper 自身能力以及你将来自定义的 tool。

设计：ProcessorTool 是上层编排（``agent`` 的 ReAct 循环）与底层能力的统一契约
（name / description / parameters / 协程 run），与具体 LLM 是否支持原生
tool-calling 无关。Registry 容器与这层契约的定义见同包的 ``base``。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from sqlalchemy import func, select

from .base import ProcessorTool, ToolRegistry

logger = logging.getLogger(__name__)

# 不给 agent 用的 MCP 工具（按原始名匹配）。默认不屏蔽——
# 屏蔽哪些属于具体部署的选择，不该由框架替用户决定。
SKIP_MCP_TOOLS: set = set()


def keeper_tools_from_agent(agent) -> ToolRegistry:
    """本地 / 自定义工具（非 MCP）。

    MCP server 的工具由 mcp_tools_from_session 在运行时动态加载，
    请勿在此手写 MCP 镜像。这里只注册 keeper 自身的非 MCP 工具。
    """
    reg = ToolRegistry()
    # 在此追加你自定义的非 MCP 类 tool
    from ..memory.tools import build_memory_tools

    for t in build_memory_tools().all():
        reg.register(t)
    return reg


def _tool_params(tool) -> Dict[str, str]:
    """从 LangChain Tool 抽取参数说明（兼容 .args 与 .args_schema 两种形式）。"""
    args = getattr(tool, "args", None)
    if isinstance(args, dict):
        return {
            k: (v.get("description", "") if isinstance(v, dict) else "")
            for k, v in args.items()
        }
    schema = getattr(tool, "args_schema", None)
    if schema is not None:
        try:
            props = schema.schema().get("properties", {})
            return {k: v.get("description", "") for k, v in props.items()}
        except Exception:
            pass
    return {}


def _is_connection_error(exc: BaseException) -> bool:
    """判断异常是不是「连接断了」，而非工具本身执行出错。

    只对连接类错误重连——参数写错、工具内部报错这类没必要重连，白白多一次握手。
    """
    # 只认「连接真的断了」。超时类（ReadTimeout / PoolTimeout）刻意不在其中：
    # agent 跑一轮很贵，超时说明这轮没跑完，重来一次多半还是超时，
    # 不如直接判失败——重试对 agent 不是免费的。
    names = {
        "ConnectError", "ReadError", "WriteError", "RemoteProtocolError",
        "ConnectionError", "BrokenPipeError", "ConnectionResetError",
        "NetworkError",
    }
    cur: BaseException | None = exc
    for _ in range(5):  # 顺着 __cause__ 往上找几层，真正的根因常在里面
        if cur is None:
            break
        if type(cur).__name__ in names:
            return True
        cur = cur.__cause__

    msg = str(exc).lower()
    return any(
        k in msg
        for k in (
            "connection", "closed", "broken pipe", "server disconnected",
            "peer closed", "not connected", "all connection attempts failed",
        )
    )


async def _invoke_with_retry(session, tool, call_args, server, action) -> str:
    """调 MCP 工具；若是对端重启导致的连接中断，重连一次再试。

    只对连接类错误重试（见 _is_connection_error），其它错误原样抛出，
    由 ProcessorTool.execute 转成错误文本交给 agent 判断。
    """
    try:
        return await session.invoke_tool(tool, call_args)
    except Exception as e:
        if not _is_connection_error(e):
            raise
        logger.warning("[MCP] %s 调用中断(%s)，尝试重连", server, type(e).__name__)
        if not await session.reconnect_server(server):
            raise
        # 重连后是全新的 Tool 对象，旧的那个已失效
        return await session.invoke_tool(session.get_tool(f"{server}__{action}"), call_args)


async def _ask_peer(
    server: str, call_args: dict, session, tool, agent_id: Optional[str] = None
) -> str:
    """按 keeper 协议向对端提问：走 PeerService，往来落 threads / agent_messages。

    只有在会话上下文中才有意义（记账要挂到某个会话上）；
    不在会话里（例如手工调用）时退回普通 MCP 工具调用。
    """
    from ..chat.context import (
        current_session_id,
        set_pending_peer_ask,
        take_peer_reply_to,
    )
    from ..peer import PeerService

    sid = current_session_id()
    question = str(call_args.get("message", "") or "")
    if not sid or not question:
        return await session.invoke_tool(tool, call_args)

    # 优先用模型显式带回的 peer_step_id；没带则取本轮「正在回答的追问」（resume
    # 从挂起步 wait_ref 注入，一次性）。两者都没有 → 这就是**新发起的问题**，
    # 开新任务，绝不静默回退到旧任务（回退会把新问题挂到旧任务上，问 A 拿到 B 的答案）。
    peer_step_id = call_args.get("peer_step_id") or take_peer_reply_to()
    r = await PeerService(sid, server, agent_id=agent_id).ask(
        question, peer_step_id=peer_step_id or None
    )
    if not r.get("ok"):
        return f"[调用 {server} 失败] {r.get('error', '未知错误')}"

    answer = str(r.get("answer", "") or "")
    if r.get("waiting"):
        # 记下对端的 peer_step_id，由 _step_writer 写进当前步骤的 wait_ref。
        # 落库后恢复时从库里取，不靠 LLM 从 observation 里抄。
        set_pending_peer_ask((r.get("ask") or {}).get("peer_step_id"))
        # 对端回答不了，去问它的人了。把 peer_step_id 交给 agent，
        # 它下次带着这个 id 再调一次 {server}__send，就能接上那段对话。
        ask = r.get("ask") or {}
        answer = (
            f"{answer}\n"
            f"[{server} 需要补充信息才能继续；把 peer_step_id 带上再调一次 "
            f"{server}__send 即可作答]\n"
            f"  peer_step_id: {ask.get('peer_step_id')}\n"
            f"  问题: {ask.get('question', '')}"
        )
        if ask.get("options"):
            answer += f"\n  选项: {ask.get('options')}"
    return answer


def mcp_tools_from_session(session, read_only_tools=None) -> ToolRegistry:
    """从已连接的 MCP session 动态加载其真实工具，包装成 ProcessorTool。

    - 工具用「全名」注册（``server__tool``）：多个 MCP server 暴露同名工具时，
      Tool 对象自己的 name 分不出来源，用原名注册会互相覆盖；
    - description 前置 ``[server]`` 标记，模型选型时才知道这是谁的能力；
    - SKIP_MCP_TOOLS 里的工具不暴露给 agent（按原始名匹配）。
    """
    reg = ToolRegistry()
    try:
        named = session.get_named_tools()
    except Exception as e:
        logger.warning("获取 MCP 工具清单失败: %s", e)
        return reg

    for full_name, tool in named:
        raw = getattr(tool, "name", None) or ""
        if not raw or raw in SKIP_MCP_TOOLS:
            continue
        server, _, action = full_name.partition("__")
        description = getattr(tool, "description", "") or raw
        if server and not description.startswith(f"[{server}]"):
            description = f"[{server}] {description}"
        params = _tool_params(tool)
        # keeper 协议的对端：send 是「对话」语义，要记账、可能挂起，走 PeerService；
        # 普通 MCP 工具的 send 只是个普通动作，直接调
        via_peer = action == "send" and session.is_keeper(server)
        # 插件清单里显式声明 read_only 的工具：既允许并行，也豁免危险守卫
        _declared_readonly = bool(read_only_tools and raw in read_only_tools)

        async def _run(args, _tool=tool, _server=server, _action=action,
                       _via_peer=via_peer):
            call_args = dict(args or {})
            if _via_peer:
                return await _ask_peer(_server, call_args, session, _tool)
            # 用 Tool 本体调用而非按名查找：名字在多个 server 上可能重复
            return await _invoke_with_retry(session, _tool, call_args, _server, _action)

        reg.register(
            ProcessorTool(
                name=full_name,
                description=description,
                parameters=params,
                run=_run,
                # MCP 协议不声明副作用，所以默认**不可并发**；插件清单里
                # 显式写了 read_only 的才允许参与并行。
                read_only=_declared_readonly,
                # MCP 工具跑在**插件自己的进程里**，参数里没有「工作空间」这个
                # 概念，keeper 侧根本管不住它读文件还是写文件。首份评测基线就是
                # 栽在这里：只读工作区里 agent 用 bash__run cp 写出了文件、用
                # bash__run grep /etc/hosts 读了系统文件。
                #
                # 复用 read_only 声明做豁免：声明为只读的工具不会写文件，能有什么
                # 危险。未声明的一律按 dangerous 处理，执行前过 keeper.tools.guard。
                # （代价：插件作者若把一个会越界读文件的工具谎报为 read_only，
                #  守卫对它就不生效——这是「信任插件声明」的取舍，与既有的
                #  并行化豁免是同一个信任假设。）
                dangerous=not _declared_readonly,
            )
        )
    return reg


async def peer_tools_from_config(agent) -> ToolRegistry:
    """从 config.peers 段的 a2a_url 拉对端 AgentCard，用 skills 生成工具。

    取代旧的「经 MCP session 拉对端 send 工具」：对端不再暴露 MCP，而是经 A2A。
    每个对端 skill 生成一个 ``{peer}__{skill_id}`` 工具，description 来自 AgentCard.skills，
    run 直接走 PeerService（不再经 MCP invoke）。

    任一对端拉卡片失败只降级跳过，不拖垮其余对端。
    """
    reg = ToolRegistry()
    # 优先取 agent 自己的 peers，没有再回退到画像（config）——
    # 两者都支持，是为了让「直接构造 agent」也能带上对端
    peers = getattr(agent, "peers", None)
    if not peers:
        cfg = getattr(agent, "config", None)
        peers = getattr(cfg, "peers", None) if cfg is not None else None
    if not peers:
        return reg

    async def _one(server: str, pcfg: Any) -> None:
        a2a_url = (
            (pcfg or {}).get("a2a_url")
            if isinstance(pcfg, dict)
            else getattr(pcfg, "a2a_url", None)
        )
        if not a2a_url:
            return
        headers = (
            ((pcfg or {}).get("headers") if isinstance(pcfg, dict) else getattr(pcfg, "headers", None))
            or {}
        )
        try:
            from ..a2a.client import A2AClient

            card = await A2AClient(a2a_url, headers=headers).fetch_agent_card()
        except Exception as e:
            logger.warning("拉对端 %s AgentCard 失败，跳过其工具: %s", server, e)
            return

        skills = list(getattr(card, "skills", None) or [])
        if not skills:
            # 对端没声明 skill：给一个兜底的统一 send 工具，保证仍可对话
            skills = [
                type("S", (), {"id": "send", "name": "send",
                               "description": f"向 {server} 发消息 / 提问"})()
            ]
        for sk in skills:
            sk_id = getattr(sk, "id", None) or getattr(sk, "name", None)
            if not sk_id:
                continue
            desc = getattr(sk, "description", None) or getattr(sk, "name", sk_id)
            reg.register(
                ProcessorTool(
                    name=f"{server}__{sk_id}",
                    description=f"[{server}] {desc}",
                    parameters={
                        "message": "要发给对端的问题 / 指令（必填）",
                        "peer_step_id": (
                            "（可选）对端 A2A taskId：若本条是回答对端之前的追问，"
                            "带上它让对端从挂起步骤恢复。区别于本 agent 的本地 step_id。"
                        ),
                    },
                    run=_make_peer_run(server, agent),
                )
            )
        logger.info("已从对端 %s AgentCard 生成 %d 个工具", server, len(skills))

    await asyncio.gather(*(_one(s, p) for s, p in peers.items()))
    return reg


def _make_peer_run(server: str, agent: Any = None):
    """生成对端工具的 run：直接走 PeerService（A2A），不依赖 MCP session。

    ``agent`` 是发起方实例：把它的 agent_id 带给 PeerService，后者据此取到该
    agent 的对端配置。少了它 PeerService 取不到 agent，a2a_url 为空就会误走
    MCP 回退分支报「keeper 未就绪或未连接 MCP」。
    """
    agent_id = getattr(agent, "agent_id", None)

    async def _run(args: Dict[str, Any]) -> str:
        from ..chat.context import (
            current_session_id,
            set_pending_peer_ask,
            take_peer_reply_to,
        )
        from ..peer import PeerService

        sid = current_session_id()
        question = str((args or {}).get("message", "") or "")
        if not sid:
            return "[对端调用需在会话上下文中，否则无法记账]"
        # 同 _ask_peer：模型显式带 > 本轮「正在回答的追问」> 都没有则新开任务。
        peer_step_id = (args or {}).get("peer_step_id") or take_peer_reply_to()
        r = await PeerService(sid, server, agent_id=agent_id).ask(
            question, peer_step_id=peer_step_id or None
        )
        if not r.get("ok"):
            return f"[调用 {server} 失败] {r.get('error', '未知错误')}"

        answer = str(r.get("answer", "") or "")
        if r.get("waiting"):
            # 对端反过来追问：记下它的 peer_step_id，下次带回来即可恢复挂起
            set_pending_peer_ask((r.get("ask") or {}).get("peer_step_id"))
            ask = r.get("ask") or {}
            answer = (
                f"{answer}\n"
                f"[{server} 需要补充信息才能继续；把 peer_step_id 带上再调一次该对端的工具即可作答]\n"
                f"  peer_step_id: {ask.get('peer_step_id')}\n"
                f"  问题: {ask.get('question', '')}"
            )
            if ask.get("options"):
                answer += f"\n  选项: {ask.get('options')}"
        return answer

    return _run
