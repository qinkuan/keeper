"""HTTP 接口（供 keeper/web 界面调用）。

单进程装载多个 agent：所有接口都带 ``/agents/{agent_id}`` 前缀，按 agent_id
路由到对应实例；``GET /agents`` 本身列出已装载的 agent（landing 页枚举用）。

接口只负责收参数、回结果；agent 实例统一通过 ``get_keeper(agent_id)`` 取用，
不持有任何 agent 状态。
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from ..agent.keeper import get_keeper, list_keepers
from ..store import SessionMessage, get_session_factory
from ..plugin import agent_plugins_dir, scan_library
from .artifacts import ArtifactResolutionError, resolve_artifact_uri
from .context import set_agent, set_task_id, set_task_item
from .service import ChatService, default_initiator_id, resolve_workspace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents")


def _agent(agent_id: str):
    """按 agent_id 取实例；不存在 / 未就绪 → 404。

    顺带把 agent 标记进当前上下文——之后所有日志都会带上它的 id / 名称，
    单进程多 agent 时才分得清哪行日志属于谁。
    """
    agent = get_keeper(agent_id)
    if agent is None:
        raise HTTPException(
            status_code=404, detail=f"agent 不存在或未就绪: {agent_id}"
        )
    set_agent(agent_id, getattr(agent, "name", None))
    return agent


async def _agent_descriptions(agent_ids: list) -> Dict[str, str]:
    """批量取 agent 简介。

    description 只在本地库（平台配置的缓存）里，运行时实例上没有——概览卡片要
    展示它，所以按 id 一次性从库里取，避免每个 agent 各查一遍。

    返回 ``(descriptions, origins)``：来源要在概览卡片上标出来——平台来源的改了
    得回平台改，本地来源的当场就能改。
    """
    from sqlalchemy import select

    from ..store import Agent as AgentRow, get_session_factory

    ids = [i for i in agent_ids if i]
    if not ids:
        return {}, {}
    async with get_session_factory()() as db:
        rows = (
            await db.execute(
                select(AgentRow.id, AgentRow.description, AgentRow.origin).where(
                    AgentRow.id.in_(ids)
                )
            )
        ).all()
    return (
        {r.id: (r.description or "") for r in rows},
        {r.id: (r.origin or "platform") for r in rows},
    )


@router.get("")
async def list_agents(request: Request) -> Dict[str, Any]:
    """已装载的 agent 概览（landing 页枚举用）。

    顺带返回每个 agent 自己的 **A2A 入站地址** ``a2a_url``：前端「添加对端」时
    直接取用即可。地址按**服务端自身** base_url 拼出，而不是让前端拼——dev 下
    前端跑在 vite(5273) 而后端在 8080，前端拿到的源并非后端真实地址，自己拼必错。
    """
    base = str(request.base_url).rstrip("/")
    keepers = list_keepers()
    descs, origins = await _agent_descriptions([a.agent_id for a in keepers])
    return {
        "agents": [
            {
                "id": a.agent_id,
                "name": a.name,
                "origin": origins.get(a.agent_id) or "platform",
                "description": descs.get(a.agent_id) or None,
                "ready": a.check_readiness().ok,
                "mcp_connected": a.mcp is not None,
                # agent_id 在「纯配置直接运行」时为 None，此时没有可路由的端点
                "a2a_url": f"{base}/agents/{a.agent_id}/a2a" if a.agent_id else None,
            }
            for a in keepers
        ]
    }


@router.get("/{agent_id}/chat")
async def chat(
    agent_id: str,
    q: str = "",
    session_id: str = "",
    step_id: str = "",
    user_space_id: str = "",
) -> Dict[str, Any]:
    """聊天：不带 session_id 视为新建会话；响应回传 session_id，后续请求带上即可续接。

    step_id 有值 = 回答某条挂起的提问（走恢复）；没值 = 新话轮（会作废挂起步骤）。
    user_space_id 仅在新会话（无 session_id）时生效，用于绑定该会话的用户空间。
    """
    _agent(agent_id)
    logger.info(
        "chat 收到问题: %s (session=%s, step=%s)",
        q,
        session_id or "新建",
        step_id or "无",
    )
    try:
        return await ChatService(
            agent_id=agent_id, session_id=session_id or None
        ).ask(q, step_id=step_id or None, user_space_id=user_space_id or None)
    except ValueError as e:
        return {"error": str(e), "answer": "（会话不存在或已失效）", "llm": False}


class ChatStreamIn(BaseModel):
    """流式对话入参。用 POST + body：问题可能很长，不适合塞 query string。"""

    q: str
    session_id: str = ""
    step_id: str = ""
    user_space_id: str = ""
    # 本次运行的标识（前端生成）：点「停止生成」时按它精确中止这一次，
    # 而不是按 session 粗粒度停——同一会话可能有别的请求在跑。
    run_id: str = ""


class ChatAbortIn(BaseModel):
    """中止一次流式运行。"""

    run_id: str


# 被请求中止的 run_id 集合：流式循环每步 / 每块都来这里问一次「该停了吗」
_STOP_RUNS: set = set()


def _sse(event: str, data: Dict[str, Any]) -> str:
    """拼一帧 SSE：`event:` + `data:`(JSON) + 空行。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse_stream(factory, run_id: str = "") -> StreamingResponse:
    """把一次「带回调的执行」包成 SSE 响应（任务执行 / 补充输入等长耗时动作通用）。

    ``factory``：``async (on_step, on_delta, should_stop) -> dict``，返回值即
    ``done`` 帧的内容。执行期间：

    - ``event: step``：每完成一步 ReAct；
    - ``event: delta``：最终回答的一小段；
    - ``event: done``：执行完成，带权威结果；
    - ``event: error``：异常（含 HTTPException 的 detail）。

    ``run_id`` 非空时，执行循环每到一个检查点就查一次是否被请求中止（停止生成）。
    """
    q: asyncio.Queue = asyncio.Queue()

    async def on_step(seq: int, step) -> None:
        await q.put(
            (
                "step",
                {
                    "seq": seq,
                    "thought": getattr(step, "thought", "") or "",
                    "tool": getattr(step, "tool", None),
                    "args": getattr(step, "args", None),
                    "observation": getattr(step, "observation", None),
                    "kind": getattr(step, "kind", None),
                },
            )
        )

    async def on_delta(text: str) -> None:
        await q.put(("delta", {"text": text}))

    # 注意：不要在 runner 启动前 discard(run_id)。run_id 由前端每次 newRunId 全局
    # 唯一，不存在「上次遗留同 id」的情况；若在此 pre-discard，会误清掉 apiAbortChat
    # （add）先于本请求执行时写入的停止标记，导致「停止生成」失效。清理交给 runner
    # 的 finally。
    async def should_stop() -> bool:
        return bool(run_id and run_id in _STOP_RUNS)

    async def runner() -> None:
        try:
            res = await factory(on_step, on_delta, should_stop)
            await q.put(("done", res))
        except HTTPException as e:
            await q.put(("error", {"message": e.detail}))
        except Exception as e:  # 兜底：异常也要收尾，否则前端会一直挂着
            logger.exception("流式执行失败")
            await q.put(("error", {"message": str(e)}))
        finally:
            if run_id:
                _STOP_RUNS.discard(run_id)
            await q.put(None)  # 结束哨兵

    task = asyncio.create_task(runner())

    async def gen():
        try:
            while True:
                try:
                    # 15s 无事件就发心跳：防代理 / 浏览器按空闲掐断连接
                    item = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if item is None:
                    break
                event, data = item
                yield _sse(event, data)
        finally:
            # 客户端断开：取消还在跑的任务，别让它空转
            if not task.done():
                task.cancel()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/{agent_id}/chat/stream")
async def chat_stream(agent_id: str, body: ChatStreamIn):
    """流式聊天（阶段一：逐步推送 ReAct 步骤，最终一次性给出完整回答）。

    语义与 ``GET /chat`` 完全一致（同会话串行、逐步落库、追问 / 暂停），
    只是把执行过程通过 SSE 逐步吐给前端：

    - ``event: step``：每完成一步（思考 / 工具调用 / 观察）推一次；
    - ``event: delta``：最终回答的一小段（识别到 FINAL 之后开始逐块推）；
    - ``event: done``：跑完，带完整结果（answer / artifacts / waiting 等）；
      前端以它为准覆盖正文，避免流式拼接与服务端不一致；
    - ``event: error``：出错。
    """
    _agent(agent_id)
    q: asyncio.Queue = asyncio.Queue()

    async def on_step(seq: int, step) -> None:
        await q.put(
            (
                "step",
                {
                    "seq": seq,
                    "thought": getattr(step, "thought", "") or "",
                    "tool": getattr(step, "tool", None),
                    "args": getattr(step, "args", None),
                    "observation": getattr(step, "observation", None),
                    "kind": getattr(step, "kind", None),
                },
            )
        )

    async def on_delta(text: str) -> None:
        """最终回答的一小段（token 级流式）。"""
        await q.put(("delta", {"text": text}))

    run_id = body.run_id or ""
    # 注意：不要在 runner 启动前 discard(run_id)。run_id 由前端每次 newRunId 全局
    # 唯一，不存在「上次遗留同 id」的情况；若在此 pre-discard，会误清掉 apiAbortChat
    # （add）先于本请求执行时写入的停止标记，导致「停止生成」失效。清理交给 finally。

    async def should_stop() -> bool:
        """用户点了「停止生成」？流式每收到一块、每跑完一步都问一次。"""
        return bool(run_id and run_id in _STOP_RUNS)

    async def runner() -> None:
        try:
            res = await ChatService(
                agent_id=agent_id, session_id=body.session_id or None
            ).ask(
                body.q,
                step_id=body.step_id or None,
                user_space_id=body.user_space_id or None,
                on_step=on_step,
                on_delta=on_delta,
                should_stop=should_stop,
            )
            await q.put(("done", res))
        except Exception as e:  # 兜底：异常也要收尾，否则前端会一直挂着
            logger.exception("chat/stream 执行失败")
            await q.put(("error", {"message": str(e)}))
        finally:
            if run_id:
                _STOP_RUNS.discard(run_id)
            await q.put(None)  # 结束哨兵

    task = asyncio.create_task(runner())

    async def gen():
        try:
            while True:
                try:
                    # 15s 无事件就发心跳：防代理 / 浏览器按空闲掐断连接
                    item = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if item is None:
                    break
                event, data = item
                yield _sse(event, data)
        finally:
            # 客户端断开：取消还在跑的任务，别让它空转
            if not task.done():
                task.cancel()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/{agent_id}/chat/abort")
async def abort_chat(agent_id: str, body: ChatAbortIn) -> Dict[str, Any]:
    """停止生成：中止 run_id 对应的那次流式运行。

    只置标记，不强制 kill——执行循环在下一个检查点（每收到一块 / 每跑完一步）
    停下来，已生成的内容和已完成的步骤都会保留并落库。
    """
    _agent(agent_id)
    if body.run_id:
        _STOP_RUNS.add(body.run_id)
    return {"ok": True, "run_id": body.run_id}


@router.get("/{agent_id}/sessions")
async def sessions(
    agent_id: str, initiator_id: Optional[int] = None, limit: int = 50
) -> Dict[str, Any]:
    """会话列表（按最近更新排序）。传 initiator_id 则只列该发起人的会话。"""
    _agent(agent_id)
    items = await ChatService(
        agent_id=agent_id, initiator_id=initiator_id
    ).list_sessions(limit=limit)
    return {"sessions": items}


@router.get("/{agent_id}/sessions/{session_id}/messages")
async def session_messages(
    agent_id: str, session_id: str, limit: int = 200
) -> Dict[str, Any]:
    """取某会话的历史消息（按 seq 升序）与挂起的追问。前端打开聊天框时恢复用。

    pending 非空表示该会话正等用户回答，前端应在 message_id 对应消息下渲染追问框。
    """
    _agent(agent_id)
    svc = ChatService(agent_id=agent_id, session_id=session_id)
    try:
        await svc.verify_session()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return await svc.load_messages(session_id, limit=limit)


@router.get("/{agent_id}/config")
async def config(agent_id: str) -> Dict[str, Any]:
    """只读配置视图（api_key 仅暴露“是否已配置”），供 keeper/web 控制台展示。"""
    agent = _agent(agent_id)

    llm: Dict[str, Any] = {}
    if agent.config is not None:
        raw = agent.config.llm or {}
        llm = {
            "provider": raw.get("provider"),
            "model_name": raw.get("model_name") or raw.get("model"),
            "temperature": raw.get("temperature", 0.2),
            "has_api_key": bool(raw.get("api_key")),  # 不返回密钥本体
        }
    ws_root = agent.workspace_root
    return {
        "name": agent.name,
        "workspace": str(ws_root) if ws_root else None,
        "mcp_connected": agent.mcp is not None,
        # 注意：实例上没有 mcp_servers，只有 config 上有。写错会在「没配 MCP 的
        # agent」上抛 AttributeError → 500（Agent 配置面板打不开就是这个原因）。
        "mcp_servers": list((agent.config.mcp_servers or {}) if agent.config else []),
        "skills": [s.name for s in agent.skills],
        "llm": llm,
    }


@router.get("/{agent_id}/skills")
async def skills(agent_id: str):
    """已注册的技能清单。"""
    agent = _agent(agent_id)
    return {"skills": [s.name for s in agent.config.skill_registry.all()]}


# ---------------------------------------------------------------- 工具管理
def _try_json(s: Optional[str]):
    """把落库的 JSON 字符串尽量解析回对象；失败则原样返回。"""
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return s


