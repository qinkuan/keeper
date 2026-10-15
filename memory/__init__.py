"""记忆模块（keeper/memory）。

存放记忆的存储、检索与摘要逻辑：
- ``retriever``：jieba 预分词 + SQLite FTS5 检索封装（seg / MemoryIndex）；
- ``store``：**按天**短期记忆文件存储写出器（SessionMemory，JSONL + 增量索引）；
- ``extractor``：异步记忆块摘要（summarize_turn，LLM 解耦）；
- ``tools``：agentic retrieval 工具（recall / read，见 doc/memory.md 7.5）。

**作用域是天，不是会话**：同一天开的多个会话共享记忆，跨天才断。对工具型 agent
无所谓（每次任务本来就该干净），对一个陪伴型角色是刚需——否则换个会话再问一遍
它就不记得了。见 ``store.SessionMemory`` 的说明。

设计依据见 doc/memory.md。
"""
from .extractor import summarize_turn
from .retriever import MemoryIndex, add_user_word, seg
from .store import SessionMemory, day_of, memory_for
from .tools import build_memory_tools

__all__ = [
    "MemoryIndex",
    "seg",
    "add_user_word",
    "SessionMemory",
    "day_of",
    "memory_for",
    "summarize_turn",
    "build_memory_tools",
]
