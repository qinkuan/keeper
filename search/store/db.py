"""SQLite 存储：documents（文件级元数据）+ chunks（切片与向量）。

按《Markdown 切片规则与数据库设计》实现：

- documents：file_path 唯一，doc_type / index_status / 时间戳等文件级信息。
- chunks：chunk_id 为主键；父块 is_parent=1 且 vector=NULL，不参与向量检索，
  仅用于命中叶子后按 parent_id 回溯。
- chunks_fts：FTS5 外部内容表（关键词召回），由触发器与 chunks 同步。
"""
import re
import sqlite3
from pathlib import Path


from .. import config
from ..deps import require_numpy

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path     TEXT NOT NULL UNIQUE,
    doc_type      TEXT NOT NULL,
    title         TEXT,
    mtime         REAL NOT NULL,
    size          INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,
    index_status  TEXT NOT NULL DEFAULT 'pending',
    indexed_at    REAL,
    error         TEXT,
    page_count    INTEGER,        -- PDF 的总页数（Markdown 为 NULL）
    source_kind   TEXT,           -- 源类型：markdown / pdf（决定前端怎么显示定位）
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id      TEXT PRIMARY KEY,
    doc_id        INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    parent_id     TEXT,
    is_parent     INTEGER NOT NULL DEFAULT 0,
    is_doc_root   INTEGER NOT NULL DEFAULT 0,
    title_level   INTEGER NOT NULL,
    titlepath     TEXT NOT NULL,
    text          TEXT NOT NULL,
    embed_text    TEXT,
    vector        BLOB,
    start_line    INTEGER NOT NULL,
    end_line      INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,
    chunk_index   INTEGER NOT NULL,
    tokens        TEXT,
    title_tokens  TEXT,
    name_tokens   TEXT,           -- 文件名 token（整体规范化形式 + 分词形式）
    doc_title_tokens TEXT,        -- 文档级标题 token（路径第一级 + 文档标题）
    body_len      INTEGER,       -- tokens 的词数（长度归一用）
    title_len     INTEGER,       -- title_tokens 的词数（长度归一用）
    locator       TEXT,           -- 定位串：PDF 形如 "第 12 页"，Markdown 为 NULL
    embed_version INTEGER         -- 嵌入文本格式版本（config.EMBED_FORMAT_VERSION）
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc    ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_parent ON chunks(parent_id);
CREATE INDEX IF NOT EXISTS idx_chunks_leaf   ON chunks(doc_id, is_parent, chunk_index);
CREATE TABLE IF NOT EXISTS idx_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# FTS5 外部内容表：索引内容存在 chunks.tokens / chunks.title_tokens /
# chunks.name_tokens / chunks.doc_title_tokens 里，触发器保证两边同步。
# unicode61 + 预分词文本（jieba 切好的词，空格分隔）。
#
# 四列的分工（**FTS5 只负责召回，不负责排序**——排序见 rank.py 的 BM25F）：
#   tokens         正文（块级）
#   title_tokens   章节路径（块级：标题路径去掉第一级）
#   name_tokens    文件名（文档级）
#   doc_title_tokens 文档标题（文档级：标题路径第一级 + 文档标题）
#
# 为什么要把文档级字段单独成列：FTS5 的 bm25() **一个词只算一个 idf**（跨列共用），
# 而它的 df 是**按 chunk 行**算的——文档级字段写进每个 chunk 会把 df 撑大，
# idf 变负被钳到 1e-6，连带把正文里同一个词的命中也"毒化"掉。
# 自己算 BM25F（文档级 idf）就没有这个问题，见 search/rank.py。
#
# FTS5 虚表的列在 CREATE 时就定死了，**不能 ALTER**；列数变了只能删了重建
# （见 _ensure_fts）。
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    tokens, title_tokens, name_tokens, doc_title_tokens,
    content='chunks', content_rowid='rowid',
    tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, tokens, title_tokens, name_tokens, doc_title_tokens)
    VALUES (new.rowid, new.tokens, new.title_tokens, new.name_tokens,
            new.doc_title_tokens);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, tokens, title_tokens, name_tokens,
                           doc_title_tokens)
    VALUES ('delete', old.rowid, old.tokens, old.title_tokens, old.name_tokens,
            old.doc_title_tokens);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, tokens, title_tokens, name_tokens,
                           doc_title_tokens)
    VALUES ('delete', old.rowid, old.tokens, old.title_tokens, old.name_tokens,
            old.doc_title_tokens);
    INSERT INTO chunks_fts(rowid, tokens, title_tokens, name_tokens, doc_title_tokens)
    VALUES (new.rowid, new.tokens, new.title_tokens, new.name_tokens,
            new.doc_title_tokens);
END;
"""

#: FTS5 虚表的列（顺序 = bm25() 权重参数的顺序）。
FTS_COLS = ("tokens", "title_tokens", "name_tokens", "doc_title_tokens")


def connect(db_path):
    """打开数据库；检测到旧 schema 时自动重建（需重新索引）。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    if _needs_migration(conn):
        _migrate_legacy(conn)
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    _ensure_keyword_version(conn)
    _ensure_fts(conn)
    _drop_legacy_meta(conn)
    return conn


