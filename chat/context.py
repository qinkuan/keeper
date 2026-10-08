"""当前请求的会话上下文。

工具函数在 ReAct 循环里执行时，需要知道「这次调用属于哪个会话」才能往
threads / agent_messages 记账。但工具注册表是 keeper 级别的（多会话共享），
闭包捕获 session_id 会在并发时串台——A 会话的调用记到 B 会话头上。

用 ContextVar 做请求级隔离：每个异步任务一份，天然互不干扰。
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Optional
from ulid import ULID

_current_session_id: ContextVar[Optional[str]] = ContextVar(
    "keeper_session_id", default=None
)
_current_message_id: ContextVar[Optional[str]] = ContextVar(
    "keeper_message_id", default=None
)


def current_session_id() -> Optional[str]:
    """取当前请求/任务所属的会话 id；不在任何会话中时为 None。"""
    return _current_session_id.get()


def set_session_id(session_id: Optional[str]) -> None:
    """标记当前任务所处的会话。一般在 ChatService.ask 确定 session 后调用。"""
    _current_session_id.set(session_id)


def current_message_id() -> Optional[str]:
    """取本轮那条 user 消息的 id。

    react_steps 挂在消息下（不是会话下），要把「对端在等我回答」这类状态落库，
    就得知道挂到哪条消息上。
    """
    return _current_message_id.get()


def set_message_id(message_id: Optional[str]) -> None:
    """标记本轮的 user 消息。在 ChatService.ask 落库 user 消息后调用。"""
    _current_message_id.set(message_id)


# 「对端正在等我补充」：由工具记下，由 _step_writer 取走写进 wait_ref。
# 不单独占一个 react_step —— 那样会和 Process 的 step 序号撞车。
_pending_peer_ask: ContextVar[Optional[str]] = ContextVar(
    "keeper_pending_peer_ask", default=None
)


def set_pending_peer_ask(peer_step_id: Optional[str]) -> None:
    """记下对端追问的 step_id，待写入当前步骤的 wait_ref。"""
    _pending_peer_ask.set(peer_step_id)


def take_pending_peer_ask() -> Optional[str]:
    """取走并清空（一次性，避免污染后续步骤）。"""
    v = _pending_peer_ask.get()
    if v is not None:
        _pending_peer_ask.set(None)
    return v


# 「本轮是在回答对端的哪一次追问」：由 executor.resume 从挂起步的 wait_ref 注入，
# 由对端工具取走（一次性）。
#
# 有了它，「回答追问」能**精确**恢复，不必再靠「静默回退最近一个挂起任务」去猜
# —— 猜会把**新发起的问题**误当成回答旧追问，新问题被挂到旧任务上，对端在旧任务
# 上下文里作答，于是「问上海 3 天」却拿到「内蒙古攻略」（串台）。
_peer_reply_to: ContextVar[Optional[str]] = ContextVar(
    "keeper_peer_reply_to", default=None
)


def set_peer_reply_to(task_id: Optional[str]) -> None:
    """标记：本轮是在回答对端的这个 taskId（恢复 waiting_to_peer 步骤时调用）。"""
    _peer_reply_to.set(task_id)


def take_peer_reply_to() -> Optional[str]:
    """取走并清空（一次性）：本轮确是回答追问时返回对端 taskId，否则 None。

    一次性很关键——本轮若先回答了追问、后又发起新提问，新提问不能再带上这个 id。
    """
    v = _peer_reply_to.get()
    if v is not None:
        _peer_reply_to.set(None)
    return v


# ---- 可观测：把每次 LLM 调用归属到「哪一步 / 哪个任务」 ----
#
# 与上面 session / message 那几个 ContextVar 同理：请求级隔离，天然并发安全。
# LLM 打点时读它们，用量才能落到具体 step / 任务上（见 doc/observability-design.md）。
# 之所以不用参数层层透传：调用链很深（process → llm），透参会污染一堆签名。
_current_step_seq: ContextVar[Optional[int]] = ContextVar(
    "keeper_step_seq", default=None
)
_current_task_id: ContextVar[Optional[str]] = ContextVar(
    "keeper_task_id", default=None
)
_current_trace_id: ContextVar[Optional[str]] = ContextVar(
    "keeper_trace_id", default=None
)


def set_step_seq(seq: Optional[int]) -> None:
    """标记当前正在跑第几步（轮内序号）。

    打点时用 step_seq 而非 step_id：现有流程是「先执行、后落库」，打点那一刻
    step 还没写库、拿不到 id；等 step 落库后再按 (message_id, step_seq) 回填。
    """
    _current_step_seq.set(seq)


def current_step_seq() -> Optional[int]:
    return _current_step_seq.get()


def set_task_id(task_id: Optional[str]) -> None:
    """标记当前请求所属的任务（任务模式执行时调用）；计划点用既有的 set_task_item。"""
    _current_task_id.set(task_id)


def current_task_id() -> Optional[str]:
    return _current_task_id.get()


def ensure_trace_id() -> str:
    """取（没有则生成并记住）本次请求的 trace id。

    预留给跨端链路串联 / 将来接外部 APM——本期不消费，但先记进 llm_calls，
    免得将来要补时历史数据没有。
    """
    v = _current_trace_id.get()
    if not v:
        v = str(ULID())
        _current_trace_id.set(v)
    return v


def current_trace_id() -> Optional[str]:
    return _current_trace_id.get()


# 本次 LLM 调用的**用途**：react_step / final_summary / turn_summary / other。
# 由调用方（process）在发起调用前标记，打点时读取——用来把「不对应任何 step 的
# 额外调用」（如超步数后的强制总结）与真正的 ReAct 步骤区分开。
_current_llm_kind: ContextVar[Optional[str]] = ContextVar(
    "keeper_llm_kind", default=None
)


def set_llm_call_kind(kind: Optional[str]) -> None:
    """标记下一次 LLM 调用的用途（打点后建议置回 None，避免污染后续调用）。"""
    _current_llm_kind.set(kind)


def current_llm_call_kind() -> Optional[str]:
    return _current_llm_kind.get()


# 当前请求所属的 agent：日志靠它区分多 agent（同进程装多个时日志会混在一起）。
# 由请求入口（HTTP 端点 / A2A handler）设置，logging filter 读出来挂到每条记录上。
_current_agent_id: ContextVar[Optional[str]] = ContextVar(
    "keeper_agent_id", default=None
)
_current_agent_name: ContextVar[Optional[str]] = ContextVar(
    "keeper_agent_name", default=None
)


def set_agent(agent_id: Optional[str], name: Optional[str] = None) -> None:
    """标记当前任务所属的 agent。请求入口处调用一次即可（会被子任务继承）。"""
    _current_agent_id.set(agent_id)
    _current_agent_name.set(name)


def current_agent_id() -> Optional[str]:
    return _current_agent_id.get()


def agent_label() -> str:
    """日志用的 agent 标识：``{id}({name})``；不在任何 agent 上下文里时为 ``-``。"""
    aid = _current_agent_id.get()
    if aid is None:
        return "-"
    nm = _current_agent_name.get()
    return f"{aid}({nm})" if nm else aid


# 当前请求生效的「工作空间」：由 ChatService.ask 在解析出 (会话, agent) 后注入，
# 内置工具（fs/git）执行时取文件根与只读标志；插件取 agent 私有产物目录。
# 与 agent 实例解耦——同一 agent 在不同会话里可指向不同用户空间。
from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402


@dataclass
class WorkspaceCtx:
    """一次请求生效的工作空间。

    - ``root``：用户空间路径（agent 干活的主目录）；会话没绑用户空间时回落到
      agent 默认工作目录。
    - ``read_only``：该用户空间是否只读。
    - ``agent_space``：本 (session_id, agent_id) 的私有产物目录（自动创建、
      纯路径推导、不落库），插件索引等放这里，互不污染。
    """

    root: Path
    read_only: bool
    agent_space: Path


_current_workspace: ContextVar[Optional[WorkspaceCtx]] = ContextVar(
    "keeper_workspace", default=None
)


def set_workspace(ws: WorkspaceCtx | None) -> None:
    """注入当前请求的工作空间（请求入口处调用一次）。"""
    _current_workspace.set(ws)


def current_workspace() -> WorkspaceCtx | None:
    """取当前工作空间；工具/插件执行时调用。无上下文（如构建期）返回 None。"""
    return _current_workspace.get()


# 任务模式下「当前正在执行哪个计划点」：由执行器在发一轮对话前设置，
# ``task.mark_item_done`` 工具执行时取它，才知道该把哪个点勾掉。
# 同样用 ContextVar 而不是闭包：工具注册表是多会话共享的，闭包会串台。
_current_task_item_id: ContextVar[Optional[str]] = ContextVar(
    "keeper_task_item_id", default=None
)


def set_task_item(item_id: Optional[str]) -> None:
    """标记当前正在执行的任务计划点（执行器发对话前调用一轮一次）。"""
    _current_task_item_id.set(item_id)


def current_task_item_id() -> Optional[str]:
    """取当前计划点 id；不在任务执行中时为 None（此时 mark_item_done 会拒绝）。"""
    return _current_task_item_id.get()
