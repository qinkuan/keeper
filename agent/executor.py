"""执行器：一轮对话从提问到落地的过程。

与 ``keeper.py``（装配）的分工：

- 装配：配置 → 实例 → 可运行的 Process，回答「这个 agent 有什么能力」；
- **执行**（本文件）：拿着装配好的 Process 跑完一轮，回答「这一轮怎么跑完」——
  就绪守卫、逐步落库、挂起与恢复。

执行器不持有跨轮状态：agent 与步骤都在 agent 实例 / 数据库里，它只负责编排。
"""
from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import select

if TYPE_CHECKING:  # 仅类型检查时引入，避免运行时与 keeper 循环导入
    from .keeper import KeeperAgent

logger = logging.getLogger(__name__)


class AgentExecutor:
    """一轮对话的执行逻辑。

    典型用法：``await AgentExecutor(agent).chat(text, session_message_id=...)``
    """

    def __init__(self, agent: "KeeperAgent") -> None:
        self.agent = agent

    @staticmethod
    def _budget_scope(session_id, task_id=None) -> tuple:
        """预算守卫的作用域：``(session_id, task_id)``。

        ``task_id`` 没显式传就从上下文取——任务模式下执行器已 ``set_task_id``，
        这样 ``KEEPER_TASK_TOKEN_LIMIT`` 不用层层透参也能生效。
        """
        if task_id is None:
            from ..chat.context import current_task_id

            task_id = current_task_id()
        return session_id, task_id

    # ---- 新话轮 ----
    async def chat(
        self,
        text: str,
        session_message_id: Optional[str] = None,
        history: Optional[list] = None,
        memory_hint: Optional[str] = None,
        should_stop=None,
        on_step=None,
        on_delta=None,
        session_id=None,
        task_id=None,
    ) -> Any:
        """跑一轮 ReAct 并逐步落库，返回 ProcessResult。

        多轮上下文（历史消息）由 chat/service.py 构造后通过 ``history`` 传入并注入，
        不在这一层读取。

        session_id：预算守卫的作用域——每步之后查一次本会话累计用量，逼近上限
        就让模型收尾、触顶就停（不传则不做预算检查）。

        session_message_id：触发本轮的那条 user 消息，步骤挂在它下面。
        history：最近 N 轮等历史消息，拼在当前问题前注入 LLM。
        memory_hint：系统自动召回的「相关历史记忆」摘要，注入 system prompt（L1）。
        should_stop：可选的异步回调，每跑完一步问一次「该停了吗」（按 step 暂停）。
        on_step：可选的异步观察回调 ``async (seq, step)``，每跑完一步调用一次。
            流式输出（SSE）用它把思考 / 工具调用逐步推给前端；不影响落库逻辑。
        on_delta：可选的异步回调 ``async (text)``，最终回答按块推送（打字机效果）。
        """
        from .process import ProcessResult

        ready = self.agent.check_readiness()
        if not ready.ok:
            # 依赖没到位：立即回「稍后重试」，重连放后台，不占用户等待
            self.agent.recover_in_background(ready.missing)
            return ProcessResult(answer=ready.message())

        # 对端 agent 可能比本实例晚部署（启动顺序不该有要求）。顺手重连，
        # 不阻塞本轮——这一轮也许根本用不到那个对端。
        self.agent.recover_peers_in_background()

        sid, tid = self._budget_scope(session_id, task_id)
        process = await self.agent.prepare()
        result = await process.run(
            text,
            on_step=self._step_writer(session_message_id, on_step=on_step),
            on_delta=on_delta,
            history=history,
            memory_hint=memory_hint,
            should_stop=should_stop,
            session_id=sid,
            task_id=tid,
        )
        if result.ask and session_message_id:
            # 模型要向用户提问：补写一条挂起步骤，等该会话的下一条消息来恢复
            result.ask_step_id = await self._persist_ask_step(
                session_message_id, len(result.steps) + 1, result
            )
        if result.paused and session_message_id:
            # 用户点了暂停：写一个断点标记，下次执行从这里重放续跑
            result.paused_step_id = await self._persist_pause_step(
                session_message_id, len(result.steps) + 1
            )
        return result

    # ---- 从挂起步恢复 ----
    async def resume(
        self,
        step_id: str,
        answer: str,
        *,
        should_stop=None,
        on_step=None,
        on_delta=None,
        task_id=None,
    ) -> Any:
        """从挂起的提问恢复：重建上下文 + 接上用户回答 + 继续跑。

        与 chat() 的区别：不从头再跑一遍，而是把该轮已落库的步骤重放给模型，
        它据此接着走——避免重复调用同一个工具、重复问同一个对端。

        用户回答先落库成一条 human_answer 步骤（占挂起步的下一个序号），
        再随 prior_steps 一起重放。此前它只临时拼进 messages，本轮一旦
        再次挂起就会丢失，导致模型看不到先前几次的回答。
        """
        from ..store import ReactStep, SessionMessage, get_session_factory
        from .process import Step

        factory = get_session_factory()
        async with factory() as db:
            cur: Optional[ReactStep] = await db.get(ReactStep, step_id)
            if cur is None:
                raise ValueError(f"step_id 不存在: {step_id}")
            # 恢复的是「对端在等我回答」那一步：把对端 taskId 注入本轮上下文，
            # 对端工具据此精确恢复该追问。这样不必靠「静默回退最近一个挂起任务」
            # 去猜（猜会把新发起的问题误当成回答旧追问，导致问 A 拿到 B 的答案）。
            if cur.wait_kind == "waiting_to_peer" and cur.wait_ref:
                from ..chat.context import set_peer_reply_to

                set_peer_reply_to(cur.wait_ref)
            qmsg: Optional[SessionMessage] = await db.get(
                SessionMessage, cur.session_message_id
            )
            # 预算守卫的作用域：续跑同样在烧本会话的额度，必须一起拦
            resume_session_id = qmsg.chat_session_id if qmsg else None
            # 同一条挂起步被重复回答时不再补写（唯一约束 (session_message_id, step)）
            dup = await db.execute(
                select(ReactStep.id).where(
                    ReactStep.session_message_id == cur.session_message_id,
                    ReactStep.step == cur.step + 1,
                    ReactStep.kind == "human_answer",
                )
            )
            has_answer = dup.scalars().first() is not None

        if not has_answer:
            await self._persist_step(
                cur.session_message_id,
                cur.step + 1,
                Step(thought="用户回答", observation=answer),
                kind="human_answer",
            )

        async with factory() as db:
            rows = await db.execute(
                select(ReactStep)
                .where(
                    ReactStep.session_message_id == cur.session_message_id,
                    # +1 含刚补写的用户回答：它也占序号，新步骤因此不撞号
                    ReactStep.step <= cur.step + 1,
                )
                .order_by(ReactStep.step)
            )
            records = list(rows.scalars())

        prior_steps = [self._step_from_record(r) for r in records]

        _sid, tid = self._budget_scope(resume_session_id, task_id)
        process = await self.agent.prepare()
        result = await process.resume(
            qmsg.content if qmsg else "",
            prior_steps,
            on_step=self._step_writer(cur.session_message_id, on_step=on_step),
            on_delta=on_delta,
            should_stop=should_stop,
            session_id=resume_session_id,
            task_id=tid,
        )
        if result.ask and cur.session_message_id:
            result.ask_step_id = await self._persist_ask_step(
                cur.session_message_id, len(result.steps) + 1, result
            )
        return result

    # ---- 步骤落库（执行过程的痕迹）----
    def _step_writer(self, session_message_id: Optional[str], on_step=None):
        """构造逐步落库的回调，交给 Process 在每步执行完后调用。

        没给 session_message_id 时返回 None（不落库）——除非同时给了 ``on_step``，
        那时只推送不落库。

        on_step：可选的外部观察回调（流式输出用），落库之后调用，签名 ``async (seq, step)``。
        """
        if not session_message_id and not on_step:
            return None

        # 逐步耗时：以「上一步结束」为起点，到本步结束为止（第一步以整轮开始为起点），
        # 因此每步耗时 = LLM 思考 + 工具执行，正是用户感受到的一步耗时。
        clock = {"last": time.perf_counter()}

        async def _write(seq: int, step) -> None:
            now = time.perf_counter()
            duration_ms = int((now - clock["last"]) * 1000)
            clock["last"] = now
            if session_message_id:
                from ..chat.context import take_pending_peer_ask
                from ..observability import backfill_step_id

                # 这步若触发了对端追问，把对方的 peer_step_id 记进 wait_ref——
                # 恢复时从库里取，不靠 LLM 从 observation 文本里抄。
                peer_step = take_pending_peer_ask()
                sid = await self._persist_step(
                    session_message_id,
                    seq,
                    step,
                    # waiting_to_peer：对端反过来追问了，是我们欠它一个回答，
                    # 不是我们在等它（那是 waiting_peer）
                    wait_kind="waiting_to_peer" if peer_step else None,
                    wait_ref=peer_step,
                    duration_ms=duration_ms,
                )
                # 可观测：step 落库拿到 id 后，回填本步那些 LLM 调用的归属
                # （打点时 step 还没落库，只能先记 step_seq）。
                await backfill_step_id(session_message_id, seq, sid)
            # 流式：把这一步推给订阅方（前端据此实时展示思考 / 工具调用）
            if on_step:
                await on_step(seq, step)

        return _write

    async def _persist_step(
        self,
        session_message_id: str,
        seq: int,
        step,
        *,
        status: str = "done",
        kind: Optional[str] = None,
        output: Optional[str] = None,
        wait_kind: Optional[str] = None,
        wait_ref: Optional[str] = None,
        step_input: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> str:
        """写一条步骤记录，返回 step id。

        逐步实时写入（不是跑完批量写），这样中途挂起或异常时已完成的步骤不会丢。
        step_input 显式传入时优先（挂起步骤用它存 options 等附加信息）。
        duration_ms：本步总耗时（LLM 思考 + 工具执行），由 ``_step_writer`` 测算。
        """
        from ..store import ReactStep, get_session_factory
        from ..store.models import new_id

        sid = new_id()
        has_tool = bool(getattr(step, "tool", None))
        factory = get_session_factory()
        async with factory() as db:
            db.add(
                ReactStep(
                    id=sid,
                    session_message_id=session_message_id,
                    step=seq,
                    kind=kind or (step.tool if has_tool else "think"),
                    input=step_input
                    if step_input is not None
                    else (
                        json.dumps(
                            {
                                "thought": getattr(step, "thought", ""),
                                "args": getattr(step, "args", None),
                            },
                            ensure_ascii=False,
                        )
                        if has_tool
                        else (getattr(step, "thought", "") or "")
                    ),
                    output=output
                    if output is not None
                    else getattr(step, "observation", None),
                    status=status,
                    wait_kind=wait_kind,
                    wait_ref=wait_ref,
                    duration_ms=duration_ms,
                )
            )
            await db.commit()
        return sid

    async def _persist_ask_step(self, session_message_id: str, seq: int, result) -> str:
        """模型向用户提问：写一条挂起步骤，等该会话的下一条消息来恢复。"""
        return await self._persist_step(
            session_message_id,
            seq,
            result,
            status="suspended",
            kind="ask_human",
            output=result.ask or "",
            wait_kind="waiting_human",
            wait_ref=session_message_id,
            # options 存在 input 列里，刷新恢复追问框时要用
            step_input=json.dumps({"options": result.options}, ensure_ascii=False),
        )

    async def _persist_pause_step(self, session_message_id: str, seq: int) -> str:
        """用户点了「暂停」：写一个断点标记，下次执行从这里重放续跑。

        它只是定位用的断点（不承载提问 / 回答内容），``resume_paused`` 恢复时会把
        它删掉，不让它混进展示给用户的步骤列表里。
        """
        from .process import Step

        return await self._persist_step(
            session_message_id,
            seq,
            Step(thought="用户暂停"),
            status="suspended",
            kind="paused",
            output="",
            wait_kind="paused",
            wait_ref=session_message_id,
        )

    async def resume_paused(
        self,
        step_id: str,
        *,
        should_stop=None,
        on_step=None,
        on_delta=None,
        task_id=None,
    ) -> Any:
        """从「用户暂停」的断点继续：重放断点之前已落库的步骤，接着跑。

        与 chat() 的区别：不开新一轮，而是把已完成的步骤重放给模型，它据此接着
        走——避免把已完成的工作重做一遍（即「前 4 步拿出来拼接，继续走第 5 步」）。

        on_step / on_delta：同 chat()——续跑过程同样可以逐步 / 逐字推给前端。
        """
        from ..store import ReactStep, SessionMessage, get_session_factory

        factory = get_session_factory()
        async with factory() as db:
            cur: Optional[ReactStep] = await db.get(ReactStep, step_id)
            if cur is None:
                raise ValueError(f"step_id 不存在: {step_id}")
            qmsg: Optional[SessionMessage] = await db.get(
                SessionMessage, cur.session_message_id
            )
            smid = cur.session_message_id
            question = qmsg.content if qmsg else ""
            # 预算守卫的作用域：断点续跑同样是新一轮消耗
            resume_session_id = qmsg.chat_session_id if qmsg else None
            rows = await db.execute(
                select(ReactStep)
                .where(
                    ReactStep.session_message_id == smid,
                    # 只取断点之前已完成的步骤：断点本身不重放
                    ReactStep.step < cur.step,
                )
                .order_by(ReactStep.step)
            )
            records = list(rows.scalars())
            # 断点标记用完即删：它只用于定位，不该留在步骤列表里
            await db.delete(cur)
            await db.commit()

        prior_steps = [self._step_from_record(r) for r in records]
        _sid, tid = self._budget_scope(resume_session_id, task_id)
        process = await self.agent.prepare()
        result = await process.resume(
            question,
            prior_steps,
            on_step=self._step_writer(smid, on_step=on_step),
            on_delta=on_delta,
            should_stop=should_stop,
            session_id=resume_session_id,
            task_id=tid,
        )
        if result.ask and smid:
            result.ask_step_id = await self._persist_ask_step(
                smid, len(result.steps) + 1, result
            )
        if result.paused and smid:
            result.paused_step_id = await self._persist_pause_step(
                smid, len(result.steps) + 1
            )
        return result

    @staticmethod
    def _step_from_record(row):
        """把库里的步骤记录还原成 Process 的 Step，用于重建上下文。"""
        from .process import Step

        data = {}
        try:
            parsed = json.loads(row.input) if row.input else {}
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = {}

        if row.kind == "ask_human":
            # observation 里存的是当时问用户的问题
            return Step(
                thought=data.get("thought", "") or "",
                kind="ask",
                observation=row.output,
            )
        if row.kind == "human_answer":
            # 用户对上一条 ask 的回答，重放时还原成一条 user 消息
            return Step(
                thought=row.input or "用户回答",
                kind="human_answer",
                observation=row.output,
            )
        if row.kind == "think":
            return Step(thought=row.input or "")
        observation = row.output
        if row.wait_kind == "waiting_to_peer" and row.wait_ref:
            # 这一步把对端问住了：把它的 peer_step_id 拼进 observation，
            # 恢复后才知道该回复对方的哪一次追问
            observation = (
                f"{observation or ''}\n"
                f"[对端需要补充信息，回复时带上 peer_step_id={row.wait_ref} 再调一次]"
            )
        return Step(
            thought=data.get("thought") or "",
            tool=row.kind,
            args=data.get("args"),
            observation=observation,
        )
