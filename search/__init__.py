"""搜索：本地文档的**切片 → 索引 → 混合检索**。

从独立项目 k-search 迁入（原始设计见 ``doc/indexing.md``）。
keeper 语境下它解决的是「**空间里的文件只能靠名字找**」这个问题：
目录树翻、``ls`` + ``grep`` 都只认字面词，问「那个讲缓存穿透的文档在哪」
就找不到。

三条能力线
----------
- **切片**（``ingest/chunk.py``）：Markdown 结构化切片——标题树 → 块级元素 →
  叶子 chunk → 父子链父块。代码块与表格不切，段落按句子边界兜底。
- **索引**（``indexer.py`` + ``store/db.py``）：以 ``content_hash`` 为单位做增量，
  只对新增/变化的叶子重新嵌入；文件移动只改路径不重嵌。SQLite 里同时存
  向量与 FTS5 关键词索引。
- **检索**（``query.py``）：向量召回 + 关键词（BM25）召回 → 融合排序。
  ``mode`` 可选 ``hybrid`` / ``vector`` / ``keyword``。

依赖与降级
----------
- **numpy** 是硬依赖（向量用全量点积算余弦）。
- **litert-lm** 是可选依赖：没装时**关键词检索照常可用**（``mode="keyword"``），
  只是没有语义召回。这条降级路径是刻意留的——embedding 模型动辄几百 MB，
  不该成为「能搜字面词」的前置条件。

导入约定
--------
``numpy`` / ``litert-lm`` 相关的符号走**模块级 ``__getattr__`` 懒加载**：
``import keeper.search`` 本身不碰重依赖，只有真的用到 ``Embedder`` /
``index_roots`` 时才加载。否则「只是想知道有哪些配置项」也要先装 numpy。
"""
from __future__ import annotations

from typing import Any

from . import config, tokenize
from .ingest.chunk import Chunk, chunk_markdown

__all__ = [
    # 常量与轻量工具（直接可 import）
    "config",
    "tokenize",
    "Chunk",
    "chunk_markdown",
    # 重依赖符号：懒加载
    "Embedder",
    "index_roots",
    "index_path",
    "search",
    "connect",
    "SearchService",
]

#: 懒加载映射：名字 → (子模块路径, 模块内的符号名)。
#:
#: 这些符号全都依赖 numpy 或 litert-lm，**不能在包初始化时 import**——
#: 否则 ``from keeper.search import config`` 也会被 numpy 拖住。
_LAZY: dict = {
    "Embedder": (".ingest.embed", "Embedder"),
    "index_roots": (".indexer", "index_roots"),
    "index_path": (".indexer", "index_path"),
    "search": (".query", "search"),
    "connect": (".store.db", "connect"),
    "SearchService": (".service", "SearchService"),
}


def __getattr__(name: str) -> Any:
    """懒加载重依赖符号（PEP 562）。"""
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    mod = import_module(target[0], __name__)
    value = getattr(mod, target[1])
    globals()[name] = value
    return value


def __dir__() -> list:
    return sorted(set(list(globals()) + __all__))
