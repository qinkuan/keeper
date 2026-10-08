"""A2A 业务 handler：桥接现有 ``ChatService``（keeper 内核）。

映射关系：
- ``SendMessage``（user 消息）→ ``ChatService.ask`` → ``Task``
    - 正常答完        → ``COMPLETED``，答案进 ``artifacts``
    - 模型反问        → ``INPUT_REQUIRED``，问题进 ``status.message``
    - 对端带着 taskId 回来回答反问 → 用记录的 ``ask_step_id`` 恢复挂起步骤
- ``GetTask``   → 按 taskId 返回 Task
- ``CancelTask``→ 取消（终态前）

``contextId`` 直接复用为 keeper 的 ``session_id``（``upsert=True`` 容错跨端对齐）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ...chat.context import set_agent
from ...store.models import new_id
from .models import Artifact, Message, Part, Role, TaskState
from .task_manager import TaskManager


def _extract_text(message: Dict[str, Any]) -> str:
    parts = (message or {}).get("parts") or []
    texts = [
        p.get("text", "")
        for p in parts
        if isinstance(p, dict) and p.get("text")
    ]
    return "\n".join(texts).strip()


def _agent_message(task_id: str, context_id: Optional[str], text: str) -> Message:
    return Message(
        messageId=new_id(),
        role=Role.ROLE_AGENT,
        parts=[Part(text=text)],
        contextId=context_id,
        taskId=task_id,
    )


async def send_message(params: Any, *, agent: Any, tm: TaskManager) -> Dict[str, Any]:
    # params 可能是 Message 本身，也可能是 {message, configuration}
    if isinstance(params, dict) and "message" in params:
        incoming = params["message"]
    else:
        incoming = params
    if not isinstance(incoming, dict):
        raise ValueError("SendMessage 需要 message 参数")

    context_id = incoming.get("contextId")
    task_id = incoming.get("taskId")
    text = _extract_text(incoming)
    # 标记所属 agent：入站不经 HTTP /agents/{id} 端点，日志要靠这里带上它
    set_agent(getattr(agent, "agent_id", None), getattr(agent, "name", None))

    # 定位已有 task（对端回来继续 INPUT_REQUIRED 时带 taskId）。
    # 注意：收到旧 taskId 且对端在等输入 → 恢复旧任务续跑，这是 A2A resume 的正常
    # 语义，无需改动；真正要修的是「本地发新提问时不应带上旧 taskId」（见 peer/service.py）。
    ask_step_id: Optional[str] = None
    if task_id and (t := await tm.get(task_id)):
        context_id = context_id or t.contextId
        ask_step_id = (t.metadata or {}).get("ask_step_id")

    # ChatService 负责会话/落库；contextId 通过 context_id 解析内部 session
    # （协议 context 与 session 主键解耦，不再把 contextId 直接当 session_id）。
    from ...chat.service import ChatService

    chat = ChatService(
        context_id=context_id,
        upsert=True,
        # 关键：必须把 URL 指定的 agent 传下去。不传时 ChatService 内部
        # get_keeper(None) 返回 None → ask() 直接回「agent 实例未就绪」占位文本，
        # 入站请求就永远落不到 /agents/{agent_id}/a2a 指的那个 agent 上。
        agent_id=getattr(agent, "agent_id", None),
    )
    result = await chat.ask(text, step_id=ask_step_id)

    session_id = result["session_id"]
    cid = context_id or session_id
    tid = task_id or new_id()

    if await tm.get(tid) is None:
        await tm.create(tid, cid, state=TaskState.TASK_STATE_SUBMITTED)

    waiting = bool(result.get("waiting_human"))
    if waiting:
        # 反思问：问题文本在 result["answer"]（keeper 把 ask 文本放 answer）
        status_msg = _agent_message(tid, cid, result["answer"])
        await tm.set_state(tid, TaskState.TASK_STATE_INPUT_REQUIRED, message=status_msg)
    else:
        await tm.add_artifact(tid, Artifact(parts=[Part(text=result["answer"])]))
        await tm.set_state(tid, TaskState.TASK_STATE_COMPLETED)

    # 指回本地 session + 挂起步骤（恢复用），统一持久化
    await tm.set_metadata(
        tid,
        {
            "ask_step_id": result.get("step_id") if waiting else None,
            "session_id": session_id,
        },
    )

    # 记往来历史（user 来 / agent 回）
    await tm.append_history(
        tid,
        Message(
            messageId=incoming.get("messageId", new_id()),
            role=Role.ROLE_USER,
            parts=[Part(text=text)],
            contextId=cid,
            taskId=tid,
        ),
    )
    await tm.append_history(tid, _agent_message(tid, cid, result["answer"]))

    task = await tm.get(tid)
    return task.model_dump(by_alias=True, exclude_none=True)


async def get_task(params: Any, *, agent: Any, tm: TaskManager) -> Dict[str, Any]:
    if not isinstance(params, dict) or not params.get("taskId"):
        raise ValueError("GetTask 需要 taskId")
    task = await tm.get(params["taskId"])
    if task is None:
        raise KeyError(params["taskId"])
    return task.model_dump(by_alias=True, exclude_none=True)


async def cancel_task(params: Any, *, agent: Any, tm: TaskManager) -> Dict[str, Any]:
    if not isinstance(params, dict) or not params.get("taskId"):
        raise ValueError("CancelTask 需要 taskId")
    task_id = params["taskId"]
    if await tm.get(task_id) is None:
        raise KeyError(task_id)
    task = await tm.cancel(task_id)
    return task.model_dump(by_alias=True, exclude_none=True)


METHODS = {
    "SendMessage": send_message,
    "GetTask": get_task,
    "CancelTask": cancel_task,
}
