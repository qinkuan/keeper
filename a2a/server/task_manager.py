"""A2A Task 持久化管理器（替代原进程内字典）。

设计定位：Task 是**协议门面**，不重复承载 ReAct 执行（ReAct 仍在 chat_sessions /
react_steps）。本表只存协议态——state / artifacts / 挂起问题 / history 摘要 /
指回本地 session 的 metadata。重启、多实例共享同一份 task 状态，支撑 GetTask /
CancelTask / 挂起恢复。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ...store import A2ATask as A2ATaskRow
from ...store import get_session_factory
from .models import Artifact, Message, Task, TaskState, TaskStatus


def _loads(s: Optional[str], default: Any) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return default


def _dumps(v: Any) -> str:
    return json.dumps(
        v,
        ensure_ascii=False,
        default=lambda o: o.model_dump() if hasattr(o, "model_dump") else str(o),
    )


def _state_value(state: TaskState) -> str:
    return state.value if hasattr(state, "value") else str(state)


class TaskManager:
    """基于本地 SQLite 的 A2A 任务持久化管理器。"""

    async def create(
        self,
        task_id: str,
        context_id: str,
        state: TaskState = TaskState.TASK_STATE_SUBMITTED,
        *,
        history: Optional[List[Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        factory = get_session_factory()
        async with factory() as db:
            db.add(
                A2ATaskRow(
                    id=task_id,
                    context_id=context_id,
                    state=_state_value(state),
                    artifacts="[]",
                    history=_dumps(
                        [
                            h.model_dump() if hasattr(h, "model_dump") else h
                            for h in (history or [])
                        ]
                    ),
                    meta=_dumps(dict(metadata or {})),
                )
            )
            await db.commit()

    async def get(self, task_id: str) -> Optional[Task]:
        factory = get_session_factory()
        async with factory() as db:
            row = await db.get(A2ATaskRow, task_id)
            if row is None:
                return None
            return self._to_task(row)

    @staticmethod
    def _to_task(row: A2ATaskRow) -> Task:
        status = TaskStatus(
            state=TaskState(row.state),
            message=_loads(row.status_message, None),
        )
        artifacts = [Artifact(**a) for a in _loads(row.artifacts, [])]
        history = [Message(**h) for h in _loads(row.history, [])]
        return Task(
            id=row.id,
            contextId=row.context_id,
            status=status,
            artifacts=artifacts,
            history=history,
            metadata=_loads(row.meta, {}),
        )

    async def set_state(
        self, task_id: str, state: TaskState, message: Optional[Any] = None
    ) -> Task:
        status_msg = None
        if message is not None:
            status_msg = message.model_dump() if hasattr(message, "model_dump") else message
        factory = get_session_factory()
        async with factory() as db:
            row = await db.get(A2ATaskRow, task_id)
            if row is None:
                raise KeyError(f"task 不存在: {task_id}")
            row.state = _state_value(state)
            row.status_message = _dumps(status_msg) if status_msg is not None else None
            await db.commit()
            return self._to_task(row)

    async def add_artifact(self, task_id: str, artifact: Artifact) -> None:
        factory = get_session_factory()
        async with factory() as db:
            row = await db.get(A2ATaskRow, task_id)
            if row is None:
                raise KeyError(f"task 不存在: {task_id}")
            arts = _loads(row.artifacts, [])
            arts.append(artifact.model_dump())
            row.artifacts = _dumps(arts)
            await db.commit()

    async def append_history(self, task_id: str, message: Message) -> None:
        factory = get_session_factory()
        async with factory() as db:
            row = await db.get(A2ATaskRow, task_id)
            if row is None:
                raise KeyError(f"task 不存在: {task_id}")
            hist = _loads(row.history, [])
            hist.append(message.model_dump())
            row.history = _dumps(hist)
            await db.commit()

    async def set_metadata(self, task_id: str, metadata: Dict[str, Any]) -> None:
        factory = get_session_factory()
        async with factory() as db:
            row = await db.get(A2ATaskRow, task_id)
            if row is None:
                raise KeyError(f"task 不存在: {task_id}")
            row.meta = _dumps(metadata)
            await db.commit()

    async def cancel(self, task_id: str) -> Task:
        return await self.set_state(task_id, TaskState.TASK_STATE_CANCELED)
