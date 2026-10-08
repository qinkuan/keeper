"""对端链路服务：定位 thread、记录跨端往来。

典型用法（一次「我问它答」）：

    peer = PeerService(chat_session_id, "product")
    tid = await peer.ensure_thread()
    out_id = await peer.append(
        tid, "库存变更影响哪些订单？", "out",
        in_reply_to=await peer.last_reply_to(tid),
    )
    # ... 调对端的 send ...
    await peer.append(tid, answer, "in", message_id=对端返回的 message_id,
                      in_reply_to=out_id)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import contextvars
import json
import logging

from sqlalchemy import func, select

from ..store import AgentMessage, A2AOutboundTask, Thread, get_session_factory
from ..store.models import new_id

logger = logging.getLogger(__name__)


# A2A 调用递归深度保护：避免「对端互相调用」或「keeper 对端缺 a2a_url 时回退到
# via_peer 的 send 工具（collect.py 又调回 PeerService.ask）」形成的无限递归
# （maximum recursion depth exceeded）。正常多跳 A2A 不会触及此上限。
_PEER_CALL_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "peer_call_depth", default=0
)
_PEER_CALL_DEPTH_LIMIT = 16


def _dumps(v: Any) -> str:
    return json.dumps(
        v,
        ensure_ascii=False,
        default=lambda o: o.model_dump() if hasattr(o, "model_dump") else str(o),
    )


def _loads(s: str) -> Any:
    return json.loads(s) if s else {}

DIRECTIONS = ("out", "in")


class PeerService:
    """本会话与某个对端之间的那条链。

    一条链由 ``(chat_session_id, peer)`` 唯一确定——同一会话换对端是另一条链，
    LLM 不需要记 thread_id，靠这对定位即可。
    """

    def __init__(
        self, chat_session_id: str, peer: str, agent_id: Optional[str] = None
    ) -> None:
        self.chat_session_id = chat_session_id
        self.peer = peer
        # 发起方 agent：PeerService 靠它取该 agent 的对端配置。
        # 不传时 get_keeper(None) 恒为 None → 取不到 a2a_url → 误走 MCP 回退分支，
        # 报「keeper 未就绪或未连接 MCP」（工具明明在列表里，却调不动）。
        self.agent_id = agent_id

    async def ensure_thread(self) -> tuple[str, str]:
        """取本会话与该对端之间的链，没有则新建。

        返回 ``(thread_id, context_id)``。``context_id`` 是独立的协议 context
        （A2A contextId），与 ``thread.id`` 解耦——而不是把 thread.id 当 contextId。

        注：出站实际发出的 ``contextId`` 已改为 **per-task**：每次新任务新生成一个，
        resume 时沿用该 task 的（见 ``_ask_a2a``）。因为对端把 contextId 当自己的
        session 用，per-thread 复用会让新发起的问题串进旧任务那段会话历史。
        此处返回的 thread 级 ``context_id`` 仅为兼容保留，**出站不再使用**。
        """
        factory = get_session_factory()
        async with factory() as db:
            r = await db.execute(
                select(Thread).where(
                    Thread.chat_session_id == self.chat_session_id,
                    Thread.peer == self.peer,
                )
            )
            t = r.scalars().first()
            if t is not None:
                if not t.context_id:
                    t.context_id = new_id()
                    await db.commit()
                return t.id, t.context_id

            tid = new_id()
            ctx = new_id()
            db.add(
                Thread(
                    id=tid,
                    chat_session_id=self.chat_session_id,
                    peer=self.peer,
                    context_id=ctx,
                )
            )
            await db.commit()
            return tid, ctx

    async def append(
        self,
        thread_id: str,
        content: str,
        direction: str,
        *,
        message_id: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> str:
        """追加一条跨端消息，返回 message_id（seq 自动取当前最大值 +1）。

        - ``direction``：out=我发出的，in=对端回的
        - ``message_id``：不传则生成；**记录对端的回复时应沿用对端返回的 id**，
          这样同一条消息在两端是同一个值，后续对账才对得上
        - ``task_id``：该消息归属的 A2A taskId（续聊/重建 history 用）
        """
        if direction not in DIRECTIONS:
            raise ValueError(f"direction 只能是 {DIRECTIONS}，收到 {direction!r}")

        mid = message_id or new_id()
        factory = get_session_factory()
        async with factory() as db:
            r = await db.execute(
                select(func.coalesce(func.max(AgentMessage.seq), 0)).where(
                    AgentMessage.thread_id == thread_id
                )
            )
            seq = int(r.scalar_one()) + 1
            db.add(
                AgentMessage(
                    id=mid,
                    thread_id=thread_id,
                    seq=seq,
                    direction=direction,
                    in_reply_to=in_reply_to,
                    content=content,
                    task_id=task_id,
                )
            )
            await db.commit()
        return mid

    async def _set_message_task(self, message_id: str, task_id: str) -> None:
        """补登某条 agent_messages 的 task_id（出站请求在收到 taskId 后才知归属）。"""
        factory = get_session_factory()
        async with factory() as db:
            r = await db.execute(
                select(AgentMessage).where(AgentMessage.id == message_id)
            )
            m = r.scalars().first()
            if m is not None:
                m.task_id = task_id
                await db.commit()

    async def _record_outbound_task(
        self,
        *,
        task_id: Optional[str],
        context_id: str,
        peer: str,
        remote_url: Optional[str],
        thread_id: str,
        local_session_id: Optional[str],
        state: Optional[str],
        snapshot: Any,
    ) -> None:
        """upsert 一条出站 task 到 ``a2a_outbound_tasks``（本地索引）。

        状态由对端掌握，本表只记录「我发起了哪些 task、在哪个对端、最后已知
        状态」，让本地能查「有什么 task 在对端跑」，详情再凭 task_id 调对端
        GetTask 刷新。
        """
        if not task_id:
            return
        factory = get_session_factory()
        async with factory() as db:
            row = await db.get(A2AOutboundTask, task_id)
            if row is None:
                row = A2AOutboundTask(
                    id=task_id,
                    context_id=context_id,
                    peer=peer,
                    remote_url=remote_url,
                    thread_id=thread_id,
                    local_session_id=local_session_id,
                    state=state,
                    snapshot=_dumps(snapshot),
                )
                db.add(row)
            else:
                row.state = state
                row.snapshot = _dumps(snapshot)
            await db.commit()

    @classmethod
    async def list_outbound_tasks(
        cls, peer: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """列出本地索引的出站 task——「我发起、当前运行在对端的」任务。

        状态由对端掌握，本表是本地索引（最后已知 state + 对端返回快照）。
        详情可凭 ``task_id`` 调对端 ``GetTask`` 刷新。
        """
        factory = get_session_factory()
        async with factory() as db:
            stmt = select(A2AOutboundTask)
            if peer:
                stmt = stmt.where(A2AOutboundTask.peer == peer)
            stmt = stmt.order_by(A2AOutboundTask.updated_at.desc())
            rows = (await db.execute(stmt)).scalars().all()
            return [
                {
                    "task_id": r.id,
                    "context_id": r.context_id,
                    "peer": r.peer,
                    "remote_url": r.remote_url,
                    "thread_id": r.thread_id,
                    "local_session_id": r.local_session_id,
                    "state": r.state,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
                for r in rows
            ]

    async def last_reply_to(self, thread_id: str) -> Optional[str]:
        """上一条「对端回的」消息 id。

        用作下一轮 out 消息的 ``in_reply_to``——串联的是对端的消息，不是自己发的。
        首轮没有，返回 None。
        """
        factory = get_session_factory()
        async with factory() as db:
            r = await db.execute(
                select(AgentMessage)
                .where(
                    AgentMessage.thread_id == thread_id,
                    AgentMessage.direction == "in",
                )
                .order_by(AgentMessage.seq.desc())
            )
            m = r.scalars().first()
            return m.id if m else None

    async def ask(self, question: str, peer_step_id: Optional[str] = None) -> Dict[str, Any]:
        # 递归深度保护：见模块级 _PEER_CALL_DEPTH 说明。避免「对端互相调用」或
        # 「keeper 对端缺 a2a_url 时回退到 via_peer 的 send 工具」导致无限递归
        # （maximum recursion depth exceeded）。
        depth = _PEER_CALL_DEPTH.get()
        if depth >= _PEER_CALL_DEPTH_LIMIT:
            return {
                "ok": False,
                "error": (
                    "A2A 调用深度超限（疑似对端互相调用或 a2a_url 未配置，"
                    "请检查各 agent 的 peer 配置）"
                ),
            }
        token = _PEER_CALL_DEPTH.set(depth + 1)
        try:
            return await self._ask_impl(question, peer_step_id)
        finally:
            _PEER_CALL_DEPTH.reset(token)

    async def _ask_impl(self, question: str, peer_step_id: Optional[str] = None) -> Dict[str, Any]:
        """走完一次「我问它答」：建链 → 记 out → 调对端 → 记 in。

        peer_step_id 有值时表示**这是对它上次追问的回答**（对端此前返回过
        waiting=true），透传过去让它从挂起点继续，而不是当成新问题。
        （peer_step_id 即「对端 A2A taskId」，区别于本 agent 内部的
        ReactStep.step_id / waiting_human 恢复用的本地 step_id。）

        对端接入优先走 **A2A**（config.yaml 的 ``peers`` 段配了 ``a2a_url``）；
        否则回退到旧的 MCP 调用 ``{peer}__send``（兼容非 keeper 对端）。

        返回的 ``answer / waiting / ask`` 结构与上层 ``_ask_peer`` 兼容：
        ``waiting`` 时把对端返回的 A2A ``taskId`` 当作 ``ask.peer_step_id``，
        下次带它回来即可恢复挂起（对端 A2A handler 用 taskId 定位挂起步骤）。
        """
        from ..agent.keeper import get_keeper

        agent = get_keeper(self.agent_id)
        # 优先 A2A：取该对端的 a2a 配置。
        # **优先运行时层 agent.peers**（控制台添加的对端写在这里），回退静态层
        # config.peers（平台同步来的）——与 tools.collect.peer_tools_from_config 的
        # 取值顺序保持一致。否则会出现「工具列表里有它、调用时却找不到 a2a_url」
        # 而误走 MCP 回退分支，报「keeper 未就绪或未连接 MCP」。
        peers: Dict[str, Any] = {}
        if agent is not None:
            peers = (
                getattr(agent, "peers", None)
                or getattr(getattr(agent, "config", None), "peers", None)
                or {}
            )
        peer_cfg = peers.get(self.peer) if isinstance(peers, dict) else None
        a2a_url = (
            peer_cfg.get("a2a_url")
            if isinstance(peer_cfg, dict)
            else getattr(peer_cfg, "a2a_url", None)
        )
        if a2a_url:
            headers = (
                (peer_cfg or {}).get("headers")
                if isinstance(peer_cfg, dict)
                else getattr(peer_cfg, "headers", None)
            ) or {}
            # 代理策略：添加对端时已按来源（本进程 / 外部）自动推导并持久化
            trust_env = (
                (peer_cfg or {}).get("trust_env")
                if isinstance(peer_cfg, dict)
                else getattr(peer_cfg, "trust_env", None)
            )
            return await self._ask_a2a(
                a2a_url, headers, question, peer_step_id, trust_env=bool(trust_env)
            )

        # ---- 回退：旧 MCP 路径 ----
        mcp = getattr(agent, "mcp", None)
        if agent is None or mcp is None:
            return {"ok": False, "error": "keeper 未就绪或未连接 MCP"}

        tid = (await self.ensure_thread())[0]
        out_id = await self.append(
            tid, question, "out", in_reply_to=await self.last_reply_to(tid)
        )

        payload = {"message": question, "thread_id": tid, "message_id": out_id}
        if peer_step_id:
            payload["peer_step_id"] = peer_step_id  # 回答它上次的追问，让它续着跑

        try:
            raw = await mcp.call_tool_json(f"{self.peer}__send", payload)
        except Exception as e:
            return {
                "ok": False,
                "thread_id": tid,
                "out_message_id": out_id,
                "error": str(e),
            }

        if not isinstance(raw, dict):
            raw = {"answer": str(raw)}

        in_id = await self.append(
            tid,
            raw.get("answer", ""),
            "in",
            message_id=raw.get("message_id"),   # 沿用对端返回的 id
            in_reply_to=out_id,
        )
        return {
            "ok": True,
            "thread_id": tid,
            "out_message_id": out_id,
            "in_message_id": in_id,
            "answer": raw.get("answer", ""),
            "waiting": bool(raw.get("waiting")),
            "ask": raw.get("ask"),
        }

    async def _get_outbound_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """取本地索引的出站 task（含解析后的 snapshot）。

        用于校验「用户/模型带回的 peer_step_id 是否还有效、当前是什么状态」：
        - 找不到（换了对端 / 被清理 / 根本没发起过）→ 返回 None，调用方当作新问题；
        - 找到 → 返回 ``{"id", "state", "context_id", "snapshot"}``，snapshot 即上次
          对端返回的完整 task（终态后不会再变，可直接当结果返回，省一次往返）；
          ``context_id`` 供 resume 时沿用（对端靠它定位同一段会话）。

        改用按 task 索引（而非 thread 单值 last_task_*）是本 agent 多任务并存、互不
        覆盖的前提（详见 _ask_impl / _ask_a2a 的状态分流）。
        """
        factory = get_session_factory()
        async with factory() as db:
            row = await db.get(A2AOutboundTask, task_id)
            if row is None:
                return None
            return {
                "id": row.id,
                "state": row.state,
                "context_id": row.context_id,
                "snapshot": _loads(row.snapshot) if row.snapshot else {},
            }

    @staticmethod
    def _answer_from_task(task: Dict[str, Any]) -> str:
        """从 A2A task（或对端返回快照）里提取「用户该看到的答案文本」。

        与 _ask_a2a 里直接解析对端返回的逻辑保持一致：COMPLETED 取最后一条 artifact，
        其它状态取 status.message，都没有则给个兜底文案。
        """
        status = task.get("status", {})
        state = status.get("state")
        if state == "TASK_STATE_COMPLETED":
            return PeerService._last_text_of_artifacts(task.get("artifacts", []))
        text = PeerService._first_text((status.get("message") or {}).get("parts", []))
        return text or f"对端任务结束于 {state}"

    async def _ask_a2a(
        self,
        a2a_url: str,
        headers: Dict[str, str],
        question: str,
        peer_step_id: Optional[str],
        *,
        trust_env: bool = False,
    ) -> Dict[str, Any]:
        """经 A2A 协议调对端：发 SendMessage → 同步等终态 → 把 Task 翻译成兼容结构。

        peer_step_id 即「对端 A2A taskId」，区别于本 agent 内部 ReactStep.step_id。
        """
        from ..a2a import settings as a2a_settings
        from ..a2a.client import TIMEOUT_FLAG, A2AClient

        tid, _ = await self.ensure_thread()
        out_id = await self.append(
            tid, question, "out", in_reply_to=await self.last_reply_to(tid)
        )

        # contextId 必须 **per-task**，不能 per-thread 固定复用：
        # 对端（keeper A2A handler）把 contextId 直接当自己的 session_id 用
        # （ChatService(context_id=..., upsert=True)），同一个 contextId 会让对端把所有
        # 消息塞进**同一段对话历史**——新发起的问题（内蒙古）因此被追加到上一个挂起
        # 追问（上海）那段会话里，表现就是「串台」。
        #   - 新任务 → 生成全新 contextId：对端开独立会话，与旧任务彻底隔离；
        #   - resume  → 沿用该 task 原有的 contextId：回到对端原来那段会话继续。
        context_id = new_id()

        # 无 peer_step_id → 这就是**新发起的问题**，开新任务，不做任何静默回退。
        #
        # 曾经「回退到最近一个挂起任务」去猜（哪怕是 per-task 判断后的「唯一
        # input_required」）：只要 thread 里恰好挂着一个追问，新问题就会被当成在回答
        # 它，被挂到旧任务上，对端在旧任务上下文作答 → 问「上海 3 天」却拿到
        # 「内蒙古攻略」这类串台。回退无法区分「新问题」和「回答旧追问」，故彻底删除：
        # 回答追问由上层显式带 peer_step_id（工具参数 / resume 注入的 peer_reply_to）。

        # 显式 peer_step_id（用户/模型带回的「我在说这一条」）状态分流：
        # 它直接指向某个具体出站任务，语义是「我在说这一条」，不该无条件 resume——
        #   - 不在本地索引（换对端/被清理/没发起过）→ 当新问题，不带 taskId 开新任务；
        #   - 仍是 input_required → 正常 resume：透传 taskId 让对端从挂起恢复；
        #   - 已终态（completed/failed/canceled/rejected）→ 直接返回既有结果，不再
        #     发请求（答案不会变，省往返；也避免往终态任务追加输入，复现「川西计划」bug）。
        if peer_step_id:
            ob = await self._get_outbound_task(peer_step_id)
            if ob is None:
                peer_step_id = None
            elif ob["state"] in (
                "TASK_STATE_COMPLETED",
                "TASK_STATE_FAILED",
                "TASK_STATE_CANCELED",
                "TASK_STATE_REJECTED",
            ):
                return await self._reply_from_stored(peer_step_id, ob, tid, out_id)
            else:
                # resume：沿用该 task 原有的 contextId（+ taskId），回到对端原会话
                context_id = ob.get("context_id") or context_id

        message = {
            "messageId": out_id,
            "role": "ROLE_USER",
            "parts": [{"text": question}],
            "contextId": context_id,  # 发独立的协议 context，而非 thread.id
        }
        if peer_step_id:
            # 回答上次追问：带上次对端的 taskId，让它从挂起步骤恢复
            message["taskId"] = peer_step_id

        # trust_env：本进程内对端直连（localhost 走系统代理会 502），
        # 外部对端才交给系统代理。添加对端时已按来源自动推导好。
        client = A2AClient(a2a_url, headers=headers, trust_env=bool(trust_env))

        # 熔断：对端连续失败（连不上 / 超时）达到阈值后短期不再拨它，直接快速失败。
        # 没有它，对端一挂，后面每一轮都要等满超时才返回，整轮体验被拖垮。
        peer_key = a2a_url or self.peer
        blocked = a2a_settings.guard(peer_key, self.peer)
        if blocked:
            logger.warning("A2A 熔断生效，跳过对端 %s：%s", self.peer, blocked)
            return {
                "ok": False,
                "thread_id": tid,
                "out_message_id": out_id,
                "error": blocked,
                "circuit_open": True,
            }

        try:
            task = await client.send_message(message)
            task = await client.wait_for_terminal(task)
        except Exception as e:
            # 网络/协议层失败才计入熔断；对端「业务上失败」（FAILED）不算——
            # 那是它给的正常答案，不该因此把对端拉黑。
            a2a_settings.note_failure(peer_key, str(e), self.peer)
            return {
                "ok": False,
                "thread_id": tid,
                "out_message_id": out_id,
                "error": str(e),
            }

        # 超时：对端没在配置时长内给出结果（已按配置取消该任务）。明确告知用户，
        # 而不是回一句「结束于 SUBMITTED」让人以为是正常结果。
        if task.get(TIMEOUT_FLAG):
            cfg = a2a_settings.a2a_cfg()
            waited = f"{float(getattr(cfg, 'task_timeout', 120) or 0):.0f}s"
            text = (
                f"对端 {self.peer} 在 {waited} 内没有返回结果"
                + ("（已取消该任务）" if getattr(cfg, "cancel_on_timeout", True) else "")
                + "。可稍后重试，或在「设置 → 协作（A2A）」里调大等待时长。"
            )
            a2a_settings.note_failure(peer_key, "timeout", self.peer)
            in_id = await self.append(
                tid, text, "in", in_reply_to=out_id,
                task_id=task.get("id"),
            )
            return {
                "ok": False,
                "thread_id": tid,
                "out_message_id": out_id,
                "in_message_id": in_id,
                "answer": text,
                "error": text,
                "timeout": True,
            }

        status = task.get("status", {})
        state = status.get("state")
        # 对端返回的 A2A taskId + 状态：索引到 a2a_outbound_tasks（续聊回传用）与本轮消息（归属）
        remote_task_id = task.get("id")
        if remote_task_id:
            await self._set_message_task(out_id, remote_task_id)
        # 本地索引这条出站 task（我发起、运行在对端），便于查「对端在跑什么」
        await self._record_outbound_task(
            task_id=remote_task_id,
            context_id=context_id,
            peer=self.peer,
            remote_url=a2a_url,
            thread_id=tid,
            local_session_id=self.chat_session_id,
            state=state,
            snapshot=task,
        )

        if state == "TASK_STATE_INPUT_REQUIRED":
            a2a_settings.note_success(peer_key)
            text = PeerService._first_text((status.get("message") or {}).get("parts", []))
            in_id = await self.append(tid, text, "in", in_reply_to=out_id, task_id=remote_task_id)
            return {
                "ok": True,
                "thread_id": tid,
                "out_message_id": out_id,
                "in_message_id": in_id,
                "answer": text,
                "waiting": True,
                "ask": {"peer_step_id": remote_task_id, "question": text, "options": None},
            }
        if state == "TASK_STATE_COMPLETED":
            a2a_settings.note_success(peer_key)
            # 读最后一条 artifact：续跑（回答追问）后新答案会追加在后面，
            # 第一条是上一轮的旧结论，不是本轮真正要返回的。
            text = PeerService._last_text_of_artifacts(task.get("artifacts", []))
            in_id = await self.append(tid, text, "in", in_reply_to=out_id, task_id=remote_task_id)
            return {
                "ok": True,
                "thread_id": tid,
                "out_message_id": out_id,
                "in_message_id": in_id,
                "answer": text,
                "waiting": False,
                "ask": None,
            }
        # FAILED / CANCELED / REJECTED / AUTH_REQUIRED 等
        text = (
            PeerService._first_text((status.get("message") or {}).get("parts", []))
            or f"对端任务结束于 {state}"
        )
        in_id = await self.append(tid, text, "in", in_reply_to=out_id, task_id=remote_task_id)
        return {
            "ok": False,
            "thread_id": tid,
            "out_message_id": out_id,
            "in_message_id": in_id,
            "answer": text,
            "error": text,
        }

    @staticmethod
    def _first_text(parts) -> str:
        for p in (parts or []):
            if isinstance(p, dict) and p.get("text"):
                return p["text"]
        return ""

    @staticmethod
    def _last_text_of_artifacts(artifacts) -> str:
        """取 artifacts 中**最后一条**有文字的部分。

        对端在续跑（回答追问）时会把新答案 append 到同任务的 artifacts 后面，
        所以「本轮真正要返回的答案」是最后一条，不是第一条。
        """
        for a in reversed(artifacts or []):
            text = PeerService._first_text((a or {}).get("parts", []))
            if text:
                return text
        return ""

    async def _reply_from_stored(
        self, task_id: str, ob: Dict[str, Any], tid: str, out_id: str
    ) -> Dict[str, Any]:
        """显式 peer_step_id 指向已终态任务时：直接返回该任务既有结果，不再发请求。

        - 把这条 out 消息归到既有 task（_set_message_task）；
        - 把既有答案作为一条 in 消息记回来（展示用，串进 thread history）；
        - 返回结构与 _ask_a2a 终态分支一致。
        """
        state = ob["state"]
        task = ob["snapshot"]
        text = PeerService._answer_from_task(task)
        await self._set_message_task(out_id, task_id)
        in_id = await self.append(tid, text, "in", in_reply_to=out_id, task_id=task_id)
        return {
            "ok": state == "TASK_STATE_COMPLETED",
            "thread_id": tid,
            "out_message_id": out_id,
            "in_message_id": in_id,
            "answer": text,
            "waiting": False,
            "ask": None,
        }

    async def history(self, thread_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        """取这条链的往来，按 seq 升序（先倒序取最近 N 条再反转）。"""
        factory = get_session_factory()
        async with factory() as db:
            r = await db.execute(
                select(AgentMessage)
                .where(AgentMessage.thread_id == thread_id)
                .order_by(AgentMessage.seq.desc())
                .limit(limit)
            )
            items = list(r.scalars())
        items.reverse()
        return [
            {
                "message_id": m.id,
                "seq": m.seq,
                "direction": m.direction,
                "in_reply_to": m.in_reply_to,
                "content": m.content,
                "task_id": m.task_id,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in items
        ]