#: 后加的列（旧库没有，靠 ALTER TABLE 补上；新库由 SCHEMA 直接建好）。
#:
#: 这些列都是**加法**——不补也不影响已有功能（locator 只是定位串、page_count
#: 只是页数、source_kind 只决定前端怎么显示），所以补列失败也不该阻断启动。
_ADDED_COLUMNS = (
    ("documents", "page_count", "INTEGER"),
    ("documents", "source_kind", "TEXT"),
    ("chunks", "locator", "TEXT"),
    ("chunks", "embed_version", "INTEGER"),
    ("chunks", "name_tokens", "TEXT"),
    ("chunks", "doc_title_tokens", "TEXT"),
    ("chunks", "body_len", "INTEGER"),
    ("chunks", "title_len", "INTEGER"),
)


def _ensure_columns(conn) -> None:
    """给旧库补上后加的列（幂等）。"""
    try:
        for table, column, decl in _ADDED_COLUMNS:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        conn.commit()
    except sqlite3.Error as e:  # noqa: BLE001 补列失败不该阻断启动
        print(f"[db] 补列失败（不影响使用）：{e}")


def _drop_legacy_meta(conn):
    """meta 表已废弃（模型/维度信息改由向量本身承载），顺手清掉旧库里的残留。"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if row:
        conn.execute("DROP TABLE meta")
        conn.commit()


def _ensure_fts(conn):
    """确保 FTS5 关键词索引就绪。

    三种情况：

    1. 旧库没有 tokens / title_tokens 列时：加列 -> 用 jieba 回填预分词文本
       （**不需要重新嵌入向量**）-> 建 FTS5 表 -> rebuild 索引。
    2. **FTS 列数变了**（新增 name_tokens 文件名列）：FTS5 虚表的列在 CREATE
       时就定死了、不能 ALTER -> 只能删掉虚表重建（drop + recreate + rebuild），
       同样**不需要重新嵌入向量**。
    3. 库里已有 chunk 没有 name_tokens（老库自动回填）：直接 UPDATE 回填，
       UPDATE 触发器会同步进 FTS 索引。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()}
    added = False
    if "tokens" not in cols:
        conn.execute("ALTER TABLE chunks ADD COLUMN tokens TEXT")
        added = True
    if "title_tokens" not in cols:
        conn.execute("ALTER TABLE chunks ADD COLUMN title_tokens TEXT")
        added = True
    if added:
        from .. import tokenize as tokmod
        rows = conn.execute(
            "SELECT chunk_id, titlepath, text FROM chunks").fetchall()
        upd = conn.cursor()
        for cid, tp, text in rows:
            upd.execute(
                "UPDATE chunks SET tokens=?, title_tokens=? WHERE chunk_id=?",
                (tokmod.token_string(text or ""),
                 tokmod.token_string(tp or ""), cid),
            )
        conn.commit()
        print(f"[db] 已回填 {len(rows)} 条片段的关键词索引（无需重新嵌入向量）")

    # 老库补 name_tokens 等字段：由 _ensure_keyword_version 统一处理

    existed = _fts_exists(conn)
    fts_cols = _fts_column_names(conn)
    need_recreate = existed and list(fts_cols) != list(FTS_COLS)
    if need_recreate:
        # 列数变了：删掉旧虚表（连同旧触发器），下面用新 schema 重建
        _drop_fts(conn)
        existed = False

    try:
        conn.executescript(FTS_SCHEMA)
    except sqlite3.OperationalError as e:
        raise RuntimeError(
            "当前 Python 的 SQLite 未启用 FTS5，关键词检索不可用。"
            "请换一个带 FTS5 的 Python（sqlite3.sqlite_version >= 3.9 且编译时带 "
            "SQLITE_ENABLE_FTS5）。原始错误：" + str(e)) from e

    if not _fts_healthy(conn):
        # 影子表被误删（chunks_fts_data/idx/docsize/config）-> 虚表无法构造
        _rebuild_fts(conn)
    elif need_recreate or not existed or added:
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        conn.commit()


