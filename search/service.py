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
        """取 embedder；没有 litert-lm 时报错说清楚缺什么。"""
        if self._embedder is None and not self._embedder_failed:
            try:
                from .ingest.embed import Embedder

                self._embedder = Embedder()
            except Exception as e:  # noqa: BLE001
                self._embedder_failed = True
                raise RuntimeError(
                    "加载 embedding 模型失败：向量检索不可用。"
                    "请先安装 litert-lm（pip install litert-lm==0.18.0）并下载模型，"
                    "或改用 mode='keyword'（纯关键词检索，无需模型）。原始错误："
                    + str(e)
                ) from e
        return self._embedder

    # ---------------------------------------------------------------- 索引

    def index(
        self,
        roots: Optional[List[str]] = None,
        force: bool = False,
        doc_type: Optional[str] = None,
        prescreen: bool = True,
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
        """
        from .indexer import index_roots

        conn = self.connect()
        try:
            embedder = self._get_embedder()
        except RuntimeError as e:
            logger.warning("未加载 embedding 模型，本次只建关键词索引：%s", e)
            embedder = None
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
        try:
            embedder = self._get_embedder()
        except RuntimeError as e:
            logger.warning("未加载 embedding 模型，本次只建关键词索引：%s", e)
            embedder = None
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
        return {
            "documents": documents,
            "chunks": counts["chunks"],
            "leaves": counts["leaves"],
            "parents": counts["parents"],
            "keywords": keywords,
            "db_path": str(self.db_path),
        }

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
