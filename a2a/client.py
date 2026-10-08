"""A2A 客户端：调用对端的 A2A JSON-RPC 端点（v1.0.0）。

用法：
    client = A2AClient("http://peer:8080/a2a", headers={"Authorization": "Bearer x"})
    task = await client.send_message({...})
    task = await client.wait_for_terminal(task)   # 同步等终态（长任务）
    card = await client.fetch_agent_card()         # 发现对端能力（GET /.well-known/agent.json）
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

from .server.models import AgentCard
from .settings import a2a_cfg

logger = logging.getLogger(__name__)

# wait_for_terminal 超时时打在 task 上的标记：上层据此给出「对端超时」的明确文案，
# 而不是含糊的「结束于 SUBMITTED」。下划线前缀，避免与 A2A 协议字段撞名。
TIMEOUT_FLAG = "_timed_out"

# Task 终态集合（不再需要轮询）
_TERMINAL = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_AUTH_REQUIRED",
}

# 不是终态，但**再等也没用**：对端在等我们补充信息，必须立刻返回给上层处理。
# 不把它算进来，wait_for_terminal 会对着一个 INPUT_REQUIRED 的 task 轮询到超时
# （默认 600 秒），表现为「疯狂刷同一条请求 + 半天不返回」。
_NEEDS_INPUT = {"TASK_STATE_INPUT_REQUIRED"}


class A2AClient:
    def __init__(
        self,
        base_url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        trust_env: bool = False,
    ) -> None:
        url = base_url.rstrip("/")
        if not url.endswith("/a2a"):
            url += "/a2a"
        self.endpoint = url
        self.base_url = url.rsplit("/a2a", 1)[0]  # 去尾得到对端 base，用于拉 AgentCard
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        # 不传就取配置（a2a.request_timeout）：默认 15s 而不是旧的 600s——
        # 单次 RPC 是「询问/投递」，不该让一个 HTTP 请求挂十分钟。
        self.timeout = (
            float(timeout) if timeout is not None
            else float(getattr(a2a_cfg(), "request_timeout", 15) or 15)
        )
        self.trust_env = trust_env

    def _client_kwargs(self) -> Dict[str, Any]:
        """httpx 客户端的公共参数。

        ``trust_env=False``：**不走系统代理**。对端常常是 ``localhost`` 或内网地址，
        而 httpx 默认会读 HTTP(S)_PROXY 环境变量，把 ``localhost:8080`` 也送去代理，
        代理连不上就返回 **502 Bad Gateway**。需要代理访问外部对端时显式传
        ``trust_env=True``。
        """
        return {"timeout": self.timeout, "trust_env": self.trust_env}

    async def _rpc(self, method: str, params: Any) -> Dict[str, Any]:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with httpx.AsyncClient(**self._client_kwargs()) as c:
            r = await c.post(self.endpoint, json=payload, headers=self.headers)
            r.raise_for_status()
            data = r.json()
        if data.get("error"):
            raise RuntimeError(f"A2A {method} error: {data['error']}")
        return data.get("result")

    async def send_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        return await self._rpc("SendMessage", message)

    async def get_task(self, task_id: str) -> Dict[str, Any]:
        return await self._rpc("GetTask", {"taskId": task_id})

    async def cancel_task(self, task_id: str) -> Dict[str, Any]:
        return await self._rpc("CancelTask", {"taskId": task_id})

    async def wait_for_terminal(
        self,
        task: Dict[str, Any],
        *,
        timeout: Optional[float] = None,
        interval: Optional[float] = None,
        cancel_on_timeout: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """同步等待 Task 进入终态（长任务）。

        超时不再「默默返回当前态」：按配置（``a2a.cancel_on_timeout``，默认开）
        主动 CancelTask，并在返回的 task 上打 ``_timed_out`` 标记——对端已经
        不回我们了，让它继续跑只是白烧对端的 token。
        """
        cfg = a2a_cfg()
        if timeout is None:
            timeout = float(getattr(cfg, "task_timeout", 120) or 0)
        if interval is None:
            interval = float(getattr(cfg, "poll_interval", 3) or 3)
        if cancel_on_timeout is None:
            cancel_on_timeout = bool(getattr(cfg, "cancel_on_timeout", True))

        state = (task.get("status") or {}).get("state")
        if state in _TERMINAL or state in _NEEDS_INPUT:
            return task
        task_id = task.get("id")
        if not task_id:
            return task
        if timeout <= 0:  # 0 = 不限时（由调用方显式承担风险）
            timeout = float("inf")
        waited = 0.0
        logger.info("等待对端 task %s 进入终态（最长 %.0fs）", task_id, timeout)
        while waited < timeout:
            await asyncio.sleep(interval)
            waited += interval
            try:
                task = await self.get_task(task_id)
            except Exception as e:  # 查状态失败：记日志继续等，别整轮失败
                logger.warning("查询对端 task %s 状态失败: %s", task_id, e)
                continue
            state = (task.get("status") or {}).get("state")
            if state in _TERMINAL or state in _NEEDS_INPUT:
                return task
        logger.warning(
            "对端 task %s 等待 %.0fs 仍未进入终态（当前 %s）", task_id, waited, state,
        )
        if cancel_on_timeout:
            try:
                await self.cancel_task(task_id)
                logger.info("已取消超时的对端 task %s", task_id)
            except Exception as e:  # 取消失败不影响主流程：至少我们不等了
                logger.debug("取消对端 task %s 失败: %s", task_id, e)
        task = dict(task)
        task[TIMEOUT_FLAG] = True
        return task

    async def fetch_agent_card(self) -> AgentCard:
        """拉对端 AgentCard（GET /.well-known/agent.json），用于发现其 skills 与 A2A 端点。"""
        async with httpx.AsyncClient(**self._client_kwargs()) as c:
            r = await c.get(
                self.base_url + "/.well-known/agent.json",
                headers={"Accept": "application/json", **self.headers},
            )
            r.raise_for_status()
            return AgentCard(**r.json())
