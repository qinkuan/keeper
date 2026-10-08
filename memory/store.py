"""会话级短期记忆文件存储：JSONL + FTS5 增量索引。

目录结构见 doc/memory.md 6.1。本模块只负责「拿到记录后写文件 + 增量更新索引」，
**不查数据库**——DB 读取由调用方（chat/service.py）负责，传入已取好的记录。

写入是同步的文件 IO（JSONL 按 message_id 幂等覆盖 + FTS5 upsert，均很快）；异步化由调用方
用 ``asyncio.to_thread`` 包装，并按 6.3 的要求「下轮 await 上一轮写任务」保证时序。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from .retriever import MemoryIndex

_DEFAULT_ROOT = Path(
    os.environ.get("KEEPER_MEMORY_DIR", "~/.keeper/memory")
).expanduser()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionMemory:
    """单个会话的短期记忆存储（隔离，见 6.1）。"""

    def __init__(self, session_id: str, root: Optional[Path] = None):
        self.session_id = session_id
        self.dir = (root or _DEFAULT_ROOT) / "sessions" / session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        # 块级原文：每个 Turn 一整块快照（见 doc/memory.md 7.5），供 read 惰性展开
        self._turns = self.dir / "turns.jsonl"
        self.index = MemoryIndex(self.dir / "index.db")
        # 分词规则变了（eg. 加了停用词）就重建存量索引。索引是**纯派生数据**，
        # 原文永远在 turns.jsonl 里，所以重建是安全且幂等的——不做这一步的
        # 后果是「改了配置却没效果」，而且没有任何报错。
        if self.index.is_stale():
            self.reindex()

    # --- 块级索引（Turn 块，见 doc/memory.md 7.5）---
    def add_turn(
        self,
        *,
        message_id: str,
        question: str,
        answer: str,
        steps: List[dict],
        summary: str = "",
    ) -> None:
        """写入一个 Turn 块的整块原文，并（带摘要时）建立块级索引。

        检索单元是「Turn 块」而非单 step：块自带摘要（进 FTS5 供 ``recall`` 粗筛）
        与原文指针（``turns.jsonl``，供 ``read`` 惰性展开）。``steps`` 元素形如
        ``{"step", "kind", "tool", "input", "output"}``。写入按 message_id 幂等覆盖。
        """
        rec = {
            "message_id": message_id,
            "question": question,
            "answer": answer,
            "steps": steps,
            "summary": summary,
            "ts": _now_iso(),
        }
        # 按 message_id 幂等覆盖：丢弃同 id 旧记录后再追加（见 doc/memory.md 7.5.4）
        self._write_turn_idempotent(rec)
        if summary:
            self.index.upsert(f"turn:{message_id}", summary)

    def reindex(self) -> int:
        """按当前 :func:`~keeper.memory.retriever.seg` 规则重建全部块级索引。

        索引是**纯派生数据**（原文始终在 ``turns.jsonl``），所以重建随时可做、
        幂等且安全。触发时机：``__init__`` 检测到格式版本变化时自动调用。

        返回重建的条数。
        """
        if not self._turns.exists():
            self.index.mark_version()
            return 0
        n = 0
        for rec in self._all_turn_records():
            summary = (rec.get("summary") or "").strip()
            if not summary:
                continue
            mid = rec.get("message_id")
            if not mid:
                continue
            self.index.upsert(f"turn:{mid}", summary)
            n += 1
        self.index.mark_version()
        return n

    def _all_turn_records(self) -> List[dict]:
        """按文件顺序读出全部 Turn 记录（跳过坏行）。"""
        if not self._turns.exists():
            return []
        out: List[dict] = []
        for line in self._turns.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def _write_turn_idempotent(self, rec: dict) -> None:
        """写入 turns.jsonl，按 message_id 去重（覆盖同 id 旧版本）。"""
        path = self._turns
        mid = rec.get("message_id")
        kept: list = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    old = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if mid is not None and old.get("message_id") == mid:
                    continue
                kept.append(line)
        kept.append(json.dumps(rec, ensure_ascii=False))
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    def recall(
        self, query: str, limit: int = 10, mode: str = "and"
    ) -> List[Tuple[str, str]]:
        """检索记忆块，仅返回 Turn 块（``turn:`` 前缀）的 ``(block_id, 可读摘要)``。

        索引只负责「是否命中」；可读摘要从 turns.jsonl 的块记录取（见 7.5）。
        mode：``"and"``（默认，精确，LLM 主动 recall）或 ``"or"``（宽松，系统侧自动召回）。
        """
        rows = self.index.search(query, limit, mode=mode)
        out: List[Tuple[str, str]] = []
        for doc_id, _ in rows:
            if not doc_id.startswith("turn:"):
                continue
            mid = doc_id[len("turn:"):]
            rec = self._read_turn_rec(mid)
            if rec is not None:
                out.append((doc_id, rec.get("summary") or ""))
        return out

    def _read_turn_rec(self, message_id: str):
        """从 turns.jsonl 读某 Turn 块的原始记录（dict），找不到返回 None。"""
        if not self._turns.exists():
            return None
        for line in reversed(self._turns.read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("message_id") == message_id:
                return rec
        return None

    def read_turn(self, message_id: str) -> str:
        """惰性展开某 Turn 块（``turn:{message_id}``）的整块原文，返回可读文本。"""
        rec = self._read_turn_rec(message_id)
        return self.render_turn_text(rec) if rec else "[未找到该记忆块]"

    def load_recent_turns(
        self, limit: int = 10, exclude_message_id: Optional[str] = None
    ) -> List[dict]:
        """读最近 ``limit`` 个 Turn 块（不含 ``exclude_message_id``），时间升序。

        供「最近 N 轮」直接注入上下文（doc/memory.md R0）。
        """
        recs = [
            r
            for r in self._all_turn_records()
            if not (exclude_message_id and r.get("message_id") == exclude_message_id)
        ]
        return recs[-limit:] if limit else recs

    def render_turn_messages(self, rec: dict) -> List[dict]:
        """把一个 Turn 块渲染成注入 LLM 的 messages 列表（与 process 重放格式一致）。"""
        msgs = [{"role": "user", "content": "用户问题：" + (rec.get("question") or "")}]
        for role, content in self._turn_blocks(rec.get("steps") or []):
            msgs.append({"role": role, "content": content})
        msgs.append({"role": "assistant", "content": rec.get("answer") or ""})
        return msgs

    def render_turn_text(self, rec: dict) -> str:
        """把一个 Turn 块渲染成可读纯文本（供 ``read`` 工具返回 / 摘要输入）。"""
        parts = [f"[用户问题]\n{rec.get('question') or ''}"]
        for _role, content in self._turn_blocks(rec.get("steps") or []):
            parts.append(content)
        parts.append(f"[最终回答]\n{rec.get('answer') or ''}")
        return "\n\n".join(parts)

    @staticmethod
    def _turn_blocks(steps: List[dict]):
        """遍历步骤生成 ``(role, content)`` 片段；think 跳过以省 token。"""
        blocks = []
        for st in steps or []:
            kind = st.get("kind")
            tool = st.get("tool")
            inp = st.get("input")
            out = st.get("output")
            if kind == "tool":
                head = (
                    f"THOUGHT: (调用工具)\nACTION: {tool or 'tool'}"
                    f"\nACTION_INPUT: {inp or ''}"
                )
                blocks.append(("assistant", head))
                blocks.append(("tool", out or ""))
            elif kind == "ask":
                blocks.append(("assistant", f"THOUGHT: (等待用户补充)\nASK: {out or ''}"))
            elif kind == "human_answer":
                blocks.append(("user", f"用户补充：{out or ''}"))
        return blocks

    def search(self, query: str, limit: int = 10) -> List[Tuple[str, str]]:
        return self.index.search(query, limit)

    def close(self) -> None:
        self.index.close()

    def __enter__(self) -> "SessionMemory":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
