"""归属上下文：从 chat/context 的 ContextVar 取 trace/agent/session 等。

本文件是 keeper.observability 包的一部分，从原来的单体
keeper/observability.py（1856 行）拆分而来。设计见
doc/observability-design.md。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

def _ctx() -> Dict[str, Any]:
    """从请求上下文里取归属信息；读不到就返回空，绝不抛错。"""
    try:
        from ..chat.context import (
            current_agent_id,
            current_llm_call_kind,
            current_message_id,
            current_session_id,
            current_step_seq,
            current_task_id,
            current_task_item_id,
            ensure_trace_id,
        )

        return {
            "session_id": current_session_id(),
            "message_id": current_message_id(),
            "step_seq": current_step_seq(),
            "task_id": current_task_id(),
            "task_item_id": current_task_item_id(),
            "agent_id": current_agent_id(),
            "trace_id": ensure_trace_id(),
            "llm_kind": current_llm_call_kind(),
        }
    except Exception as e:  # 归属失败不影响记账本身
        logger.debug("读取可观测归属上下文失败: %s", e)
        return {}


# 原属 timeline.py：按 message_id 解析出「触发该轮的用户消息」。
# 放在这里是因为它与 _ctx 同属「归属解析」，且 stats.duplicate_calls 也用得到。
async def _resolve_round_anchor(message_id: str) -> str:
    """把任意消息 id 解析到「触发该轮的用户消息」id。

    用量与步骤都挂在 user 消息上（``llm_calls.message_id`` /
    ``react_steps.session_message_id`` 都是它），但**实时回答**的用量显示在
    assistant 气泡上，前端点它会传来 assistant id——这里回退到同会话中
    seq 小于它、role=user 的最近一条（一轮 = 一条提问 + 它的回答）。
    解析失败就原样返回，不抛错。
    """
    try:
        from ..store import SessionMessage, get_session_factory

        factory = get_session_factory()
        async with factory() as db:
            row = await db.get(SessionMessage, message_id)
            if row is None or row.role == "user":
                return message_id
            prev = (
                await db.execute(
                    select(SessionMessage.id)
                    .where(
                        SessionMessage.chat_session_id == row.chat_session_id,
                        SessionMessage.role == "user",
                        SessionMessage.seq < row.seq,
                    )
                    .order_by(SessionMessage.seq.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return prev or message_id
    except Exception as e:  # noqa: BLE001
        logger.debug("解析轮次锚点失败: %s", e)
        return message_id