def _fts_exists(conn):
    """FTS5 虚表是否已存在。"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
    ).fetchone()
    return bool(row)


def _fts_column_names(conn):
    """FTS5 虚表当前的列名（按声明顺序）。"""
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(chunks_fts)").fetchall()]
    except sqlite3.Error:
        return []


def _ensure_keyword_version(conn):
    """关键词字段的格式版本对不上时，回填一遍（**不需要重新嵌入向量**）。

    关键词字段（正文 / 章节路径 / 文件名 / 文档标题的 token 串与长度）全部可以
    从 ``chunks.text`` / ``chunks.titlepath`` / ``documents`` 现算——所以版本变了
    只要重算一遍，不用重新切、也不用重新嵌。
    """
    row = conn.execute(
        "SELECT value FROM idx_meta WHERE key='keyword_version'").fetchone()
    cur = row[0] if row else None
    try:
        cur_v = int(cur) if cur is not None else 0
    except (TypeError, ValueError):
        cur_v = 0
    if cur_v == config.KEYWORD_FORMAT_VERSION:
        return
    n = _backfill_keyword_fields(conn)
    conn.execute(
        "INSERT INTO idx_meta(key, value) VALUES('keyword_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(config.KEYWORD_FORMAT_VERSION),),
    )
    conn.commit()
    if n:
        print(f"[db] 关键词字段已按新规则重算 {n} 条（无需重新嵌入向量）")


def _backfill_keyword_fields(conn):
    """按当前规则重算所有 chunk 的关键词字段。

    规则（与 ``sync_chunks`` 一致）：
      - ``tokens``         正文（已有）
      - ``title_tokens``   章节路径 = 标题路径**去掉第一级**（块级字段）
      - ``name_tokens``    文件名（文档级字段，每块都存）
      - ``doc_title_tokens`` 文档级标题 = 标题路径第一级 + 文档标题
      - ``body_len`` / ``title_len`` 词数（长度归一用）
    """
    from .. import tokenize as tokmod
    docs = {}
    for doc_id, fp, title in conn.execute(
            "SELECT id, file_path, title FROM documents"):
        docs[doc_id] = (Path(fp or "").stem.strip(), (title or "").strip())

    rows = conn.execute(
        "SELECT chunk_id, doc_id, titlepath, tokens FROM chunks").fetchall()
    upd = conn.cursor()
    n = 0
    for cid, doc_id, tp, tokens in rows:
        stem, dtitle = docs.get(doc_id, ("", ""))
        parts = [p for p in (tp or "").split("/") if p]
        doc_title_part = parts[0] if parts else ""
        if doc_title_part == stem:
            doc_title_part = ""      # 无标题文档：第一级就是文件名 → 归 name 管
        title_tokens = tokmod.token_string("/".join(parts[1:]))
        extra = dtitle if (dtitle and dtitle != stem
                           and dtitle != doc_title_part) else ""
        doc_title_tokens = tokmod.token_string(
            " ".join(x for x in (doc_title_part, extra) if x))
        name_tokens = tokmod.name_token_string(stem)
        upd.execute(
            "UPDATE chunks SET tokens=?, title_tokens=?, name_tokens=?, "
            "doc_title_tokens=?, body_len=?, title_len=? WHERE chunk_id=?",
            (tokens if tokens is not None else tokmod.token_string(""),
             title_tokens, name_tokens, doc_title_tokens,
             len((tokens or "").split()), len(title_tokens.split()), cid),
        )
        n += 1
    conn.commit()
    return n


def _fts_healthy(conn):
    """FTS5 虚表是否可用（影子表齐全、构造器能加载）。"""
    try:
        conn.execute("SELECT count(*) FROM chunks_fts").fetchone()
        return True
    except sqlite3.Error:
        return False


def _drop_fts(conn):
    """删掉 FTS5 虚表与它的三个触发器（用于列数变了要重建的场景）。"""
    for name in ("chunks_fts_ai", "chunks_fts_ad", "chunks_fts_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    try:
        conn.execute("DROP TABLE IF EXISTS chunks_fts")
    except sqlite3.Error:
        # 虚表已损坏（影子表缺失）-> 绕过 sqlite_master 删除条目与残留影子表
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("DELETE FROM sqlite_master WHERE type='table' AND name='chunks_fts'")
        conn.execute("PRAGMA writable_schema=OFF")
        conn.commit()
        for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'chunks_fts_%'").fetchall():
            conn.execute(f"DROP TABLE IF EXISTS {r[0]}")
        conn.commit()
        conn.execute("VACUUM")


def _rebuild_fts(conn):
    """重建 FTS5 虚表与倒排索引。

    影子表被误删时虚表连 DROP 都会失败，只能绕过 sqlite_master 删掉它的条目，
    再重新 CREATE + rebuild。**只影响关键词索引，向量无需重新嵌入。**
    """
    print("[db] FTS5 关键词索引损坏，正在重建（不影响向量）…")
    _drop_fts(conn)
    conn.executescript(FTS_SCHEMA)
    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
    conn.commit()


def _needs_migration(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='documents'"
    ).fetchone()
    if not row:
        return False
    cols = {r[1] for r in conn.execute("PRAGMA table_info(documents)").fetchall()}
    return "doc_type" not in cols


def _migrate_legacy(conn):
    """旧 schema -> 新 schema。

    保留文件级元数据（path -> file_path 等），旧 chunks 因结构不兼容
    （无 chunk_id / titlepath / embed_text / content_hash）无法沿用，
    统一清空并把文档标记为 pending，等待重新索引。
    """
    import time
    old_docs = []
    try:
        old_docs = conn.execute(
            "SELECT path,title,mtime,size,content_hash,indexed_at FROM documents"
        ).fetchall()
    except sqlite3.Error:
        old_docs = []

    conn.execute("DROP TABLE IF EXISTS chunks")
    conn.execute("DROP TABLE IF EXISTS documents")
    conn.execute("DROP TABLE IF EXISTS meta")
    conn.executescript(SCHEMA)

    now = time.time()
    for path, title, mtime, size, chash, indexed_at in old_docs:
        conn.execute(
            "INSERT INTO documents(file_path,doc_type,title,mtime,size,content_hash,"
            "index_status,indexed_at,error,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (path, config.DOC_TYPE, title, mtime or now, size or 0, chash or "",
             "pending", indexed_at, "旧索引结构不兼容，需重新索引", now, now),
        )
    conn.commit()
    print(f"[db] 旧版 schema 已迁移：保留 {len(old_docs)} 条文件元数据，"
          f"chunk 需重新索引")


# ---------------------------------------------------------------- 向量序列化


def _serialize(vec) -> bytes:
    # numpy 只在**向量**路径用到，延迟导入：没装 numpy 时关键词检索照常可用
    np = require_numpy()

    return np.asarray(vec, dtype=np.float32).tobytes()


# ---------------------------------------------------------------- documents


def upsert_document(conn, file_path, doc_type, title, mtime, size, content_hash,
                    index_status="done", indexed_at=None, error=None, now=None,
                    page_count=None, source_kind=None):
    """按 file_path 插入/更新文档，返回 doc_id。

    ``page_count`` / ``source_kind`` 是 PDF 才有的信息（页数 / 源类型），
    Markdown 传 None 即可。
    """
    now = now if now is not None else __import__("time").time()
    conn.execute(
        "INSERT INTO documents(file_path,doc_type,title,mtime,size,content_hash,"
        "index_status,indexed_at,error,page_count,source_kind,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(file_path) DO UPDATE SET "
        "doc_type=excluded.doc_type, title=excluded.title, mtime=excluded.mtime, "
        "size=excluded.size, content_hash=excluded.content_hash, "
        "index_status=excluded.index_status, indexed_at=excluded.indexed_at, "
        "error=excluded.error, page_count=excluded.page_count, "
        "source_kind=excluded.source_kind, updated_at=excluded.updated_at",
        (str(file_path), doc_type, title, mtime, size, content_hash,
         index_status, indexed_at, error, page_count, source_kind, now, now),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM documents WHERE file_path=?", (str(file_path),)
    ).fetchone()[0]


def get_document(conn, doc_id):
    row = conn.execute(
        "SELECT id,file_path,doc_type,title,mtime,size,content_hash,index_status,"
        "indexed_at,error,page_count,source_kind,created_at,updated_at "
        "FROM documents WHERE id=?",
        (doc_id,),
    ).fetchone()
    if not row:
        return None
    cols = ["id", "file_path", "doc_type", "title", "mtime", "size", "content_hash",
            "index_status", "indexed_at", "error", "page_count", "source_kind",
            "created_at", "updated_at"]
    return dict(zip(cols, row))


def get_document_by_path(conn, file_path):
    row = conn.execute(
        "SELECT id FROM documents WHERE file_path=?", (str(file_path),)
    ).fetchone()
    return get_document(conn, row[0]) if row else None


def find_document_by_hash(conn, content_hash):
    """按内容 hash 查找文档（用于识别"文件移动"）。"""
    row = conn.execute(
        "SELECT id FROM documents WHERE content_hash=? LIMIT 1", (content_hash,)
    ).fetchone()
    return get_document(conn, row[0]) if row else None


def move_document(conn, old_path, new_path):
    """文件移动：更新 file_path，chunks.doc_id 不变（不触发重新嵌入）。

    chunk_id 是「路径 + 标题路径 + 序号」的派生值，所以路径一变，所有 chunk_id 都要跟着变，
    否则下次该文件内容一改，切出来的新 chunk_id 跟旧的全不一样 → 旧向量全部作废、整篇重嵌。
    这里按新路径原子重算 chunk_id 并同步 parent_id 引用（移动不碰内容，向量照旧复用）。
    """
    from ..ingest.chunk import leaf_chunk_id, parent_chunk_id
    from .. import tokenize as tokmod

    old_path, new_path = str(old_path), str(new_path)
    row = conn.execute(
        "SELECT id FROM documents WHERE file_path=?", (old_path,)
    ).fetchone()
    if not row:
        return
    doc_id = row[0]

    rows = conn.execute(
        "SELECT chunk_id, parent_id, titlepath, chunk_index, is_parent "
        "FROM chunks WHERE doc_id=?", (doc_id,)
    ).fetchall()
    id_map = {}
    for cid, _pid, tp, idx, is_parent in rows:
        suffix = "parent" if is_parent else str(idx)
        id_map[cid] = (parent_chunk_id(new_path, tp) if is_parent
                       else leaf_chunk_id(new_path, tp, idx))

    conn.execute(
        "UPDATE documents SET file_path=?, updated_at=? WHERE file_path=?",
        (new_path, __import__("time").time(), old_path),
    )
    # 文件名变了 -> 关键词字段要跟着重算（否则搜旧文件名还能命中、新文件名搜不到）：
    #   - name_tokens：新文件名
    #   - 无标题文档的 titlepath 第一级就是文件名 -> 也要换
    #   - title_tokens / doc_title_tokens：跟着 titlepath 重算
    old_stem = Path(old_path).stem.strip()
    new_stem = Path(new_path).stem.strip()
    dtitle_row = conn.execute(
        "SELECT title FROM documents WHERE id=?", (doc_id,)).fetchone()
    dtitle = (dtitle_row[0] or "").strip() if dtitle_row else ""
    for cid, tp in conn.execute(
            "SELECT chunk_id, titlepath FROM chunks WHERE doc_id=?", (doc_id,)).fetchall():
        parts = [p for p in (tp or "").split("/") if p]
        if parts and parts[0] == old_stem:
            parts[0] = new_stem
            tp_new = "/" + "/".join(parts)
            conn.execute("UPDATE chunks SET titlepath=? WHERE chunk_id=?",
                         (tp_new, cid))
            tp = tp_new
        parts = [p for p in (tp or "").split("/") if p]
        doc_title_part = parts[0] if parts else ""
        if doc_title_part == new_stem:
            doc_title_part = ""
        title_tokens = tokmod.token_string("/".join(parts[1:]))
        extra = dtitle if (dtitle and dtitle != new_stem
                           and dtitle != doc_title_part) else ""
        doc_title_tokens = tokmod.token_string(
            " ".join(x for x in (doc_title_part, extra) if x))
        name_tokens = tokmod.name_token_string(new_stem)
        conn.execute(
            "UPDATE chunks SET title_tokens=?, name_tokens=?, doc_title_tokens=?, "
            "title_len=? WHERE chunk_id=?",
            (title_tokens, name_tokens, doc_title_tokens,
             len(title_tokens.split()), cid))
    if rows:
        # 先清空 parent_id（它指向同文档内的父块 chunk_id），重算完再指回去
        conn.execute("UPDATE chunks SET parent_id=NULL WHERE doc_id=?", (doc_id,))
        for cid, new_cid in id_map.items():
            conn.execute(
                "UPDATE chunks SET chunk_id=? WHERE chunk_id=?", (new_cid, cid)
            )
        for cid, pid, _tp, _idx, _is_parent in rows:
            if pid:
                conn.execute(
                    "UPDATE chunks SET parent_id=? WHERE chunk_id=?",
                    (id_map.get(pid, pid), id_map.get(cid, cid)),
                )
    conn.commit()


def update_document_stat(conn, doc_id, mtime, size):
    """只更新文件的 stat 信息（mtime / size），不动内容与向量。

    用于「内容没变、只是 stat 变了」的情况（``touch``、编辑器保存了相同内容、
    从别处拷来内容一致的文件）：把 stat 更新到最新，下一轮扫描就能直接靠
    stat 跳过，**不用再读文件算 hash**。
    """
    conn.execute(
        "UPDATE documents SET mtime=?, size=?, updated_at=? WHERE id=?",
        (mtime, size, __import__("time").time(), doc_id),
    )
    conn.commit()


def delete_document(conn, doc_id):
    conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    conn.commit()


def list_documents(conn):
    cols = ["id", "file_path", "doc_type", "title", "mtime", "size", "content_hash",
            "index_status", "indexed_at", "error"]
    rows = conn.execute(
        "SELECT id,file_path,doc_type,title,mtime,size,content_hash,index_status,"
        "indexed_at,error FROM documents"
    ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


# ---------------------------------------------------------------- chunks


def get_chunk_state(conn, doc_id):
    """返回该文档已有 chunk 的 {chunk_id: (content_hash, vector_bytes, embed_version)}。

    ``embed_version`` 是**嵌入文本格式版本**（config.EMBED_FORMAT_VERSION）：
    向量复用的判据只有 ``content_hash``（正文），它**不包含 embed_text**——
    所以切片格式一改（embed_text 变了），必须靠版本号让旧向量失效，
    否则「改了格式等于没改」。
    """
    rows = conn.execute(
        "SELECT chunk_id, content_hash, vector, embed_version FROM chunks WHERE doc_id=?",
        (doc_id,),
    ).fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def doc_embed_version(conn, doc_id):
    """该文档已有 chunk 的嵌入格式版本（只有一个值时才返回；混着就返回 None）。

    prescreen 用它判断「能不能直接跳过」：版本变了就得重新切片重嵌，
    不能只看 mtime / size 就跳过。
    """
    rows = conn.execute(
        "SELECT DISTINCT embed_version FROM chunks WHERE doc_id=?", (doc_id,)
    ).fetchall()
    versions = {r[0] for r in rows}
    if len(versions) == 1:
        return versions.pop()
    return None


def sync_chunks(conn, doc_id, chunks, vectors):
    """同步某文档的 chunk 集合。

    chunks: Chunk 列表（含父块）；vectors: {chunk_id: 向量}（仅叶子）。
    库中多余（本次不再出现）的 chunk 会被删除。
    同时写入 jieba 预分词文本，供 FTS5 关键词召回使用（由触发器同步进索引）。
    """
    from .. import tokenize as tokmod
    keep_ids = [c.chunk_id for c in chunks]
    existing = {cid for (cid, _) in get_chunk_state(conn, doc_id).items()}

    # 文档级标题与文件名：**都要进关键词索引**，但走**不同的列**：
    #   - 文件名 -> name_tokens（bm25 权重 8.0，最高）：用户最可能记得的就是
    #     文件名，权重理应最高。之前文件名混在 title_tokens 里（权重 3.0），
    #     而且对「有标题的 md」压根没进去——titlepath 是标题树，不含文件名，
    #     于是文件名完全搜不到。
    #   - 文档标题（frontmatter 的 title / PDF 的 /Title）-> title_tokens
    #     （权重 3.0），它跟标题路径是一路的。
    doc_title, doc_path = "", ""
    row = conn.execute(
        "SELECT title, file_path FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    if row:
        doc_title = (row[0] or "").strip()
        doc_path = row[1] or ""
    file_stem = Path(doc_path).stem.strip() if doc_path else ""
    name_tokens = tokmod.name_token_string(file_stem)

    for c in chunks:
        vec = vectors.get(c.chunk_id)
        tokens = tokmod.token_string(c.text)

        # ---- 字段切分：块级 vs 文档级 ----
        # 标题路径的**第一级**是文档级标题（H1 / PDF 的文档标题 / 无标题文档的
        # 文件名），它会出现在该文档的**每一个** chunk 里 → 属于文档级字段，
        # 不能算成块级 tf（否则同一篇文档的每一块都被它抬高）。
        tp_list = list(c.title_path or [])
        doc_title_part = tp_list[0] if tp_list else ""
        sec_path = "/".join(tp_list[1:])
        title_tokens = tokmod.token_string(sec_path)          # 块级：章节路径
        if doc_title_part == file_stem:
            doc_title_part = ""      # 无标题文档：第一级就是文件名 → 归 name 管
        extra_title = ""
        if (doc_title and doc_title != file_stem
                and doc_title != doc_title_part):
            extra_title = doc_title
        doc_title_tokens = tokmod.token_string(
            " ".join(x for x in (doc_title_part, extra_title) if x))
        # 文件名：**每块都存**（召回要靠它：搜文件名时整篇的块都会被召回，
        # 文档级的 tf 由打分器按文档算一次，不会重复计数）。
        name_tokens = tokmod.name_token_string(file_stem)
        body_len = len(tokens.split())
        title_len = len(title_tokens.split())

        # locator：PDF 的「第 12 页」定位串（Markdown 没有 → None）
        locator = (c.metadata or {}).get("locator")
        conn.execute(
            "INSERT INTO chunks(chunk_id,doc_id,parent_id,is_parent,is_doc_root,"
            "title_level,titlepath,text,embed_text,vector,start_line,end_line,"
            "content_hash,chunk_index,tokens,title_tokens,name_tokens,"
            "doc_title_tokens,body_len,title_len,locator,embed_version) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(chunk_id) DO UPDATE SET "
            "doc_id=excluded.doc_id, parent_id=excluded.parent_id, "
            "is_parent=excluded.is_parent, is_doc_root=excluded.is_doc_root, "
            "title_level=excluded.title_level, titlepath=excluded.titlepath, "
            "text=excluded.text, embed_text=excluded.embed_text, vector=excluded.vector, "
            "start_line=excluded.start_line, end_line=excluded.end_line, "
            "content_hash=excluded.content_hash, chunk_index=excluded.chunk_index, "
            "tokens=excluded.tokens, title_tokens=excluded.title_tokens, "
            "name_tokens=excluded.name_tokens, "
            "doc_title_tokens=excluded.doc_title_tokens, "
            "body_len=excluded.body_len, title_len=excluded.title_len, "
            "locator=excluded.locator, embed_version=excluded.embed_version",
            (
                c.chunk_id, doc_id, c.parent_id, 1 if c.is_parent else 0,
                1 if c.is_doc_root else 0, c.title_level, c.titlepath, c.text,
                c.embed_text, _serialize(vec) if vec is not None else None,
                c.start_line, c.end_line, c.content_hash, c.chunk_index,
                tokens, title_tokens, name_tokens, doc_title_tokens,
                body_len, title_len, locator,
                config.EMBED_FORMAT_VERSION,
            ),
        )

    stale = existing - set(keep_ids)
    for cid in stale:
        conn.execute("DELETE FROM chunks WHERE chunk_id=?", (cid,))
    conn.commit()


def get_chunk(conn, chunk_id):
    row = conn.execute(
        "SELECT chunk_id,doc_id,parent_id,is_parent,is_doc_root,title_level,titlepath,"
        "text,embed_text,vector,start_line,end_line,content_hash,chunk_index "
        "FROM chunks WHERE chunk_id=?", (chunk_id,)
    ).fetchone()
    if not row:
        return None
    cols = ["chunk_id", "doc_id", "parent_id", "is_parent", "is_doc_root", "title_level",
            "titlepath", "text", "embed_text", "vector", "start_line", "end_line",
            "content_hash", "chunk_index"]
    return dict(zip(cols, row))


def get_sibling_chunks(conn, doc_id, chunk_index, span=1):
    """取同一文档中 chunk_index 前后各 span 个兄弟叶子片段（含自身）。"""
    span = max(0, int(span))
    rows = conn.execute(
        "SELECT chunk_id,titlepath,text,start_line,end_line,chunk_index,parent_id "
        "FROM chunks WHERE doc_id=? AND is_parent=0 AND chunk_index BETWEEN ? AND ? "
        "ORDER BY chunk_index",
        (doc_id, chunk_index - span, chunk_index + span),
    ).fetchall()
    cols = ["chunk_id", "titlepath", "text", "start_line", "end_line",
            "chunk_index", "parent_id"]
    return [dict(zip(cols, r)) for r in rows]


def delete_chunk(conn, chunk_id):
    """删除单个 chunk。"""
    conn.execute("DELETE FROM chunks WHERE chunk_id=?", (chunk_id,))
    conn.commit()


def delete_chunks_of_document(conn, doc_id):
    """删除某文档的全部 chunk（保留文档记录，标记为待重新索引）。"""
    conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    conn.execute("UPDATE documents SET index_status='pending' WHERE id=?", (doc_id,))
    conn.commit()


def clear_index(conn, drop_documents=True):
    """清空索引缓存（向量 + 关键词索引全清）。

    drop_documents=True：删除全部文档与 chunk（完全重置）；
    False：只删除全部 chunk（保留文件清单，便于只重新嵌入）。

    关键词索引（FTS5/BM25）一并清空：删掉虚表连同影子表后重建，
    既消除残留的倒排 posting，也回收磁盘空间；触发器会重建好，后续索引照常同步。
    """
    conn.execute("DELETE FROM chunks")
    if drop_documents:
        conn.execute("DELETE FROM documents")
    else:
        conn.execute("UPDATE documents SET index_status='pending'")
    conn.commit()
    _reset_fts(conn)


def _reset_fts(conn):
    """清空并重建空的 FTS5 索引（虚表 + 影子表 + 触发器）。"""
    _drop_fts(conn)
    conn.executescript(FTS_SCHEMA)
    conn.commit()


def count_chunks(conn):
    total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    leaves = conn.execute("SELECT COUNT(*) FROM chunks WHERE is_parent=0").fetchone()[0]
    parents = conn.execute("SELECT COUNT(*) FROM chunks WHERE is_parent=1").fetchone()[0]
    return {"chunks": total, "leaves": leaves, "parents": parents}


def count_keywords(conn):
    """关键词索引（FTS5/BM25）中的条目数。"""
    try:
        return conn.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
    except sqlite3.Error:
        return 0


# ---------------------------------------------------------------- 检索


_CHUNK_COLS = ("c.chunk_id, c.doc_id, c.parent_id, c.title_level, c.titlepath, "
               "c.text, c.start_line, c.end_line, c.chunk_index, c.locator, "
               "d.file_path, d.title, d.mtime, d.source_kind")


def _filter_clause(filters):
    """把过滤条件编译成 (where_sql, params)。"""
    filters = filters or {}
    wheres, params = [], []
    if filters.get("path_prefix"):
        wheres.append("d.file_path LIKE ?")
        params.append(filters["path_prefix"] + "%")
    if filters.get("title"):
        wheres.append("d.title LIKE ?")
        params.append("%" + filters["title"] + "%")
    if filters.get("doc_type"):
        wheres.append("d.doc_type = ?")
        params.append(filters["doc_type"])
    return " AND ".join(wheres), params


def _page_of(locator):
    """从定位串里取出**起始页码**（1-based）；Markdown 之类没有页概念的返回 None。

    为什么不在 chunks 表里再存一列页码：那份数据已经在 locator 里了
    （PDF 形如「第 12 页」/「第 12-13 页」），加列就得重建索引才能填上——
    为了一个能推导出来的数字重建整库不划算。解析放在这一处，
    格式变了只改这里一处，前端拿到的始终是数值。

    前端要用它「跳到第 N 页」：给用户一个可点的页码，比给一句
    「第 12 页」的中文串好用得多。
    """
    if not locator:
        return None
    m = re.search(r"(\d+)", locator)
    return int(m.group(1)) if m else None


def _make_result(row):
    """把一行 chunk + document 记录组装成统一的结果结构。"""
    (cid, doc_id, parent_id, title_level, titlepath, text, start_line, end_line,
     chunk_index, locator, file_path, title, mtime, source_kind) = row
    return {
        "chunk_id": cid, "doc_id": doc_id, "parent_id": parent_id,
        "title_level": title_level, "titlepath": titlepath,
        "title_path": [p for p in titlepath.split("/") if p],
        "text": text, "content": text,
        "start_line": start_line, "end_line": end_line,
        "chunk_index": chunk_index, "locator": locator,
        # PDF 的起始页码（1-based）；Markdown 为 None
        "page": _page_of(locator),
        "path": file_path, "title": title,
        "mtime": mtime, "source_kind": source_kind,
    }


def search_similar(conn, query_vec, top_k=10, filters=None):
    """余弦相似度检索：只检索叶子（is_parent=0），父块 vector 为 NULL 不参与。

    top_k=None 时返回全部候选（按分数降序），供混合检索做重排使用。
    """
    sql = (f"SELECT {_CHUNK_COLS}, c.vector "
           "FROM chunks c JOIN documents d ON c.doc_id = d.id "
           "WHERE c.is_parent = 0")
    where, params = _filter_clause(filters)
    if where:
        sql += " AND " + where

    rows = conn.execute(sql, params).fetchall()
    np = require_numpy()

    q = np.asarray(query_vec, dtype=np.float32)
    dim = int(q.shape[0])

    # 批量算分：一次 np.frombuffer + 一次矩阵乘，替代逐行 np.dot
    blobs = [r[-1] for r in rows]
    ok = [i for i, b in enumerate(blobs) if b is not None and len(b) == dim * 4]
    scores = {}
    if ok:
        mat = np.frombuffer(b"".join(blobs[i] for i in ok),
                            dtype=np.float32).reshape(len(ok), dim)
        for i, s in zip(ok, mat @ q):
            scores[i] = float(s)

    results = []
    for i, r in enumerate(rows):
        if i not in scores:
            continue
        res = _make_result(r[:-1])
        res["score"] = scores[i]
        results.append(res)
    results.sort(key=lambda x: x["score"], reverse=True)
    return results if top_k is None else results[:top_k]


def keyword_candidates(conn, expr, limit=400, filters=None):
    """关键词**召回**：FTS5 MATCH 找出「谁含这些词」。

    这里**不负责排序**——排序由 ``search/rank.py`` 自己算 BM25F
    （FTS5 的 bm25() 用的是 chunk 级的 df，文档级字段会被它算废）。
    FTS5 的 bm25 只用来给召回结果排个序，好截断候选。

    返回：结果字典 + 以 ``_`` 开头的内部字段（各字段的 token 串与长度）。
    """
    sql = (f"SELECT {_CHUNK_COLS}, c.tokens, c.title_tokens, c.name_tokens, "
           "c.doc_title_tokens, c.body_len, c.title_len, bm25(chunks_fts) AS bm "
           "FROM chunks_fts "
           "JOIN chunks c ON c.rowid = chunks_fts.rowid "
           "JOIN documents d ON c.doc_id = d.id "
           "WHERE chunks_fts MATCH ? AND c.is_parent = 0")
    params = [expr]
    where, fparams = _filter_clause(filters)
    if where:
        sql += " AND " + where
    sql += " ORDER BY bm LIMIT ?"
    params += fparams + [max(1, int(limit))]

    out = []
    for r in conn.execute(sql, params).fetchall():
        res = _make_result(r[:-7])
        res.update({
            "_tokens": r[-7], "_title_tokens": r[-6], "_name_tokens": r[-5],
            "_doc_title_tokens": r[-4], "_body_len": r[-3] or 0,
            "_title_len": r[-2] or 0,
        })
        out.append(res)
    return out


def count_indexed_docs(conn):
    """有内容的文档数（BM25 的 N）。"""
    return conn.execute(
        "SELECT COUNT(DISTINCT doc_id) FROM chunks WHERE is_parent=0").fetchone()[0]


def keyword_doc_df(conn, term):
    """一个词出现在几个**文档**里（BM25 的 df，文档级）。

    这正是 FTS5 算错的地方：它的 df 是**按 chunk 行**算的，
    文档级字段（文件名/文档标题）写进每个 chunk 会把 df 撑大 → idf 变负 → 信号消失。
    """
    expr = '"' + str(term).replace('"', '""') + '"'
    return conn.execute(
        "SELECT COUNT(DISTINCT c.doc_id) FROM chunks_fts "
        "JOIN chunks c ON c.rowid = chunks_fts.rowid "
        "WHERE chunks_fts MATCH ? AND c.is_parent = 0", (expr,)
    ).fetchone()[0]


def keyword_avg_lengths(conn):
    """正文 / 章节路径的平均词数（长度归一用）。"""
    row = conn.execute(
        "SELECT AVG(body_len), AVG(title_len) FROM chunks WHERE is_parent=0"
    ).fetchone()
    return (row[0] or 0.0), (row[1] or 0.0)


def search_keyword(conn, query, top_k=50, filters=None):
    """关键词检索：FTS5 召回 + 自己算 BM25F（见 ``search/rank.py``）。

    score 越大越相关（字段权重已生效：正文 1 / 章节路径 3 / 文件名 8 / 文档标题 3）。
    """
    from ..rank import keyword_search

    return keyword_search(conn, query, top_k=top_k, filters=filters)


def get_parent_chunk(conn, parent_id):
    """按 parent_id 回溯父块（章节级上下文）。"""
    if not parent_id:
        return None
    row = conn.execute(
        "SELECT chunk_id,parent_id,title_level,titlepath,text,start_line,end_line "
        "FROM chunks WHERE chunk_id=? AND is_parent=1", (parent_id,)
    ).fetchone()
    if not row:
        return None
    cols = ["chunk_id", "parent_id", "title_level", "titlepath", "text",
            "start_line", "end_line"]
    return dict(zip(cols, row))
