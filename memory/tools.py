"""agentic retrieval 工具：``recall`` / ``read``（见 doc/memory.md 7.5）。

读侧 / 检索侧工具（与 5.2 否决的「写侧工具化」不矛盾）：让模型自主决定
拉取哪些记忆块——先 ``recall(query)`` 拿到命中块的摘要与 block_id，再对
认为相关的块 ``read(block_id)`` 惰性展开整块原文。从而在「补齐孤立 step 缺上下文」
与「过滤无关记忆」之间取得平衡。

工具运行在 ReAct 循环内，会话归属经 ``chat.context.current_session_id()`` 取，
不闭包捕获（避免并发串台，见 context.py 说明）。
"""
from __future__ import annotations

from ..chat.context import current_session_id
from ..tools.base import ProcessorTool, ToolRegistry
from .store import SessionMemory

_RECALL_DESC = (
    "（自救式检索）系统已按当前问题自动把「可能相关的历史记忆摘要」放进上下文，"
    "通常无需调用本工具。仅当那些摘要都不够、你确实需要换关键词再搜一遍时才调用："
    "输入检索词（关键词或实体，单个或多个），返回两类块的「摘要 + block_id」：\n"
    "- turn:xxxx —— 历史记忆块（某一轮对话的压缩）；\n"
    "- ctx:xxxx —— 本轮被外部化的上下文块（超长工具输出、被压缩掉的早期步骤原文）。"
    "**想找回之前某个工具结果但记不住 block_id 时，就用它搜。**"
    "若某块相关，再用 read 工具传入其 block_id 查看完整内容。"
)
_READ_DESC = (
    "（按需展开）按 block_id 惰性展开完整内容。支持两类块：\n"
    "- turn:xxxx —— 记忆块（某一轮对话：用户问题、你的思考/工具调用/结果、最终回答）；\n"
    "- ctx:xxxx —— 上下文块（本轮里被外部化的超长工具输出，或被压缩掉的早期步骤原文）。\n"
    "输入单个或多个 block_id（用 block_id / block_ids）。只有在你已看到某块摘要、"
    "认为它确实相关、且需要其完整细节时才调用，避免把无关内容塞进上下文。"
)


def _as_list(val) -> list:
    """把入参归一化为非空字符串列表：None→[]；str→[str]（去空）；list/tuple→逐元素取非空 str。"""
    if val is None:
        return []
    if isinstance(val, str):
        return [val] if val.strip() else []
    if isinstance(val, (list, tuple)):
        out = []
        for v in val:
            if isinstance(v, str) and v.strip():
                out.append(v)
        return out
    return [str(val)]


# 预算上限：避免「一次 read 太多块 / recall 命中太多」撑爆上下文。
# 默认值与 config.ContextSection 对齐；实际取值**现读配置**（设置页可改），
# 这里的常量只在配置拿不到时兜底。
READ_BUDGET = 6000   # read 多块展开的总字符预算（单块超限也会截断）
MAX_RECALL = 20      # recall 合并后的最大记忆块数
MAX_CTX_RECALL = 5   # recall 另外返回的「本轮外部化上下文块」上限（块内容大，少返回几条）


def _ctx_int(name: str, default: int) -> int:
    """从 context 配置里读一个整数（拿不到就用默认）。"""
    try:
        from ..agent.context_store import ctx_cfg

        cfg = ctx_cfg()
        if cfg is None or not getattr(cfg, "enabled", True):
            return default
        return int(getattr(cfg, name, default) or default)
    except Exception:  # noqa: BLE001
        return default


def build_memory_tools() -> ToolRegistry:
    """构造记忆检索工具注册表（recall / read）。"""
    reg = ToolRegistry()
    reg.register(
        ProcessorTool(
            name="recall",
            description=_RECALL_DESC,
            parameters={
                "query": "单个检索词（字符串）",
                "queries": "多个检索词（字符串数组，可一次搜多个主题，与 query 二选一）",
            },
            read_only=True,  # 纯检索：可并发
            run=_recall,
        )
    )
    reg.register(
        ProcessorTool(
            name="read",
            description=_READ_DESC,
            parameters={
                "block_id": "单个 block_id（字符串，形如 turn:xxxx）",
                "block_ids": "多个 block_id（字符串数组，一次展开多个块，与 block_id 二选一）",
            },
            read_only=True,  # 纯读取：可并发
            run=_read,
        )
    )
    return reg


