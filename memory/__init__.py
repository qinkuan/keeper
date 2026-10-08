"""记忆模块（keeper/memory）。

存放记忆的存储、检索与摘要逻辑：
- ``retriever``：jieba 预分词 + SQLite FTS5 检索封装（seg / MemoryIndex）；
- ``store``：会话级短期记忆文件存储写出器（SessionMemory，JSONL + 增量索引）；
- ``extractor``：异步记忆块摘要（summarize_turn，LLM 解耦）；
- ``tools``：agentic retrieval 工具（recall / read，见 doc/memory.md 7.5）。

设计依据见 doc/memory.md。
"""
from .extractor import summarize_turn
from .retriever import MemoryIndex, add_user_word, seg
from .store import SessionMemory
from .tools import build_memory_tools

__all__ = [
    "MemoryIndex",
    "seg",
    "add_user_word",
    "SessionMemory",
    "summarize_turn",
    "build_memory_tools",
]
