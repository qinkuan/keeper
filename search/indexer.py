"""索引编排：扫描目录 -> 变更检测 -> 结构化切片 -> 增量向量化 -> 写入 SQLite。

进度回调
--------
``index_roots`` / ``index_paths`` 都接受一个 ``on_progress`` 回调，用于在后台
建索引时把进度（已处理文件数 / 总文件数 / 当前文件路径 / 阶段）交给上层
（``search/api.py`` 写进 ``_index_state``，前端轮询 ``GET /search/status`` 展示
「第 N 个 / 共 M 个」）。回调签名::

    on_progress(done: int, total: int, path: str, stage: str)

- ``done``：1-based 当前文件序号（收尾时为 ``total``）；
- ``total``：参与本轮的文件总数；
- ``path``：正在处理的文件路径（收尾时为空串）；
- ``stage``：``"scanning"``（移动识别 / 算 hash）/ ``"indexing"``（切片 + 嵌入）/
  ``"done"``（收尾）。
不传（``None``）则完全不回调——周期自动同步、测试等都走这个默认，零开销。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Optional

from . import config

from .deps import require_numpy
from .ingest import chunk, detect, pdf
from .store import db as dbmod

logger = logging.getLogger(__name__)

#: 最近索引过的路径 -> 时刻（用于抑制重复通知，见 ``index_paths``）。
_RECENT_INDEX: dict[str, float] = {}
#: 抑制窗口（秒）。
_RECENT_TTL = 5.0

#: 「已知没有可索引正文」的文件：路径 -> (mtime, size, CHUNK_FORMAT_VERSION)。
#:
#: 为什么需要它：切不出块的文件**不写 documents 表**（见下面 ``index_path`` 里
#: 的 ``return "empty"``），也就没有 ``existing`` 记录可供下一轮 stat 预筛——
#: 于是每轮周期同步都要重新读一遍全文、再跑一次 PDF 解析，而解析恰恰是这批
#: 文件里最贵的一步。扫描件越多，同步越慢，日志还每轮重刷一遍。
#:
#: 为什么不落库：这只是**省一次注定失败的解析**的加速缓存，不是业务状态。
#: 落库就得定义状态语义、处理迁移、还得从前端文档计数里排除掉——为一个优化
#: 引入一整套状态管理不划算。所以放内存：进程重启即失效，重启后第一轮重新
#: 全量扫描一遍，结果自愈（扫错了最多慢一轮，不会错）。
_EMPTY_CACHE: dict[str, tuple] = {}
#: 缓存上限，超了整体清空。正常语料远达不到这个量；设它是防病态场景（某个
#: 临时目录被反复生成无正文文件）下无界增长。
_EMPTY_CACHE_MAX = 20_000


def index_path(conn, embedder, path, force=False, doc_type=None, prescreen=False,
               known_hash=None):
    """索引单个文件，返回状态：indexed / skipped / empty / moved。

    ``prescreen=True``：**先看 stat（mtime + size），没变就完全不读文件**。
    这是把「扫描一遍」从「读一遍全部语料」降成「stat 一遍」的开关——
    3 万文件 / 2.8GB 的语料，全量 hash 要 6～7 秒（读 2.8GB 磁盘），
    只 stat 只要 200ms。

    ``known_hash``：调用方已经算过这个文件的 hash 时传进来，避免**同一个文件
    被读两遍**（移动识别阶段算过一次、这里又算一次 = 首次索引时磁盘 IO 翻倍）。
    """
    path = Path(path)
    doc_type = doc_type or config.DOC_TYPE
    mtime, size = detect.stat_info(path)
    existing = dbmod.get_document_by_path(conn, str(path))

    # 预筛：stat 没变 -> 内容几乎不可能变，连文件都不打开
    # **但嵌入格式版本变了不行**：embed_text 的构造方式变了（比如把文件名
    # 加进去），旧向量就是过期的，必须重新切片 + 重新嵌入。
    # **切片格式版本变了也不行**：切片方式变了（正文可能一个字没动），chunk 里的
    # 定位信息过期了，得重新切一遍——但向量仍按 content_hash 复用，不花钱。
    if (prescreen and not force
            and existing
            and existing["index_status"] == "done"
            and existing["mtime"] == mtime
            and existing["size"] == size
            and dbmod.doc_embed_version(conn, existing["id"])
                == config.EMBED_FORMAT_VERSION
            and dbmod.doc_chunk_version(conn, existing["id"])
                == config.CHUNK_FORMAT_VERSION
            # 有向量模型时，向量也得齐全才能跳过：文档是"先有关键词索引、
            # 后来才下载的模型"这种情况，向量还是空的，得留给补建。
            # 没有 embedder（没模型）时不要求向量——否则每次扫描都要把所有
            # 文档重切一遍，纯属白费力气。
            and (embedder is None
                 or dbmod.doc_missing_vectors(conn, existing["id"]) == 0)):
        return "skipped"

    chash = known_hash if known_hash else detect.compute_hash(path)

    # 文件移动识别：内容 hash 相同、路径不同 -> 只更新 file_path，不重新嵌入
    if not force and existing is None:
        candidate = dbmod.find_document_by_hash(conn, chash)
        if candidate and candidate["file_path"] != str(path):
            dbmod.move_document(conn, candidate["file_path"], str(path))
            return "moved"

    if (not force and existing
            and existing["content_hash"] == chash
            and existing["index_status"] == "done"
            and dbmod.doc_embed_version(conn, existing["id"])
                == config.EMBED_FORMAT_VERSION
            and dbmod.doc_chunk_version(conn, existing["id"])
                == config.CHUNK_FORMAT_VERSION
            and (embedder is None
                 or dbmod.doc_missing_vectors(conn, existing["id"]) == 0)):
        # 内容没变 **且** 嵌入格式版本也没变 → 真的可以跳过。
        # （版本变了说明 embed_text 的构造方式变了，旧向量是过期的，
        #   必须继续往下走：重新切片 + 重新嵌入。）
        # 另外：内容没变、只是 stat 变了（touch / 保存了相同内容 / 从别处拷来一样的）
        # → 把 stat 更新到最新，下一轮就能靠 stat 直接跳过，不再读这个文件。
        if existing["mtime"] != mtime or existing["size"] != size:
            dbmod.update_document_stat(conn, existing["id"], mtime, size)
        return "skipped"

    # 按文件类型分派切片器：
    # - Markdown：读全文 → 标题树切片
    # - PDF：读大纲 + 逐页文本 → 大纲标题树切片（扫描件会切不出东西 → 跳过）
    source_kind = "pdf" if detect.is_pdf(path) else "markdown"
    # 指纹里带上切片格式版本：切片方式变了（比如 CHUNK_MIN_CHARS 调低），
    # 同一个文件可能就切得出块了，此时缓存必须失效。
    sig = (mtime, size, config.CHUNK_FORMAT_VERSION)
    if not force and _EMPTY_CACHE.get(str(path)) == sig:
        # 文件自上次判定后没动过，而「没有文字层」不会自己变出来 → 跳过解析。
        logger.debug("命中「已知无正文」缓存，跳过解析：%s", path)
        chunks = []
    elif source_kind == "pdf":
        try:
            chunks = pdf.chunk_pdf(
                path,
                file_path=str(path),
                doc_type=doc_type,
                min_chars=config.CHUNK_MIN_CHARS,
                max_chars=config.CHUNK_MAX_CHARS,
                parent_max_chars=config.PARENT_MAX_CHARS,
            )
        except Exception as e:  # noqa: BLE001 坏文件 / 加密 / 没装 pypdf
            logger.warning("PDF 解析失败，跳过：%s（%s）", path, e)
            chunks = []
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
        chunks = chunk.chunk_markdown(
            text,
            file_path=str(path),
            doc_type=doc_type,
            min_chars=config.CHUNK_MIN_CHARS,
            max_chars=config.CHUNK_MAX_CHARS,
            parent_max_chars=config.PARENT_MAX_CHARS,
        )

    if not chunks:
        if source_kind == "pdf":
            # 扫描件 / 纯图片 PDF：正文是图，没有文字层可抽 → 切不出块。
            #
            # 走 debug 而不是 info：全量扫描时这类文件往往成百上千（一个扫描
            # 件资料库就是一整批），逐条 info 会把日志刷满，真正要看的报错
            # 反而被淹掉。批量层（index_roots / index_paths）会在收尾时按
            # stats["empty"] 汇总出一条，既保留"索引没坏、只是这些文件没正文"
            # 的信息，又不刷屏；要查具体是哪些文件，开 DEBUG 即可。
            logger.debug(
                "PDF 没有可索引的正文（多半是扫描件 / 纯图片 PDF），跳过：%s", path
            )
        # 记进内存缓存：下一轮只要文件没变，就别再解析一遍了。
        if len(_EMPTY_CACHE) >= _EMPTY_CACHE_MAX:
            _EMPTY_CACHE.clear()
        _EMPTY_CACHE[str(path)] = sig
        if existing:
            dbmod.delete_document(conn, existing["id"])
        return "empty"

    title = chunks[0].metadata.get("title") or path.stem
    page_count = chunks[0].metadata.get("page_count")
    doc_id = dbmod.upsert_document(
        conn, str(path), doc_type, title, mtime, size, chash,
        index_status="done", indexed_at=time.time(),
        page_count=page_count, source_kind=source_kind,
    )

    # 增量向量化：仅对新增或内容变化的叶子 chunk 重新嵌入
    state = dbmod.get_chunk_state(conn, doc_id)
    leaves = [c for c in chunks if not c.is_parent]
    to_embed = []
    vectors = {}
    if embedder is None:
        # **没有 embedding 模型**（没装 litert-lm）：只建关键词索引，向量列留 NULL。
        # 向量召回自然召不回东西（向量是空的），但 BM25 通道完全可用——
        # 「能搜字面词」不该被「装不装得动几百 MB 的模型」卡住。
        pass
    else:
        # 向量段：**任何一步失败都只降级为"只建关键词索引"，不让整个索引任务
        # 挂掉**。以前这里是裸的 `embedder.dimension`——模型没下载 / litert-lm
        # 没装时会直接抛，整批文件一个都索引不了（连关键词都没有）。
        # 现在：关键词照写，向量留空，等模型就绪后再补建（见 service.backfill_vectors）。
        dim = None
        try:
            dim = int(embedder.dimension)      # 当前模型的向量维度
            # numpy 延迟导入（同 db.py）：只在向量路径需要
            np = require_numpy()
            for c in leaves:
                prev = state.get(c.chunk_id)
                # 复用旧向量的前提：内容没变 **且** 维度与当前模型一致
                # （换过 embedding 模型时维度会变，必须重新嵌入，否则检索时会被静默跳过）
                # **且** 嵌入文本格式版本一致（embed_text 的构造方式变了，
                # 比如把文件名加进去 —— content_hash 只算正文，看不到这个变化，
                # 不校验版本就会「改了格式等于没改」）。
                # force=True 表示「强制重建」：一律重新嵌入，不复用任何旧向量。
                if (not force
                        and prev and prev[0] == c.content_hash and prev[1] is not None
                        and prev[2] == config.EMBED_FORMAT_VERSION
                        and len(prev[1]) == dim * 4):
                    vectors[c.chunk_id] = np.frombuffer(prev[1], dtype=np.float32)
                else:
                    to_embed.append(c)
            if to_embed:
                embs = embedder.embed_documents([c.embed_text for c in to_embed])
                for c, e in zip(to_embed, embs):
                    vectors[c.chunk_id] = e
        except Exception as e:  # noqa: BLE001 —— 缺模型 / 推理炸了都不该中断索引
            logger.warning(
                "向量化失败，本次只建关键词索引（%s）：%s（%s）",
                path, type(e).__name__, e,
            )
            # **别把已有的向量抹掉**：写回时 vector 列会被覆盖，写成 NULL 等于
            # 把之前算好的向量删了（用户还得再等一次重算）。所以这里把旧向量
            # 原样带回，让"向量失败"只影响新增的那部分。
            vectors = {}
            try:
                np = require_numpy()
            except Exception as np_err:  # noqa: BLE001 numpy 都没有 → 只能不动库
                logger.warning("numpy 不可用，本次不更新该文档的索引：%s", np_err)
                return "skipped"
            for c in leaves:
                prev = state.get(c.chunk_id)
                if prev and prev[1] is not None:
                    try:
                        vectors[c.chunk_id] = np.frombuffer(prev[1], dtype=np.float32)
                    except Exception:  # noqa: BLE001 坏数据就当它没有
                        pass

    # 切出块了 → 清掉「已知无正文」的旧条目，免得以后被缓存误跳过
    _EMPTY_CACHE.pop(str(path), None)

    # 父块不参与向量化（vector 保持 NULL）
    dbmod.sync_chunks(conn, doc_id, chunks, vectors)

    # 记下"这个索引现在有哪些通道"与向量元信息 —— 界面上"要不要补建向量索引"
    # 就是靠这些字段判断的（模型下载完但向量还是空的，得提示用户补建）。
    kinds = set(dbmod.index_kinds(conn))
    kinds.add("keyword")
    if vectors:
        kinds.add("vector")
        dbmod.set_meta(conn, "embed_model", config.MODEL_REPO or "")
        dbmod.set_meta(conn, "embed_dim", str(dim or 0))
        dbmod.set_meta(conn, "last_vector_at", str(int(time.time())))
    dbmod.set_meta(conn, "index_kinds", ",".join(sorted(kinds)))
    conn.commit()
    return "indexed"


def _log_empty_summary(stats: dict) -> None:
    """把「本轮有多少文件没有可索引正文」汇成一条日志。

    逐条打印在语料里有一批扫描件时会直接刷屏（见 ``index_path`` 里的注释），
    所以明细留在 DEBUG，这里只给计数。文案不写死"扫描件"——markdown 文件
    是空的也会归到 ``empty``，一并说清楚，免得误导排查方向。
    """
    n = stats.get("empty", 0)
    if n > 0:
        logger.info(
            "本轮跳过 %d 个没有可索引正文的文件（PDF 无文字层，如扫描件 / 纯图片；"
            "或文件本身为空）；具体文件名需开 DEBUG 日志查看",
            n,
        )


def index_roots(conn, embedder, roots=None, force=False, doc_type=None,
                 prescreen=False, on_progress=None):
    """索引多个根目录，并清理已不存在的文件对应的索引。

    ``prescreen=True`` 时按 stat（mtime + size）预筛：没变过的文件连内容都不读。

    ``on_progress``：进度回调（见模块 docstring）。建索引是后台分钟级操作，
    用它把「第 N 个 / 共 M 个」透出去；不传则零开销。
    """
    roots = roots or config.default_roots()
    doc_type = doc_type or config.DOC_TYPE
    stats = {"indexed": 0, "skipped": 0, "empty": 0, "deleted": 0, "moved": 0}

    all_files = []
    for root in roots:
        all_files.extend(detect.list_indexable_files(Path(root)))

    total = len(all_files)

    # 先处理文件移动：内容 hash 相同、路径不同 -> 只更新 file_path，不重新嵌入
    # （必须在删除同步之前，否则移动会被当成"删除+新建"）
    # force=True 时跳过移动识别：强制重建的语义是"这个文件必须重新切+重新嵌"，
    # 若先被判成 moved 就直接返回了，等于没重建。
    #
    # 这里算出来的 hash 会被下面的 index_path 复用——否则首次索引时每个文件
    # 要被读两遍（移动识别一次、切片前又一次），磁盘 IO 直接翻倍。
    hashes: dict = {}
    if not force:
        for i, f in enumerate(all_files):
            if on_progress:
                on_progress(done=i + 1, total=total, path=str(f), stage="scanning")
            if dbmod.get_document_by_path(conn, str(f)) is not None:
                continue
            chash = detect.compute_hash(f)
            hashes[str(f)] = chash
            candidate = dbmod.find_document_by_hash(conn, chash)
            if candidate and candidate["file_path"] != str(f):
                dbmod.move_document(conn, candidate["file_path"], str(f))
                stats["moved"] += 1

    # 删除同步：数据库中存在但磁盘已不存在
    indexed_paths = {r[0] for r in conn.execute("SELECT file_path FROM documents").fetchall()}
    current_paths = {str(f) for f in all_files}
    for p in indexed_paths - current_paths:
        doc = dbmod.get_document_by_path(conn, p)
        if doc:
            dbmod.delete_document(conn, doc["id"])
            stats["deleted"] += 1

    for i, f in enumerate(all_files):
        if on_progress:
            on_progress(done=i + 1, total=total, path=str(f), stage="indexing")
        res = index_path(conn, embedder, f, force=force, doc_type=doc_type,
                         prescreen=prescreen, known_hash=hashes.get(str(f)))
        stats[res] = stats.get(res, 0) + 1

    if on_progress:
        on_progress(done=total, total=total, path="", stage="done")
    _log_empty_summary(stats)
    return stats


def index_paths(conn, embedder, paths, force=False, doc_type=None,
                 on_progress=None):
    """索引**指定的一批文件**（文件监听用），返回统计。

    与 ``index_roots`` 的区别：不遍历目录、不做删除同步——只处理传进来的路径。
    文件已经不存在（用户删了）→ 把它的索引一并删掉。

    ``on_progress``：进度回调（见模块 docstring）。桌面端文件监听触发的小批量
    索引也会经过它，单个文件时 ``total`` 为 1。
    """
    doc_type = doc_type or config.DOC_TYPE
    stats = {"indexed": 0, "skipped": 0, "empty": 0, "deleted": 0, "moved": 0}

    now = time.time()
    # 清掉过期的抑制记录（dict 会一直长）
    for k in [k for k, t in _RECENT_INDEX.items() if now - t > _RECENT_TTL]:
        _RECENT_INDEX.pop(k, None)

    files = [Path(p) for p in (paths or [])]
    total = len(files)
    for i, p in enumerate(files):
        key = str(p)

        # 文件已经不存在（用户删了）→ 立刻把索引删掉。
        # 这一步**不受抑制**：删除必须马上生效。
        if not p.is_file():
            if on_progress:
                on_progress(done=i + 1, total=total, path=key, stage="indexing")
            doc = dbmod.get_document_by_path(conn, key)
            if doc:
                dbmod.delete_document(conn, doc["id"])
                stats["deleted"] += 1
            continue

        # 短期抑制：几秒内刚索引过这个文件 → 直接跳过。
        # 原因有二：
        # 1. 一次保存会连发好几个事件（write + rename），没必要重复处理；
        # 2. **内核自己读文件也会改 atime** —— 在会即时更新 atime 的系统上，
        #    「索引 → 读 → atime 变 → 又触发事件 → 再索引」会自激成死循环。
        last = _RECENT_INDEX.get(key)
        if last is not None and (now - last) < _RECENT_TTL:
            if on_progress:
                on_progress(done=i + 1, total=total, path=key, stage="indexing")
            stats["skipped"] += 1
            continue

        if not detect.is_indexable(p):
            continue
        if on_progress:
            on_progress(done=i + 1, total=total, path=key, stage="indexing")
        res = index_path(conn, embedder, p, force=force, doc_type=doc_type)
        stats[res] = stats.get(res, 0) + 1
        _RECENT_INDEX[key] = time.time()

    if on_progress:
        on_progress(done=total, total=total, path="", stage="done")
    _log_empty_summary(stats)
    return stats