async def _recall(args: dict) -> str:
    sid = current_session_id()
    if not sid:
        return "[recall 需在会话上下文中使用]"
    queries = _as_list((args or {}).get("queries")) or _as_list((args or {}).get("query"))
    if not queries:
        return "[recall: 至少需要一个非空 query（query 或 queries）]"
    sm = SessionMemory(sid)
    try:
        # 多 query 各自 OR 召回后按 block_id 去重合并（取首次出现的摘要），上限 MAX_RECALL
        seen: dict = {}
        for q in queries:
            for block_id, summary in sm.recall(q, limit=10, mode="or"):
                seen.setdefault(block_id, summary)
                if len(seen) >= MAX_RECALL:
                    break
            if len(seen) >= MAX_RECALL:
                break
    finally:
        sm.close()
    if not seen:
        lines = []
    else:
        note = (
            "" if len(seen) < MAX_RECALL
            else f"\n（命中较多，已截断至前 {MAX_RECALL} 条）"
        )
        lines = ["【历史记忆块（turn:）】"]
        for block_id, summary in seen.items():
            lines.append(f"- block_id={block_id}\n  摘要: {summary}")
        lines.append(note)

    # 本轮外部化的上下文块（超长工具输出 / 被压缩的早期步骤）：
    # 模型记不住 block_id 时，这是唯一能把它们找回来的入口。
    try:
        from ..agent.context_store import search_chunks

        ctx_hits = search_chunks(
            " ".join(queries), limit=_ctx_int("max_ctx_recall", MAX_CTX_RECALL)
        )
    except Exception:  # noqa: BLE001
        ctx_hits = []
    if ctx_hits:
        lines.append("【本轮外部化的上下文块（ctx:）】")
        for block_id, summary in ctx_hits:
            lines.append(f"- block_id={block_id}\n  摘要: {summary}")

    if not lines:
        return "[未检索到相关记忆块]"
    return "\n".join(lines)


async def _read(args: dict) -> str:
    sid = current_session_id()
    if not sid:
        return "[read 需在会话上下文中使用]"
    ids = _as_list((args or {}).get("block_ids")) or _as_list((args or {}).get("block_id"))
    if not ids:
        return "[read: 至少需要一个 block_id（block_id 或 block_ids）]"
    sm = SessionMemory(sid)
    try:
        blocks = []
        for bid in ids:
            if bid.startswith("ctx:"):
                # 上下文块：本轮外部化的工具输出 / 被压缩的早期步骤（见 doc/context-design.md）
                from ..agent.context_store import read_chunk

                blocks.append(f"=== block: {bid} ===\n{read_chunk(bid)}")
                continue
            if not bid.startswith("turn:"):
                blocks.append(
                    f"=== block: {bid} ===\n[block_id 格式不正确，应为 turn:... 或 ctx:... ]"
                )
                continue
            message_id = bid[len("turn:"):]
            blocks.append(f"=== block: {bid} ===\n{sm.read_turn(message_id)}")
    finally:
        sm.close()
    # 总字符预算截断：避免一次 read 太多块撑爆上下文
    budget = budget0 = _ctx_int("read_budget", READ_BUDGET)
    out = []
    for i, b in enumerate(blocks):
        if budget <= 0:
            out.append(f"（其余 {len(blocks) - i} 个块已省略：超出 {budget0} 字符预算）")
            break
        if len(b) <= budget:
            out.append(b)
            budget -= len(b)
        else:
            out.append(
                b[:budget]
                + f"\n…（该块已截断，剩余省略；如需完整内容可分次 read 单个 block_id）"
            )
            budget = 0
    return "\n\n".join(out)
