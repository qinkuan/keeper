"""A2A server：暴露 JSON-RPC 端点 + AgentCard。

替代旧的 ``server/mcp``（对端改经 A2A 协议接入）。``main.py`` 通过 ``build_a2a_app``
把本 app 挂载到 ``/a2a``，AgentCard 暴露为 ``/.well-known/agent.json``。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, FastAPI, HTTPException, Request

from .handler import METHODS
from .models import (
    AgentCard,
    AgentCapabilities,
    AgentInterface,
    AgentSkill,
)
from .rpc import handle as rpc_handle
from .task_manager import TaskManager


def build_agent_card(agent: Any, base_url: str) -> AgentCard:
    """由 agent 生成 A2A AgentCard。

    - ``name`` 取 agent 名
    - ``description`` 取 **agent 简介**（面向外部的描述性文本）。刻意**不用
      persona**：那是内部提示词，外泄既暴露内部规则、也放大提示词注入面。
      简介足够让对方知道这个 agent 是干嘛的。
    - ``skills`` 取已注册 skill 的名与 system_prompt 摘要；**一个 skill 都没有时
      兜底一个「对话」skill**——否则对方拉到的卡片 skills 为空，只能生成无语义的
      send 工具，模型判断不出该不该调它。
    - capabilities 暂关 streaming / pushNotifications（后续可开）
    """
    desc = (
        getattr(agent, "description", None)
        or getattr(getattr(agent, "config", None), "description", "")
        or ""
    )
    skills = [
        AgentSkill(
            id=s.name,
            name=s.name,
            description=(s.system_prompt or s.name)[:300],
        )
        for s in agent.config.skill_registry.all()
    ]
    if not skills:
        skills = [
            AgentSkill(
                id="send",
                name="send",
                description=desc[:300] or f"与 {agent.name} 对话",
            )
        ]
    return AgentCard(
        name=agent.name,
        description=desc or f"A2A agent '{agent.name}'.",
        supportedInterfaces=[
            AgentInterface(
                url=f"{base_url}/a2a",
                protocolBinding="a2a-jsonrpc",
                protocolVersion="1.0.0",
            )
        ],
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=False, pushNotifications=False),
        defaultInputModes=["text/plain"],
        defaultOutputModes=["text/plain"],
        skills=skills,
    )


def build_a2a_app(agent: Any) -> FastAPI:
    """构建 A2A 子应用（FastAPI），由宿主 mount 到 /a2a。"""
    tm = TaskManager()

    app = FastAPI(title=f"A2A[{agent.name}]")

    @app.post("/a2a")
    async def rpc_endpoint(request: Request):
        body = await request.json()

        async def dispatch(method: str, params: Any) -> Any:
            fn = METHODS.get(method)
            if fn is None:
                raise NotImplementedError(method)
            return await fn(params, agent=agent, tm=tm)

        if isinstance(body, list):  # 批量请求
            return [await rpc_handle(item, dispatch) for item in body]
        return await rpc_handle(body, dispatch)

    @app.get("/.well-known/agent.json")
    async def agent_card(request: Request):
        base = str(request.base_url).rstrip("/")
        return build_agent_card(agent, base).model_dump(by_alias=True, exclude_none=True)

    return app


def build_a2a_router() -> APIRouter:
    """构建**按 agent 路由**的 A2A 入站端点（单进程多 agent 场景）。

    与 ``build_a2a_app(agent)`` 的区别：后者在**构建期**把一个 agent 闭包进子应用，
    一个进程只能有一个 A2A 端点；本函数注册的路由在**请求期**按路径里的
    ``agent_id`` 取实例，因此：

    - 进程里装了 N 个 agent 就有 N 个 A2A 端点；
    - 运行时新装载的 agent 自动拥有端点，无需重建 app。

    端点：
    - JSON-RPC：``POST /agents/{agent_id}/a2a``
    - AgentCard：``GET  /agents/{agent_id}/.well-known/agent.json``

    AgentCard 的 ``supportedInterfaces.url`` 会指向自己的 ``/a2a`` 端点，正好与
    ``A2AClient`` 的拼法一致（client 会把 url 去尾后拼 ``/.well-known/agent.json``）。
    """
    from ...agent.keeper import get_keeper

    router = APIRouter()

    async def _loaded(agent_id: str) -> Any:
        agent = get_keeper(agent_id)
        if agent is None:
            raise HTTPException(
                status_code=404, detail=f"agent 不存在或未就绪: {agent_id}"
            )
        return agent

    @router.post("/agents/{agent_id}/a2a")
    async def rpc_endpoint(agent_id: str, request: Request) -> Any:
        agent = await _loaded(agent_id)
        tm = TaskManager()
        body = await request.json()

        async def dispatch(method: str, params: Any) -> Any:
            fn = METHODS.get(method)
            if fn is None:
                raise NotImplementedError(method)
            return await fn(params, agent=agent, tm=tm)

        if isinstance(body, list):  # 批量请求
            return [await rpc_handle(item, dispatch) for item in body]
        return await rpc_handle(body, dispatch)

    @router.get("/agents/{agent_id}/.well-known/agent.json")
    async def agent_card(agent_id: str, request: Request) -> Dict[str, Any]:
        agent = await _loaded(agent_id)
        base = str(request.base_url).rstrip("/")
        # base_url 传 "{base}/agents/{agent_id}"，卡片里即 "{base}/agents/{agent_id}/a2a"
        return build_agent_card(agent, f"{base}/agents/{agent_id}").model_dump(
            by_alias=True, exclude_none=True
        )

    return router


__all__ = ["build_a2a_app", "build_a2a_router", "build_agent_card"]
