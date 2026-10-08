"""chat 服务：负责对话上下文与落库，问答执行交给执行器。

- agent 实例由本模块通过 get_keeper() 取用，接口层不感知；
- 会话（chat_sessions）与消息（session_messages）在此落库，
  session_id / message_id 也在此生成：**不带 session_id 视为新建会话**；
- 拿到问题后交给 AgentExecutor 跑，由它走 agent 自主规划（按需调 MCP 工具）。

表结构见 keeper/doc/session-design.md。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Dict, Optional

from sqlalchemy import func, select, update

from pathlib import Path

from ..tools.builtin.sandbox import (
    PathEscape,
    TMP_PREFIX,
    safe_path,
)

logger = logging.getLogger(__name__)

# 摘要输入中每个 step 的 observation 截断上限（字符）；最终回答保留完整（省 token）
_TURN_SUMMARY_OBS_LIMIT = 600

# 产物 MIME 推断（按扩展名）
_MIME_BY_EXT = {
    ".html": "text/html", ".htm": "text/html",
    ".md": "text/markdown", ".markdown": "text/markdown",
    ".txt": "text/plain", ".text": "text/plain",
    ".json": "application/json", ".csv": "text/csv",
    ".css": "text/css", ".js": "application/javascript",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
    ".pdf": "application/pdf", ".zip": "application/zip",
    # 源码：映射为 text/x-*，前端凭扩展名做语法高亮（在卡片内内联展示）
    ".py": "text/x-python", ".java": "text/x-java",
    ".ts": "text/x-typescript", ".tsx": "text/x-tsx", ".jsx": "text/x-jsx",
    ".go": "text/x-go", ".rs": "text/x-rust",
    ".c": "text/x-c", ".h": "text/x-c",
    ".cpp": "text/x-c++", ".cc": "text/x-c++", ".cxx": "text/x-c++", ".hpp": "text/x-c++",
    ".rb": "text/x-ruby", ".php": "text/x-php",
    ".sh": "text/x-sh", ".bash": "text/x-sh",
    ".sql": "text/x-sql",
    ".yaml": "text/yaml", ".yml": "text/yaml", ".xml": "text/xml",
}


def _guess_mime(name: str) -> str:
    """按文件扩展名推断 MIME（用于产物卡片展示 / 预览类型判断）。"""
    return _MIME_BY_EXT.get(Path(name).suffix.lower(), "application/octet-stream")

from ..peer import PeerService
from ..agent.executor import AgentExecutor
from ..agent.keeper import get_keeper
from .context import (
    WorkspaceCtx,
    current_task_id,
    set_agent,
    set_message_id,
    set_session_id,
    set_workspace,
)
from ..store import (
    ChatSession,
    ReactStep,
    SessionMessage,
    USER_SPACE_DEFAULT_NAME,
    UserSpace,
    get_session_factory,
)
from ..store.models import new_id


default_initiator_id = 9999


async def resolve_workspace(agent, session_id: str) -> WorkspaceCtx:
    """按 (会话, agent) 解析本次请求生效的工作空间。

    - 会话绑定的用户空间（``ChatSession.user_space_id`` → ``UserSpace``）：取其
      ``path`` / ``read_only``；
    - 没绑（或绑的已删）→ 回落系统默认用户空间（``workspace/user/default``）；
    - agent 空间：``default_agent_space_root(session_id, agent_id)``，自动 mkdir
      （决策：路径可不存在，允许自动创建）。
    """
    from ..agent.config import default_agent_space_root

    user_space = None
    factory = get_session_factory()
    async with factory() as db:
        row = await db.get(ChatSession, session_id)
        usid = getattr(row, "user_space_id", None) if row is not None else None
        if usid:
            user_space = await db.get(UserSpace, usid)
        # 没绑（或绑定的已删）→ 回落系统默认用户空间，保证一定有主目录
        if user_space is None:
            user_space = (
                await db.execute(
                    select(UserSpace).where(UserSpace.name == USER_SPACE_DEFAULT_NAME)
                )
            ).scalars().first()

    if user_space is not None:
        root = Path(user_space.path).expanduser()
        read_only = bool(user_space.read_only)
    else:
        # 极端兜底：默认记录都没（不应发生），用 agent 默认目录
        root = agent.workspace_root
        read_only = bool(getattr(agent.config.workspace, "read_only", False))

    agent_space = default_agent_space_root(session_id, agent.agent_id)
    agent_space.mkdir(parents=True, exist_ok=True)
    return WorkspaceCtx(root=root, read_only=read_only, agent_space=agent_space)
class ChatService:
    # 会话级串行锁：同一会话一次只跑一个 ask。
    #
    # 架构本来就假定「同一会话串行」（比如新话轮会 _abandon_suspended 掉挂起步骤），
    # 但两个并发请求会破坏这个假定：_next_seq 取 max+1 会撞号、后到的请求会把先到
    # 的挂起等待误作废、两边互相看不到对方刚写的内容。这里按 session_id 加
    # asyncio.Lock 强制排队——上一条跑完再跑下一条。
    #
    # 锁按会话累积不回收：会话数是有限量级（本地单人使用），且回收需要处理
    # 「正在使用中被删」的竞态，复杂度不值得。
    _session_locks: Dict[str, asyncio.Lock] = {}

    @classmethod
    def _session_lock(cls, session_id: str) -> asyncio.Lock:
        """取（或建）该会话的串行锁。"""
        lock = cls._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            cls._session_locks[session_id] = lock
        return lock

    def __init__(
        self,
        session_id: Optional[str] = None,
        initiator_id: Optional[int] = None,
        upsert: bool = False,
        agent_id: Optional[str] = None,
        context_id: Optional[str] = None,
    ) -> None:
        self.agent_id = agent_id
        self.context_id = context_id
        self.agent = get_keeper(agent_id)
        # 执行侧：一轮怎么跑全在 AgentExecutor，agent 本身只提供能力
        self.executor = AgentExecutor(self.agent) if self.agent is not None else None
        self.session_id = session_id
        # 发起人必须有值：显式传 None 时回退默认。
        # 否则列表接口不带参数就会返回全部会话（跨发起人数据混看），也不该出现"无主"会话。
        self.initiator_id = (
            initiator_id if initiator_id is not None else default_initiator_id
        )
        # session_id 传了但本库没有时怎么办：False=报错（前端防呆），
        # True=用它新建（跨端：对端给的 thread_id 本库必然没有，契约要求拿来当 session_id）
        self.upsert = upsert
        # 本会话要绑定的用户空间（新建会话时由 ask 传入；已有会话沿用库里的绑定）
        self.user_space_id: Optional[str] = None

    async def ask(
        self,
        text: str,
        step_id: Optional[str] = None,
        message_id: Optional[str] = None,
        user_space_id: Optional[str] = None,
        should_stop=None,
        on_step=None,
        on_delta=None,
    ) -> Dict[str, Any]:
        """对外入口：**同一会话串行执行**（串行锁见 ``_session_lock``）。

        on_step：可选的异步回调 ``async (seq, step)``，每完成一步 ReAct 调用一次，
        供流式输出（SSE）逐步推送过程；不传则行为与之前完全一致。
        on_delta：可选的异步回调 ``async (text)``，最终回答按块推送（打字机）。

        上一条还没跑完时，后到的同一会话请求会在锁上排队等待，而不是并发跑两个
        ReAct——避免 seq 撞号、挂起被后到的请求误作废、以及两边互相看不到对方
        刚写入的内容。不同会话之间互不影响，仍可并行。
        """
        if self.agent is None:
            return {"answer": "（agent 实例未就绪，服务尚未启动完成）", "llm": False}

        # 新建会话时记录要绑定的用户空间；已有会话忽略（沿用库里的绑定，中途不可换）
        self.user_space_id = user_space_id

        if not text.strip():
            return {"answer": "", "llm": self.agent.llm is not None}

        # 会话：带 session_id 就沿用，没带就新建（新建时按 user_space_id 绑定工作空间）
        session_id = await self._ensure_session()

        async with self._session_lock(session_id):
            return await self._ask_locked(
                session_id,
                text,
                step_id=step_id,
                message_id=message_id,
                should_stop=should_stop,
                on_step=on_step,
                on_delta=on_delta,
            )

    async def _ask_locked(
        self,
        session_id: str,
        text: str,
        *,
        step_id: Optional[str] = None,
        message_id: Optional[str] = None,
        should_stop=None,
        on_step=None,
        on_delta=None,
    ) -> Dict[str, Any]:
        """真正跑一轮问答（调用方需已持有该会话的串行锁）。

        on_step / on_delta 见 ``ask``：透传给执行器，用于流式逐步推送。
        """
        # 可观测：本轮开始计时（端到端墙钟，含工具执行与等待）
        t0 = time.perf_counter()

        # 标记本次任务所属会话：ReAct 过程中调用的工具（比如 keeper 对端的 send）
        # 会读它来记账。用 ContextVar 而非闭包，是因为工具表是多会话共享的，
        # 闭包捕获会在并发时串台。
        set_session_id(session_id)
        # 标记所属 agent：日志据此区分多 agent（A2A 入站等不经 HTTP 端点的
        # 路径也靠这里补上）
        set_agent(self.agent_id, getattr(self.agent, "name", None))
        # 注入本 (会话, agent) 的工作空间：后续 fs/git 工具与插件执行时从 context 取
        # 文件根与只读标志，使「同一 agent 在不同会话指向不同用户空间」生效。
        set_workspace(await resolve_workspace(self.agent, session_id))

        # 计算记忆锚点（原始问题消息）：新话轮用本轮 user 消息；恢复用挂起步骤所属消息
        if step_id:
            # 用户在追问框里回答：这条挂起步骤到此结束，返回其归属消息 id 作锚点
            anchor_message_id = await self._resolve_step(step_id)
        else:
            # 新话轮：该会话挂起的步骤一律作废（换话题或不想等了，不卡住用户）
            await self._abandon_suspended(session_id)
            anchor_message_id = None  # 建完 user 消息再确定

        # 落库本轮的用户消息（生成 message_id，或沿用对端指定的）
        user_message_id = await self._append_message(
            session_id, "user", text, message_id=message_id
        )
        if anchor_message_id is None:
            anchor_message_id = user_message_id  # 新话轮：锚 = 本轮 user 消息
        # react_steps 挂在消息下，工具里要写挂起状态得知道挂哪条消息
        set_message_id(user_message_id)

        # 历史上下文：最近 N 轮直接注入（R0）；另做 L1 系统侧自动召回，把命中旧轮的
        # 摘要塞进 system prompt（memory_hint），避免「LLM 忘记 recall」的漏检。
        history, memory_hint = self._build_history(
            session_id, exclude_message_id=user_message_id, question=text
        )

        # 预算 / 配额：已超限就别再跑了，免得一轮对话把额度烧穿。
        # 检查失败（查库异常等）一律放行——配额不该把正常对话挡在门外。
        try:
            from ..observability import check_budget

            # 会话与任务两个维度都查：任务模式下的执行同样占额度
            budget = await check_budget(
                session_id=session_id, task_id=current_task_id()
            )
            if budget.get("over"):
                scope = "任务" if budget.get("scope") == "task" else "会话"
                return {
                    "session_id": session_id,
                    "user_message_id": user_message_id,
                    "message_id": None,
                    "answer": (
                        f"（已超出{scope} token 预算：已用 {budget.get('used')} / "
                        f"上限 {budget.get('limit')}，本轮未执行。"
                        f"请新开会话，或调高 KEEPER_SESSION_TOKEN_LIMIT。）"
                    ),
                    "artifacts": [],
                    "waiting_human": False,
                    "step_id": None,
                    "options": None,
                    "llm": True,
                    "steps": [],
                    "used_tools": False,
                    "usage": None,
                    "duration_ms": 0,
                    "paused": False,
                    "paused_step_id": None,
                    "canceled": False,
                }
        except Exception:  # noqa: BLE001
            pass

        if step_id:
            # 用户在追问框里回答：从挂起那步恢复，不重跑
            result = await self.executor.resume(
                step_id,
                text,
                should_stop=should_stop,
                on_step=on_step,
                on_delta=on_delta,
            )
        else:
            result = await self.executor.chat(
                text,
                session_message_id=user_message_id,
                history=history,
                memory_hint=memory_hint,
                should_stop=should_stop,
                on_step=on_step,
                on_delta=on_delta,
                # 预算守卫：本轮多步调用也要被拦，而不是只挡在会话开始那一次
                session_id=session_id,
            )

        # 落库助手回复（生成 message_id）。被用户叫停（canceled）且尚未产出任何
        # 正文时，不落库空助手消息——否则重载会话会多出一条空白回复。
        answer_text = result.answer or ""
        if getattr(result, "canceled", False) and not answer_text.strip():
            assistant_message_id = None
        else:
            assistant_message_id = await self._append_message(
                session_id, "assistant", answer_text
            )

        # 记忆只在「最终完成」时落盘：被用户叫停（canceled）或仍在追问则跳过，
        # 不写、不摘要（见 doc/memory.md 7.5.4）
        captured: list = []
        # 预算触顶收尾的轮次不写记忆：摘要本身还要再调一次 LLM，那就等于
        # 拦完又超支（验收口径是「实际消耗不超过上限」）。
        if (
            not result.ask
            and not getattr(result, "canceled", False)
            and not getattr(result, "budget_stopped", False)
        ):
            anchor_question = (
                text if not step_id else await self._question_of(anchor_message_id)
            )
            await self._write_memory(
                session_id, anchor_message_id, anchor_question, result
            )
            # 产物捕获：与记忆同时机（最终完成才登记）。覆盖两类动作：
            # 1) 本轮 fs.write_file 生成的；2) agent 用 fs.publish 挂载的工作空间已有文件
            captured = await self._capture_artifacts(
                result, assistant_message_id, session_id
            )
            # 兜底：本轮一个产物都没登记，但工作区里确实出现了本轮新建的文件
            # → 补登记（详见 _capture_unpublished_files）。
            # 「一个都没有」是唯一可靠的触发条件：登记过说明模型按规则 publish 了，
            # 不干预；一个都没有而文件又真实存在，说明文件是 bash 生成的、或模型
            # 撞上步数上限来不及 publish——两种情况下用户都拿不到卡片。
            if not captured and assistant_message_id:
                captured = await self._capture_unpublished_files(
                    assistant_message_id,
                    session_id,
                    since_ts=time.time() - (time.perf_counter() - t0),
                )

        # 可观测：本轮端到端耗时。记在 user 消息上与 llm_calls 的 message_id
        # 归属保持一致，便于「这一轮花了多少 token、多久」一起查。
        from ..observability import (
            aggregate,
            set_message_duration,
            step_metrics_for_message,
        )

        duration_ms = int((time.perf_counter() - t0) * 1000)
        logger.info("[dbg] 轮次结束：开始写耗时与聚合用量")
        await set_message_duration(user_message_id, duration_ms)
        # 逐步用量与本轮汇总一并带回：否则前端要为每个 step 各发一次请求
        step_usage = await step_metrics_for_message(user_message_id)
        usage = await aggregate(message_id=user_message_id)
        logger.info("[dbg] 聚合完成：准备返回响应")

        return {
            "session_id": session_id,
            "user_message_id": user_message_id,
            "message_id": assistant_message_id,
            "answer": result.answer,
            # 本轮产出的文件（agent 写的 HTML 等），前端实时渲染产物卡片
            # 对外剥离内部字段 ws_root（绝对工作空间根）
            "artifacts": self._public_artifacts(captured),
            # 模型在向用户提问：前端应在该条消息内渲染选项 + 追问输入框
            "waiting_human": bool(result.ask),
            "step_id": getattr(result, "ask_step_id", None),
            "options": result.options,
            "llm": self.agent.llm is not None,
            "steps": [
                {
                    "thought": s.thought,
                    "tool": s.tool,
                    "args": s.args,
                    "observation": s.observation,
                    # 与 load_messages 对齐：带 kind 让前端区分「提问 / 用户补充 /
                    # 工具结果」。Step.kind 用 "ask" 表示向用户提问，落库时记为
                    # ask_human，这里统一成同一个值。
                    "kind": "ask_human" if s.kind == "ask" else s.kind,
                    # 可观测：本步的 token / 缓存命中 / 耗时（step 序号从 1 起，
                    # 与落库的 react_steps.step 对齐）
                    "usage": step_usage.get(i + 1),
                }
                for i, s in enumerate(result.steps)
            ],
            "used_tools": result.used_tools,
            # 可观测：本轮汇总（token / 缓存命中 / 成本 / LLM 耗时）与端到端墙钟
            "usage": usage,
            "duration_ms": duration_ms,
            # 按 step 暂停：这一轮是被用户叫停的（已跑完的步骤都已落库）。
            # paused_step_id 即断点，下次执行从它续跑（重放已完成步骤继续）。
            "paused": bool(getattr(result, "paused", False)),
            "paused_step_id": getattr(result, "paused_step_id", None),
            # 用户在生成途中点了「停止生成」：已生成的内容已落库，前端据此后处理
            "canceled": bool(getattr(result, "canceled", False)),
        }

    async def resume_paused(
        self,
        step_id: str,
        *,
        should_stop=None,
        on_step=None,
        on_delta=None,
    ) -> Dict[str, Any]:
        """从「用户暂停」的断点继续跑：不新增 user 消息、不追加 human_answer。

        与 ``ask(step_id=...)`` 的区别：那是「用户回答追问」的恢复（会把回答落库成
        一条新的 user 消息）；这是「用户点了暂停」的恢复——没有新回答，只是把断点
        之前**已落库的步骤**重放给模型，从断点接着走（即「前 4 步拿出来拼接，继续
        走第 5 步」），已做完的工作不会重做。
        """
        if self.agent is None:
            return {"answer": "（agent 实例未就绪，服务尚未启动完成）", "llm": False}

        session_id = await self._ensure_session()
        set_session_id(session_id)
        set_agent(self.agent_id, getattr(self.agent, "name", None))
        set_workspace(await resolve_workspace(self.agent, session_id))

        # 与 ask 同理：同一会话串行执行，避免续跑与新一轮（或另一次续跑）并发写坏
        # seq、或互相覆盖挂起状态。
        async with self._session_lock(session_id):
            result = await self.executor.resume_paused(
                step_id,
                should_stop=should_stop,
                on_step=on_step,
                on_delta=on_delta,
            )

            # 与 ask 一致：助手回复落库成一条 assistant 消息。
            # 注：这里不写记忆——续跑的这轮可能再次被暂停，只有真正给出最终回答时才
            # 该沉淀（判定与 ask 的 ``if not result.ask`` 同理，此处一并跳过待后续统一）。
            assistant_message_id = await self._append_message(
                session_id, "assistant", result.answer or ""
            )
        return {
            "session_id": session_id,
            "message_id": assistant_message_id,
            "answer": result.answer or "",
            "waiting_human": bool(result.ask),
            "step_id": getattr(result, "ask_step_id", None),
            "options": result.options,
            "llm": self.agent.llm is not None,
            "steps": [
                {
                    "thought": s.thought,
                    "tool": s.tool,
                    "args": s.args,
                    "observation": s.observation,
                    "kind": "ask_human" if s.kind == "ask" else s.kind,
                    # 可观测：本步的 token / 缓存命中 / 耗时（step 序号从 1 起，
                    # 与落库的 react_steps.step 对齐）
                    "usage": step_usage.get(i + 1),
                }
                for i, s in enumerate(result.steps)
            ],
            "used_tools": result.used_tools,
            # 可观测：本轮汇总（token / 缓存命中 / 成本 / LLM 耗时）与端到端墙钟
            "usage": usage,
            "duration_ms": duration_ms,
            "paused": bool(getattr(result, "paused", False)),
            "paused_step_id": getattr(result, "paused_step_id", None),
            # 用户在生成途中点了「停止生成」：已生成的内容已落库，前端据此后处理
            "canceled": bool(getattr(result, "canceled", False)),
        }

    # ---- 短期记忆（keeper/memory，见 doc/memory.md 7.5 / R0 / 混合召回）----
    @staticmethod
    def _build_history(
        session_id: str,
        *,
        exclude_message_id: Optional[str],
        question: Optional[str] = None,
        limit: int = 10,
        recall_limit: int = 5,
    ) -> tuple:
        """构造注入 LLM 的上下文，返回 ``(history_messages, memory_hint)``。

        为省 token，**历史一律以「摘要」形式注入 system prompt（memory_hint）**，
        不再把最近 N 轮的全文塞进 messages；模型需要某轮完整内容时用 ``read(block_id)`` 展开。

        - 「最近 N 轮摘要」：最近 ``limit`` 轮（Turn 块）的块摘要（带 block_id）；
          摘要缺失时回退为用户问题 + 助手回答的极简文本，保证连续性不丢。
        - 「更早的相关记忆」：L1 系统侧自动召回——用 ``question`` 对记忆索引做 OR 宽松检索，
          取命中、且**不在最近 N 轮窗口内**的旧轮摘要，补进同一段。
          根除「LLM 忘记调用 recall 工具」的漏检（见 7.5 混合方案）。
        """
        from ..memory import SessionMemory

        sm = SessionMemory(session_id)
        try:
            recs = sm.load_recent_turns(limit=limit, exclude_message_id=exclude_message_id)
            recent_ids = {r.get("message_id") for r in recs}
            lines: list = []

            # 1) 最近 N 轮：摘要注入（省 token；全文可 read 展开）
            if recs:
                lines.append("【最近 N 轮摘要】")
                for r in recs:
                    mid = r.get("message_id")
                    bid = f"turn:{mid}" if mid else ""
                    summary = r.get("summary") or ""
                    if not summary:
                        # 摘要缺失（如 LLM 摘要失败）时的极简回退，保证连续性
                        q = (r.get("question") or "").strip()
                        a = (r.get("answer") or "").strip()
                        summary = f"用户问：{q}；助手答：{a[:200]}"
                    lines.append(f"- block_id={bid}  摘要: {summary}")

            # 2) L1 自动召回：用当前问题 OR 检索，补最近 N 轮之外的相关旧轮摘要
            if question and question.strip():
                hits = sm.recall(question, limit=recall_limit, mode="or")
                older: list = []
                for block_id, summary in hits:
                    mid = block_id[len("turn:"):] if block_id.startswith("turn:") else block_id
                    if mid in recent_ids:
                        continue  # 已在「最近 N 轮摘要」中出现，不重复
                    if summary:
                        older.append(f"- block_id={block_id}  摘要: {summary}")
                if older:
                    lines.append("【更早的相关记忆（系统按当前问题自动召回）】")
                    lines.extend(older)

            memory_hint = "\n".join(lines)
        finally:
            sm.close()
        return [], memory_hint

    async def _write_memory(
        self, session_id: str, anchor_message_id: str, question: str, result: Any
    ) -> None:
        """最终完成时把整条话轮写进短期记忆（含过程中的追问 / 用户补充）。

        只写 **Turn 块**（turns.jsonl）——它已是 user/assistant/steps 的超集，且是
        recall / read_turn / load_recent_turns 唯一读取的文件。步骤链以 ``anchor_message_id``
        为锚，从 DB（react_steps）按 step 升序读全量，天然涵盖 ask_human / human_answer，
        跨请求也完整（见 doc/memory.md 7.5.4）。``add_turn`` 按 message_id 幂等覆盖。
        """
        from ..memory import SessionMemory, summarize_turn

        sm = SessionMemory(session_id)
        try:
            # 1) 以锚点消息为归属，从 DB 读整条步骤链（含追问），按 step 升序
            factory = get_session_factory()
            async with factory() as db:
                rows = await db.execute(
                    select(ReactStep)
                    .where(ReactStep.session_message_id == anchor_message_id)
                    .order_by(ReactStep.step)
                )
                records = list(rows.scalars())

            turn_steps: list = [self._react_step_to_turn(r) for r in records]
            rec = {
                "message_id": anchor_message_id,
                "question": question,
                "answer": result.answer,
                "steps": turn_steps,
            }
            # 2) 摘要输入仅截断单步 observation，最终回答保留完整（省 token）
            turn_text = sm.render_turn_text(self._truncate_turn_for_summary(rec))
            try:
                summary = await summarize_turn(turn_text, self.agent.llm)
            except Exception as e:
                logger.warning("记忆块摘要生成失败，仅存原文: %s", e)
                summary = ""
            sm.add_turn(
                message_id=anchor_message_id,
                question=question,
                answer=result.answer,
                steps=turn_steps,
                summary=summary,
            )
        finally:
            sm.close()

    @staticmethod
    def _react_step_to_turn(row) -> dict:
        """把 DB 里的 ReactStep 还原成 turn 步骤片段。

        ReactStep.kind：工具步=工具名、ask_human、human_answer、think。
        工具步的 input/output 取自 row.input/output；ask / human_answer 内容在 output。
        """
        kind = row.kind
        if kind == "ask_human":
            return {
                "step": row.step,
                "kind": "ask",
                "tool": None,
                "input": None,
                "output": row.output or "",
            }
        if kind == "human_answer":
            return {
                "step": row.step,
                "kind": "human_answer",
                "tool": None,
                "input": None,
                "output": row.output or "",
            }
        if kind == "think":
            return {
                "step": row.step,
                "kind": "think",
                "tool": None,
                "input": None,
                "output": None,
            }
        # 其余：工具步，kind 即工具名
        return {
            "step": row.step,
            "kind": kind,
            "tool": kind,
            "input": row.input,
            "output": row.output or "",
        }

    def _truncate_turn_for_summary(self, rec: dict) -> dict:
        """拷贝并截断每个 step 的 output（保留前 N 字符），长工具输出不撑爆摘要输入。"""
        steps = []
        for st in rec.get("steps") or []:
            c = dict(st)
            if c.get("output"):
                c["output"] = c["output"][:_TURN_SUMMARY_OBS_LIMIT]
            steps.append(c)
        r = dict(rec)
        r["steps"] = steps
        return r

    async def _question_of(self, message_id: str) -> str:
        """取某条 SessionMessage 的内容（恢复时回溯原始问题文本）。"""
        factory = get_session_factory()
        async with factory() as db:
            row = await db.get(SessionMessage, message_id)
            return row.content if row is not None else ""

    async def ask_peer(self, peer: str, question: str) -> Dict[str, Any]:
        """向某个对端 agent 提问，往来记进 threads / agent_messages。

        与 ask() 的区别：ask() 处理「别人问我」，这里处理「我问别人」——
        存储落在 PeerService 那两张表，不进 session_messages。

        session_id 显式传给 PeerService（本方法调用前已由 _ensure_session 确定），
        不走隐式上下文：一次调用基于哪个会话，看参数就知道。
        """
        if not self.session_id:
            await self._ensure_session()
        return await PeerService(
            str(self.session_id), peer, agent_id=self.agent_id
        ).ask(question)

    async def _ensure_session(self) -> str:
        """有 session_id 则沿用；没有则新建 chat_session。

        传了却查不到时取决于 upsert：
        - False：抛 ValueError。不静默新建，否则前端拿着过期 id 会一直在新会话里
          发消息却不自知
        - True：用它新建。跨端调用时发起方给的 thread_id 本库必然没有，
          按契约要拿它当自己这侧的 session_id，两端才能对齐同一条链
        """
        factory = get_session_factory()
        async with factory() as db:
            if self.session_id:
                row = await db.get(ChatSession, self.session_id)
                if row is not None:
                    # 防串台：session 已存在但归属别的 agent，绝不允许写入。
                    if self.agent_id is not None and row.agent_id != self.agent_id:
                        raise ValueError(
                            f"session_id 属于 agent {row.agent_id}，"
                            f"与当前 agent {self.agent_id} 不符"
                        )
                    # 存量 session 未绑定 context 时补上（协议 context 解耦）
                    if self.context_id and row.context_id is None:
                        row.context_id = self.context_id
                        await db.commit()
                    return row.id
                if not self.upsert:
                    raise ValueError(f"session_id 不存在: {self.session_id}")
                row = ChatSession(
                    id=self.session_id,
                    initiator_id=self.initiator_id,
                    agent_id=self.agent_id,
                    context_id=self.context_id,
                    user_space_id=self.user_space_id,
                )
                db.add(row)
                await db.commit()
                return row.id

            # 无 session_id：按协议 context 解析（入站跨端——context 与 session
            # 主键解耦，不能把 contextId 直接当 session_id 用）。
            if self.context_id:
                r = await db.execute(
                    select(ChatSession).where(
                        ChatSession.context_id == self.context_id
                    )
                )
                existing = r.scalars().first()
                if existing is not None:
                    return existing.id
                row = ChatSession(
                    id=new_id(),
                    initiator_id=self.initiator_id,
                    agent_id=self.agent_id,
                    context_id=self.context_id,
                    user_space_id=self.user_space_id,
                )
                db.add(row)
                await db.commit()
                self.session_id = row.id
                return row.id

            row = ChatSession(
                initiator_id=self.initiator_id,
                agent_id=self.agent_id,
                context_id=self.context_id,
                user_space_id=self.user_space_id,
            )
            db.add(row)
            await db.commit()
            self.session_id = row.id
            return row.id

    async def _resolve_step(self, step_id: str) -> str:
        """用户回答了某条挂起提问：把该步骤置为 done，pending 随之消失，并返回其归属的消息 id（作记忆锚点）。

        随后由 AgentExecutor.resume() 依据这些已落库的步骤重建上下文续跑（不重跑）。
        """
        factory = get_session_factory()
        async with factory() as db:
            row = await db.get(ReactStep, step_id)
            if row is not None and row.status == "suspended":
                row.status = "done"
                await db.commit()
            return row.session_message_id if row is not None else ""

    async def _abandon_suspended(self, session_id: str) -> None:
        """把该会话所有挂起的步骤标为 abandoned。

        约束：一个会话同时最多一个 suspended。开新话轮前先清掉，
        避免"用户已经换话题了，那边还挂着等回复"。
        """
        factory = get_session_factory()
        async with factory() as db:
            rows = await db.execute(
                select(ReactStep)
                .join(SessionMessage, ReactStep.session_message_id == SessionMessage.id)
                .where(
                    SessionMessage.chat_session_id == session_id,
                    ReactStep.status == "suspended",
                )
            )
            for s in rows.scalars():
                s.status = "abandoned"
            await db.commit()

    async def _append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        message_id: Optional[str] = None,
    ) -> str:
        """追加一条消息（seq 取当前会话最大值 +1），返回 message_id。

        message_id 由调用方给出时沿用——跨端调用时两端存同一条消息需要同 id。
        """
        factory = get_session_factory()
        async with factory() as db:
            msg = SessionMessage(
                id=message_id or new_id(),
                chat_session_id=session_id,
                seq=await self._next_seq(db, session_id),
                role=role,
                content=content,
            )
            db.add(msg)
            await db.commit()
            return msg.id

    async def list_sessions(self, limit: int = 50) -> list[Dict[str, Any]]:
        """会话列表，按最近更新排序。

        **必须按 agent_id 过滤**：一个进程装多个 agent 时，同一个人
        （initiator_id 相同）在不同 agent 下的会话要分开，否则切换 agent
        会看到别的 agent 的历史。给了 initiator_id 再叠加发起人过滤。
        """
        factory = get_session_factory()
        async with factory() as db:
            stmt = select(ChatSession).order_by(ChatSession.updated_at.desc()).limit(limit)
            if self.agent_id is not None:
                stmt = stmt.where(ChatSession.agent_id == self.agent_id)
            if self.initiator_id is not None:
                stmt = stmt.where(ChatSession.initiator_id == self.initiator_id)
            rows = await db.execute(stmt)
            items = list(rows.scalars())
        return [
            {
                "id": s.id,
                "title": s.title,
                "initiator_id": s.initiator_id,
                "agent_id": s.agent_id,
                "user_space_id": s.user_space_id,
                "status": s.status,
                "kind": s.kind,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            }
            for s in items
        ]

    async def verify_session(self) -> None:
        """校验 self.session_id 存在且归属当前 agent_id；不存在 / 串台则抛 ValueError。

        接口层据此转 404：浏览器用过期或伪造的 session_id 拉历史时，
        不会越权读到别的 agent 的会话。
        """
        if not self.session_id:
            raise ValueError("缺少 session_id")
        factory = get_session_factory()
        async with factory() as db:
            row = await db.get(ChatSession, self.session_id)
        if row is None:
            raise ValueError(f"会话不存在: {self.session_id}")
        if self.agent_id is not None and row.agent_id != self.agent_id:
            raise ValueError(
                f"会话 {self.session_id} 不属于 agent {self.agent_id}"
            )

    @staticmethod
    def _react_step_to_agent_step(s) -> Dict[str, Any]:
        """把落库的 ReactStep 还原成前端 AgentStep 形状（thought/tool/args/observation）。

        - think 步：思考文存在 input 列；
        - 工具步：input 列是 {"thought":..., "args":...} 的 JSON，observation 在 output；
        - ask_human / human_answer：问题/回答放 observation，不渲染「调用工具」。
        """
        raw_in = s.input or ""
        kind = s.kind
        if kind == "think":
            return {
                "thought": raw_in, "tool": None, "args": None,
                "observation": None, "kind": kind,
            }
        if kind in ("ask_human", "human_answer"):
            # 带上 kind：前端据此把「向用户提问」渲染成「提问：」、「用户补充」渲染成
            # 「用户补充：」，否则它们会被当成工具结果渲染成「结果：」，语义不对。
            return {
                "thought": None, "tool": None, "args": None,
                "observation": s.output, "kind": kind,
            }
        thought = None
        args = None
        if raw_in:
            try:
                d = json.loads(raw_in)
                thought = d.get("thought")
                args = d.get("args")
            except (json.JSONDecodeError, TypeError):
                thought = raw_in
        return {
            "thought": thought, "tool": kind, "args": args,
            "observation": s.output, "kind": kind,
        }

    @staticmethod
    def _split_steps_by_round(raw: list) -> list:
        """按 human_answer 边界把一轮 steps 切成若干段，依次归属各条 assistant 回复。

        一轮完整交互可能含追问：user 提问 → agent 思考/调工具 → ask_human（提问）
        → 用户回答（human_answer）→ agent 继续思考 → 最终回答。这些 steps **全部挂在
        最初的 user 消息下**（executor 以 user_message_id 落库，resume 也沿用同一个
        session_message_id），但 UI 上应分别归属到对应的 assistant 回复：提问那条
        只显示提问前的步骤，最终回答那条显示用户回答之后的步骤。
        """
        segs: list = []
        cur: list = []
        for kind, st in raw:
            if kind == "human_answer":
                # 用户回答：结束上一段（提问那轮），开启归属下一条回复的新段
                if cur:
                    segs.append(cur)
                    cur = []
                cur.append(st)
                continue
            cur.append(st)
        if cur:
            segs.append(cur)
        return segs

    async def load_messages(self, session_id: str, limit: int = 200) -> Dict[str, Any]:
        """取某会话最近 limit 条消息（按 seq 升序），并带回挂起的追问（若有）。

        先倒序取再反转，保证拿到的是"最近 N 条"而不是"最早 N 条"。
        pending 供前端刷新后恢复追问框：按约束，一个会话最多一个 suspended 步骤。
        """
        factory = get_session_factory()
        async with factory() as db:
            rows = await db.execute(
                select(SessionMessage)
                .where(SessionMessage.chat_session_id == session_id)
                .order_by(SessionMessage.seq.desc())
                .limit(limit)
            )
            items = list(rows.scalars())

            pending_payload = None
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
            pending: Optional[ReactStep] = pr.scalars().first()
            if pending is not None:
                pending_payload: Dict[str, Any] = {
                    "step_id": pending.id,
                    "question": pending.output,
                    "options": self._pending_options(pending),
                    "message_id": None,
                }
                # 承载该提问的 assistant 消息 = 触发它的 user 消息的下一条
                ur = await db.execute(
                    select(SessionMessage).where(
                        SessionMessage.id == pending.session_message_id
                    )
                )
                umsg: Optional[SessionMessage] = ur.scalars().first()
                if umsg is not None:
                    ar = await db.execute(
                        select(SessionMessage).where(
                            SessionMessage.chat_session_id == session_id,
                            SessionMessage.seq == umsg.seq + 1,
                        )
                    )
                    amsg: Optional[SessionMessage] = ar.scalars().first()
                    pending_payload["message_id"] = amsg.id if amsg else None

            # 把逐步思考（react_steps）按消息聚合，前端据此展示「思考过程」。
            # 注意：SQLite 不支持 IN () 空列表；items 为空（新会话 / 无消息）时
            # 必须跳过该查询，否则会抛语法错误令整条 /messages 接口 500，前端
            # catch 后历史消息全丢、只剩欢迎语（表现为「会话页没内容」）。
            steps_by_msg: Dict[str, list] = {}
            if items:
                try:
                    step_rows = (
                        await db.execute(
                            select(ReactStep)
                            .where(
                                ReactStep.session_message_id.in_(
                                    [m.id for m in items]
                                )
                            )
                            .order_by(ReactStep.step)
                        )
                    ).scalars().all()
                    for s in step_rows:
                        # 存 (kind, step)：kind 用于按 human_answer 边界切段，
                        # step 才是给前端的 AgentStep 形状。
                        steps_by_msg.setdefault(s.session_message_id, []).append(
                            (s.kind, self._react_step_to_agent_step(s))
                        )
                except Exception as e:  # 思考步骤查询失败不应拖垮主消息列表
                    logger.warning("加载 react_steps 失败（已忽略）：%s", e)

        items.reverse()

        # steps 归位：react_steps 挂在「触发该轮的 user 消息」下（executor 以
        # user_message_id 落库，全局 345 条 steps 全在 user 消息上），但 UI 只在
        # assistant 气泡里渲染思考过程（ChatBubble 判断 role==="assistant"、
        # TaskPage 过滤掉 user），历史轮次会因此永远看不到步骤。
        #
        # 一轮可能含追问（ask_human → human_answer → 继续思考 → 最终回答），其 steps
        # 全挂在同一条 user 消息下，却对应多条 assistant 回复（提问 / 最终回答）。
        # 这里先按 human_answer 边界切段，再把各段依次分配给该 user 消息之后的
        # 各条 assistant 消息；user 消息自身不再带 steps。
        final_steps: Dict[str, list] = {}
        carry: list = []  # 待分配的段（已按 human_answer 边界切好）
        acc: list = []    # 已分配给前面 assistant 回复的步骤（累积）
        for m in items:
            if m.role == "user":
                raw = steps_by_msg.get(m.id) or []
                if raw:
                    # 只有该 user 消息自带 steps 时才重置。追问回答这类 user 消息
                    # 本身没有 steps（它们挂在最初那条 user 消息下），若在这里
                    # 清空 carry，后续 assistant 就拿不到剩余段了。
                    carry = self._split_steps_by_round(raw)
                    acc = []  # 新一轮，累积清零
                final_steps[m.id] = []
            else:
                if carry:
                    # 累积：后面的回复要带上前面所有的思考步骤。模型 resume 时
                    # 重放的本来就是整条链路（提问前的步骤 + 用户回答 + 后续思考），
                    # UI 展示应与之一致——最终回答能看到完整推理过程。
                    acc = acc + carry.pop(0)
                    final_steps[m.id] = acc
                else:
                    final_steps[m.id] = [
                        st for _, st in (steps_by_msg.get(m.id) or [])
                    ]

        # 可观测：把用量挂到每条消息上，刷新后历史轮次也能看到消耗。
        # llm_calls 的 message_id 归属是**触发该轮的 user 消息**，而 UI 在
        # assistant 气泡里展示，故 assistant 取「所属轮 user」的用量。
        usage_out: Dict[str, Any] = {}
        try:
            from ..observability import usage_by_messages

            by_mid = await usage_by_messages([m.id for m in items])
            cur_round: Optional[Dict[str, Any]] = None
            for m in items:
                if m.role == "user":
                    cur_round = by_mid.get(m.id)
                    usage_out[m.id] = cur_round
                else:
                    usage_out[m.id] = cur_round
        except Exception as e:  # 用量加载失败不应拖垮消息列表
            logger.debug("加载历史消息用量失败（已忽略）：%s", e)

        return {
            "session_id": session_id,
            "messages": [
                {
                    "id": m.id,
                    "seq": m.seq,
                    "role": m.role,
                    "content": m.content,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                    "artifacts": self._public_artifacts(
                        json.loads(m.artifacts) if m.artifacts else []
                    ),
                    "steps": final_steps.get(m.id, []),
                    # 可观测：本轮用量 + 端到端墙钟（usage 只含 LLM 耗时）
                    "usage": usage_out.get(m.id),
                    "duration_ms": m.duration_ms,
                }
                for m in items
            ],
            "pending": pending_payload,
        }

    @staticmethod
    def _pending_options(pending) -> Optional[list]:
        """从挂起步骤的 input（JSON）里取回可选项。"""
        try:
            return (json.loads(pending.input) or {}).get("options")
        except Exception:
            return None

    @staticmethod
    async def _next_seq(db, session_id: str) -> int:
        """会话内下一个 seq。查库取 max+1，不用内存自增（进程重启/并发都不会撞）。"""
        r = await db.execute(
            select(func.coalesce(func.max(SessionMessage.seq), 0)).where(
                SessionMessage.chat_session_id == session_id
            )
        )
        return int(r.scalar_one()) + 1

    @staticmethod
    def _public_artifacts(arts: list) -> list:
        """对外（前端）产物列表：剥掉内部字段 ``path``（绝对路径），只留展示用字段。

        绝对路径只存 DB、绝不外传——避免把用户真实目录（可能是 home 下的软链）
        泄露到前端 / 网络。文件服务内部读 DB 直接用 ``path`` 定位真实文件。
        前端构建访问 URL 只用 ``id``，本就不需要 ``path``。
        """
        out = []
        for a in arts:
            a = dict(a)
            a.pop("path", None)
            out.append(a)
        return out

    # 兜底扫描的边界：只在「本轮零产物」时跑一次，所以宁可保守
    _SWEEP_SKIP_DIRS = frozenset(
        {"node_modules", "__pycache__", ".venv", "venv", ".git", ".idea", ".vscode"}
    )
    _SWEEP_MAX_BYTES = 20 * 1024 * 1024  # 单个文件上限，避免把日志 / 转 dumps 挂上来
    _SWEEP_MAX_FILES = 5  # 一次最多补登记几个

    def _sweep_new_files(self, root: Path, since_ts: float) -> list:
        """扫出 root 下「本轮开始之后新建 / 改过」的文件，按 mtime 倒序。

        单独拆出来是因为这段是**纯文件系统逻辑**，没有 await、没有 DB，
        可以直接单测——落库那段再单独看。
        """
        found: list = []
        try:
            for p in root.rglob("*"):
                try:
                    rel_parts = p.relative_to(root).parts
                except ValueError:
                    continue
                # 隐藏目录 / 依赖目录一律跳过：它们是过程产物，挂上去只会干扰
                if any(
                    part in self._SWEEP_SKIP_DIRS or part.startswith(".")
                    for part in rel_parts
                ):
                    continue
                if not p.is_file():
                    continue
                st = p.stat()
                if st.st_mtime < since_ts - 2:  # 2 秒容差：文件系统时间戳精度
                    continue
                if st.st_size > self._SWEEP_MAX_BYTES:
                    continue
                found.append((st.st_mtime, st.st_size, p))
        except Exception as e:  # noqa: BLE001 扫盘失败只是兜底没做成
            logger.debug("兜底扫描失败: %s", e)
            return []
        found.sort(key=lambda x: x[0], reverse=True)
        return found

    async def _capture_unpublished_files(
        self, assistant_message_id: str, session_id: str, *, since_ts: float
    ) -> list:
        """本轮**零产物**时，把工作区里本轮新建 / 改过的文件补登记为产物。

        为什么需要
        ----------
        `_capture_artifacts` 只认 ``fs.write_file`` 与 ``fs.publish`` 两步动作。
        但文件经常是**别的动作**造出来的：``cp a b``、``cat a > b``、``npm run build``。
        这类文件真实存在、模型也在回答里说了名字，却因为没有对应的那两个动作
        而**不产生产物卡片**——用户只看到一句「文件在工作区」，点不开。
        实测一轮 30 步写 HTML 游戏就是这么结束的：``cp`` 生成了 down100.html，
        模型忙着调自设的测试指标，``fs.publish`` 从头到尾没发过。

        为什么限定「零产物」才扫
        ------------------------
        模型按规则 publish 过就不干预——那时它自己对「哪个是交付物」做过判断，
        比按 mtime 扫出来的准。只在它一个都没登记时才兜底。

        为什么按 mtime 扫而不是解析命令
        --------------------------------
        从 bash 命令文本里猜输出文件不可靠（重定向、管道、脚本里再写文件……）。
        mtime 是**结果**，且限定在本轮开始之后，误伤面很小。
        """
        try:
            ws = await resolve_workspace(self.agent, session_id)
            root = ws.root
        except Exception as e:  # noqa: BLE001 拿不到工作区就放弃兜底，不影响主流程
            logger.debug("兜底扫描跳过（工作区不可用）: %s", e)
            return []
        if root is None or not Path(root).exists():
            return []

        found = self._sweep_new_files(Path(root), since_ts)
        if not found:
            return []

        artifacts: list = []
        seen: set = set()
        for mtime, size, p in found[: self._SWEEP_MAX_FILES]:
            rel = str(p.relative_to(root))
            aid = hashlib.sha1(
                f"{assistant_message_id}:{rel}".encode()
            ).hexdigest()[:12]
            if aid in seen:
                continue
            seen.add(aid)
            artifacts.append(
                {
                    "id": aid,
                    "name": p.name,
                    "path": "file://" + str(p),
                    "mime": _guess_mime(p.name),
                    "size": size,
                }
            )
        if not artifacts:
            return []
        try:
            factory = get_session_factory()
            async with factory() as db:
                await db.execute(
                    update(SessionMessage)
                    .where(SessionMessage.id == assistant_message_id)
                    .values(artifacts=json.dumps(artifacts, ensure_ascii=False))
                )
                await db.commit()
            logger.info(
                "本轮无 fs.publish，兜底登记 %s 个产物: %s",
                len(artifacts),
                "、".join(a["name"] for a in artifacts),
            )
        except Exception as e:  # noqa: BLE001 落库失败只记日志
            logger.warning("兜底产物落库失败: %s", e)
            return []
        return artifacts

    async def _capture_artifacts(
        self, result, assistant_message_id: str, session_id: str
    ) -> list:
        """扫本轮 ReAct 步骤，把成功写入 / 成功发布的**交付物**登记为产物。

        零解析文本、零改工具：完全依赖结构化的 react_steps。捕获两类动作：
        - ``fs.write_file``：agent 本轮新写出的文件；
        - ``fs.publish``：agent 把工作空间里**已存在**的文件登记为产物（用于「帮我找
          xx 文件并展示」——agent 先用 ``fs.find`` 定位，再 ``fs.publish`` 挂载）。

        **``@tmp/`` 开头的一律不登记**：那是会话私有临时目录，放的是临时测试脚本、补丁
        脚本、同一文件的中间版本——属于**过程**而不是交付。登记它们只会让会话里堆满
        一次性的check.py / patch_*.js，而这些东西既不该给用户看，收尾时还会被 agent
        清掉，列出来点了直接 404。要追溯过程看 ``react_steps`` 里那几步 write_file
        记录即可，文件本身也还在临时目录里。

        失败 / 拒绝的动作（observation 以 ``[`` 开头）不登记。每条产物的 ``path`` 直接
        存**写入 / 发布时刻的绝对路径**（``safe_path`` 解析并校验过，杜绝 ``../`` 穿越）：
        这样即使会话后来切换了工作空间、或工作空间整体换了目录，旧产物仍能按当时的
        真实绝对路径定位。``path`` 仅存 DB 内部，对外响应经 ``_public_artifacts`` 剥离，
        前端拿不到绝对路径。
        """
        # 写入 / 发布时刻的真实根：以 (会话, agent) 解析，与 fs 工具实际生效的根一致。
        # 剔掉 ``@tmp/`` 后剩下的都是工作空间内的相对路径，故用 ``safe_path`` 即可
        # （``@tmp/`` 会解析到工作空间外的 agent_space，是另一套边界检查，不适用）。
        try:
            write_root = (await resolve_workspace(self.agent, session_id)).root
        except Exception:
            write_root = None
        artifacts: list = []
        seen: set = set()
        for step in result.steps:
            if getattr(step, "tool", None) not in ("fs.write_file", "fs.publish"):
                continue
            rel = (step.args or {}).get("path")
            obs = step.observation or ""
            if not rel or obs.startswith("["):
                # 缺参数 / 拒绝 / 只读限制等错误均以 [ 开头
                continue
            rel = rel.strip().strip("/")
            if not rel:
                continue
            # 临时目录里的东西不是交付物，不登记为产物
            if rel == TMP_PREFIX or rel.startswith(TMP_PREFIX + "/"):
                continue
            aid = hashlib.sha1(f"{assistant_message_id}:{rel}".encode()).hexdigest()[:12]
            if aid in seen:
                continue
            seen.add(aid)
            name = Path(rel).name or rel
            # 解析成绝对路径并校验不越界（写入侧兜底）；解析失败则退回相对路径。
            # 存为file:// scheme 的 URI（与产物 URI 设计一致；旧裸路径数据默认按 file 处理）。
            if write_root:
                try:
                    abs_path = "file://" + str(safe_path(write_root, rel))
                except PathEscape:
                    continue
            else:
                abs_path = rel
            artifacts.append(
                {
                    "id": aid,
                    "name": name,
                    "path": abs_path,
                    "mime": _guess_mime(name),
                    "size": None,
                }
            )
        if not artifacts:
            return []
        factory = get_session_factory()
        async with factory() as db:
            await db.execute(
                update(SessionMessage)
                .where(SessionMessage.id == assistant_message_id)
                .values(artifacts=json.dumps(artifacts, ensure_ascii=False))
            )
            await db.commit()
        return artifacts
