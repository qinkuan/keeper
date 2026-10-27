"""检索服务的装配层：把「连接 + 模型 + 索引 + 检索」收成一个可复用的对象。

为什么需要这一层
----------------
迁进来的 k-search 原本是个**独立 HTTP 服务**（它的 ``api.py`` 每个端点自己
``connect()`` + ``get_embedder()``）。keeper 里没有那层 HTTP——检索最终要变成
agent 的一个工具（或「我的空间」页里的一个搜索框），所以把组装逻辑收进
:class:`SearchService`，调用方只管调方法。

两个关键取舍
------------
- **embedder 懒加载**：没装 ``litert-lm`` 时，``mode="keyword"`` 完全可用；
  只有真的要向量召回时才去加载模型并报错。加载一次模型要几百 MB 内存 +
  几秒初始化，不该让「只搜字面词」也付这个代价。
- **连接是普通 sqlite3 连接**（不是线程安全的）：一次调用内用完即关，
  不要跨线程共享同一个实例。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config

logger = logging.getLogger(__name__)


class SearchService:
    """检索服务。

    典型用法::

        svc = SearchService()
        try:
            svc.index(roots=["~/notes"])
            hits = svc.search("缓存穿透", top_k=5)
        finally:
            svc.close()
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        embedder: Any = None,
    ) -> None:
        self.db_path = Path(db_path).expanduser() if db_path else config.DB_PATH
        self._embedder = embedder
        self._embedder_failed = False
        self._conn = None

    # ---------------------------------------------------------------- 连接

    def connect(self):
        """打开数据库连接（懒开，重复调用返回同一条）。"""
        if self._conn is None:
            from .store import db as dbmod

            self._conn = dbmod.connect(self.db_path)
        return self._conn

    def close(self) -> None:
        """关掉连接（可重复调用）。"""
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def __enter__(self) -> "SearchService":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- 模型

    def _get_embedder(self):
        """取 embedder；**向量通道不可用时返回 ``None``**（不是抛异常）。

        为什么改成"返回 None"而不是"报错"：向量通道不可用有两种常见原因
        （没装 litert-lm / 模型没下载），它们都不该让**关键词检索**跟着一起
        挂掉。以前这里抛 RuntimeError，调用方虽然接住了，但 ``Embedder`` 是懒
        加载的——构造它不会失败，真正的失败发生在第一次 embed 时（那时已经
        在建索引的路上了，整批文件都白跑）。现在改成**提前判断**：不可用就
        老老实实降级为纯关键词，并把原因记下来给界面显示。

        模型可能刚下载好，所以每次都重新看一眼可用性（不可用→可用时要能
        立刻拿到 embedder，而不是被一次失败永久挡住）。
        """
        from .ingest import embed as embedmod

        reason = embedmod.unavailable_reason()
        if not reason and self._embedder_failed:
            # 通道恢复了（模型刚下载好 / litert-lm 刚装上）→ 允许重新加载
            self._embedder_failed = False
        if self._embedder is None and not self._embedder_failed:
            self._embedder = embedmod.get_embedder()
            if self._embedder is None:
                self._embedder_failed = True
        return self._embedder

    @property
    def embedder_reason(self) -> str:
        """向量通道不可用的原因（空串 = 可用）。"""
        from .ingest import embed as embedmod

        return embedmod.unavailable_note()

    # ---------------------------------------------------------------- 索引

    def index(
        self,
        roots: Optional[List[str]] = None,
        force: bool = False,
        doc_type: Optional[str] = None,
        prescreen: bool = True,
        want_vector: Optional[bool] = None,
        on_progress=None,
    ) -> Dict[str, int]:
        """索引目录（默认「我的空间」），返回统计。

        ``roots`` 为空时用 ``config.default_roots()``——也就是「我的空间」
        （用户没配就是空列表，索引到 0 个文件，不报错）。

        ``prescreen=True``（默认）：先看 ``mtime + size``，没变过的文件**连打开
        都不打开**。3 万文件 / 2.8GB 的语料，全量 hash 要 6～7 秒（读 2.8GB
        磁盘），只 stat 只要 200ms。

        **没有 embedding 模型也能索引**：此时只建关键词索引（向量列留空），
        检索时用 ``mode="keyword"``。不该让「装不装得动几百 MB 的模型」卡住
        「能搜字面词」这件事。

        ``want_vector=False``：明确只要关键词索引（连模型都不去碰）。
        """
        from .indexer import index_roots

        conn = self.connect()
        # want_vector=False = 明确只要关键词索引（用户选了"仅关键词"）
        embedder = None if want_vector is False else self._get_embedder()
        if embedder is None and want_vector is not False:
            logger.info(
                "向量通道不可用（%s），本次只建关键词索引",
                self.embedder_reason or "原因未知",
            )
        return index_roots(
            conn,
            embedder,
            roots=[Path(r).expanduser() for r in roots] if roots else None,
            force=force,
            doc_type=doc_type,
            prescreen=prescreen,
            on_progress=on_progress,
        )

    def index_paths(
        self,
        paths: List[str],
        force: bool = False,
        doc_type: Optional[str] = None,
        on_progress=None,
    ) -> Dict[str, int]:
        """索引**指定的一批文件**（桌面端文件监听用），返回统计。

        不遍历目录、不做删除同步——只处理传进来的路径；文件已不存在（用户删了）
        就把它的索引一并删掉。
        """
        from .indexer import index_paths as _index_paths

        conn = self.connect()
        embedder = self._get_embedder()
        if embedder is None:
            logger.info(
                "向量通道不可用（%s），本次只建关键词索引",
                self.embedder_reason or "原因未知",
            )
        return _index_paths(
            conn,
            embedder,
            [Path(p).expanduser() for p in (paths or [])],
            force=force,
            doc_type=doc_type,
            on_progress=on_progress,
        )

    # ---------------------------------------------------------------- 检索

    def search(
        self,
        query: str,
        top_k: Optional[int] = None,
        filters: Optional[Dict[str, Any]] = None,
        mode: Optional[str] = None,
        alpha: Optional[float] = None,
        fusion: Optional[str] = None,
        neighbor_span: int = 0,
        with_parent: bool = True,
        min_score: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """检索。

        ``mode="keyword"`` 不加载模型（纯 FTS5/BM25）；其余模式需要 embedder。
        ``neighbor_span`` > 0 时附带同文档前后各 N 个兄弟片段；
        ``with_parent`` 为真时附带父块（章节级上下文）。

        ``min_score`` 是**最终分下限（0~1）**：低于它的结果直接丢掉。
        不传 = 用 ``config.MIN_SCORE_UI``（0.5，文件目录搜索用）；
        agent 场景传 ``config.MIN_SCORE_AGENT``（0.8）；传 0 = 不过滤。
        """
        from .query import search as _search
        from .store import db as dbmod

        conn = self.connect()
        mode = (mode or config.SEARCH_MODE or "hybrid").lower()
        embedder = None if mode == "keyword" else self._get_embedder()
        if mode != "keyword" and embedder is None:
            # 模型没下载 / litert-lm 没装 → 降级为纯关键词。宁可搜得糙一点，
            # 也别让整个对话因为"没有向量模型"直接失败。
            logger.warning(
                "向量通道不可用（%s），本次检索降级为纯关键词",
                self.embedder_reason or "原因未知",
            )
            mode = "keyword"
        results = _search(
            conn,
            embedder,
            query,
            top_k=top_k if top_k is not None else config.DEFAULT_TOP_K,
            filters=filters,
            mode=mode,
            alpha=alpha,
            fusion=fusion,
            min_score=min_score,
        )
        for r in results:
            if with_parent:
                r["parent"] = dbmod.get_parent_chunk(conn, r.get("parent_id"))
            if neighbor_span > 0:
                r["siblings"] = dbmod.get_sibling_chunks(
                    conn, r["doc_id"], r["chunk_index"], span=neighbor_span
                )
        return results

    # ---------------------------------------------------------------- 维护

    def stats(self) -> Dict[str, Any]:
        """索引统计（文档数 / 片段数 / 关键词条目数）。"""
        from .store import db as dbmod

        conn = self.connect()
        counts = dbmod.count_chunks(conn)
        documents = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        keywords = dbmod.count_keywords(conn)
        vec = dbmod.vector_index_state(conn)
        return {
            "documents": documents,
            "chunks": counts["chunks"],
            "leaves": counts["leaves"],
            "parents": counts["parents"],
            "keywords": keywords,
            "db_path": str(self.db_path),
            # 向量索引现状：界面据此提示"模型已就绪但向量还没建"
            "vectors": vec["with_vector"],
            "vectors_missing": vec["missing"],
            "vector_index": vec["state"],
            "index_kinds": vec["kinds"],
            "embed_model": vec["model"],
            "embed_dim": vec["dim"],
            "last_vector_at": vec["last_vector_at"],
        }

    def backfill_vectors(self, on_progress=None) -> Dict[str, int]:
        """**只补缺向量的文档**（不全量重建）。

        什么时候需要它：用户先用了关键词索引（没下载向量模型），后来下载了
        模型 —— 这时索引里的向量还是空的，但文档本身没变，普通的增量扫描会
        认为"没变过"直接跳过，永远补不上。这里主动把**缺向量**的文档挑出来
        重新走一遍（内容没变的部分会复用旧切片，只补嵌入）。

        向量通道不可用时直接返回空统计（不报错）：没模型就没法补。
        """
        from .indexer import index_path
        from .store import db as dbmod

        stats = {"indexed": 0, "skipped": 0, "empty": 0, "moved": 0,
                 "documents": 0}
        embedder = self._get_embedder()
        if embedder is None:
            logger.info(
                "向量通道不可用（%s），跳过补建向量索引",
                self.embedder_reason or "原因未知",
            )
            return stats

        conn = self.connect()
        todo = dbmod.documents_missing_vectors(conn)
        total = len(todo)
        for i, d in enumerate(todo):
            if on_progress:
                on_progress(done=i + 1, total=total, path=d["file_path"],
                            stage="indexing")
            res = index_path(conn, embedder, d["file_path"], force=False)
            stats[res] = stats.get(res, 0) + 1
            stats["documents"] += 1
        if on_progress:
            on_progress(done=total, total=total, path="", stage="done")
        return stats

    def clear(self, drop_documents: bool = True) -> Dict[str, Any]:
        """清空索引（向量 + 关键词索引全清）。

        ``drop_documents=True`` 连文件清单一起清；``False`` 只清片段，
        保留清单以便只重新嵌入。
        """
        from .store import db as dbmod

        conn = self.connect()
        dbmod.clear_index(conn, drop_documents=drop_documents)
        counts = dbmod.count_chunks(conn)
        return {
            "status": "cleared",
            "drop_documents": drop_documents,
            "chunks": counts["chunks"],
            "keywords": dbmod.count_keywords(conn),
        }