@router.get("/{agent_id}/plugins")
async def list_plugins(agent_id: str) -> Dict[str, Any]:
    """该 agent 绑定的插件 —— **直接列链接目录**得出，附链接是否可用。

    绑定关系就是 ``~/.keeper/agents/<id>/plugins/<name>`` 这个符号链接，所以没有
    查表的必要：列目录是什么就是什么。链接有效 = 插件还在；悬空 = 库里的目录
    没了，装配时会跳过。
    """
    agent = _agent(agent_id)
    lib = {p.dirname: p for p in scan_library()}
    linked = agent_plugins_dir(agent.id)
    out = []
    if not linked.is_dir():  # 还没勾过插件，目录不存在
        return {"plugins": out}
    for name in sorted(
        p.name for p in linked.iterdir() if p.is_dir() or p.is_symlink()
    ):
        # 也把**悬空**链接列出来：它代表"勾过但库里没了"，用户需要看见并处理
        raw = linked / name
        p = lib.get(name)
        out.append(
            {
                "name": name,
                "description": (p.description if p else "") or "",
                "version": (p.version if p else "") or "",
                "manifest": _try_json(p.path / "keeper-plugin.json") if p else None,
                "enabled": True,
                "available": raw.is_dir(),  # 悬空为 False
            }
        )
    return {"plugins": out}


# ---------------------------------------------------------------- 能力开关
class CapabilityToggleIn(BaseModel):
    """开关某件能力。

    ``kind`` 两类：``builtin``（keeper 自带）/ ``plugin``（平台下发的插件，统一承载
    MCP server、可执行工具与技能）。
    """
    kind: str
    name: str
    enabled: bool


def _cap_state(kind: str, *, bound: bool, resource_on: bool, local_on: bool, missing: bool) -> str:
    """把「绑定 / 资源启用 / 本地开关 / 依赖齐不齐」合成一个展示态。"""
    if not local_on:
        return "已关闭"
    if kind != "builtin" and not bound:
        return "未绑定"
    if missing:
        return "依赖缺失"
    if not resource_on:
        return "已停用"
    return "已装配"


@router.get("/{agent_id}/capabilities")
async def list_capabilities(agent_id: str) -> Dict[str, Any]:
    """列出该 agent 的**两类能力**及其开关状态（供配置面板展示）。

    内置工具来自 keeper 本地清单（平台不参与）；插件来自平台 binding，本地开关可
    覆盖。插件统一承载 MCP server、可执行工具（bin）与技能（skill）；这里顺便把
    该插件名下已连上的 MCP 工具带出来，供前端展开查看。
    """
    agent = _agent(agent_id)

    from ..store import AgentCapabilityOverride, get_session_factory
    from ..tools.builtin.registry import BUILTIN_TOOLS, KIND as BUILTIN_KIND

    # 注意：实例上没有 workspace_read_only，只读标志在 config.workspace 上。
    # 直接取属性会 AttributeError → 500（四个模块一起挂，就是这个原因）。
    _ws = getattr(getattr(agent, "config", None), "workspace", None)
    read_only = bool(getattr(_ws, "read_only", False))
    # 状态文字里的「只读」要说清是**哪个工作空间**只读：只读是工作空间的属性
    # （user_space.read_only），agent 画像上那个同名字段已经不参与判断了。
    _ws_path = str(getattr(_ws, "path", "") or "")

    async with get_session_factory()() as db:
        ov_rows = (
            await db.execute(
                select(AgentCapabilityOverride).where(
                    AgentCapabilityOverride.agent_id == agent_id
                )
            )
        ).scalars().all()
        # 插件库扫一遍：已绑定的用它取描述/存在性，未绑定的作为可勾选项
        lib_by_name = {p.dirname: p for p in scan_library()}
        # 「绑了哪些插件」= 链接目录里有什么。目录里只列**有效**链接，悬空的
        # 代表库里的插件没了，装配时会跳过，没必要当成可用项列出来。
        bound = sorted(
            p.name
            for p in (agent_plugins_dir(agent_id).iterdir() if agent_plugins_dir(agent_id).is_dir() else [])
            if p.is_dir()
        )

        # 内置：keeper 本地清单，默认启用
        builtin = []
        for t in BUILTIN_TOOLS:
            local_on = next(
                (
                    r.enabled
                    for r in ov_rows
                    if r.kind == BUILTIN_KIND and r.ref_name == t["name"]
                ),
                True,
            )
            builtin.append(
                {
                    "name": t["name"],
                    "description": t["description"],
                    "enabled": local_on,
                    "mutating": t["mutating"],
                    "state": "只读工作区跳过"
                    if (local_on and t["mutating"] and read_only)
                    else ("已装配" if local_on else "已关闭"),
                    "readonly_reason": (
                        f"工作区 {_ws_path} 标记为只读，写操作会在运行时被拒绝"
                        if read_only else ""
                    ),
                }
            )

        # 插件：来自 binding（统一承载 MCP server / 可执行工具 / 技能）
        out: Dict[str, Any] = {
            "builtin": builtin,
            "plugin": [],
        }
        for kind in ("plugin",):
            for ref in bound:
                lib = lib_by_name.get(ref)
                if lib is None:
                    continue
                local_on = next(
                    (r.enabled for r in ov_rows if r.kind == kind and r.ref_name == ref),
                    True,
                )
                item: Dict[str, Any] = {
                    "name": ref,
                    "description": lib.description or "",
                    "enabled": local_on,
                    "state": _cap_state(
                        kind,
                        bound=True,
                        resource_on=True,
                        local_on=local_on,
                        missing=False,  # 到这里说明链接有效
                    ),
                }
                # 插件：把该插件名下已连上的 MCP 工具带出来（MCP server 名形如
                # ``{plugin}__{server}``），供前端点击展开查看
                if kind == "plugin" and agent.mcp:
                    item["tools"] = [
                        t
                        for server, tools in agent.mcp.tools_by_server().items()
                        if server.startswith(f"{ref}__")
                        for t in tools
                    ]
                out[kind].append(item)

    # **不列「库里有但没绑的」。**
    #
    # 这一页的开关只管启用/停用（写 override），不动绑定。所以把一个没绑的插件
    # 列成"可勾选"是骗人的：勾上去什么也不会发生，因为根本没有链接可供装配。
    #
    # 绑定在「智能体」页的「管理插件」里改——改完这里的列表自然就出现了。
    return out


@router.post("/{agent_id}/capabilities")
async def toggle_capability(agent_id: str, data: CapabilityToggleIn) -> Dict[str, Any]:
    """开关某件能力，写本地 override，然后**自动重建**该 agent 实例。

    **只管启用/停用，不管绑定。**

    绑定（这个 agent 有没有装这个插件）由 ``~/.keeper/agents/<id>/plugins/<name>``
    这个符号链接表达，在「智能体」页的「管理插件」里改。这里的开关只是
    "装了、但现在要不要用它"——所以只写 ``AgentCapabilityOverride``，**绝不去动
    链接**。

    两者分开的好处很实际：临时把某个插件关掉排查问题，关完再开，不用重新选一遍
    插件；而如果开关会删链接，"关一下"就等于"卸载"，得重新勾回来。

    必须重建：能力是在装配阶段读取的（``load_agent_config`` / ``build``），
    改开关不会自动反映到已建好的实例上。
    """
    agent = _agent(agent_id)
    if data.kind not in ("builtin", "plugin"):
        raise HTTPException(status_code=400, detail=f"未知的能力类型: {data.kind}")

    before = sorted(getattr(t, "name", "?") for t in (await agent._collect_tools()).all())
    logger.info(
        "切换能力 agent=%s 类型=%s 名称=%s -> %s（切换前工具数=%d）",
        agent_id, data.kind, data.name, "启用" if data.enabled else "停用", len(before),
    )

    # 只写 override。**不动链接**：绑定归「智能体」页管，这里的开关只是启用/停用。
    from ..store import AgentCapabilityOverride, get_session_factory

    async with get_session_factory()() as db:
        r = await db.execute(
            select(AgentCapabilityOverride).where(
                AgentCapabilityOverride.agent_id == agent_id,
                AgentCapabilityOverride.kind == data.kind,
                AgentCapabilityOverride.ref_name == data.name,
            )
        )
        row = r.scalars().first()
        if row is None:
            db.add(
                AgentCapabilityOverride(
                    agent_id=agent_id,
                    kind=data.kind,
                    ref_name=data.name,
                    enabled=data.enabled,
                )
            )
        else:
            row.enabled = data.enabled
        await db.commit()
        logger.info(
            "  override 已写入 agent=%s 类型=%s 名称=%s enabled=%s（%s）",
            agent_id, data.kind, data.name, data.enabled,
            "新建记录" if row is None else "更新已有记录",
        )

    # 重建：旧实例先释放（关 MCP 等），再按新配置装配
    from ..agent.keeper import build_agent, register_keeper, unregister_keeper

    await agent.shutdown()
    unregister_keeper(agent_id)
    rebuilt = await build_agent(agent_id=agent_id)
    register_keeper(agent_id, rebuilt)
    after = sorted(getattr(t, "name", "?") for t in (await rebuilt._collect_tools()).all())
    added = [t for t in after if t not in before]
    removed = [t for t in before if t not in after]
    logger.info(
        "能力切换完成 agent=%s 类型=%s 名称=%s enabled=%s | 工具数 %d -> %d | 新增=%s 消失=%s",
        agent_id, data.kind, data.name, data.enabled, len(before), len(after),
        added or "无", removed or "无",
    )
    logger.info(
        "已切换能力 %s/%s 为 %s，并重建 agent %s",
        data.kind, data.name, data.enabled, agent_id,
    )
    return {"kind": data.kind, "name": data.name, "enabled": data.enabled, "ok": True}


# ---------------------------------------------------------------- 对端管理（A2A）
class PeerIn(BaseModel):
    """添加一个 A2A 对端。

    对端可以是：
    - 本进程内另一个 agent：``http://127.0.0.1:8080/agents/{id}/a2a``
    - 外部 agent：``http://other-host:8080/agents/{id}/a2a``

    ``source`` / ``trust_env`` **一般不用传**：添加时来源是已知的（下拉选=本进程、
    手填=外部），后端据此自动推导——本进程直连（否则 localhost 被送去代理会 502），
    外部走系统代理。要覆盖默认行为时才显式传。
    """

    name: str
    a2a_url: str
    headers: Optional[Dict[str, Any]] = None
    source: Optional[str] = None  # local / external；不传则按 URL 自动判定
    trust_env: Optional[bool] = None  # 不传则按来源自动推导


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


