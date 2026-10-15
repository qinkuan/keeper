"""会话回收站（``/manage/sessions``）：看已删除的会话 + 把它们恢复回来。

删除会话是**软删除**（见 ``chat.service.ChatService.delete_session``）：那一行还在
库里，只是 ``status="deleted"``，所以列表里看不到、而按 id 拉历史会404。本模块就是
这套软删除的另一半——**看得见、捞得回来**。

为什么挂在 ``/manage`` 而不是 ``/agents/{agent_id}/sessions`` 下
------------------------------------------------------------
会话列表是**按 agent 分**的（``ChatSession.agent_id`` 隔离，切agent 不能串台），
但回收站是**跨 agent 的管理视图**：删掉的会话可能属于任何一个 agent，甚至属于一个
已经卸掉的 agent。把它做成按 agent 的接口，就得先问"看哪个 agent 的回收站"——
而这个问题没有答案。所以这里列出全部，并在每行标出它原本属于谁。

另外 ``agent`` 表里**没有**核心 agent 那一行（它从本地目录构建，不落库），所以
agent 名字必须允许查不到：查不到就退回显示 agent_id，不能因此把这行藏起来。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select

from ..chat.service import SESSION_STATUS_ACTIVE, SESSION_STATUS_DELETED
from ..store import (
    Agent,
    ChatSession,
    SessionMessage,
    get_session_factory,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/manage/sessions", tags=["session-manage"])

# 单页上限。不做无限滚动：回收站是"偶尔找回一条东西"的地方，真有上千条时用户
# 该做的是批量清理，而不是一页页翻。
_MAX_LIMIT = 200


def _session_out(
    row: ChatSession, agent_name: Optional[str], message_count: int
) -> Dict[str, Any]:
    """回收站一行。

    ``agent_name`` 查不到时退回 ``agent_id``：核心 agent 不在 ``agent`` 表里，
    而"这个会话属于谁"必须始终有答案（否则用户看到一条无主的会话，只能干瞪眼）。
    """
    return {
        "id": row.id,
        "title": row.title,
        "agent_id": row.agent_id,
        "agent_name": agent_name or row.agent_id,
        "kind": row.kind,
        "message_count": message_count,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
    }


@router.get("/trash")
async def list_trash(
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """已删除的会话列表，**按删除时间倒序**（最近删的在最上面）。

    按 ``deleted_at`` 而不是 ``updated_at`` 排：回收站的语义是"我什么时候删的它"，
    找东西时按删除时间回想最准。按更新时间排会让很久以前删的会话因为"删之前刚好
    被人动过"而冒到最前面，顺序和用户的记忆对不上。

    ``deleted_at`` 为空的存量行（极老的数据）排在最后——SQLite 里 DESC 排序本来
    就把 NULL 放在末尾，这里只是把它当作一条显式的约定写下来。

    每行带 ``message_count``：标题是自动派生的，常常只有「新对话」这种占位，
    消息条数是判断"这是不是我要找的那条"最快的线索。
    """
    limit = max(1, min(limit, _MAX_LIMIT))
    offset = max(0, offset)
    async with get_session_factory()() as db:
        base = select(ChatSession).where(
            ChatSession.status == SESSION_STATUS_DELETED
        )
        total = (
            await db.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar_one()
        rows = (
            (
                await db.execute(
                    base.order_by(
                        ChatSession.deleted_at.desc(), ChatSession.id.desc()
                    )
                    .limit(limit)
                    .offset(offset)
                )
            )
            .unique()
            .scalars()
            .all()
        )
        counts: Dict[str, int] = {}
        if rows:
            counts = {
                r[0]: r[1]
                for r in (
                    await db.execute(
                        select(
                            SessionMessage.chat_session_id,
                            func.count(SessionMessage.id),
                        )
                        .where(
                            SessionMessage.chat_session_id.in_([r.id for r in rows])
                        )
                        .group_by(SessionMessage.chat_session_id)
                    )
                ).all()
            }
        # agent 名字一次取回（IN 查询），不做 N+1；查不到名字的（核心 agent）留 None
        names: Dict[str, str] = {}
        agent_ids = {r.agent_id for r in rows if r.agent_id}
        if agent_ids:
            names = {
                r[0]: r[1]
                for r in (
                    await db.execute(
                        select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids))
                    )
                ).all()
            }
    return {
        "sessions": [
            _session_out(r, names.get(r.agent_id or ""), counts.get(r.id, 0))
            for r in rows
        ],
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
    }


@router.post("/{session_id}/restore")
async def restore_session(session_id: str) -> Dict[str, Any]:
    """把一条已删除的会话恢复回列表。

    只是把 ``status`` 置回 ``active`` 并清空 ``deleted_at``——消息、ReAct 步骤、
    跨端线程、产物引用都原样还在（软删除时一行都没删），所以恢复后这段对话可以
    接着聊，不需要重建任何东西。

    会话**本来就没被删**时报错而不是静默成功：调用方（界面的「恢复」按钮）拿到的
    成功响应应该意味着"它刚从回收站里出来"，否则用户会看着一条本就在列表里的会话
    收到"已恢复"，还以为自己点错了地方。
    """
    async with get_session_factory()() as db:
        row = await db.get(ChatSession, session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        if row.status != SESSION_STATUS_DELETED:
            raise HTTPException(
                status_code=409, detail="该会话不在回收站里（可能已经恢复过了）"
            )
        row.status = SESSION_STATUS_ACTIVE
        row.deleted_at = None
        await db.commit()
    logger.info("会话已从回收站恢复: %s（agent=%s）", session_id, row.agent_id)
    return {"id": session_id, "restored": True, "agent_id": row.agent_id}