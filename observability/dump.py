"""调试用：把发给大模型的**完整 prompt** 落盘到本地文件。

默认**关闭**，由 ``config.yaml`` 的 ``observability.prompt_dump`` 控制：

.. code-block:: yaml

    observability:
      prompt_dump:
        enabled: false                  # 默认关
        dir: ~/.keeper/prompt-dump
        min_prompt_tokens: 0            # 只记输入 token 超过该值的调用（0 = 全记）
        retain_days: 7                  # 过期文件自动清理

为什么不进数据库：完整 prompt 动辄几万字，入库会撑爆存储，而且它只在排查
（上下文膨胀、模型答非所问、某步为什么这么慢）时才有价值——落本地文件 + 开关
正好。落盘结构与 UI 一致：

    <dir>/<session_id>/<message_id>/<毫秒时间戳>.json

即「会话 → 某一轮 → 该轮每次 LLM 调用」，从某一轮点进去就能直接找到。

任何一步失败都只记 debug 日志——**绝不影响主流程**。
"""
# 本模块原为 ``keeper/prompt_dump.py``，属可观测的一部分（完整 prompt 落盘，
# 供事后追查「模型到底看到了什么」），故并入本包。
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from ._guard import note_if_bug, raise_if_bug, warn_bug_only

logger = logging.getLogger(__name__)

# 配置读一次文件，5 秒足够——dump 是低频调试动作，不必每次都解析 yaml。
# 另外记一份「文件指纹」：settings.yaml 被改（设置页保存或手改）时立刻重读，
# 不等 TTL——否则用户会觉得「改了没反应」。
_cfg_cache: Any = None
_cfg_cache_at: float = 0.0
_cfg_fp: Any = None
_last_cleanup_at: float = 0.0


def invalidate_cfg_cache() -> None:
    """让配置缓存立即失效（设置保存 / 手动重载后调用）。"""
    global _cfg_cache, _cfg_cache_at, _cfg_fp
    _cfg_cache = None
    _cfg_cache_at = 0.0
    _cfg_fp = None

_ROLE_BY_CLS = {
    "SystemMessage": "system",
    "HumanMessage": "user",
    "AIMessage": "assistant",
    "ToolMessage": "tool",
}


def _cfg():
    """读 prompt_dump 配置（5 秒缓存；文件一改就立即重读）。"""
    global _cfg_cache, _cfg_cache_at, _cfg_fp
    now = time.time()
    try:
        from ..setting import fingerprint

        fp: Any = fingerprint()
    except Exception as e:  # noqa: BLE001
        # 拿不到指纹只是退化成「每次都重读配置」，不影响正确性。
        warn_bug_only(e, "读 settings 指纹")
        fp = None
    if (
        _cfg_cache is not None
        and fp is not None
        and _cfg_fp == fp
        and now - _cfg_cache_at < 5
    ):
        return _cfg_cache
    try:
        from ..config import load_config

        _cfg_cache = load_config().observability.prompt_dump
        _cfg_cache_at = now
        _cfg_fp = fp
    except Exception as e:  # noqa: BLE001
        note_if_bug(e, "读取 prompt dump 配置")
        logger.debug("读取 prompt dump 配置失败: %s", e)
        _cfg_cache = None
    return _cfg_cache


def _msg_to_dict(m: Any) -> dict:
    """LangChain Message → 可序列化的 {"role", "content"}。"""
    role = _ROLE_BY_CLS.get(type(m).__name__, "user")
    content = _as_text(getattr(m, "content", ""))
    d: dict = {"role": role, "content": content}
    name = getattr(m, "name", None)
    if name:
        d["name"] = name
    return d


def _system_of(msgs) -> str:
    """取出 system 提示词（完整 prompt 里最该看的一段）。"""
    for m in msgs or []:
        if type(m).__name__ == "SystemMessage":
            c = getattr(m, "content", "")
            return c if isinstance(c, str) else str(c)
    return ""


def maybe_cleanup(base: Path, retain_days: int) -> None:
    """清理过期的 dump 文件与空目录；最多每小时跑一次，避免每次调用都扫盘。"""
    global _last_cleanup_at
    now = time.time()
    if now - _last_cleanup_at < 3600:
        return
    _last_cleanup_at = now
    try:
        if retain_days <= 0 or not base.exists():
            return
        deadline = now - retain_days * 86400
        for f in base.rglob("*.json"):
            try:
                if f.stat().st_mtime < deadline:
                    f.unlink()
            except Exception as e:  # noqa: BLE001
                warn_bug_only(e, f"清理过期 dump {f.name}")
        # 由深到浅删空目录
        for d in sorted(base.rglob("*"), key=lambda p: -len(p.parts)):
            try:
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            except Exception as e:  # noqa: BLE001
                warn_bug_only(e, f"清理空目录 {d.name}")
    except Exception as e:  # noqa: BLE001
        note_if_bug(e, "清理 prompt dump")
        logger.debug("清理 prompt dump 失败: %s", e)