def _is_local_url(url: str) -> bool:
    """判断 a2a_url 是否指向**本进程**（本机）。"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    if host in _LOCAL_HOSTS:
        return True
    try:
        import socket

        return host in {socket.gethostname().lower(), socket.gethostbyname(socket.gethostname())}
    except Exception:  # noqa: BLE001
        return False


def _resolve_peer_kind(
    a2a_url: str, source: Optional[str], trust_env: Optional[bool]
) -> tuple[str, bool]:
    """推导对端的来源与代理策略。显式传值优先，否则按 URL 自动判定。"""
    if source not in ("local", "external"):
        source = "local" if _is_local_url(a2a_url) else "external"
    if trust_env is None:
        # 本进程直连（localhost 走代理会 502）；外部才交给系统代理
        trust_env = source == "external"
    return source, trust_env


def _peer_view(cfg: Any) -> Dict[str, Any]:
    """把运行时 peers 的取值统一成 dict 形状（兼容 dict / 对象两种来源）。"""
    if isinstance(cfg, dict):
        return dict(cfg)
    return {
        "a2a_url": getattr(cfg, "a2a_url", None),
        "headers": getattr(cfg, "headers", None) or {},
    }


@router.get("/{agent_id}/peers")
async def list_peers(agent_id: str) -> Dict[str, Any]:
    """列出该 agent 当前生效的 A2A 对端（运行时层 ``agent.peers``）。"""
    agent = _agent(agent_id)
    return {
        "peers": [
            {"name": name, **_peer_view(cfg)}
            for name, cfg in (agent.peers or {}).items()
        ]
    }


@router.post("/{agent_id}/peers")
async def add_peer(agent_id: str, data: PeerIn) -> Dict[str, Any]:
    """运行时添加一个 A2A 对端：落持久化表 + 写运行时层 + 热刷新工具表。

    落 ``agent_peers`` 表让重启不丢；随后写 ``agent.peers``（工具收集优先读它）
    并调 ``refresh_tools()``，使新对端立刻变成可用工具 ``{name}__{skill}``。
    """
    agent = _agent(agent_id)
    headers = data.headers or {}
    # 来源与代理策略：显式传值优先，否则按 URL 自动判定（本进程直连 / 外部走代理）
    source, trust_env = _resolve_peer_kind(data.a2a_url, data.source, data.trust_env)

    from ..store import AgentPeer, get_session_factory

    async with get_session_factory()() as db:
        r = await db.execute(
            select(AgentPeer).where(
                AgentPeer.agent_id == agent_id, AgentPeer.name == data.name
            )
        )
        row = r.scalars().first()
        if row is None:
            row = AgentPeer(
                agent_id=agent_id,
                name=data.name,
                a2a_url=data.a2a_url,
                headers=json.dumps(headers),
                source=source,
                trust_env=trust_env,
                enabled=True,
            )
            db.add(row)
        else:
            row.a2a_url = data.a2a_url
            row.headers = json.dumps(headers)
            row.source = source
            row.trust_env = trust_env
            row.enabled = True
        await db.commit()

    agent.peers[data.name] = {
        "a2a_url": data.a2a_url,
        "headers": headers,
        "source": source,
        "trust_env": trust_env,
    }
    await agent.refresh_tools()
    return {
        "name": data.name,
        "a2a_url": data.a2a_url,
        "source": source,
        "trust_env": trust_env,
        "ok": True,
    }


class PeerPatch(BaseModel):
    """局部更新对端：目前只支持改「是否走系统代理」。"""

    trust_env: Optional[bool] = None


@router.patch("/{agent_id}/peers/{name}")
async def patch_peer(agent_id: str, name: str, data: PeerPatch) -> Dict[str, Any]:
    """就地改某个对端的设置（当前只改代理开关），立即生效并持久化。

    不需要重新生成工具：``trust_env`` 只影响**调用时**怎么建 HTTP 客户端，
    不影响 AgentCard / 工具清单——因此这里刻意**不**调 ``refresh_tools()``，
    省掉一轮无谓的拉卡请求。
    """
    agent = _agent(agent_id)
    if data.trust_env is None:
        raise HTTPException(status_code=400, detail="没有要更新的字段")

    from ..store import AgentPeer, get_session_factory

    async with get_session_factory()() as db:
        r = await db.execute(
            select(AgentPeer).where(
                AgentPeer.agent_id == agent_id, AgentPeer.name == name
            )
        )
        row = r.scalars().first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"对端不存在: {name}")
        row.trust_env = data.trust_env
        await db.commit()

    # 运行时层同步：否则当前进程里下一次调用仍用旧值
    cfg = (agent.peers or {}).get(name)
    if isinstance(cfg, dict):
        cfg["trust_env"] = data.trust_env
    return {"name": name, "trust_env": data.trust_env, "ok": True}


@router.delete("/{agent_id}/peers/{name}")
async def remove_peer(agent_id: str, name: str) -> Dict[str, Any]:
    """移除一个对端：删持久化记录 + 从运行时层摘掉 + 热刷新工具表。"""
    agent = _agent(agent_id)

    from ..store import AgentPeer, get_session_factory

    async with get_session_factory()() as db:
        r = await db.execute(
            select(AgentPeer).where(
                AgentPeer.agent_id == agent_id, AgentPeer.name == name
            )
        )
        row = r.scalars().first()
        if row is not None:
            await db.delete(row)
            await db.commit()

    agent.peers.pop(name, None)
    await agent.refresh_tools()
    return {"name": name, "removed": True}


@router.get("/{agent_id}/tools/effective")
async def effective_tools(agent_id: str) -> Dict[str, Any]:
    """该 agent **运行时实际生效**的工具表（四类来源合并后的结果）。

    区别于 ``/tools``：后者查 ``tool`` 表（静态登记清单），这里是当期装配结果——
    本地 / 自定义 + ``config.tools`` + MCP(``server__tool``) + A2A 对端(``peer__skill``)。
    """
    agent = _agent(agent_id)
    reg = await agent._collect_tools()
    return {
        "tools": [
            {
                "name": getattr(t, "name", None),
                "description": getattr(t, "description", "") or "",
            }
            for t in reg.all()
        ]
    }


# ---------------------------------------------------------------------------
# 用户空间（UserSpace）：用户自管的命名文件路径模块
# ---------------------------------------------------------------------------
import os  # noqa: E402
import re  # noqa: E402
from ..agent.config import WORKSPACE_ROOT  # noqa: E402
from ..store import (
    ChatSession,
    Task,
    TaskItem,
    UserSpace,
    USER_SPACE_DEFAULT_NAME,
    get_session_factory,
)  # noqa: E402
from ..store.models import ReactStep, SessionMessage  # noqa: E402

user_space_router = APIRouter(prefix="/user-spaces", tags=["user-spaces"])

# 用户空间统一落在 workspace/user/ 下；新建的空间是这里的一个软链接（指向用户真实文件夹）。
USER_DIR = WORKSPACE_ROOT / "user"


def _validate_ws_name(name: str) -> str:
    """校验用户空间名称：作为 user/ 下的软链接文件名，必须是单段、不含路径分隔符、
    不能是 '.' 或 '..'（防止路径穿越）；允许中文/Unicode 等任意字符。返回去空白后的名称。
    """
    n = name.strip()
    if not n:
        raise HTTPException(status_code=400, detail="名称不能为空")
    if n in (".", "..") or "/" in n or "\\" in n:
        raise HTTPException(
            status_code=400,
            detail="名称不能含路径分隔符（/ 或 \\），也不能是 '.' 或 '..'（用作 user/ 下的目录名）",
        )
    return n


class UserSpaceIn(BaseModel):
    name: str
    path: str
    read_only: bool = True
    description: Optional[str] = None


class UserSpacePatch(BaseModel):
    name: Optional[str] = None
    path: Optional[str] = None
    read_only: Optional[bool] = None
    description: Optional[str] = None


def _normalize_ws_path(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise HTTPException(status_code=400, detail="路径必须是绝对路径")
    return p


def _resolve_safe(p: Path) -> Path:
    """解析真实路径；若该链路上已有损坏的自指软链接，resolve 会抛 ELOOP，此时退回原值。"""
    try:
        return p.resolve()
    except OSError:
        return p


def _target_if_changed(submitted: Path, entry: Path) -> Optional[Path]:
    """编辑时判断「绝对路径」是否被真正改写，返回要指向的新目标；没改则返回 None。

    库里存的 path 是**软链接入口**（user/<名称>），前端回填后会原样提交回来。
    这时若照常执行 unlink + symlink(target, entry)，就是 symlink(entry, entry)，
    直接造出一个指向自己的链接（ELOOP，读写/列目录/git 全挂）。

    所以：提交的仍指向入口本身 ⇒ 判定为「用户没改路径」，返回 None 沿用现有链接、
    不再重建；否则返回新目标交给上层校验 + 重建。
    """
    if _resolve_safe(submitted) == _resolve_safe(entry) or submitted == entry:
        return None
    return submitted


def _guard_symlink_target(target: Path, entry: Path) -> None:
    """保证「真实文件夹」不会绕回受管目录 user/ —— 否则软链接会成环或直接无限递归。

    两种翻车方式：
    ① 把填的路径正好是这条软链接自己 → unlink 后 symlink(target==entry, entry)，
       链接指向自己，之后任何读写 / 列目录 / git 都报 ELOOP（Too many levels of symbolic links）；
    ② 填的是 user/ 的上层目录（如 ~/.keeper/workspace）→ user/<名>/user/<名>/… 无限套娃。

    因此这里要求：目标既不能在受管目录 user/ 之内，也不能是它的上层。
    """
    t = _resolve_safe(target)
    e = _resolve_safe(entry)
    if t == e:
        raise HTTPException(
            status_code=400,
            detail="目标目录不能就是该用户空间自己的路径（会形成软链接循环），请另选一个真实文件夹",
        )
    managed = _resolve_safe(USER_DIR)
    if t == managed or managed in t.parents or t in managed.parents:
        raise HTTPException(
            status_code=400,
            detail=(
                f"目标目录不能落在受管目录 {managed} 之内，也不能是它的上层目录"
                "（会造成路径自引用或无限递归），请另选一个真实文件夹"
            ),
        )


def _is_inside_workspace(p: Path) -> bool:
    """判断真实目录是否落在 workspace 根内部（含 workspace 根本身）。

    内部目录不走软链接，直接以真实路径作为工作空间路径，
    从而避免「在 user/ 内再建软链接」造成的自环 / 无限递归。
    """
    rp = p.resolve()
    root = WORKSPACE_ROOT.resolve()
    return rp == root or root in rp.parents


@user_space_router.get("")
async def list_user_spaces() -> Dict[str, Any]:
    async with get_session_factory()() as db:
        rows = (await db.execute(select(UserSpace).order_by(UserSpace.name))).scalars().all()
        return {
            "user_spaces": [
                {
                    "id": r.id,
                    "name": r.name,
                    "path": r.path,
                    "read_only": r.read_only,
                    "description": r.description,
                }
                for r in rows
            ]
        }


@user_space_router.get("/browse")
async def browse_dir(path: str = "") -> Dict[str, Any]:
    """浏览服务器本机目录，供前端在选择工作目录时挑选文件夹。

    只读列目录，返回目录与文件两类条目（目录可进入/选择，文件仅展示），
    每条带 size（字节，目录为 null）与 mtime（修改时间，epoch 秒）。
    不落库、不写盘。
    """
    # Linux/macOS 从用户 home 目录开始浏览；Windows 回退到系统盘根。
    _default_root = Path.home() if os.name != "nt" else Path("/")
    root = Path(path).expanduser() if path else _default_root
    if not root.is_absolute():
        root = _default_root
    # 不存在或不是目录：回退到父目录（再不行就用 home）
    if not root.is_dir():
        root = root.parent if root.parent != root else Path.home()

    def _info(e: Path) -> Dict[str, Any]:
        info: Dict[str, Any] = {"name": e.name, "path": str(e)}
        try:
            st = e.stat()
            info["size"] = st.st_size if e.is_file() else None
            info["mtime"] = int(st.st_mtime)
        except OSError:
            info["size"] = None
            info["mtime"] = None
        return info

    try:
        entries = sorted(root.iterdir(), key=lambda e: e.name.lower())
    except PermissionError:
        return {"path": str(root), "parent": None, "dirs": [], "files": [], "error": "无权限访问该目录"}
    except OSError as e:  # noqa: BLE001
        return {"path": str(root), "parent": None, "dirs": [], "files": [], "error": f"无法读取目录: {e}"}
    dirs = [_info(e) for e in entries if e.is_dir()]
    files = [_info(e) for e in entries if e.is_file()]
    parent = str(root.parent) if root != root.parent else None
    return {"path": str(root), "parent": parent, "dirs": dirs, "files": files, "error": None}


@user_space_router.post("")
async def create_user_space(data: UserSpaceIn) -> Dict[str, Any]:
    name = _validate_ws_name(data.name)
    if name == USER_SPACE_DEFAULT_NAME:
        raise HTTPException(status_code=400, detail="名称 'default' 为系统保留，不可新建")
    # 用户给的是「真实文件夹」；不存在则自动创建（决策：允许自动 mkdir）。
    target = _normalize_ws_path(data.path)
    USER_DIR.mkdir(parents=True, exist_ok=True)
    entry = USER_DIR / name
    # 真实目录若落在 workspace 内部，则不建软链接，直接以真实路径作为工作空间路径
    # （默认空间本就是真实路径；这样可避免 user/ 内再建软链接导致的自环 / 递归）。
    if _is_inside_workspace(target):
        if entry.exists() or entry.is_symlink():
            raise HTTPException(status_code=409, detail=f"user/ 下已存在同名条目: {name}")
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"无法创建目标目录: {e}")
        final_path = target
    else:
        # 先校验再落盘：避免非法目标（落在受管目录内）被拒绝时仍把真实目录建出来。
        _guard_symlink_target(target, entry)
        if entry.is_symlink():
            entry.unlink()
        elif entry.exists():
            raise HTTPException(status_code=409, detail=f"user/ 下已存在同名条目: {name}")
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"无法创建目标目录: {e}")
        try:
            os.symlink(target, entry)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"创建软链接失败: {e}")
        final_path = entry
    async with get_session_factory()() as db:
        clash = (await db.execute(select(UserSpace).where(UserSpace.name == name))).scalars().first()
        if clash:
            if entry.is_symlink():
                entry.unlink()
            raise HTTPException(status_code=409, detail=f"名称已存在: {name}")
        # 记录的 path：外部空间存软链接路径（agent 统一在 workspace/user/<name> 下工作），
        # 内部空间直接存真实路径。二者都能被 resolve_workspace 直接用。
        row = UserSpace(
            name=name, path=str(final_path), read_only=data.read_only, description=data.description
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return {
            "id": row.id,
            "name": row.name,
            "path": row.path,
            "read_only": row.read_only,
            "description": row.description,
        }


@user_space_router.put("/{us_id}")
async def update_user_space(us_id: str, data: UserSpacePatch) -> Dict[str, Any]:
    async with get_session_factory()() as db:
        row = (await db.execute(select(UserSpace).where(UserSpace.id == us_id))).scalars().first()
        if row is None:
            raise HTTPException(status_code=404, detail="用户空间不存在")
        if row.name == USER_SPACE_DEFAULT_NAME:
            raise HTTPException(status_code=400, detail="默认用户空间不可编辑（除非手动改库）")

        # 改名：软链接直接 rename。
        if data.name is not None and data.name.strip():
            nm = _validate_ws_name(data.name)
            if nm == USER_SPACE_DEFAULT_NAME:
                raise HTTPException(status_code=400, detail="名称 'default' 为系统保留")
            if nm != row.name:
                clash = (await db.execute(select(UserSpace).where(UserSpace.name == nm))).scalars().first()
                if clash:
                    raise HTTPException(status_code=409, detail=f"名称已存在: {nm}")
                old_entry = USER_DIR / row.name
                new_entry = USER_DIR / nm
                if old_entry.is_symlink():
                    if new_entry.exists() or new_entry.is_symlink():
                        raise HTTPException(status_code=409, detail=f"user/ 下已存在同名条目: {nm}")
                    old_entry.rename(new_entry)
                    row.path = str(new_entry)
                row.name = nm

        # 改路径（真实文件夹）。
        if data.path is not None and data.path.strip():
            entry = USER_DIR / row.name
            submitted = _normalize_ws_path(data.path)
            # 路径没被真正改写（提交的仍是本空间自己的入口）→ 沿用现有链接，禁止重建。
            target = _target_if_changed(submitted, entry)
            if target is None:
                logger.info("编辑用户空间 %s：路径未改变，沿用现有链接 %s", row.name, entry)
            else:
                # 若此前是外部空间（有软链接），先移除旧软链接。
                if entry.is_symlink():
                    entry.unlink()
                if _is_inside_workspace(target):
                    # 内部目录：不建软链接，直接用真实路径。
                    if entry.exists():
                        raise HTTPException(
                            status_code=409, detail=f"user/ 下已存在同名条目: {row.name}"
                        )
                    try:
                        target.mkdir(parents=True, exist_ok=True)
                    except Exception as e:  # noqa: BLE001
                        raise HTTPException(status_code=400, detail=f"无法创建目标目录: {e}")
                    row.path = str(target)
                else:
                    _guard_symlink_target(target, entry)
                    try:
                        target.mkdir(parents=True, exist_ok=True)
                    except Exception as e:  # noqa: BLE001
                        raise HTTPException(status_code=400, detail=f"无法创建目标目录: {e}")
                    if entry.exists() and not entry.is_symlink():
                        raise HTTPException(
                            status_code=409, detail=f"user/ 下已存在同名条目: {row.name}"
                        )
                    try:
                        os.symlink(target, entry)
                    except Exception as e:  # noqa: BLE001
                        raise HTTPException(status_code=400, detail=f"创建软链接失败: {e}")
                    row.path = str(entry)

        if data.read_only is not None:
            row.read_only = data.read_only
        if data.description is not None:
            row.description = data.description
        await db.commit()
        await db.refresh(row)
        return {
            "id": row.id,
            "name": row.name,
            "path": row.path,
            "read_only": row.read_only,
            "description": row.description,
        }


@user_space_router.delete("/{us_id}")
async def delete_user_space(us_id: str) -> Dict[str, Any]:
    async with get_session_factory()() as db:
        row = (await db.execute(select(UserSpace).where(UserSpace.id == us_id))).scalars().first()
        if row is None:
            raise HTTPException(status_code=404, detail="用户空间不存在")
        if row.name == USER_SPACE_DEFAULT_NAME:
            raise HTTPException(status_code=400, detail="默认用户空间不可删除（除非手动改库）")
        # 有 agent 把它当默认工作区绑定时**不能删**：装配期只认 user_space 的记录，
        # 记录没了「工作区不存在」，那个 agent 会在下次重建时直接起不来。
        # 会话引用可以安全解绑（会话能回落 default），agent 绑定没有回落语义
        # ——它绑的就是这个目录。
        from ..store import Agent as AgentRow

        bound = (
            await db.execute(
                select(AgentRow).where(AgentRow.workspace_path == row.path)
            )
        ).scalars().all()
        if bound:
            names = "、".join(f"{a.name}({a.id})" for a in bound)
            raise HTTPException(
                status_code=409,
                detail=(
                    f"还有 agent 绑定着这个工作区：{names}。"
                    "它们的工作区只认「用户空间」里的这条记录，删了就起不来。"
                    "请先把这些 agent 改绑到别的空间。"
                ),
            )
        # 删掉 user/ 下的软链接（真实文件夹不動）。
        entry = USER_DIR / row.name
        if entry.is_symlink():
            entry.unlink()
        # 解绑引用它的会话（避免外键约束），让其回落到默认用户空间
        await db.execute(
            ChatSession.__table__.update()
            .where(ChatSession.user_space_id == us_id)
            .values(user_space_id=None)
        )
        await db.delete(row)
        await db.commit()
        return {"id": us_id, "removed": True}


@router.get("/{agent_id}/sessions/{session_id}/workspace")
async def session_workspace(agent_id: str, session_id: str) -> Dict[str, Any]:
    """返回某会话当前生效的工作空间（用户空间 + agent 私有空间），供聊天头部展示。"""
    agent = _agent(agent_id)
    from .service import resolve_workspace

    ctx = await resolve_workspace(agent, session_id)
    user_space = None
    async with get_session_factory()() as db:
        sess = await db.get(ChatSession, session_id)
        usid = getattr(sess, "user_space_id", None) if sess else None
        if usid:
            us = await db.get(UserSpace, usid)
            if us:
                user_space = {
                    "id": us.id,
                    "name": us.name,
                    "path": us.path,
                    "read_only": us.read_only,
                }
    return {
        "user_space": user_space,
        "agent_space": str(ctx.agent_space),
        "effective_root": str(ctx.root),
        "read_only": ctx.read_only,
    }


class SessionWorkspacePatch(BaseModel):
    user_space_id: Optional[str] = None  # 传 null 表示回落 agent 默认目录


@router.put("/{agent_id}/sessions/{session_id}")
async def update_session_workspace(
    agent_id: str, session_id: str, data: SessionWorkspacePatch
) -> Dict[str, Any]:
    """切换某会话绑定的用户空间。

    约束：会话正处于「等待人类输入」的挂起态时禁止（对话中途不可换）；
    结束后（无挂起步骤）可换。传 ``user_space_id=null`` 回落 agent 默认目录。
    """
    async with get_session_factory()() as db:
        sess = await db.get(ChatSession, session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        susp = (
            await db.execute(
                select(ReactStep)
                .join(SessionMessage, ReactStep.session_message_id == SessionMessage.id)
                .where(
                    SessionMessage.chat_session_id == session_id,
                    ReactStep.status == "suspended",
                )
            )
        ).scalars().first()
        if susp is not None:
            raise HTTPException(
                status_code=409, detail="会话正处于等待输入状态，结束后才能换工作空间"
            )
        target = data.user_space_id
        if target:
            us = await db.get(UserSpace, target)
            if us is None:
                raise HTTPException(status_code=400, detail="用户空间不存在")
        sess.user_space_id = target
        await db.commit()
        return {"session_id": session_id, "user_space_id": target, "ok": True}


# ---------------------------------------------------------------------------
# 产物（artifact）：agent 本轮产出的文件，供前端展示 / 下载 / 本机打开
# ---------------------------------------------------------------------------
def _is_local_deploy() -> bool:
    """是否本机部署：本机才允许「在文件夹中打开」；远程部署只支持下载。

    远程时通过环境变量 KEEPER_DEPLOY=remote 显式关闭；默认按本机处理。
    """
    return os.environ.get("KEEPER_DEPLOY", "local").lower() != "remote"


async def _load_artifact(agent, message_id: str, artifact_id: str):
    """查消息 → 取出产物 → 按 scheme 解析出取数来源。

    返回 ``(artifact, source)``；任何环节失败抛 404。
    真实取数逻辑（本机 / 远程）见 ``artifacts.resolve_artifact_uri``。
    """
    async with get_session_factory()() as db:
        msg = await db.get(SessionMessage, message_id)
        if msg is None:
            raise HTTPException(status_code=404, detail="消息不存在")
        session_id = msg.chat_session_id
        arts = json.loads(msg.artifacts) if msg.artifacts else []
    art = next((a for a in arts if a["id"] == artifact_id), None)
    if art is None:
        raise HTTPException(status_code=404, detail="产物不存在")
    # path 是带 scheme 的 URI（file:// 本机 / http(s):// 远程），按 scheme 解析。
    try:
        source = await resolve_artifact_uri(art["path"])
    except ArtifactResolutionError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return art, source


@router.get("/{agent_id}/deploy-info")
async def deploy_info(agent_id: str) -> Dict[str, Any]:
    """返回部署形态，前端据此决定显示「打开文件夹」还是只「下载」。"""
    return {"local": _is_local_deploy()}


@router.get("/{agent_id}/files/{message_id}/{artifact_id}")
async def get_artifact(
    agent_id: str,
    message_id: str,
    artifact_id: str,
    dl: bool = Query(False),
) -> Response:
    """预览或下载产物文件。

    - ``dl=1`` 或不可预览类型 → 作为附件下载；
    - ``text/html`` → 内联返回并加 CSP（配合前端 iframe ``sandbox`` 防 XSS）；
    - 图片 / 纯文本 / markdown 等 → 内联返回，由前端展示。

    路径经 ``safe_path`` 锁在工作空间内，杜绝穿越（S2）；远程走受控代取。
    """
    agent = _agent(agent_id)
    art, source = await _load_artifact(agent, message_id, artifact_id)
    mime = art.get("mime", "application/octet-stream")
    # 能在浏览器里直接看的：图片、文本类（含源码 text/x-*）、json / javascript，
    # 以及 pdf 和音视频——这两类由**浏览器自己的查看器**处理（前端对它们是
    # 新窗口打开），浏览器能渲染/播放就比下载下来强。
    is_previewable = (
        mime.startswith("image/")
        or mime.startswith("text/")
        or mime.startswith("video/")
        or mime.startswith("audio/")
        or mime == "application/pdf"
        or mime in ("application/json", "application/javascript")
    )
    if dl or not is_previewable:
        if source.is_local:
            return FileResponse(source.path, media_type=mime, filename=art["name"])
        return Response(
            source.data,
            media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="{art["name"]}"'},
        )
    if mime == "text/html":
        # HTML 是低可信内容：即便前端用 sandbox iframe 隔离，后端也加 CSP 兜底。
        # 注意 A7：HTML 内嵌的相对资源（如 ./style.css）在 MVP 下会 404，
        # 推荐 agent 生成「单文件内联 HTML」以规避。
        content = source.path.read_bytes() if source.is_local else source.data
        return Response(
            content,
            media_type="text/html",
            headers={
                "Content-Security-Policy": (
                    "default-src 'none'; "
                    "script-src 'unsafe-inline'; "
                    "style-src 'unsafe-inline' 'self' data:; "
                    "img-src 'self' data: https: http:; "
                    "font-src 'self' data:; "
                    "connect-src 'none';"
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )
    if source.is_local:
        return FileResponse(source.path, media_type=mime)
    return Response(source.data, media_type=mime)


@router.post("/{agent_id}/files/{message_id}/{artifact_id}/reveal")
async def reveal_artifact(
    agent_id: str,
    message_id: str,
    artifact_id: str,
) -> Dict[str, Any]:
    """本机部署：在文件管理器中打开产物所在目录（仅本机有意义）。"""
    if not _is_local_deploy():
        raise HTTPException(
            status_code=403, detail="仅本机部署支持「在文件夹中打开」"
        )
    agent = _agent(agent_id)
    art, source = await _load_artifact(agent, message_id, artifact_id)
    if not source.is_local:
        raise HTTPException(
            status_code=400, detail="仅本机文件支持「在文件夹中打开」"
        )
    if source.path is None or not source.path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    folder = str(source.path.parent)
    opener = {
        "darwin": ["open", folder],
        "win32": ["explorer", folder],
    }.get(sys.platform, ["xdg-open", folder])
    try:
        subprocess.run(opener, check=False)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"打开文件夹失败: {e}")
    return {"ok": True, "folder": folder}


def _git_diff_contents(abspath: str) -> Dict[str, Any]:
    """取文件相对 git HEAD 的「旧(HEAD) / 新(当前)」全文，供前端 Monaco diff 编辑器。

    返回 ``{"status": ..., "original": <HEAD 内容>, "modified": <当前内容>}``：
    - not_git：文件不在 git 仓库内；
    - untracked_new：未跟踪文件，original 为空（整份视为新增）；
    - no_changes：已跟踪但相对 HEAD 无改动；
    - ok：有改动。
    """
    p = Path(abspath)
    if not p.is_file():
        return {"status": "missing", "original": "", "modified": ""}
    try:
        top = subprocess.run(
            ["git", "-C", str(p.parent), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:  # noqa: BLE001
        return {"status": "not_git", "original": "", "modified": ""}
    if top.returncode != 0:
        return {"status": "not_git", "original": "", "modified": ""}
    repo = top.stdout.strip()
    try:
        rel = str(p.relative_to(repo)).replace(os.sep, "/")
    except ValueError:
        return {"status": "not_git", "original": "", "modified": ""}

    ls = subprocess.run(
        ["git", "-C", repo, "ls-files", "--error-unmatch", rel],
        capture_output=True, text=True,
    )
    tracked = ls.returncode == 0

    def _read() -> str:
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""

    modified = _read()
    if not tracked:
        return {"status": "untracked_new", "original": "", "modified": modified}

    old = subprocess.run(
        ["git", "-C", repo, "show", f"HEAD:{rel}"],
        capture_output=True, text=True,
    )
    original = old.stdout if old.returncode == 0 else ""
    status = "no_changes" if original == modified else "ok"
    return {"status": status, "original": original, "modified": modified}


@router.get("/{agent_id}/files/{message_id}/{artifact_id}/diff")
async def get_artifact_diff(
    agent_id: str,
    message_id: str,
    artifact_id: str,
):
    """文件相对 git HEAD 的「旧/新」全文，供前端 Monaco diff 编辑器内联标注改动。"""
    agent = _agent(agent_id)
    art, source = await _load_artifact(agent, message_id, artifact_id)
    if not source.is_local:
        return {"status": "not_git", "original": "", "modified": ""}
    if source.path is None or not source.path.exists():
        return {"status": "missing", "original": "", "modified": ""}
    return _git_diff_contents(str(source.path))


def _workspace_path(ws_root: Path, rel: str) -> Path:
    """把相对路径安全地解析到工作空间根内，防目录穿越。"""
    if not rel:
        return ws_root
    target = (ws_root / rel).resolve()
    root_resolved = ws_root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise HTTPException(status_code=400, detail="路径超出工作空间范围")
    return target


def _workspace_git_status(ws_root: Path) -> Dict[str, str]:
    """返回工作空间内各文件的 git 状态（相对 ws_root 的路径 → ' M'/'??'/...）。"""
    try:
        out = subprocess.run(
            ["git", "-C", str(ws_root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:  # noqa: BLE001
        return {}
    if out.returncode != 0:
        return {}
    status: Dict[str, str] = {}
    for line in out.stdout.splitlines():
        if len(line) < 4:
            continue
        code = line[:2].strip()
        # 处理 R100 old -> new 重命名
        path = line[3:].split(" -> ")[-1].strip()
        status[path] = code
    return status


@router.get("/{agent_id}/sessions/{session_id}/workspace/tree")
async def workspace_tree(
    agent_id: str,
    session_id: str,
    path: str = Query("", description="相对工作空间根的路径，空为根"),
):
    """列工作空间目录（限制在 effective_root 内），并附每个条目的 git 状态。"""
    agent = _agent(agent_id)
    ws = await resolve_workspace(agent, session_id)
    base = _workspace_path(ws.root, path)
    if not base.exists():
        raise HTTPException(status_code=404, detail="路径不存在")
    if not base.is_dir():
        raise HTTPException(status_code=400, detail="不是目录")
    git_map = _workspace_git_status(ws.root)
    entries = []
    for child in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        rel = str(child.relative_to(ws.root)).replace(os.sep, "/")
        st = child.stat()
        entries.append(
            {
                "name": child.name,
                "path": rel,
                "is_dir": child.is_dir(),
                "size": st.st_size if child.is_file() else None,
                "mtime": st.st_mtime,
                "git": git_map.get(rel, ""),
            }
        )
    return {
        "root": str(ws.root),
        "read_only": ws.read_only,
        "current": path,
        "entries": entries,
    }


@router.get("/{agent_id}/sessions/{session_id}/workspace/file")
async def workspace_file(
    agent_id: str,
    session_id: str,
    path: str = Query(..., description="相对工作空间根的文件路径"),
):
    """读取工作空间内任意文件内容 + 语言 + 相对 git HEAD 的改动。"""
    agent = _agent(agent_id)
    ws = await resolve_workspace(agent, session_id)
    target = _workspace_path(ws.root, path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    mime, _ = mimetypes.guess_type(str(target))
    text = target.read_text(encoding="utf-8", errors="replace")
    diff = _git_diff_contents(str(target))
    return {
        "path": path,
        "name": target.name,
        "mime": mime or "application/octet-stream",
        "read_only": ws.read_only,
        "text": text,
        "git": diff,
    }


@router.get("/{agent_id}/sessions/{session_id}/workspace/file/raw")
async def workspace_file_raw(
    agent_id: str,
    session_id: str,
    path: str = Query(..., description="相对工作空间根的文件路径"),
    dl: int = Query(0, description="1 = 作为附件下载，0 = 内联预览"),
):
    """返回文件的**原始字节**，供前端按类型展示（图片 / PDF 内联预览、二进制下载）。

    为什么单独一个端点：``workspace_file`` 返回的是 JSON，且用
    ``errors="replace"`` 解码——图片这类二进制文件经过它就已经损坏了，
    前端拿不到能 ``<img>`` 显示的字节。判「用什么展示器」在前端（见
    ``web/src/components/viewers.ts``），这里只管安全地给出原始内容。
    """
    from fastapi.responses import FileResponse

    agent = _agent(agent_id)
    ws = await resolve_workspace(agent, session_id)
    target = _workspace_path(ws.root, path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    mime, _ = mimetypes.guess_type(str(target))
    # 未知类型不让浏览器去"猜"，一律当附件，避免内联渲染出奇怪行为
    if not mime:
        dl = 1
    return FileResponse(
        str(target),
        media_type=mime or "application/octet-stream",
        filename=target.name,
        content_disposition_type="attachment" if dl else "inline",
    )


class _WorkspaceFileBody(BaseModel):
    path: str
    content: str


@router.put("/{agent_id}/sessions/{session_id}/workspace/file")
async def workspace_file_save(
    agent_id: str, session_id: str, body: _WorkspaceFileBody
):
    """保存（覆盖写）工作空间内文件；只读工作空间拒绝。"""
    agent = _agent(agent_id)
    ws = await resolve_workspace(agent, session_id)
    if ws.read_only:
        raise HTTPException(status_code=403, detail="工作空间只读，无法保存")
    target = _workspace_path(ws.root, body.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body.content, encoding="utf-8")
    return {"ok": True, "git": _git_diff_contents(str(target))}


@router.delete("/{agent_id}/sessions/{session_id}/workspace/file")
async def workspace_file_delete(
    agent_id: str,
    session_id: str,
    path: str = Query(..., description="相对工作空间根的路径（文件或目录）"),
):
    """删除工作空间内文件或目录；只读工作空间拒绝。"""
    agent = _agent(agent_id)
    ws = await resolve_workspace(agent, session_id)
    if ws.read_only:
        raise HTTPException(status_code=403, detail="工作空间只读，无法删除")
    target = _workspace_path(ws.root, path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="路径不存在")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"ok": True}


class _WorkspaceMkdirBody(BaseModel):
    path: str


@router.post("/{agent_id}/sessions/{session_id}/workspace/folder")
async def workspace_folder_create(
    agent_id: str, session_id: str, body: _WorkspaceMkdirBody
):
    """新建目录（自动创建父级）；只读工作空间拒绝。"""
    agent = _agent(agent_id)
    ws = await resolve_workspace(agent, session_id)
    if ws.read_only:
        raise HTTPException(status_code=403, detail="工作空间只读，无法创建目录")
    target = _workspace_path(ws.root, body.path)
    target.mkdir(parents=True, exist_ok=True)
    return {"ok": True}


class _WorkspaceRenameBody(BaseModel):
    path: str
    new_name: str


@router.post("/{agent_id}/sessions/{session_id}/workspace/rename")
async def workspace_rename(
    agent_id: str, session_id: str, body: _WorkspaceRenameBody
):
    """重命名（同目录内改基名）；只读工作空间拒绝。"""
    agent = _agent(agent_id)
    ws = await resolve_workspace(agent, session_id)
    if ws.read_only:
        raise HTTPException(status_code=403, detail="工作空间只读，无法重命名")
    src = _workspace_path(ws.root, body.path)
    if not src.exists():
        raise HTTPException(status_code=404, detail="路径不存在")
    new_name = body.new_name.strip().strip("/")
    if not new_name or "/" in new_name:
        raise HTTPException(status_code=400, detail="新名称非法")
    dst = (src.parent / new_name).resolve()
    root_resolved = ws.root.resolve()
    if dst != root_resolved and root_resolved not in dst.parents:
        raise HTTPException(status_code=400, detail="目标超出工作空间范围")
    src.rename(dst)
    new_rel = str(dst.relative_to(ws.root)).replace(os.sep, "/")
    return {"ok": True, "path": new_rel}


# ---------------------------------------------------------------------------
# 任务模式（Task）：一个任务绑定一个 session，task_items 即计划点
# ---------------------------------------------------------------------------
# 设计见 keeper/doc/task-design.md。MVP 边界（D6 / 已确认项）：
# - 只支持手动触发，不做定时调度；
# - 不做任务删除 / 归档（后续再考虑），因此这里不提供 DELETE。
# 步骤 2 只给「任务本身的增删改查 + 启动」；
# 计划产出与审批（步骤 3）、逐点执行（步骤 4）、验收（步骤 5）后续接上。

# 非终态：这些状态下仍可推进（重新规划 / 补充输入 / 验收打回）
TASK_ACTIVE_STATUSES = (
    "planning",
    "plan_review",
    "executing",
    "waiting_input",
    "waiting_review",
)
TASK_TERMINAL_STATUSES = ("done", "failed", "cancelled")


def _json_arr(s: Optional[str]) -> list:
    """产物 JSON 串 → 列表；空 / 解析失败一律空列表（产物只用于展示）。"""
    if not s:
        return []
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


def _item_out(it: TaskItem) -> Dict[str, Any]:
    return {
        "id": it.id,
        "seq": it.seq,
        "plan_version": it.plan_version,
        "content_md": it.content_md,
        "status": it.status,
        "conclusion": it.conclusion,
        "artifacts": _json_arr(it.artifacts),
        "started_at": it.started_at.isoformat() if it.started_at else None,
        "finished_at": it.finished_at.isoformat() if it.finished_at else None,
    }


def _task_out(t: Task, items: Optional[Any] = None) -> Dict[str, Any]:
    """任务 → 响应字典。

    ``items`` 为 None 表示「不返回计划点明细」（列表接口用，避免过重）；
    传空列表也会返回 ``items: []``（详情接口语义：任务还没有计划点）。
    """
    return {
        "id": t.id,
        "agent_id": t.agent_id,
        "title": t.title,
        "description_md": t.description_md,
        "session_id": t.session_id,
        "status": t.status,
        "plan_md": t.plan_md,
        "plan_feedback": t.plan_feedback,
        "plan_reject_count": t.plan_reject_count,
        "plan_version": t.plan_version,
        "replan_count": t.replan_count,
        "review_reject_count": t.review_reject_count,
        "review_feedback": t.review_feedback,
        "result_summary": t.result_summary,
        "artifacts": _json_arr(t.artifacts),
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "items": None if items is None else [_item_out(i) for i in items],
    }


async def _get_task(db, agent_id: str, task_id: str) -> Task:
    """取任务并校验归属：不存在 / 属于别的 agent 一律 404（不泄露跨 agent 数据）。

    顺带把 task_id 标进请求上下文：本请求里 LLM 调用的用量据此归属到该任务，
    「每个任务消耗多少 token」才查得到（见 doc/observability-design.md）。
    放在这里而不是各接口里，是因为所有任务接口都要过这一道。
    """
    row = await db.get(Task, task_id)
    if row is None or row.agent_id != agent_id:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    set_task_id(row.id)
    return row


class TaskIn(BaseModel):
    """新建任务。

    ``user_space_id`` 可选：决定本任务那个 session 的工作空间（不传回落默认空间）。
    工作空间落在 session 上而非任务上，是为了复用既有的「会话 → 工作空间」解析。
    """

    title: str
    description_md: str = ""
    user_space_id: Optional[str] = None


class TaskPatch(BaseModel):
    """改任务的输入（标题 / 描述）；终态任务不允许改，避免「输入与结果对不上」。"""

    title: Optional[str] = None
    description_md: Optional[str] = None


@router.get("/{agent_id}/tasks")
async def list_tasks(agent_id: str) -> Dict[str, Any]:
    """任务列表：按创建时间倒序，只返回任务本体（计划点明细走详情接口）。"""
    _agent(agent_id)
    async with get_session_factory()() as db:
        rows = (
            await db.execute(
                select(Task)
                .where(Task.agent_id == agent_id)
                .order_by(Task.created_at.desc())
            )
        ).scalars().all()
        return {"tasks": [_task_out(r) for r in rows]}


@router.post("/{agent_id}/tasks")
async def create_task(agent_id: str, body: TaskIn) -> Dict[str, Any]:
    """新建任务（draft）。

    同时**建好绑定的那个 session**（D1：一个任务一个 session）——之后产出计划、
    逐点执行、打回修补全程都在它里面对话，上下文天然连续。
    建完停在 draft，等人点「启动」才进 planning。
    """
    _agent(agent_id)
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="任务标题不能为空")
    async with get_session_factory()() as db:
        if body.user_space_id:
            us = await db.get(UserSpace, body.user_space_id)
            if us is None:
                raise HTTPException(
                    status_code=400, detail=f"用户空间不存在: {body.user_space_id}"
                )
        sess = ChatSession(
            agent_id=agent_id,
            initiator_id=default_initiator_id,
            user_space_id=body.user_space_id or None,
            title=title,
            kind="task",  # D1：任务绑定会话，区别于普通会话
        )
        db.add(sess)
        await db.commit()
        await db.refresh(sess)

        row = Task(
            agent_id=agent_id,
            title=title,
            description_md=body.description_md or "",
            session_id=sess.id,
            status="draft",
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return {"task": _task_out(row, items=[])}


@router.get("/{agent_id}/tasks/{task_id}")
async def get_task(agent_id: str, task_id: str) -> Dict[str, Any]:
    """任务详情：本体 + 计划点（按 seq 升序，含各版本留痕的点）。"""
    _agent(agent_id)
    async with get_session_factory()() as db:
        t = await _get_task(db, agent_id, task_id)
        items = (
            await db.execute(
                select(TaskItem)
                .where(TaskItem.task_id == t.id)
                .order_by(TaskItem.seq)
            )
        ).scalars().all()
        return {"task": _task_out(t, items=items)}


@router.patch("/{agent_id}/tasks/{task_id}")
async def patch_task(agent_id: str, task_id: str, body: TaskPatch) -> Dict[str, Any]:
    """改任务输入（标题 / 描述）；终态（done / failed / cancelled）不允许改。"""
    _agent(agent_id)
    async with get_session_factory()() as db:
        t = await _get_task(db, agent_id, task_id)
        if t.status in TASK_TERMINAL_STATUSES:
            raise HTTPException(
                status_code=409, detail=f"任务已处于终态 {t.status}，不可再修改"
            )
        if body.title is not None:
            title = body.title.strip()
            if not title:
                raise HTTPException(status_code=400, detail="任务标题不能为空")
            t.title = title
        if body.description_md is not None:
            t.description_md = body.description_md
        await db.commit()
        await db.refresh(t)
        return {"task": _task_out(t)}


@router.post("/{agent_id}/tasks/{task_id}/start")
async def start_task(agent_id: str, task_id: str) -> Dict[str, Any]:
    """手动触发（MVP 唯一的触发方式，D6）：draft → planning。

    这里只做「确保 session 存在 + 转 planning」；**计划的实际产出在步骤 3 接上**
    （在绑定的 session 里发一轮对话让 agent 产出计划点，写入 task_items）。
    """
    _agent(agent_id)
    async with get_session_factory()() as db:
        t = await _get_task(db, agent_id, task_id)
        if t.status != "draft":
            raise HTTPException(
                status_code=409,
                detail=f"任务当前状态 {t.status} 不可启动（只有 draft 可启动）",
            )
        # 兜底：理论上建任务时已建好 session，这里防止历史数据缺 session
        if not t.session_id:
            sess = ChatSession(
                agent_id=agent_id,
                initiator_id=default_initiator_id,
                title=t.title,
                kind="task",  # D1：兜底补建会话时也标 task
            )
            db.add(sess)
            await db.commit()
            await db.refresh(sess)
            t.session_id = sess.id
        t.status = "planning"
        t.plan_feedback = None
        await db.commit()
        await db.refresh(t)
        logger.info("任务 %s 已启动，进入 planning（session=%s）", t.id, t.session_id)
        return {"task": _task_out(t, items=[])}


# --- 步骤 3：计划产出 / 审批 / 重新规划 --------------------------------------

MAX_PLAN_ITEMS = 20  # 计划点数上限（防 LLM 产出爆炸，见风险 R2）
MAX_PLAN_REJECT = 3  # 计划打回次数上限（见风险 R4）
MAX_REPLAN = 5  # 重新规划次数上限（见风险 R8）
MAX_REVIEW_REJECT = 3  # 验收打回次数上限（见风险 §七），超出转 failed

# 「- [ ] xxx」/「- [x] xxx」
_ITEM_CHECK_RE = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*(.+?)\s*$")
# 退路：有序列表「1. xxx」/「1) xxx」/「1、xxx」
_ITEM_NUM_RE = re.compile(r"^\s*\d+[.)、]\s*(.+?)\s*$")


def _parse_plan_items(text: str) -> list:
    """把 agent 的回复解析成计划点文本列表。

    优先认 markdown 勾选列表；没有再退到有序列表。两者都认不出 → 空列表，
    由调用方按失败处理（不写库），避免把整段解释文字当成一个计划点。
    """
    items: list = []
    for line in (text or "").splitlines():
        m = _ITEM_CHECK_RE.match(line)
        if m:
            items.append(m.group(1).strip())
    if not items:
        for line in (text or "").splitlines():
            m = _ITEM_NUM_RE.match(line)
            if m:
                items.append(m.group(1).strip())
    # 滤掉噪声（过短 / 纯符号），再按上限截断
    return [s for s in items if len(s) >= 2][:MAX_PLAN_ITEMS]


def _collect_item_artifacts(items) -> list:
    """汇总所有计划点的过程产物（按 path/id 去重），作为任务级产物的来源。

    任务级只呈现**最终结果**：同一个文件被反复修改时，保留**最后一次**登记的版本
    （dict 覆盖即最终版，且保持首次出现的顺序）；过程中的中间版本不进任务级，
    它们仍留在对话消息里可查。

    ``Task.artifacts`` 之前没有任何地方写入，任务完成后就没有一个「看产物」的
    入口；这里由计划点（事实来源）汇总出来，避免再引入一处双写。
    """
    merged: dict = {}
    for it in items:
        if not it.artifacts:
            continue
        try:
            arts = json.loads(it.artifacts)
        except Exception:
            continue
        for a in arts or []:
            key = a.get("path") or a.get("id") or a.get("name")
            if key:
                # 后出现覆盖前面 = 保留最终版本
                merged[key] = a
            else:
                # 既无 path 也无 id/name，无法去重，单独占位保留
                merged[f"__anon_{len(merged)}"] = a
    return list(merged.values())


def _render_plan_md(items) -> str:
    """由计划点渲染 markdown 视图（表是事实来源，这里只出文本）。"""
    lines = []
    for it in items:
        if it.status == "skipped":
            lines.append(f"- [ ] ~~{it.content_md}~~（已作废）")
        elif it.status == "done":
            lines.append(f"- [x] {it.content_md}")
        else:
            lines.append(f"- [ ] {it.content_md}")
    return "\n".join(lines)


async def _refresh_plan_md(db, task: Task) -> None:
    """按当前计划点重渲染 plan_md（不 commit，交由调用方统一提交）。

    用 populate_existing 强制从库里重读：工具改写过的行若在身份映射里是旧值，
    渲染出的勾选态就是错的。
    """
    items = (
        await db.execute(
            select(TaskItem)
            .where(TaskItem.task_id == task.id)
            .order_by(TaskItem.seq)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    task.plan_md = _render_plan_md(items)
    # 任务级产物 = 各计划点过程产物的汇总。Task.artifacts 之前从未被填充，
    # 导致任务完成后没有地方能看到产物；这里与勾选态同源、同一次提交刷新。
    arts = _collect_item_artifacts(items)
    task.artifacts = json.dumps(arts, ensure_ascii=False) if arts else None


def _build_plan_prompt(task: Task, done_contents: list, instruction: str) -> str:
    """拼产出计划的提示词。

    ``done_contents`` 是已完成的点——重新规划时告诉 agent 别重复规划；
    ``task.plan_feedback`` 是上一轮打回意见，一并带上让它据此调整。
    """
    parts = [
        "你是任务规划助手。请根据下面的任务描述，产出一份**可执行的步骤计划**。",
        "",
        "要求：",
        "1. 只输出计划本身，用 markdown 无序列表，每步一行，形如「- [ ] 步骤描述」；",
        f"2. 步骤数量控制在 3~{MAX_PLAN_ITEMS} 个，按执行顺序排列，"
        "每步应能独立完成并产出可验证的结果；",
        "3. 不要输出列表以外的任何解释文字。",
        "",
        "任务描述：",
        task.description_md or task.title,
    ]
    if task.plan_feedback:
        parts += ["", "人对上一版计划的意见（务必据此调整）：", task.plan_feedback]
    if instruction:
        parts += ["", "本次补充说明：", instruction]
    if done_contents:
        parts += [
            "",
            "已完成的工作（不要重复规划，也不要把它们写进新计划）：",
        ]
        parts += [f"- [x] {c}" for c in done_contents]
    return "\n".join(parts)


async def _ask_for_plan(agent_id: str, session_id: str, prompt: str) -> list:
    """在任务绑定的 session 里发一轮对话，让 agent 产出计划点。

    走 ``ChatService.ask`` 而不是裸调 LLM：产出计划本身就是会话里的一轮对话，
    后续逐点执行能吃到这段上下文（D1 的收益）。

    只收 ``session_id``、不收 ``Task``：调用方那个 Task 属于已释放的 session，
    跨 session 再碰它会触发异步懒加载（MissingGreenlet）。更关键的是本函数
    会跑一整轮 LLM（几十秒起），期间调用方必须**不持有**任何数据库事务——
    ChatService 内部要写 session_messages、llm_calls 等，撞上就是
    「database is locked」。收 session_id 让这个约束在签名上自明。
    """
    resp = await ChatService(agent_id=agent_id, session_id=session_id).ask(prompt)
    if resp.get("error"):
        raise HTTPException(status_code=500, detail=f"产出计划失败: {resp['error']}")
    items = _parse_plan_items(resp.get("answer") or "")
    if not items:
        raise HTTPException(
            status_code=502, detail="未能从 agent 回复中解析出计划步骤"
        )
    return items


class _RefLock:
    """带引用计数的锁（``asyncio.Lock`` 不带计数，外部无法判断何时可回收）。"""

    __slots__ = ("lock", "refs")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.refs = 0


# 按 task_id 串行化「产出计划 / 重新规划」的应用层锁。
# 为什么需要它：busy_timeout 只能让写事务「等已有锁」，管不到「deferred 事务升级为
# 写锁」那一瞬——那一瞬撞上别的写者会抛 SQLITE_BUSY_SNAPSHOT，busy_timeout 无效。
# 计划产出必然要落库（删旧点 + 写新点 + 改状态），两个请求真并发就可能撞它。
# 在应用层提前排队，把这个口子彻底关掉。
_plan_locks: Dict[str, _RefLock] = {}


@asynccontextmanager
async def _plan_lock(task_id: str):
    """同一任务的计划产出互斥；``generate_plan`` 与 ``replan_task`` 共用一把。

    引用计数回收：长跑进程里 task_id 无界，不回收会持续泄漏条目——计数归零即从
    表里摘掉。计数自增到进入 ``async with`` 之间、以及自减到 ``pop`` 之间都没有
    ``await``，在 asyncio 单线程里是原子的，故无需再拿一把锁保护这张表。
    """
    entry = _plan_locks.get(task_id)
    if entry is None:
        entry = _plan_locks[task_id] = _RefLock()
    entry.refs += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.refs -= 1
        if entry.refs == 0:
            _plan_locks.pop(task_id, None)


async def _write_items(db, task: Task, contents: list, start_seq: int) -> None:
    """把解析出的计划点写入当前版本，seq 从 start_seq+1 起递增（不与历史冲突）。"""
    for offset, c in enumerate(contents):
        db.add(
            TaskItem(
                task_id=task.id,
                seq=start_seq + offset + 1,
                plan_version=task.plan_version,
                content_md=c,
                status="pending",
            )
        )


async def _load_items(db, task: Task):
    """读计划点明细。

    populate_existing：执行时工具会通过自己的会话改行，强制重读才能在响应里
    返回真实状态（否则接口回的仍是「未完成」）。
    """
    return (
        await db.execute(
            select(TaskItem)
            .where(TaskItem.task_id == task.id)
            .order_by(TaskItem.seq)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()


class PlanGenerateIn(BaseModel):
    """产出计划的附加说明（可选）。"""

    instruction: Optional[str] = None


class PlanApproveIn(BaseModel):
    """审批计划：通过进执行；不通过必须给意见（写进 plan_feedback 供重新产出参考）。"""

    approved: bool
    feedback: Optional[str] = None


@router.post("/{agent_id}/tasks/{task_id}/plan/generate")
async def generate_plan(agent_id: str, task_id: str, body: PlanGenerateIn):
    """产出计划（对外端点）：把同一任务的计划产出串行化后委派给 ``_generate_plan``。

    为什么要有这把锁：两个计划产出请求真并发时（如用户连点两次「重新产出」，
    或产出计划的同时点了重新规划），第二个写事务在升级为写锁那一瞬会撞
    ``SQLITE_BUSY_SNAPSHOT`` —— busy_timeout 只管「等已有锁」，管不到「升级」，
    等多久都没用。在应用层提前串行化，才彻底关掉这个口子。
    """
    async with _plan_lock(task_id):
        return await _generate_plan(agent_id, task_id, body)


async def _generate_plan(agent_id: str, task_id: str, body: PlanGenerateIn) -> dict:
    """产出计划：在绑定 session 里对话拿到步骤 → 写 task_items → 渲染 plan_md → 待审批。

    只在 ``planning`` 态可用（含「被打回后重新产出」——打回不升版本，同版本内迭代）。

    **事务边界（本函数存在的最大约束）**：产出计划要跑一整轮 LLM，几十秒起步，
    期间绝不能持有 SQLite 写锁——同一会话的 ReAct、记忆、llm_calls 记账都在
    另开 session 写库，一旦撞上就是 ``database is locked``。故拆成三段：
    「只读取数 → 无事务跑 LLM → 重新取库落盘」。删除旧计划点也**从 LLM 之前挪到
    落盘段**，与写新点合并成一次提交（放在读取段会提前开写事务，正是这个 bug
    只在「打回后重新产出」时才炸的原因：第一版计划后库里有待删的点，flush 即
    持锁；首版计划时没有点要删，flush 空转不持锁，于是第一次侥幸能过）。
    """
    _agent(agent_id)

    # ---- 第一段：只读取数，事务随 with 结束立刻释放 ----
    async with get_session_factory()() as db:
        t = await _get_task(db, agent_id, task_id)
        if t.status != "planning":
            raise HTTPException(
                status_code=409, detail=f"当前状态 {t.status} 不能产出计划（仅 planning）"
            )
        if not t.session_id:
            raise HTTPException(status_code=400, detail="任务未绑定 session")
        session_id = t.session_id
        existing = await _load_items(db, t)
        done_contents = [it.content_md for it in existing if it.status == "done"]
        start_seq = max([it.seq for it in existing if it.status == "done"] or [0])
        # prompt 纯内存拼装，必须在离开事务前做完（t 是本 session 的对象）
        prompt = _build_plan_prompt(t, done_contents, body.instruction or "")

    # ---- 第二段：跑 LLM，全程不碰数据库事务 ----
    contents = await _ask_for_plan(agent_id, session_id, prompt)

    # ---- 第三段：重新取库落盘（跑 LLM 期间状态可能被并发改动，重校一次）----
    async with get_session_factory()() as db:
        t = await _get_task(db, agent_id, task_id)
        if t.status != "planning":
            raise HTTPException(
                status_code=409,
                detail=f"产出计划期间状态已变为 {t.status}，本次结果未保存，请重试",
            )
        # 同版本内迭代：清掉本版本还没开始的点（planning 阶段不会有 done 点）
        for it in await _load_items(db, t):
            if it.status != "done":
                await db.delete(it)
        await db.flush()
        await _write_items(db, t, contents, start_seq)
        await db.flush()
        await _refresh_plan_md(db, t)
        t.status = "plan_review"
        t.plan_feedback = None  # 意见已被本次产出消费掉
        await db.commit()
        await db.refresh(t)
        return {"task": _task_out(t, items=await _load_items(db, t))}


@router.post("/{agent_id}/tasks/{task_id}/plan/approve")
async def approve_plan(agent_id: str, task_id: str, body: PlanApproveIn):
    """审批计划：通过 → executing；不通过 → 写意见回 planning 重出（超上限转 failed）。"""
    _agent(agent_id)
    async with get_session_factory()() as db:
        t = await _get_task(db, agent_id, task_id)
        if t.status != "plan_review":
            raise HTTPException(
                status_code=409, detail=f"当前状态 {t.status} 不接受审批（仅 plan_review）"
            )
        if body.approved:
            t.status = "executing"
            t.plan_feedback = None
            logger.info("任务 %s 计划已通过，进入 executing", t.id)
        else:
            t.plan_reject_count += 1
            feedback = (body.feedback or "").strip()
            if t.plan_reject_count > MAX_PLAN_REJECT:
                t.status = "failed"
                t.result_summary = (
                    f"计划连续被打回 {t.plan_reject_count} 次，超出上限 {MAX_PLAN_REJECT}"
                )
                await db.commit()
                await db.refresh(t)
                raise HTTPException(
                    status_code=409,
                    detail=f"打回次数超出上限 {MAX_PLAN_REJECT}，任务已转 failed",
                )
            t.plan_feedback = feedback or "（未填写意见）"
            t.status = "planning"
        await db.commit()
        await db.refresh(t)
        return {"task": _task_out(t, items=await _load_items(db, t))}


@router.post("/{agent_id}/tasks/{task_id}/plan/replan")
async def replan_task(agent_id: str, task_id: str, body: PlanGenerateIn):
    """重新生成计划（对外端点）：与「产出计划」共用同一把任务锁，避免两者并发。

    业务语义见 ``_replan``：任意非终态可触发，升版本、保留 done 点、
    未完成点置 skipped、产出新版本计划后回 plan_review 再审。
    """
    async with _plan_lock(task_id):
        return await _replan(agent_id, task_id, body)


async def _replan(agent_id: str, task_id: str, body: PlanGenerateIn) -> dict:
    """重新生成计划（任意非终态可触发，见 doc 5.7）。

    升版本 → 保留 done 点 → 未完成点置 skipped → 产出新版本计划 → 回 plan_review 再审。
    与「计划打回」的区别：打回只在 plan_review 且**不升版本**。

    事务边界同 ``generate_plan``：跑 LLM 那一段不持有任何事务；所有状态变更
    （replan_count / plan_version / 旧点置 skipped / 写新点 / 渲染 plan_md）
    统一在 LLM 成功后的落盘段一次提交。LLM 失败即整体回滚，
    等价于「这次重新规划没发生过」——replan_count 也不会白涨。
    """
    _agent(agent_id)

    # ---- 第一段：只读校验 + 取索引数据，事务随 with 结束立刻释放 ----
    async with get_session_factory()() as db:
        t = await _get_task(db, agent_id, task_id)
        if t.status not in TASK_ACTIVE_STATUSES:
            raise HTTPException(
                status_code=409, detail=f"终态任务（{t.status}）不能重新规划"
            )
        if not t.session_id:
            raise HTTPException(status_code=400, detail="任务未绑定 session")
        session_id = t.session_id
        items = await _load_items(db, t)
        done_contents = [it.content_md for it in items if it.status == "done"]
        # 新版本的 seq 接在所有旧点之后（旧的 done / skipped 点都留在库里不动）
        start_seq = max([it.seq for it in items] or [0])
        # prompt 纯内存拼装，必须在离开事务前做完（t 是本 session 的对象）
        prompt = _build_plan_prompt(t, done_contents, body.instruction or "")

    # ---- 第二段：跑 LLM，全程不碰数据库事务 ----
    contents = await _ask_for_plan(agent_id, session_id, prompt)

    # ---- 第三段：重新取库，一次性落盘所有状态变更 ----
    async with get_session_factory()() as db:
        t = await _get_task(db, agent_id, task_id)
        if t.status not in TASK_ACTIVE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"重新规划期间任务已进入终态 {t.status}，本次结果未保存",
            )
        t.replan_count += 1
        if t.replan_count > MAX_REPLAN:
            t.status = "failed"
            t.result_summary = (
                f"重新规划 {t.replan_count} 次，超出上限 {MAX_REPLAN}"
            )
            await db.commit()
            await db.refresh(t)
            raise HTTPException(
                status_code=409,
                detail=f"重新规划次数超出上限 {MAX_REPLAN}，任务已转 failed",
            )
        for it in await _load_items(db, t):
            if it.status in ("pending", "doing"):
                it.status = "skipped"
                if not it.conclusion:
                    it.conclusion = "因重新规划中止"
        t.plan_version += 1
        await db.flush()
        await _write_items(db, t, contents, start_seq)
        await db.flush()
        await _refresh_plan_md(db, t)
        t.status = "plan_review"
        t.plan_feedback = None
        await db.commit()
        await db.refresh(t)
        logger.info(
            "任务 %s 已重新规划，plan_version=%s replan_count=%s",
            t.id, t.plan_version, t.replan_count,
        )
        return {"task": _task_out(t, items=await _load_items(db, t))}


# --- 步骤 4：逐点执行（完成判定 / 挂起恢复 / 人工勾选与跳过） -----------------

# 「一轮」= 一次 ask() = 一个完整 ReAct 运行（内部最多 max_steps=30 步）；
# 一个计划点可能要跑好几轮才做完（比如中途 ask_human 追问、或一轮没跑完）。
MAX_ROUNDS_PER_ITEM = 30  # 单个计划点最多对话轮数（风险 R2）
MAX_TOTAL_ROUNDS = 200  # 单个任务所有计划点累计最多对话轮数


async def _pending_human(db, session_id: str) -> Optional[Dict[str, Any]]:
    """会话是否正等用户回答（agent 走了 ask_human）；是则返回挂起信息。

    判定口径与 ``ChatService.load_messages`` 的 pending 一致：react_steps 里
    status=suspended 且 wait_kind=waiting_human。
    """
    pr = await db.execute(
        select(ReactStep)
        .join(SessionMessage, ReactStep.session_message_id == SessionMessage.id)
        .where(
            SessionMessage.chat_session_id == session_id,
            ReactStep.status == "suspended",
            ReactStep.wait_kind == "waiting_human",
        )
        .order_by(ReactStep.step.desc())
    )
    row = pr.scalars().first()
    if row is None:
        return None
    return {"step_id": row.id, "question": row.output}


async def _pending_pause(db, session_id: str) -> Optional[Dict[str, Any]]:
    """会话是否停在「用户暂停」的断点上；是则返回断点信息。

    与 ``_pending_human`` 同构：react_steps 里 status=suspended 且
    wait_kind=paused（由 ``executor._persist_pause_step`` 写入）。
    """
    pr = await db.execute(
        select(ReactStep)
        .join(SessionMessage, ReactStep.session_message_id == SessionMessage.id)
        .where(
            SessionMessage.chat_session_id == session_id,
            ReactStep.status == "suspended",
            ReactStep.wait_kind == "paused",
        )
        .order_by(ReactStep.step.desc())
    )
    row = pr.scalars().first()
    if row is None:
        return None
    return {"step_id": row.id}


def _pause_checker(task_id: str):
    """构造「该停了吗」回调：每跑完一步查一次 tasks.paused（按 step 暂停的开关）。

    用独立会话查询：主流程的 db 开着事务，ReAct 内部也会开自己的会话，
    复用同一个会互相干扰。
    """

    async def _check() -> bool:
        async with get_session_factory()() as db2:
            row = await db2.get(Task, task_id)
            return bool(row.paused) if row is not None else False

    return _check


async def _current_item(db, t: Task):
    """当前该执行的计划点：只看**当前版本**里未完成的，按 seq 取最前一个。

    顺序语义：一次只推一个点——后续点可能依赖前序结果。
    """
    r = await db.execute(
        select(TaskItem)
        .where(
            TaskItem.task_id == t.id,
            TaskItem.plan_version == t.plan_version,
            TaskItem.status.in_(["pending", "doing"]),
        )
        .order_by(TaskItem.seq)
        .execution_options(populate_existing=True)
    )
    return r.scalars().first()


# 给当前步骤参考的「前序已完成步骤」条数上限。已完成步骤只摘结论与产物，
# 且只取最近几条——把十几条原始步骤描述整段复述既冗长，又容易让模型以为要重做。
DONE_CTX_LIMIT = 5


def _item_prompt(
    t: Task, item: TaskItem, done_items: list, remind_mark_done: bool = False
) -> str:
    """拼「执行这一个计划点」的提示词。

    ``done_items`` 是已完成的计划点对象。这里只摘它们的**结论与产出文件**作为
    上下文——那些步骤已经做完了，把它们"要做什么"的描述再复述一遍没有意义，
    模型真正需要知道的是"已经得到了什么"。

    ``remind_mark_done``：本次是**同一个点的第 2 轮或更晚**（``rounds > 0``）却仍
    没登记完成时置 True。模型偶尔会「用自然语言宣布做完了」而忘了调用
    ``task.mark_item_done``——框架于是以为这点还没完，前端会再推一轮，同一个点
    被重复做一遍（实测：小游戏任务的第 1、2 步各做了两遍，第 1 轮只写文件、
    说完就停，第 2 轮才补登记）。所以续跑时把这条要求单独拎出来强调一遍。
    """
    parts = [
        f"你现在要完成任务里的**第 {item.seq} 步**（只做这一步，不要顺手做后面的）：",
        "",
        item.content_md,
        "",
        "背景任务：",
        t.description_md or t.title,
    ]
    if done_items:
        parts += ["", "前序步骤已经做完（**不要重做**，直接基于它们的成果继续）："]
        for i in done_items[-DONE_CTX_LIMIT:]:
            line = f"- [x] 第 {i.seq} 步：{i.content_md}"
            extra = []
            if i.conclusion:
                extra.append(f"成果：{i.conclusion}")
            if i.artifacts:
                try:
                    arts = json.loads(i.artifacts) or []
                except Exception:
                    arts = []
                paths = [
                    a.get("path") or a.get("name")
                    for a in arts
                    if isinstance(a, dict)
                ]
                paths = [str(p) for p in paths if p]
                if paths:
                    extra.append("产出文件：" + "、".join(paths))
            if extra:
                line += "\n    " + "；".join(extra)
            parts.append(line)
        parts += [
            "",
            "  （需要查看已有文件内容时用 fs.read_file / fs.list_dir，不要凭记忆重写。）",
        ]
    parts += [
        "",
        "要求：",
        "1. 只专注完成当前这一步；",
        "2. 这一步要产出代码 / 文档 / 文件时：**必须先用 fs.write_file 写成文件，"
        "再用 fs.publish 挂出**，让用户能直接预览、下载、复制。"
        "若任务描述写了「输出完整代码 / 可直接运行的代码」，那是指**产出这个文件**"
        "——把代码写进文件并挂出即可，**严禁**把完整代码粘贴到回复正文里，"
        "回复里只说明写了哪个文件、怎么运行；",
        "3. 确认做完后**必须调用 task.mark_item_done**，并给出 conclusion（结论）；"
        "artifacts **只登记要交付给用户的最终文件**——你自己写来验证的临时测试脚本、"
        "调试脚本、补丁脚本（如 check.py、test_*.js、patch_*.py、extract_*.py）"
        "**不要登记**，它们留在工作区即可，登记了只会挤占「最终产物」列表；",
        "4. **临时文件（测试脚本、调试脚本、抽取脚本等）一律用 `@tmp/` 前缀写**，"
        "如 `@tmp/check.py`、`@tmp/test_a.js` —— 它落在本会话私有的临时目录，"
        "不会把用户工作区弄脏。**只有要交付给用户的文件才直接写在工作区里**"
        "（fs.write_file 的回执会告诉你临时文件的真实绝对路径，"
        "bash 命令里请用那个绝对路径）；",
        "5. 缺少必要信息（如预算、偏好）无法继续时，提问问用户，不要瞎猜；",
        "6. 只做这一步，做完即停，不要顺带做后面的步骤。",
    ]
    if remind_mark_done:
        parts += [
            "",
            f"**⚠ 重要：这是第 {item.seq} 步的第 {item.rounds + 1} 次执行——上一轮你"
            f"没有调用 task.mark_item_done 登记完成。**",
            "本轮做完时**必须**调用 `task.mark_item_done`（给出 conclusion、有产物"
            "就用 artifacts 登记）。不调用的话，这一步会被当作「没做完」而重复执行，"
            "白白重做一遍。",
        ]
    return "\n".join(parts)


async def _drive_item(
    agent_id: str,
    t: Task,
    item: TaskItem,
    rounds: int,
    db,
    on_step=None,
    on_delta=None,
    should_stop=None,
) -> str:
    """对某计划点连续推进若干轮对话。

    返回结束原因：``done``（该点已完成）/ ``waiting``（等人输入）/ ``paused``
    （被人叫停）/ ``running``（轮数用尽但仍未完成）/ ``limit_item``
    / ``limit_total`` / ``error``。

    按 step 暂停：每轮都把 ``_pause_checker`` 传进 ReAct，它每跑完一步查一次
    ``tasks.paused``，命中就停在该步之后（该步已实时落库，未开始的下一步不跑）。
    下次执行时若发现会话里留着暂停断点，就走 ``resume_paused`` 重放已完成的步骤
    继续，而不是另起一轮——已做完的工作不会重做。

    on_step / on_delta / should_stop：流式执行（SSE）用——逐步 / 逐字推送过程，
    并在下一个检查点响应「停止生成」。不传时行为与之前完全一致。
    """
    # 「执行」即继续：先把暂停开关清掉，否则续跑的第一步又会被立刻叫停
    if t.paused:
        t.paused = False
        await db.commit()

    # 停止判定：任务自带的「暂停开关」+ 外部（流式）的中止请求，任一命中即停
    pause_check = _pause_checker(t.id)
    if should_stop is None:
        should_stop = pause_check
    else:
        # 先把外部传入的中止回调存到别的名字：_combined 是闭包，捕获的是变量
        # should_stop（按引用）；下方会把 should_stop 重绑成 _combined 自己，若直接
        # 写 await should_stop() 就会自递归（maximum recursion depth exceeded）。
        external_stop = should_stop

        async def _combined() -> bool:
            return await pause_check() or await external_stop()

        should_stop = _combined

    for _ in range(max(1, rounds)):
        fresh = await db.get(TaskItem, item.id)
        if fresh is None:
            return "error"
        if fresh.status == "done":
            return "done"
        if fresh.status == "skipped":
            return "skipped"
        if fresh.rounds >= MAX_ROUNDS_PER_ITEM:
            return "limit_item"
        if sum(i.rounds for i in await _load_items(db, t)) >= MAX_TOTAL_ROUNDS:
            return "limit_total"

        if fresh.status == "pending":
            fresh.status = "doing"
            if fresh.started_at is None:
                fresh.started_at = datetime.now(timezone.utc)
        await db.commit()

        # 标记当前计划点：task.mark_item_done 工具靠它知道该勾掉哪一步
        set_task_item(fresh.id)
        try:
            # 上一轮被人暂停：从断点重放续跑，不另起一轮
            pend_pause = await _pending_pause(db, t.session_id)
            if pend_pause is not None:
                resp = await ChatService(
                    agent_id=agent_id, session_id=t.session_id
                ).resume_paused(
                    pend_pause["step_id"],
                    should_stop=should_stop,
                    on_step=on_step,
                    on_delta=on_delta,
                )
            else:
                done_items = [
                    i for i in await _load_items(db, t) if i.status == "done"
                ]
                # rounds > 0 = 这个点已经跑过至少一轮却仍未登记 → 续跑，加重提醒
                prompt = _item_prompt(
                    t, fresh, done_items, remind_mark_done=fresh.rounds > 0
                )
                resp = await ChatService(
                    agent_id=agent_id, session_id=t.session_id
                ).ask(
                    prompt,
                    should_stop=should_stop,
                    on_step=on_step,
                    on_delta=on_delta,
                )
        finally:
            set_task_item(None)

        # 关键：工具（task.mark_item_done）是**通过自己的会话**写库的，
        # 而本会话 expire_on_commit=False —— 不显式重读就会拿到身份映射里的旧值，
        # 把「已经做完」误判成「还没做完」。
        # （不能用 expire_all：异步会话不支持过期后的惰性加载，会抛 MissingGreenlet）
        await db.refresh(fresh)

        fresh.rounds += 1
        await db.commit()

        if resp.get("error"):
            return "error"
        if resp.get("paused"):
            # 被人叫停：已完成的步骤都在库里，任务保持 executing，可随时「执行」续跑
            return "paused"
        if fresh.status == "done":
            return "done"
        if fresh.status == "skipped":
            return "skipped"
        if await _pending_human(db, t.session_id):
            return "waiting"
    return "running"


async def _finalize(db, t: Task, reason: str) -> None:
    """按推进结果收敛任务状态（只 commit，不 refresh）。"""
    if reason == "waiting":
        t.status = "waiting_input"
    elif reason in ("limit_item", "limit_total"):
        t.status = "failed"
        t.result_summary = f"执行超出轮数上限（{reason}）"
    elif reason == "error":
        t.status = "failed"
        t.result_summary = "执行中 agent 返回错误"
    elif reason in ("done", "skipped"):
        rest = await _current_item(db, t)
        t.status = "waiting_review" if rest is None else "executing"
    await db.commit()


class ExecuteIn(BaseModel):
    """推进执行：一次最多跑几轮对话（默认 1）。"""

    max_rounds: int = 1
    # 流式执行时带上：用于「停止生成」（与 chat/stream 同一套 run_id 机制）
    run_id: str = ""


class AnswerIn(BaseModel):
    """补充输入：回答 agent 的挂起提问。"""

    answer: str
    # 同 ExecuteIn.run_id：流式回答时可被中止
    run_id: str = ""


class ItemPatchIn(BaseModel):
    """人工改计划点：勾选完成 / 退回待做 / 跳过。"""

    status: Optional[str] = None
    conclusion: Optional[str] = None


@router.post("/{agent_id}/tasks/{task_id}/execute")
async def execute_task(agent_id: str, task_id: str, body: ExecuteIn):
    """推进任务：对当前计划点跑若干轮对话，直到该点完成 / 等人输入 / 触顶。

    一次调用最多跑 ``max_rounds`` 轮（默认 1）——执行是长耗时动作，交给调用方
    按次推进：既避免单次请求超时，也方便前端逐步展示「正在做第几步」。
    """
    _agent(agent_id)
    async with get_session_factory()() as db:
        t = await _get_task(db, agent_id, task_id)
        if t.status != "executing":
            raise HTTPException(
                status_code=409, detail=f"当前状态 {t.status} 不执行（仅 executing）"
            )
        item = await _current_item(db, t)
        if item is None:
            t.status = "waiting_review"
            await db.commit()
            await db.refresh(t)
            return {
                "task": _task_out(t, items=await _load_items(db, t)),
                "result": "all_done",
            }
        reason = await _drive_item(agent_id, t, item, body.max_rounds, db)
        await _finalize(db, t, reason)
        # 勾选态回写 plan_md 视图（表是事实来源，这里只同步渲染结果）
        await _refresh_plan_md(db, t)
        await db.commit()
        await db.refresh(t)
        return {"task": _task_out(t, items=await _load_items(db, t)), "result": reason}


@router.post("/{agent_id}/tasks/{task_id}/execute/stream")
async def execute_task_stream(agent_id: str, task_id: str, body: ExecuteIn):
    """流式推进任务：语义同 ``/execute``，但把每一步 ReAct 与最终回答逐块推给前端。

    任务执行步骤多、耗时长，是最需要「看着它做」的场景；这里与 chat/stream 用
    同一套帧协议（step / delta / done / error），前端渲染逻辑可以复用。
    """
    _agent(agent_id)

    async def factory(on_step, on_delta, should_stop) -> Dict[str, Any]:
        async with get_session_factory()() as db:
            t = await _get_task(db, agent_id, task_id)
            if t.status != "executing":
                raise HTTPException(
                    status_code=409, detail=f"当前状态 {t.status} 不执行（仅 executing）"
                )
            item = await _current_item(db, t)
            if item is None:
                t.status = "waiting_review"
                await db.commit()
                await db.refresh(t)
                return {
                    "task": _task_out(t, items=await _load_items(db, t)),
                    "result": "all_done",
                }
            reason = await _drive_item(
                agent_id,
                t,
                item,
                body.max_rounds,
                db,
                on_step=on_step,
                on_delta=on_delta,
                should_stop=should_stop,
            )
            await _finalize(db, t, reason)
            # 勾选态回写 plan_md 视图（表是事实来源，这里只同步渲染结果）
            await _refresh_plan_md(db, t)
            await db.commit()
            await db.refresh(t)
            return {
                "task": _task_out(t, items=await _load_items(db, t)),
                "result": reason,
            }

    return _sse_stream(factory, body.run_id or "")


class PauseIn(BaseModel):
    """暂停开关：True 暂停（默认），False 取消暂停。"""

    paused: bool = True


@router.post("/{agent_id}/tasks/{task_id}/pause")
async def pause_task(agent_id: str, task_id: str, body: PauseIn):
    """暂停 / 取消暂停执行：置 ``tasks.paused``，ReAct 每跑完一步都会检查它。

    暂停粒度是**按 step** 的：命中时当前这一步已经跑完并落库，未开始的下一步
    直接不跑，所以「执行 → 暂停 → 执行」能从断点无缝续跑，已完成的步骤不重做。
    """
    _agent(agent_id)
    async with get_session_factory()() as db:
        t = await _get_task(db, agent_id, task_id)
        t.paused = bool(body.paused)
        await db.commit()
        await db.refresh(t)
        return {
            "task": _task_out(t, items=await _load_items(db, t)),
            "paused": t.paused,
        }


async def _answer_task_flow(
    agent_id: str,
    task_id: str,
    text: str,
    db,
    on_step=None,
    on_delta=None,
    should_stop=None,
) -> Dict[str, Any]:
    """补充输入的完整流程（同步 / 流式两个端点共用）：恢复挂起那一步并继续推进。

    on_step / on_delta / should_stop 仅流式版传：逐步 / 逐字推送，并支持中止。
    """
    t = await _get_task(db, agent_id, task_id)
    if t.status != "waiting_input":
        raise HTTPException(
            status_code=409,
            detail=f"当前状态 {t.status} 不接受补充（仅 waiting_input）",
        )
    pend = await _pending_human(db, t.session_id)
    if pend is None:
        # 不一致（挂起步骤已被回答过）：直接回到 executing，交给 execute 推进
        t.status = "executing"
        await db.commit()
        await db.refresh(t)
        return {
            "task": _task_out(t, items=await _load_items(db, t)),
            "result": "resumed",
        }
    item = await _current_item(db, t)
    if item is not None and item.status == "pending":
        item.status = "doing"
        if item.started_at is None:
            item.started_at = datetime.now(timezone.utc)
        await db.commit()
    set_task_item(item.id if item is not None else None)
    try:
        resp = await ChatService(agent_id=agent_id, session_id=t.session_id).ask(
            text,
            step_id=pend["step_id"],
            should_stop=should_stop,
            on_step=on_step,
            on_delta=on_delta,
        )
    finally:
        set_task_item(None)

    if item is not None:
        # 同上：工具可能已在自己的会话里把当前点标记完成，显式重读再判定
        await db.refresh(item)
        item.rounds += 1
        await db.commit()
        if item.status == "done":
            await _finalize(db, t, "done")
        elif await _pending_human(db, t.session_id):
            await _finalize(db, t, "waiting")
        else:
            t.status = "executing"
            await db.commit()
    else:
        t.status = "executing"
        await db.commit()
    await _refresh_plan_md(db, t)
    await db.commit()
    await db.refresh(t)
    return {
        "task": _task_out(t, items=await _load_items(db, t)),
        "result": "resumed",
        "error": resp.get("error"),
    }


@router.post("/{agent_id}/tasks/{task_id}/answer")
async def answer_task(agent_id: str, task_id: str, body: AnswerIn):
    """补充输入：回答 agent 的挂起提问，并在同一 session 里**接着那一步继续**。

    挂起是会话级机制（ask_human + resume），所以这里走
    ``ChatService.ask(step_id=...)``——恢复后不是从头重跑。
    """
    _agent(agent_id)
    text = (body.answer or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="补充内容不能为空")
    async with get_session_factory()() as db:
        return await _answer_task_flow(agent_id, task_id, text, db)


@router.post("/{agent_id}/tasks/{task_id}/answer/stream")
async def answer_task_stream(agent_id: str, task_id: str, body: AnswerIn):
    """流式补充输入：语义同 ``/answer``，恢复过程逐步 / 逐字推给前端。"""
    _agent(agent_id)
    text = (body.answer or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="补充内容不能为空")

    async def factory(on_step, on_delta, should_stop) -> Dict[str, Any]:
        async with get_session_factory()() as db:
            return await _answer_task_flow(
                agent_id,
                task_id,
                text,
                db,
                on_step=on_step,
                on_delta=on_delta,
                should_stop=should_stop,
            )

    return _sse_stream(factory, body.run_id or "")


@router.patch("/{agent_id}/tasks/{task_id}/items/{item_id}")
async def patch_task_item(
    agent_id: str, task_id: str, item_id: str, body: ItemPatchIn
):
    """人工改计划点：勾选完成 / 退回待做 / 跳过（skipped 已纳入 MVP，见风险 R7）。

    改完会重渲染 ``plan_md``，所以「勾选」在计划视图上立刻可见。
    """
    _agent(agent_id)
    allowed = {"pending", "doing", "done", "skipped"}
    async with get_session_factory()() as db:
        t = await _get_task(db, agent_id, task_id)
        it = await db.get(TaskItem, item_id)
        if it is None or it.task_id != t.id:
            raise HTTPException(status_code=404, detail=f"计划点不存在: {item_id}")
        if body.status is not None:
            if body.status not in allowed:
                raise HTTPException(status_code=400, detail=f"状态非法：{body.status}")
            it.status = body.status
            if body.status == "done" and it.finished_at is None:
                it.finished_at = datetime.now(timezone.utc)
        if body.conclusion is not None:
            it.conclusion = body.conclusion
        await db.commit()
        await _refresh_plan_md(db, t)
        await db.commit()
        await db.refresh(t)
        return {"task": _task_out(t, items=await _load_items(db, t))}


class TaskReviewIn(BaseModel):
    """验收输入：approve 通过；reject 打回（建议给 feedback）。"""

    action: str  # "approve" | "reject"
    result_summary: Optional[str] = None
    feedback: Optional[str] = None


@router.post("/{agent_id}/tasks/{task_id}/review")
async def review_task(agent_id: str, task_id: str, body: TaskReviewIn):
    """验收闭环：通过 -> done；打回 -> 带反馈回 executing，在同一 session 继续修。

    仅 waiting_review 态可验收。打回次数超上限转 failed（见 doc 第七节）。
    """
    _agent(agent_id)
    async with get_session_factory()() as db:
        t = await _get_task(db, agent_id, task_id)
        if t.status != "waiting_review":
            raise HTTPException(
                status_code=409,
                detail=f"当前状态 {t.status} 不能验收（仅 waiting_review）",
            )

        if body.action == "approve":
            if body.result_summary is not None:
                t.result_summary = body.result_summary or None
            t.review_feedback = body.feedback or None
            t.review_reject_count = 0
            t.status = "done"
            await db.commit()
            await db.refresh(t)
            logger.info("任务 %s 验收通过，进入 done", t.id)
            return {"task": _task_out(t, items=await _load_items(db, t))}

        # 打回：带反馈回 executing，把反馈发回同一 session 让 agent 继续修
        t.review_reject_count = (t.review_reject_count or 0) + 1
        if t.review_reject_count > MAX_REVIEW_REJECT:
            t.status = "failed"
            t.review_feedback = body.feedback or None
            t.result_summary = (
                f"验收打回 {t.review_reject_count} 次，超出上限 {MAX_REVIEW_REJECT}"
            )
            await db.commit()
            await db.refresh(t)
            raise HTTPException(
                status_code=409,
                detail=f"验收打回次数超出上限 {MAX_REVIEW_REJECT}，任务已转 failed",
            )
        t.review_feedback = body.feedback or None
        t.status = "executing"
        await db.commit()
        await db.refresh(t)

        feedback = (body.feedback or "验收未通过，请根据反馈意见继续修改。").strip()
        last_answer = ""
        waiting_human = False
        try:
            resp = await ChatService(agent_id=agent_id, session_id=t.session_id).ask(
                feedback
            )
            last_answer = resp.get("answer", "") or ""
            waiting_human = bool(resp.get("waiting_human", False))
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "验收打回后驱动会话失败（任务已转 executing，可手动继续）：%s", e
            )
        return {
            "task": _task_out(t, items=await _load_items(db, t)),
            "last_answer": last_answer,
            "waiting_human": waiting_human,
        }
