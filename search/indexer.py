"""索引编排：扫描目录 -> 变更检测 -> 结构化切片 -> 增量向量化 -> 写入 SQLite。

增量策略：以 chunks.content_hash 为单位比对，只对新增/变化的叶子 chunk 重新嵌入；
文件移动只更新 documents.file_path（并按新路径重算 chunk_id），不触发重新嵌入。
force=True 时忽略一切复用：重新切片 + 全部叶子重新嵌入。
"""
import logging
import time
from pathlib import Path


from . import config
from .deps import require_numpy
from .ingest import chunk, detect, pdf
from .store import db as dbmod

logger = logging.getLogger(__name__)

#: 最近索引过的路径 -> 时刻（用于抑制重复通知，见 ``index_paths``）。
_RECENT_INDEX: dict[str, float] = {}
#: 抑制窗口（秒）。
_RECENT_TTL = 5.0


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
    if (prescreen and not force
            and existing
            and existing["index_status"] == "done"
            and existing["mtime"] == mtime
            and existing["size"] == size
            and dbmod.doc_embed_version(conn, existing["id"])
                == config.EMBED_FORMAT_VERSION):
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
                == config.EMBED_FORMAT_VERSION):
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
    if detect.is_pdf(path):
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
        source_kind = "pdf"
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
        source_kind = "markdown"

    if not chunks:
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

    # 父块不参与向量化（vector 保持 NULL）
    dbmod.sync_chunks(conn, doc_id, chunks, vectors)
    return "indexed"


def index_roots(conn, embedder, roots=None, force=False, doc_type=None,
                 prescreen=False):
    """索引多个根目录，并清理已不存在的文件对应的索引。

    ``prescreen=True`` 时按 stat（mtime + size）预筛：没变过的文件连内容都不读。
    """
    roots = roots or config.default_roots()
    doc_type = doc_type or config.DOC_TYPE
    stats = {"indexed": 0, "skipped": 0, "empty": 0, "deleted": 0, "moved": 0}

    all_files = []
    for root in roots:
        all_files.extend(detect.list_indexable_files(Path(root)))

    # 先处理文件移动：内容 hash 相同、路径不同 -> 只更新 file_path，不重新嵌入
    # （必须在删除同步之前，否则移动会被当成"删除+新建"）
    # force=True 时跳过移动识别：强制重建的语义是"这个文件必须重新切+重新嵌"，
    # 若先被判成 moved 就直接返回了，等于没重建。
    #
    # 这里算出来的 hash 会被下面的 index_path 复用——否则首次索引时每个文件
    # 要被读两遍（移动识别一次、切片前又一次），磁盘 IO 直接翻倍。
    hashes: dict = {}
    if not force:
        for f in all_files:
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

    for f in all_files:
        res = index_path(conn, embedder, f, force=force, doc_type=doc_type,
                         prescreen=prescreen, known_hash=hashes.get(str(f)))
        stats[res] = stats.get(res, 0) + 1

    return stats


def index_paths(conn, embedder, paths, force=False, doc_type=None):
    """索引**指定的一批文件**（文件监听用），返回统计。

    与 ``index_roots`` 的区别：不遍历目录、不做删除同步——只处理传进来的路径。
    文件已经不存在（用户删了）→ 把它的索引一并删掉。
    """
    doc_type = doc_type or config.DOC_TYPE
    stats = {"indexed": 0, "skipped": 0, "empty": 0, "deleted": 0, "moved": 0}

    now = time.time()
    # 清掉过期的抑制记录（dict 会一直长）
    for k in [k for k, t in _RECENT_INDEX.items() if now - t > _RECENT_TTL]:
        _RECENT_INDEX.pop(k, None)

    for p in paths or []:
        p = Path(p)
        key = str(p)

        # 文件已经不存在（用户删了）→ 立刻把索引删掉。
        # 这一步**不受抑制**：删除必须马上生效。
        if not p.is_file():
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
            stats["skipped"] += 1
            continue

        if not detect.is_indexable(p):
            continue
        res = index_path(conn, embedder, p, force=force, doc_type=doc_type)
        stats[res] = stats.get(res, 0) + 1
        _RECENT_INDEX[key] = time.time()

    return stats