def _as_text(v: Any) -> str:
    """把模型输出统一成字符串。

    LangChain v1 的 ``content`` 可能是「内容块列表」而非纯字符串，直接落盘会变成
    JSON 数组；这里统一压成文本，保证 dump 里看到的就是模型说的内容。
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        parts = []
        for b in v:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                t = b.get("text")
                if t:
                    parts.append(str(t))
                else:
                    parts.append(json.dumps(b, ensure_ascii=False))
            else:
                parts.append(str(b))
        return "".join(parts)
    return str(v)


def dump_llm_call(
    *,
    msgs,
    resp: Any = None,
    usage: tuple,
    duration_ms: Optional[int] = None,
    ttft_ms: Optional[int] = None,
    is_stream: bool = False,
    model: Optional[str] = None,
    resp_text: Optional[str] = None,
    tool_args_text: Optional[str] = None,
) -> None:
    """把这一次 LLM 调用的完整输入 / 输出落盘（开关关闭时直接返回）。"""
    try:
        cfg = _cfg()
        if cfg is None or not cfg.enabled:
            return
        prompt, completion, cached, cache_write, reasoning = usage
        # 阈值过滤：排查上下文膨胀时只留超大的那几次，省磁盘也省得翻文件
        if cfg.min_prompt_tokens and (prompt or 0) < cfg.min_prompt_tokens:
            return

        from ._context import _ctx

        ctx = _ctx()
        session_id = ctx.get("session_id") or "unknown-session"
        message_id = ctx.get("message_id") or "unknown-message"

        base = Path(os.path.expanduser(cfg.dir))
        d = base / session_id / message_id
        d.mkdir(parents=True, exist_ok=True)

        # 文件名语义化：序号保证排序，中间标出「第几步 / 最终总结 / 记忆摘要」。
        # 记忆摘要（turn_summary）不在 ReAct 步数里，必须单独区分——否则会被
        # 误以为是某一步。这样浏览目录时不用打开就知道每个文件是什么。
        now = datetime.now(timezone.utc)
        seq = ctx.get("step_seq")
        kind = ctx.get("llm_kind") or "other"
        # step{N} 已表明这是第几步，不再缀 -react_step（重复）
        if kind == "react_step" and seq:
            label = f"step{seq}"
        elif kind == "final_summary":
            label = "final-summary"
        elif kind == "turn_summary":
            label = "memory-summary"
        else:
            label = kind or "other"
        idx = len(list(d.glob("*.json"))) + 1
        stem = f"{idx:03d}-{label}"
        f = d / f"{stem}.json"
        n = 1
        while f.exists():  # 极端重名：加序号，避免互相覆盖
            n += 1
            f = d / f"{stem}-{n}.json"

        payload = {
            "ts": now.isoformat(),
            "file": f.name,
            "model": model,
            "is_stream": bool(is_stream),
            "kind": ctx.get("llm_kind"),
            "agent_id": ctx.get("agent_id"),
            "session_id": session_id,
            "message_id": message_id,
            "step_seq": ctx.get("step_seq"),
            "system": _system_of(msgs),
            "messages": [_msg_to_dict(m) for m in (msgs or [])],
            # 流式：最后/带 usage 的分片 content 通常是空的（正文在前面的分片里），
            # 直接用 resp.content 落盘会得到空响应——优先用调用方累积的正文。
            "response": (
                resp_text
                if resp_text
                else (_as_text(getattr(resp, "content", "")) if resp is not None else "")
            ),
            # 工具入参是增量到达的，累积后单独放，免得和正文混在一起
            "tool_args": tool_args_text or "",
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "cached_tokens": cached,
                "cache_write_tokens": cache_write,
                "reasoning_tokens": reasoning,
            },
            "duration_ms": duration_ms,
            "ttft_ms": ttft_ms,
        }
        f.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        maybe_cleanup(base, cfg.retain_days)
    except Exception as e:  # noqa: BLE001
        note_if_bug(e, "prompt dump 落盘")
        logger.debug("prompt dump 失败: %s", e)


async def read_message_dump(message_id: str) -> Dict[str, Any]:
    """读取某一轮落盘的完整 prompt（调试用，供前端「查看 dump」展示）。

    dump 目录是 ``<dir>/<session_id>/<message_id>/``，这里先从库里查到该消息
    所属会话，再按顺序读出该轮所有调用（一次 LLM 调用一个文件）。
    未开启落盘 / 没有文件时返回空列表——前端据此决定要不要显示入口。
    """
    cfg = _cfg()
    if cfg is None or not cfg.enabled:
        return {"enabled": False, "files": []}
    try:
        from ._context import _resolve_round_anchor
        from ..store import SessionMessage, get_session_factory

        factory = get_session_factory()
        async with factory() as db:
            row = await db.get(SessionMessage, message_id)
        if row is None:
            return {"enabled": True, "files": []}

        base = Path(os.path.expanduser(cfg.dir))
        # ① 先按传入的 id 直接找（目录可能就建在它下面，比如点在 user 消息上）
        d = base / row.chat_session_id / message_id
        if not d.exists():
            # ② 再回溯到「触发该轮的用户消息」：dump 是实时写的，那时助手消息
            #    还没落库（id 不存在），目录只能挂 user 消息 id，所以从助手
            #    消息点进来必须回退一步才能找到。
            anchor = await _resolve_round_anchor(message_id)
            d = base / row.chat_session_id / anchor
        if not d.exists():
            return {"enabled": True, "dir": str(d), "files": []}
        items = []
        for f in sorted(d.glob("*.json")):
            try:
                items.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception as e:  # noqa: BLE001
                # 单个文件读不了不该让整轮 dump 都返回空——跳过它即可。
                warn_bug_only(e, f"读 dump 文件 {f.name}")
                continue
        return {
            "enabled": True,
            "dir": str(d),
            "count": len(items),
            "files": items,
        }
    except Exception as e:  # noqa: BLE001
        raise_if_bug(e, "读取 prompt dump")
        logger.debug("读取 prompt dump 失败: %s", e)
        return {"enabled": True, "files": []}
