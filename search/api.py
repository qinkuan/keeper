"""检索接口（``/search``）。

从独立项目 k-search 的 ``api.py`` 迁入（2026-10）。迁移时做了四处适配：

1. **独立 app → router**：k-search 那份是自带 CORS 的独立 FastAPI app；
   keeper 里所有接口都是挂在主 app 上的 ``APIRouter``，前缀 ``/search``。
2. **阻塞端点用 ``def`` 而不是 ``async def``**：检索是 sqlite3 + numpy 的
   **阻塞**调用（向量走全量点积）。写成 ``async def`` 会把事件循环卡住，
   keeper 其它在跑的请求（对话流式输出）全被拖死。FastAPI 对 ``def`` 端点
   会自动丢到线程池里跑。
3. **embedding 模型跨请求复用**：模型权重 462MB，每个请求重载一次不可接受。
   模块级单例 + 锁；而 **SQLite 连接每次请求新建**（sqlite3 连接不能跨线程
   共享，FastAPI 的线程池会把它甩到别的线程上）。
4. **索引改成后台任务**：建索引要逐个文件做 embedding，是分钟级操作，
   不能占着 HTTP 连接等。进度写在模块级 ``_index_state`` 里，前端轮询
   ``GET /search/status`` 看。

接口一览
--------
| 端点 | 用途 |
| :--- | :--- |
| ``GET  /search/status`` | 首屏一次性拉全：模型状态 + 索引统计 + 索引进度 + 配置摘要 |
| ``GET  /search/stats`` | 索引统计（文档数 / 片段数 / 关键词条目数） |
| ``GET  /search/model/status`` | embedding 模型状态（是否就绪 / 下载中 / 失败） |
| ``POST /search/model/download`` | 后台下载 embedding 模型 |
| ``POST /search/index`` | 建索引（后台跑；``roots`` 省略 = 索引「我的空间」） |
| ``POST /search/query`` | 检索（混合 / 纯向量 / 纯关键词） |
| ``GET  /search/documents`` | 已索引文档列表（可按需删除） |
| ``GET  /search/documents/{id}`` | 单个文档的详情 |
| ``DELETE /search/documents/{id}`` | 删除某文档及其全部片段 |
| ``DELETE /search/documents/{id}/chunks`` | 只删该文档的片段，保留文件记录 |
| ``DELETE /search/chunks/{cid}`` | 删除单个片段 |
| ``GET  /search/chunks/{cid}/siblings`` | 该片段在同一文档中的相邻兄弟片段 |
| ``POST /search/cache/clear`` | 清空索引（向量 + 关键词索引） |
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from . import config
from .ingest import model as modelmod
from .service import SearchService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])


# ---------------------------------------------------------------- 进程内单例

# 说明：embedder 单例本体在 ``ingest/embed.py``（启动预热与请求共用同一个实例）。


def _shared_embedder():
    """跨请求复用的 embedder。

    单例本体在 ``ingest/embed.py::get_embedder`` —— 启动预热（``warmup.py``）
    也用它，所以「预热加载好的模型」就是「查询要用的那个模型」，预热不会白做。

    没装 ``litert-lm`` 时返回 ``None`` —— 调用方据此降级到纯关键词模式。
    """
    from .ingest.embed import get_embedder

    return get_embedder()


#: 索引任务的状态（后台跑，前端轮询 ``GET /search/status`` 看）。
_index_state: Dict[str, Any] = {
    "running": False,
    "stats": None,      # 最近一次完成的统计
    "error": "",        # 最近一次失败的原因
    "started_at": 0.0,
    "finished_at": 0.0,
    # 进度（建索引过程中实时刷新，给前端「第 N 个 / 共 M 个」用）
    "total": 0,         # 本轮参与的文件总数
    "current": 0,       # 已处理 / 正在处理的文件序号（1-based，收尾 = total）
    "current_path": "", # 正在处理的文件路径
    "current_name": "", # 正在处理的文件名（current_path 的 basename）
    "stage": "",        # scanning / indexing / done / pending
    "pct": 0.0,         # 完成百分比（0~100）
}

#: 保护 _index_state 的读改写（前端在轮询它，后台任务在写它）。
_index_state_lock = threading.Lock()


def _set_index_state(**kw) -> None:
    with _index_state_lock:
        _index_state.update(kw)


# ---------------------------------------------------------------- 请求体


class IndexRequest(BaseModel):
    """建索引请求。

    ``roots`` 省略 = 索引「我的空间」。``paths`` 给了就只索引这几个文件
    （桌面端文件监听用：文件一变就通知内核更新那一个）。
    """

    roots: Optional[List[str]] = None
    force: bool = False
    doc_type: Optional[str] = None
    paths: Optional[List[str]] = None


class QueryRequest(BaseModel):
    """检索请求。

    ``mode``：``hybrid``（向量 + 关键词，默认）/ ``vector`` / ``keyword``。
    ``min_score``：最终分下限（0~1）。不传 = 用 ``config.MIN_SCORE_UI``（0.5）；
    agent 场景传 ``0.8``；传 0 = 不过滤。

    ``fusion`` / ``alpha``（融合方式、向量权重）**是服务端的事**：它们的取值
    直接决定最终分是不是 0~1 的置信度，也就决定了分数门槛还有没有意义，
    不该交给前端调。这里保留字段只为脚本 / agent / 调试用；
    **不传 = 用 ``config.FUSION_MODE`` / ``config.HYBRID_ALPHA``**（系统定好的值）。
    """

    query: str = ""
    top_k: int = Field(default=config.DEFAULT_TOP_K, ge=1, le=100)
    path_prefix: Optional[str] = None
    title: Optional[str] = None
    doc_type: Optional[str] = None
    neighbor_span: int = Field(default=0, ge=0, le=20)
    mode: Optional[str] = None
    fusion: Optional[str] = None
    alpha: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    min_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class DownloadRequest(BaseModel):
    """模型下载请求。``source``：``auto`` / ``hf`` / ``modelscope``。"""

    source: str = "auto"


class SyncConfigRequest(BaseModel):
    """索引同步配置（保存到 ``settings.yaml`` 的 ``search`` 段，立即生效）。

    留空的字段表示「不改」。
    """

    sync_enabled: Optional[bool] = None
    sync_interval: Optional[float] = Field(default=None, gt=0, le=3600)
    sync_full_interval: Optional[float] = Field(default=None, gt=0, le=86400)


class ClearCacheRequest(BaseModel):
    """清索引请求。``drop_documents=False`` 时只清片段、保留文件清单。"""

    drop_documents: bool = True


# ---------------------------------------------------------------- 内部工具


def _service() -> SearchService:
    """开一个 service：连接是新的（不跨线程共享），模型是共享的（不重复加载）。"""
    return SearchService(embedder=_shared_embedder())


def _model_state() -> Dict[str, Any]:
    """模型状态：就绪 / 下载中 / 失败。"""
    state = modelmod.get_state()
    ready = modelmod.is_model_ready()
    return {
        "ready": ready,
        "status": "done" if ready else state["status"],
        "message": state["message"],
        "source": state["source"],
        "model_dir": str(config.MODEL_DIR),
        "model_file": config.MODEL_FILE,
        "model_repo": config.MODEL_REPO,
        # 后端三件套：
        #   backend_config —— 配置值（auto / cpu / gpu）
        #   backend_active —— **实际**用的（embedder 是懒加载的，没加载过就还没定）
        #   backend_note   —— 从 GPU 回退到 CPU 时的原因（没有就空）
        "backend": config.EMBED_BACKEND,
        "backend_config": config.EMBED_BACKEND,
        "backend_active": _active_backend(),
        "backend_note": _backend_note(),
        "gpu_probe": _gpu_probe(),
        # 模型是否已经加载并预热过（启动时后台预热的标）
        "warmed": _warmed(),
        # 没装 litert-lm 时，向量通道整个不可用（但关键词通道照常）
        "available": _shared_embedder() is not None,
    }


def _active_backend() -> str:
    """实际在用的后端（``gpu`` / ``cpu``）。

    embedder 是懒加载的：模型还没加载过时**还定不下来**（GPU 能不能用要真加载
    才知道），此时返回空字符串——比先填一个可能错的答案好。
    """
    em = _shared_embedder()
    return getattr(em, "backend_name", "") if em is not None else ""


def _backend_note() -> str:
    """回退原因（GPU 不可用时的一句话说明）。"""
    em = _shared_embedder()
    return getattr(em, "backend_note", "") if em is not None else ""


def _warmed() -> bool:
    """模型是否已经加载并预热好。

    启动时后台预热（``warmup.py``）会把它置成 True——用户第一次搜索就无感。
    """
    em = _shared_embedder()
    return bool(getattr(em, "warmed", False)) if em is not None else False


def _gpu_probe() -> Dict[str, Any]:
    """静态探测结果：有没有可能用上 GPU。"""
    try:
        from .ingest.embed import gpu_probe

        ok, reason = gpu_probe()
    except Exception:  # noqa: BLE001 litert-lm 没装时探测不了
        return {"available": False, "reason": "litert-lm 未安装"}
    return {"available": ok, "reason": reason}


def _index_status() -> Dict[str, Any]:
    return dict(_index_state)


def _run_index(roots: Optional[List[str]], force: bool, doc_type: Optional[str]) -> None:
    """后台建索引。**绝不抛到请求线程**——异常只记进状态里。"""
    from . import sync
    from pathlib import Path

    def on_progress(done: int, total: int, path: str, stage: str) -> None:
        _set_index_state(
            total=total,
            current=done,
            current_path=path,
            current_name=Path(path).name if path else "",
            stage=stage,
            pct=(done / total * 100.0) if total else 0.0,
        )

    svc = _service()
    try:
        _set_index_state(running=True, error="", started_at=time.time(),
                         finished_at=0.0, stats=None,
                         total=0, current=0, current_path="", current_name="",
                         stage="pending", pct=0.0)
        # 走 sync 的全局锁：定时扫描、手动建索引、文件监听回调三选一，
        # 拿不到锁就跳过这一轮（不排队——迟到的旧索引没有价值）。
        stats = sync.try_run_index(
            lambda: svc.index(roots=roots, force=force, doc_type=doc_type,
                              on_progress=on_progress)
        )
        if stats is None:
            _set_index_state(running=False, finished_at=time.time(),
                             stats=None, error="已有索引任务在跑，本次跳过")
        else:
            _set_index_state(running=False, stats=stats, finished_at=time.time(),
                             stage="done")
    except Exception as e:  # noqa: BLE001
        logger.exception("建索引失败")
        _set_index_state(running=False, error=str(e), finished_at=time.time())
    finally:
        svc.close()


def _run_index_paths(paths: List[str], force: bool,
                      doc_type: Optional[str]) -> None:
    """后台索引指定的一批文件（桌面端文件监听回调）。"""
    from . import sync
    from pathlib import Path

    def on_progress(done: int, total: int, path: str, stage: str) -> None:
        _set_index_state(
            total=total,
            current=done,
            current_path=path,
            current_name=Path(path).name if path else "",
            stage=stage,
            pct=(done / total * 100.0) if total else 0.0,
        )

    svc = _service()
    try:
        _set_index_state(running=True, error="", started_at=time.time(),
                         finished_at=0.0, stats=None,
                         total=0, current=0, current_path="", current_name="",
                         stage="pending", pct=0.0)
        stats = sync.try_run_index(
            lambda: svc.index_paths(paths=paths, force=force, doc_type=doc_type,
                                    on_progress=on_progress)
        )
        sync.note_watched(len(paths))
        if stats is None:
            _set_index_state(running=False, finished_at=time.time(),
                             stats=None, error="已有索引任务在跑，本次跳过")
        else:
            _set_index_state(running=False, stats=stats, finished_at=time.time(),
                             stage="done")
    except Exception as e:  # noqa: BLE001
        logger.exception("按路径建索引失败")
        _set_index_state(running=False, error=str(e), finished_at=time.time())
    finally:
        svc.close()


# ---------------------------------------------------------------- 状态


@router.get("/status")
def status() -> Dict[str, Any]:
    """首屏一次性拉全：模型状态 + 索引统计 + 索引进度 + 配置摘要。

    前端「搜索」页有两个 tab，切换时都要这些数据——合成一个端点省得首屏发
    三四个请求。
    """
    svc = _service()
    try:
        stats = svc.stats()
    except Exception as e:  # noqa: BLE001 索引库还没建时不该整页报错
        logger.warning("读索引统计失败：%s", e)
        stats = {"documents": 0, "chunks": 0, "leaves": 0, "parents": 0,
                 "keywords": 0, "db_path": str(svc.db_path)}
    finally:
        svc.close()
    from . import sync

    return {
        "model": _model_state(),
        "stats": stats,
        "index": _index_status(),
        "sync": sync.status(),
        "config": {
            "db_path": str(config.DB_PATH),
            "roots": [str(p) for p in config.default_roots()],
            "search_mode": config.SEARCH_MODE,
            "fusion_mode": config.FUSION_MODE,
            "hybrid_alpha": config.HYBRID_ALPHA,
            "default_top_k": config.DEFAULT_TOP_K,
            "chunk_min_chars": config.CHUNK_MIN_CHARS,
            "chunk_max_chars": config.CHUNK_MAX_CHARS,
        },
    }


@router.get("/stats")
def stats() -> Dict[str, Any]:
    """索引统计。"""
    svc = _service()
    try:
        return svc.stats()
    finally:
        svc.close()


@router.get("/model/status")
def model_status() -> Dict[str, Any]:
    """embedding 模型状态。"""
    return _model_state()


@router.post("/model/download")
def model_download(req: DownloadRequest, background: BackgroundTasks) -> Dict[str, Any]:
    """后台下载 embedding 模型（几百 MB，不能占着连接等）。"""
    modelmod.start_download(req.source)
    return {"status": "downloading_started", "source": req.source}


# ---------------------------------------------------------------- 索引


@router.post("/index")
def index(req: IndexRequest, background: BackgroundTasks) -> Dict[str, Any]:
    """建索引（**后台跑**）。

    响应立刻返回，真正的建索引在后台线程里做；进度看 ``GET /search/status``
    的 ``index`` 字段。

    ``paths`` 给了就只索引这几个文件（桌面端文件监听用），否则索引整个 roots。
    """
    from . import sync

    if _index_state.get("running") or sync.index_running():
        return {"status": "already_running"}
    if req.paths:
        background.add_task(_run_index_paths, req.paths, req.force, req.doc_type)
    else:
        background.add_task(_run_index, req.roots, req.force, req.doc_type)
    return {"status": "indexing_started"}


@router.get("/config")
def get_config() -> Dict[str, Any]:
    """索引同步配置（当前生效值）。"""
    from ..setting.user_settings import get_search_config

    return get_search_config()


@router.post("/config")
def save_config(req: SyncConfigRequest) -> Dict[str, Any]:
    """保存索引同步配置，**立即生效**（不用重启内核）。

    落到 ``~/.keeper/settings.yaml`` 的 ``search`` 段；保存后立刻调整后台扫描：
    改间隔马上按新的来，改开关马上起 / 停。
    """
    from ..setting.user_settings import set_search_config
    from . import sync

    values = {k: v for k, v in req.model_dump().items() if v is not None}
    saved = set_search_config(values)
    sync.apply_config()
    return saved


@router.post("/cache/clear")
def cache_clear(req: ClearCacheRequest) -> Dict[str, Any]:
    """清空索引缓存（向量 + 关键词索引全清）。"""
    svc = _service()
    try:
        return svc.clear(drop_documents=req.drop_documents)
    finally:
        svc.close()


# ---------------------------------------------------------------- 检索


@router.post("/query")
def query(req: QueryRequest) -> Dict[str, Any]:
    """检索。

    ``mode="keyword"`` 不需要模型（纯 FTS5/BM25）；``vector`` / ``hybrid``
    需要 embedding 模型——没有时返回 503 并说清楚原因，而不是静默返回空结果
    （空结果会被误读成「没找到」）。
    """
    if not (req.query or "").strip():
        return {"results": [], "mode": req.mode or config.SEARCH_MODE}

    filters: Dict[str, Any] = {}
    if req.path_prefix:
        filters["path_prefix"] = req.path_prefix
    if req.title:
        filters["title"] = req.title
    if req.doc_type:
        filters["doc_type"] = req.doc_type

    svc = _service()
    try:
        results = svc.search(
            req.query,
            top_k=req.top_k,
            filters=filters,
            mode=req.mode,
            alpha=req.alpha,
            fusion=req.fusion,
            neighbor_span=req.neighbor_span,
            with_parent=True,
            min_score=req.min_score,
        )
    except RuntimeError as e:
        # 模型不可用（没装 litert-lm）——明确报错，别装作"没找到"
        raise HTTPException(status_code=503, detail=str(e)) from e
    finally:
        svc.close()
    return {"results": results, "mode": req.mode or config.SEARCH_MODE}


# ---------------------------------------------------------------- 文档 / 片段


@router.get("/documents")
def list_documents(limit: int = 200, offset: int = 0) -> Dict[str, Any]:
    """已索引文档列表（用于「索引管理」里逐个删）。"""
    from .store import db as dbmod

    svc = _service()
    try:
        conn = svc.connect()
        docs = dbmod.list_documents(conn)
        total = len(docs)
        page = docs[offset: offset + limit]
        for d in page:
            d["path"] = d.get("file_path", "")
            d["name"] = Path(d["file_path"]).name
            d["dir"] = str(Path(d["file_path"]).parent)
        return {"documents": page, "total": total,
                "limit": limit, "offset": offset}
    finally:
        svc.close()


@router.get("/documents/{doc_id}")
def document_detail(doc_id: int) -> Dict[str, Any]:
    """单个文档的详情。"""
    from .store import db as dbmod

    svc = _service()
    try:
        doc = dbmod.get_document(svc.connect(), doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail="文档不存在")
        doc["path"] = doc["file_path"]
        doc["name"] = Path(doc["file_path"]).name
        return doc
    finally:
        svc.close()


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: int) -> Dict[str, Any]:
    """删除某文档及其全部片段。"""
    from .store import db as dbmod

    svc = _service()
    try:
        conn = svc.connect()
        doc = dbmod.get_document(conn, doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail="文档不存在")
        dbmod.delete_document(conn, doc_id)
        return {"status": "deleted", "doc_id": doc_id}
    finally:
        svc.close()


@router.delete("/documents/{doc_id}/chunks")
def delete_document_chunks(doc_id: int) -> Dict[str, Any]:
    """只删除该文档的片段，保留文件记录（标记 pending，待重新索引）。"""
    from .store import db as dbmod

    svc = _service()
    try:
        conn = svc.connect()
        doc = dbmod.get_document(conn, doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail="文档不存在")
        dbmod.delete_chunks_of_document(conn, doc_id)
        return {"status": "deleted", "doc_id": doc_id}
    finally:
        svc.close()


@router.delete("/chunks/{chunk_id}")
def delete_chunk(chunk_id: str) -> Dict[str, Any]:
    """删除单个片段。"""
    from .store import db as dbmod

    svc = _service()
    try:
        conn = svc.connect()
        chunk = dbmod.get_chunk(conn, chunk_id)
        if not chunk:
            raise HTTPException(status_code=404, detail="片段不存在")
        dbmod.delete_chunk(conn, chunk_id)
        return {"status": "deleted", "chunk_id": chunk_id}
    finally:
        svc.close()


@router.get("/chunks/{chunk_id}/siblings")
def chunk_siblings(chunk_id: str, span: int = 1) -> Dict[str, Any]:
    """该片段在同一文档中的兄弟片段（前后各 span 个，含自身）。"""
    from .store import db as dbmod

    svc = _service()
    try:
        conn = svc.connect()
        chunk = dbmod.get_chunk(conn, chunk_id)
        if not chunk:
            raise HTTPException(status_code=404, detail="片段不存在")
        siblings = dbmod.get_sibling_chunks(
            conn, chunk["doc_id"], chunk["chunk_index"], span=span
        )
        return {"chunk_id": chunk_id, "siblings": siblings}
    finally:
        svc.close()
