"""search 包的回归脚本（直接 `python keeper/tests/test_search.py` 跑）。

覆盖迁移后最容易坏的四件事：

1. **切片**：标题树 -> 块级元素 -> 叶子 chunk -> 父子链父块，titlepath / 行号对不对。
2. **存储 + 关键词检索**：SQLite 建表、FTS5 外部内容表与触发器、中文能搜到。
3. **端到端**：index_roots -> search（keyword 模式，不需要 embedding 模型）。
4. **配置**：`default_roots()` 的「显式配置 > 我的空间」优先级。

向量召回（mode="vector"）**不在这里测**：它需要 litert-lm + 几百 MB 的模型文件，
不该让常规回归依赖它。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# tests/ -> keeper/ -> 项目根（keeper 包的父目录）
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from keeper.search import config  # noqa: E402
from keeper.search.ingest import chunk as chunkmod  # noqa: E402
from keeper.search.store import db as dbmod  # noqa: E402
from keeper.search.tokenize import match_expression, token_string  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}{('  -> ' + extra) if extra else ''}")


MD = """---
title: 缓存设计
---

# 缓存设计

本文讨论缓存穿透、击穿、雪崩三类问题的成因与对策。

## 缓存穿透

查询一个数据库里根本不存在的键。每次请求都落到数据库，数据库被打垮。

### 布隆过滤器

布隆过滤器是一种概率型数据结构，用位数组加哈希函数判断「一定不存在」
或「可能存在」。它有假阳性但没有假阴性，适合挡在数据库前面做第一道拦截。

## 缓存击穿

热点键突然过期，大量请求同时打到数据库。对策是互斥锁重建或逻辑过期。
"""


class _CountEmbedder:
    """假 embedder：记录 embed_documents 被调用的片段数，用来数「真正重嵌了几块」。"""

    def __init__(self):
        self.calls = 0
        self.embedded = 0
        self.dimension = 4

    def embed_documents(self, texts, batch_size=32):
        import numpy as np

        self.calls += 1
        self.embedded += len(texts)
        return [np.zeros(4, dtype=np.float32) for _ in texts]


def _write_minimal_pdf(path, pages_text):
    """造一份最小可解析的 PDF（每页一段文本）。

    手写 PDF 而不是用 pypdf 造：pypdf 不方便往页面里写文本层。
    文本用 ASCII —— 中文需要 CID 字体 + ToUnicode 映射，测试里没必要折腾
    （真实的中文 PDF 里 pypdf 提取中文是没问题的，用户空间里的实测已验证）。
    """
    objs = []
    n = len(pages_text)
    kids = " ".join(f"{4 + i * 2} 0 R" for i in range(n))
    objs.append((1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    objs.append((2, f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode()))
    objs.append((3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))
    for i, text in enumerate(pages_text):
        pnum = 4 + i * 2
        cnum = pnum + 1
        objs.append((pnum, (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {cnum} 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
        ).encode()))
        safe = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 18 Tf 72 700 Td ({safe}) Tj ET".encode()
        objs.append((cnum, b"<< /Length " + str(len(stream)).encode()
                     + b" >>\nstream\n" + stream + b"\nendstream"))

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num, body in objs:
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    max_num = max(offsets)
    out += f"xref\n0 {max_num + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for num in range(1, max_num + 1):
        out += f"{offsets.get(num, 0):010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {max_num + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode()
    Path(path).write_bytes(bytes(out))


def main() -> int:
    print("\n=== 切片 ===")
    chunks = chunkmod.chunk_markdown(
        MD, file_path="/tmp/cache.md", doc_type="user_document",
        min_chars=50, max_chars=400, parent_max_chars=2000,
    )
    leaves = [c for c in chunks if not c.is_parent]
    parents = [c for c in chunks if c.is_parent]
    check("切出叶子 chunk", len(leaves) >= 2, str(len(leaves)))
    check("切出父块（H2 建父块）", len(parents) >= 1, str(len(parents)))
    check(
        "叶子 titlepath 形如 /标题/子标题",
        any(c.titlepath.startswith("/缓存设计/") for c in leaves),
        " | ".join(c.titlepath for c in leaves[:4]),
    )
    check(
        "父块 titlepath 到 H2 为止",
        all(c.title_level >= 2 for c in parents),
        " | ".join(f"{c.titlepath}(L{c.title_level})" for c in parents),
    )
    check(
        "叶子有 embed_text（带任务前缀）",
        all(c.embed_text and c.embed_text.startswith("task: search result | text: ")
            for c in leaves),
    )
    check(
        "父块不带 embed_text（不参与向量检索）",
        all(c.embed_text is None for c in parents),
    )
    check("叶子指向父块（父子链）", any(c.parent_id for c in leaves),
          " | ".join(f"{c.titlepath}->{(c.parent_id or '')[:8]}" for c in leaves))
    check(
        "行号在文档范围内",
        all(1 <= c.start_line <= c.end_line <= len(MD.split("\n")) for c in leaves),
    )
    check("正文没被切碎（关键词在某个叶子里）", any("布隆过滤器" in c.text for c in leaves),
          " | ".join(c.text[:20] for c in leaves))

    print("\n=== 分词 ===")
    check("中文切出词", "缓存" in token_string("缓存穿透"), token_string("缓存穿透"))
    check("英文小写化", "hello" in token_string("Hello World"), token_string("Hello World"))
    check("MATCH 表达式带引号", match_expression("缓存 穿透").startswith('"'),
          match_expression("缓存 穿透"))
    check("纯标点 -> 空表达式", match_expression("!!! ???") == "", match_expression("!!! ???"))

    print("\n=== 存储 + 关键词检索 ===")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        conn = dbmod.connect(tmp / "t.db")
        doc_id = dbmod.upsert_document(
            conn, "/tmp/cache.md", "user_document", "缓存设计", 0, 100, "h1"
        )
        dbmod.sync_chunks(conn, doc_id, chunks, {})
        check("文档入库", dbmod.get_document_by_path(conn, "/tmp/cache.md") is not None)
        counts = dbmod.count_chunks(conn)
        check("片段数对得上", counts["chunks"] == len(chunks), str(counts))
        check("叶子/父块分开计数", counts["leaves"] == len(leaves), str(counts))
        check("关键词索引有条目", dbmod.count_keywords(conn) == len(chunks),
              str(dbmod.count_keywords(conn)))

        hits = dbmod.search_keyword(conn, "缓存穿透", top_k=5)
        check("中文关键词命中", len(hits) > 0, str(len(hits)))
        if hits:
            check("命中带标题路径", bool(hits[0]["titlepath"]), hits[0]["titlepath"])
            check("命中带文件路径", hits[0]["path"] == "/tmp/cache.md", hits[0]["path"])
            check("分数越大越相关", hits[0]["score"] > 0, str(hits[0]["score"]))

        print("\n=== 老库自动迁移（FTS 列数变了）===")
        # 新增 name_tokens 列后，FTS5 虚表的列数变了——FTS5 不能 ALTER，
        # 只能删了重建（**不需要重新嵌入向量**）。这里造一个"两列 FTS"的老库验证。
        import sqlite3  # noqa: E402
        legacy = tmp / "legacy.db"
        lc = sqlite3.connect(str(legacy))
        lc.executescript(
            """
            CREATE TABLE documents (id INTEGER PRIMARY KEY AUTOINCREMENT,
              file_path TEXT NOT NULL UNIQUE, doc_type TEXT NOT NULL, title TEXT,
              mtime REAL NOT NULL, size INTEGER NOT NULL, content_hash TEXT NOT NULL,
              index_status TEXT NOT NULL DEFAULT 'pending', indexed_at REAL, error TEXT,
              page_count INTEGER, source_kind TEXT,
              created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, doc_id INTEGER NOT NULL,
              parent_id TEXT, is_parent INTEGER DEFAULT 0, is_doc_root INTEGER DEFAULT 0,
              title_level INTEGER DEFAULT 0, titlepath TEXT, text TEXT, embed_text TEXT,
              vector BLOB, start_line INTEGER, end_line INTEGER, content_hash TEXT,
              chunk_index INTEGER, tokens TEXT, title_tokens TEXT, locator TEXT);
            CREATE VIRTUAL TABLE chunks_fts USING fts5(tokens, title_tokens,
              content='chunks', content_rowid='rowid', tokenize='unicode61');
            CREATE TRIGGER chunks_fts_ai AFTER INSERT ON chunks BEGIN
              INSERT INTO chunks_fts(rowid, tokens, title_tokens)
              VALUES (new.rowid, new.tokens, new.title_tokens); END;
            CREATE TRIGGER chunks_fts_ad AFTER DELETE ON chunks BEGIN
              INSERT INTO chunks_fts(chunks_fts, rowid, tokens, title_tokens)
              VALUES ('delete', old.rowid, old.tokens, old.title_tokens); END;
            CREATE TRIGGER chunks_fts_au AFTER UPDATE ON chunks BEGIN
              INSERT INTO chunks_fts(chunks_fts, rowid, tokens, title_tokens)
              VALUES ('delete', old.rowid, old.tokens, old.title_tokens);
              INSERT INTO chunks_fts(rowid, tokens, title_tokens)
              VALUES (new.rowid, new.tokens, new.title_tokens); END;
            INSERT INTO documents(file_path,doc_type,title,mtime,size,content_hash,
              index_status,created_at,updated_at)
              VALUES ('/tmp/季度总结.md','user_document','季度总结',1.0,100,'h',
                      'done',1.0,1.0);
            INSERT INTO chunks(chunk_id,doc_id,is_parent,title_level,titlepath,text,
              content_hash,chunk_index,tokens,title_tokens)
              VALUES ('c1',1,0,1,'/第一节','正文内容','x',0,'正文 内容','第一节');
            """
        )
        lc.commit()
        lc.close()
        lconn = dbmod.connect(legacy)
        lcols = [r[1] for r in lconn.execute("PRAGMA table_info(chunks_fts)")]
        check("老库：FTS 自动升到四列",
              lcols == ["tokens", "title_tokens", "name_tokens", "doc_title_tokens"],
              str(lcols))
        lrow = lconn.execute("SELECT name_tokens FROM chunks").fetchone()
        check("老库：name_tokens 自动回填",
              bool(lrow) and "季度总结" in lrow[0], str(lrow))
        lhits = dbmod.search_keyword(lconn, "季度总结", top_k=5)
        check("老库：按文件名能命中（不用重新嵌入）", len(lhits) > 0, str(lhits)[:120])
        lconn.close()

        print("\n=== 端到端（keyword 模式，不需要模型）===")
        docs = tmp / "docs"
        (docs / "sub").mkdir(parents=True)
        (docs / "a.md").write_text(MD, encoding="utf-8")
        (docs / "sub" / "b.md").write_text(
            "# 消息队列\n\nKafka 与 RabbitMQ 的选型对比。", encoding="utf-8"
        )
        from keeper.search.indexer import index_roots  # noqa: E402
        from keeper.search.query import search as qsearch  # noqa: E402

        stats = index_roots(conn, None, roots=[docs])
        check("索引到 2 个文件", stats["indexed"] == 2, str(stats))
        # embedder 传 None：keyword 通道若错误地去碰它，会立刻炸出来
        res = qsearch(conn, None, "消息队列", top_k=3, mode="keyword")
        check("端到端关键词命中", len(res) > 0, str(len(res)))
        check("命中的是 b.md", any("b.md" in r["path"] for r in res),
              " | ".join(r["path"] for r in res))

        print("\n=== 增量 ===")
        stats2 = index_roots(conn, None, roots=[docs])
        check("重复索引 -> 全部跳过", stats2["skipped"] == 2, str(stats2))
        (docs / "a.md").write_text(
            MD + "\n\n## 新增一节\n\n这是新加的内容。", encoding="utf-8"
        )
        stats3 = index_roots(conn, None, roots=[docs])
        check("内容变了 -> 重新索引 1 个", stats3["indexed"] == 1, str(stats3))

        print("\n=== 父块回溯与兄弟片段 ===")
        doc = dbmod.get_document_by_path(conn, str(docs / "a.md"))
        sib = dbmod.get_sibling_chunks(conn, doc["id"], 0, span=1)
        check("兄弟片段可取", len(sib) >= 1, str(len(sib)))
        parent = dbmod.get_parent_chunk(conn, chunks[0].parent_id)
        check("父块可回溯", parent is None or isinstance(parent, dict))
        conn.close()

    print("\n=== force 强制重建（向量必须真重嵌）===")
    # force=True 的语义是「忽略内容哈希，强制重新切+重新嵌」。
    # 曾经的 bug：向量复用分支不看 force，于是 force=True 只会重切、不会重嵌，
    # 实测重嵌 0 块 —— 前端「强制重建索引」按钮形同虚设。
    try:
        import numpy as np
        has_np = True
    except ImportError:
        has_np = False

    if not has_np:
        check("force 强制重建（向量必须真重嵌）", True, "（没装 numpy，跳过）")
    else:
        from keeper.search.indexer import index_roots as ir

        with tempfile.TemporaryDirectory() as td:
            tmp3 = Path(td)
            conn3 = dbmod.connect(tmp3 / "f.db")
            docs3 = tmp3 / "docs"
            docs3.mkdir()
            (docs3 / "a.md").write_text(MD, encoding="utf-8")

            emb = _CountEmbedder()
            s1 = ir(conn3, emb, roots=[docs3])
            check("首次索引：已向量化", emb.embedded > 0, f"{s1} embedded={emb.embedded}")
            first_embedded = emb.embedded

            emb2 = _CountEmbedder()
            s2 = ir(conn3, emb2, roots=[docs3])
            check("未改动 → 全部跳过，且不再嵌入",
                  s2["skipped"] == 1 and emb2.embedded == 0,
                  f"{s2} embedded={emb2.embedded}")

            emb3 = _CountEmbedder()
            s3 = ir(conn3, emb3, roots=[docs3], force=True)
            check("force=True → 重新索引该文件",
                  s3["indexed"] == 1, str(s3))
            check("force=True → 向量全部重嵌（不是复用旧向量）",
                  emb3.embedded == first_embedded,
                  f"force 嵌了 {emb3.embedded} 块，首次 {first_embedded} 块")

            # force=True 时不能先被判成「文件移动」而跳过重建
            (docs3 / "a.md").rename(docs3 / "b.md")
            emb4 = _CountEmbedder()
            s4 = ir(conn3, emb4, roots=[docs3], force=True)
            check("force=True → 移动过的文件也走重建，不判 moved",
                  s4.get("moved", 0) == 0 and emb4.embedded > 0,
                  f"{s4} embedded={emb4.embedded}")
            conn3.close()

    print("\n=== 文件移动：chunk_id 同步 ===")
    # chunk_id 由「路径 + 标题路径 + 序号」派生，路径一变就得跟着变。
    # 曾经的 bug：move_document 只改 documents.file_path，chunk_id 还是旧路径派生的，
    # 于是文件一移动、下次一改内容，切出来的新 id 全对不上 → 旧向量作废、整篇重嵌。
    if not has_np:
        check("移动后 chunk_id 同步", True, "（没装 numpy，跳过）")
    else:
        from keeper.search.indexer import index_roots as ir
        from keeper.search.ingest.chunk import leaf_chunk_id, parent_chunk_id

        with tempfile.TemporaryDirectory() as td:
            tmp4 = Path(td)
            conn4 = dbmod.connect(tmp4 / "mv.db")
            docs4 = tmp4 / "docs"
            docs4.mkdir()
            (docs4 / "a.md").write_text(MD, encoding="utf-8")

            emb = _CountEmbedder()
            ir(conn4, emb, roots=[docs4])
            leaves_total = dbmod.count_chunks(conn4)["leaves"]
            check("首次索引有叶子块", leaves_total > 0, str(leaves_total))

            # 移动文件（内容不变）
            (docs4 / "a.md").rename(docs4 / "b.md")
            emb2 = _CountEmbedder()
            s2 = ir(conn4, emb2, roots=[docs4])
            check("移动 → 判为 moved，且不重新嵌入",
                  s2.get("moved", 0) == 1 and emb2.embedded == 0,
                  f"{s2} embedded={emb2.embedded}")

            # 移动后：库里的 chunk_id 必须已经按新路径重算
            new_path = str(docs4 / "b.md")
            doc4 = dbmod.get_document_by_path(conn4, new_path)
            crows = conn4.execute(
                "SELECT chunk_id, parent_id, titlepath, chunk_index, is_parent "
                "FROM chunks WHERE doc_id=?", (doc4["id"],)).fetchall()
            ok_ids = True
            ok_parents = True
            known = {r[0] for r in crows}
            for cid, pid, tp, idx, is_parent in crows:
                want = (parent_chunk_id(new_path, tp) if is_parent
                        else leaf_chunk_id(new_path, tp, idx))
                if want != cid:
                    ok_ids = False
                if pid and pid not in known:
                    ok_parents = False
            check("移动后 chunk_id 已按新路径重算", ok_ids,
                  f"{len(crows)} 块，首个 {crows[0][0][:8] if crows else '-'}")
            check("移动后 parent_id 指向仍有效", ok_parents)

            # 移动后再改内容 → 只重嵌变化的块，而不是整篇
            (docs4 / "b.md").write_text(
                MD + "\n\n## 附录\n\n新增一节内容，用来验证增量嵌入。\n",
                encoding="utf-8",
            )
            emb3 = _CountEmbedder()
            ir(conn4, emb3, roots=[docs4])
            leaves_after = dbmod.count_chunks(conn4)["leaves"]
            check("移动后修改 → 只重嵌变化的块，不是整篇",
                  0 < emb3.embedded < leaves_after,
                  f"重嵌 {emb3.embedded} 块 / 共 {leaves_after} 块")
            conn4.close()

    print("\n=== 后端探测与回退 ===")
    from keeper.search.ingest import embed as embedmod

    ok, reason = embedmod.gpu_probe()
    check(
        "gpu_probe 返回 (bool, 说明)",
        isinstance(ok, bool) and bool(reason),
        f"{ok} {reason}",
    )

    if embedmod.Backend is None:
        check("没装 litert-lm → 后端为 None", embedmod._resolve_backend() is None)
    else:
        old_cfg = config.EMBED_BACKEND
        try:
            config.EMBED_BACKEND = "cpu"
            b = embedmod._resolve_backend()
            check("显式 cpu → CPU 后端", isinstance(b, embedmod.Backend.CPU), repr(b))
            config.EMBED_BACKEND = "gpu"
            b = embedmod._resolve_backend()
            check("显式 gpu → GPU 后端", isinstance(b, embedmod.Backend.GPU), repr(b))
            config.EMBED_BACKEND = "auto"
            b = embedmod._resolve_backend()
            want_gpu = embedmod.gpu_probe()[0]
            check(
                "auto → 按探测结果选后端",
                isinstance(b, embedmod.Backend.GPU) == want_gpu,
                repr(b),
            )
        finally:
            config.EMBED_BACKEND = old_cfg

        # GPU 加载失败 → 必须回退 CPU。用「GPU 报 A、CPU 报 B」的假引擎来区分：
        # 最终抛出的必须是 CPU 那条错误，说明回退真的发生了。
        class _FailEngine:
            def __init__(self, path, backend=None, **kw):
                if isinstance(backend, embedmod.Backend.GPU):
                    raise RuntimeError("gpu-boom")
                raise RuntimeError("cpu-boom")

        old_engine = embedmod.EmbeddingEngine
        embedmod.EmbeddingEngine = _FailEngine
        try:
            em = embedmod.Embedder()
            err = ""
            try:
                em.engine
            except RuntimeError as e:
                err = str(e)
            check("GPU 加载失败 → 回退 CPU（错误来自 CPU 侧）", err == "cpu-boom", err)
            check("回退原因记下来了", "CPU" in em.backend_note, em.backend_note)
        finally:
            embedmod.EmbeddingEngine = old_engine

    print("\n=== 启动预热 ===")
    from keeper.search import warmup as warmmod

    with tempfile.TemporaryDirectory() as td:
        tmp2 = Path(td)
        # 没向量 → 不该预热
        ok, why = warmmod.should_warmup(
            db_path=tmp2 / "empty.db", model_path=tmp2 / "m.litertlm"
        )
        check("库不存在 → 不预热", ok is False, why)
        # 有向量但模型不在 → 不该预热
        conn2 = dbmod.connect(tmp2 / "v.db")
        doc2 = dbmod.upsert_document(conn2, "/tmp/y.md", "user_document", "T", 0, 9, "h")
        ch2 = chunkmod.Chunk(chunk_id="c9", text="有向量的片段", embed_text="task: search result | text: 有向量的片段",
                             start_line=1, end_line=2, content_hash="hh", title_path=["T"], chunk_index=0)
        # 带向量的片段：sync_chunks 的 vectors 参数非空才会写 vector 列。
        # 写向量要 numpy（延迟导入）——没装 numpy 的环境跳过这组断言。
        try:
            import numpy as np

            has_np = True
        except ImportError:
            has_np = False
        if has_np:
            dbmod.sync_chunks(conn2, doc2, [ch2], {"c9": np.zeros(4, dtype=np.float32)})
            has_vec = warmmod._indexed_has_vectors(tmp2 / "v.db")
            check("索引库里有向量时探测得到", has_vec, str(has_vec))
        else:
            check("索引库里有向量时探测得到", True, "（没装 numpy，跳过）")
        ok, why = warmmod.should_warmup(
            db_path=tmp2 / "v.db", model_path=tmp2 / "missing.litertlm"
        )
        check("模型不在 → 不预热", ok is False, why)
        (tmp2 / "m.litertlm").write_bytes(b"x")
        ok, why = warmmod.should_warmup(
            db_path=tmp2 / "v.db", model_path=tmp2 / "m.litertlm"
        )
        check("有向量 + 有模型 → 该预热", ok is True, why)
        conn2.close()

    print("\n=== 无 numpy 也能关键词检索 ===")
    # numpy 是**向量路径专用**的依赖，关键词通道（SQLite FTS5 + BM25）不该被它卡住。
    # 子进程里把 numpy 拦掉（sys.modules["numpy"] = None → import 立刻失败），
    # 证明顶层不 import numpy、关键词路径也不需要它。
    import subprocess

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    snippet = (
        "import sys, tempfile\n"
        "from pathlib import Path\n"
        "sys.modules['numpy'] = None\n"
        f"sys.path.insert(0, {root!r})\n"
        "from keeper.search.store import db as dbmod\n"
        "from keeper.search.ingest.chunk import Chunk\n"
        "conn = dbmod.connect(Path(tempfile.mkdtemp()) / 't.db')\n"
        "doc = dbmod.upsert_document(conn, '/tmp/x.md', 'user_document', 'T', 0, 10, 'h')\n"
        "ch = Chunk(chunk_id='c1', text='缓存穿透怎么办',\n"
        "           embed_text='task: search result | text: 缓存穿透怎么办',\n"
        "           start_line=1, end_line=2, content_hash='h1',\n"
        "           title_path=['T'], chunk_index=0)\n"
        "dbmod.sync_chunks(conn, doc, [ch], {})\n"
        "hits = dbmod.search_keyword(conn, '缓存穿透', top_k=3)\n"
        "print('HITS', len(hits))\n"
    )
    r = subprocess.run([sys.executable, "-c", snippet], capture_output=True, text=True)
    check(
        "没装 numpy 时关键词检索照常可用",
        "HITS 1" in r.stdout,
        (r.stdout + r.stderr)[-220:],
    )

    print("\n=== stat 预筛（扫描不读文件）===")
    # 预筛是「自动同步」能跑得便宜的关键：stat 没变过的文件，连打开都不打开。
    # 这里用「数 hash 调用次数」来证明它真的没读文件。
    from keeper.search.ingest import detect as detectmod
    from keeper.search.indexer import index_roots as ir2

    orig_hash = detectmod.compute_hash
    hash_calls = []

    def _counting_hash(p):
        hash_calls.append(str(p))
        return orig_hash(p)

    with tempfile.TemporaryDirectory() as td:
        tmp5 = Path(td)
        conn5 = dbmod.connect(tmp5 / "ps.db")
        docs5 = tmp5 / "docs"
        docs5.mkdir()
        f5 = docs5 / "a.md"
        f5.write_text(MD, encoding="utf-8")

        detectmod.compute_hash = _counting_hash
        try:
            s = ir2(conn5, None, roots=[docs5], prescreen=True)
            check("首次索引：文件入库", s["indexed"] == 1, str(s))
            check("首次索引读了文件（算 hash）", len(hash_calls) == 1, str(hash_calls))

            hash_calls.clear()
            s2 = ir2(conn5, None, roots=[docs5], prescreen=True)
            check("第二次预筛扫描：全部跳过", s2["skipped"] == 1, str(s2))
            check("预筛扫描没读任何文件（hash 调用 0 次）",
                  len(hash_calls) == 0, str(hash_calls))

            # 内容没变、只改了 mtime（touch / 保存了相同内容）
            os.utime(f5, (f5.stat().st_atime, f5.stat().st_mtime + 10))
            hash_calls.clear()
            s3 = ir2(conn5, None, roots=[docs5], prescreen=True)
            check("mtime 变了但内容没变 → 仍然跳过", s3["skipped"] == 1, str(s3))
            check("mtime 变了要读一次文件确认内容", len(hash_calls) == 1, str(hash_calls))

            hash_calls.clear()
            s4 = ir2(conn5, None, roots=[docs5], prescreen=True)
            check("stat 已更新到最新 → 下一轮预筛直接跳过（不读文件）",
                  s4["skipped"] == 1 and len(hash_calls) == 0,
                  f"{s4} hash_calls={hash_calls}")

            # 内容真的变了 → 必须重新索引
            f5.write_text(MD + "\n\n## 尾声\n\n改过了。\n", encoding="utf-8")
            s5 = ir2(conn5, None, roots=[docs5], prescreen=True)
            check("内容真变了 → 重新索引", s5["indexed"] == 1, str(s5))
        finally:
            detectmod.compute_hash = orig_hash
            conn5.close()

    print("\n=== 按路径索引（文件监听用）===")
    from keeper.search.indexer import index_paths

    with tempfile.TemporaryDirectory() as td:
        tmp6 = Path(td)
        conn6 = dbmod.connect(tmp6 / "ip.db")
        docs6 = tmp6 / "docs"
        docs6.mkdir()
        f6 = docs6 / "a.md"
        f6.write_text(MD, encoding="utf-8")
        s6 = index_paths(conn6, None, [str(f6)])
        check("按路径索引：入库", s6["indexed"] == 1, str(s6))

        s6b = index_paths(conn6, None, [str(f6)])
        check("按路径索引：没变 → 跳过", s6b["skipped"] == 1, str(s6b))

        # 非 markdown 不该被索引
        other = docs6 / "x.txt"
        other.write_text("不是 markdown", encoding="utf-8")
        s6c = index_paths(conn6, None, [str(other)])
        check("非 markdown 文件被忽略", s6c.get("indexed", 0) == 0, str(s6c))

        # 文件被删了 → 索引也要删
        f6.unlink()
        s6d = index_paths(conn6, None, [str(f6)])
        check("文件被删 → 索引同步删除", s6d["deleted"] == 1, str(s6d))
        check("删除后库里没有这份文档",
              dbmod.get_document_by_path(conn6, str(f6)) is None)

        # 重复通知要抑制（一次保存会连发好几个事件；内核自己读文件改 atime
        # 也会再触发一次 —— 不抑制就会自激成死循环）
        # 内容要跟别的文件不同：内容相同会被判定成「文件移动」而不是新建
        g6 = docs6 / "g.md"
        g6.write_text(MD + "\n\n## 独一无二的一节\n\n这一节只出现在 g.md 里。\n",
                      encoding="utf-8")
        s6e = index_paths(conn6, None, [str(g6)])
        check("新文件走完整索引", s6e["indexed"] == 1, str(s6e))
        s6f = index_paths(conn6, None, [str(g6)])
        check("几秒内的重复通知被抑制（不重复处理）",
              s6f["skipped"] == 1 and s6f["indexed"] == 0, str(s6f))
        conn6.close()

    print("\n=== 索引互斥（定时扫描 / 手动 / 监听 三选一）===")
    import threading
    import time as _time

    from keeper.search import sync as syncmod

    gate = threading.Event()
    done = []

    def _slow():
        gate.wait(3)
        done.append(1)
        return "slow-done"

    th = threading.Thread(target=lambda: done.append(
        syncmod.try_run_index(_slow)))
    th.start()
    _time.sleep(0.15)
    check("有索引任务在跑时，后来者被跳过（不排队）",
          syncmod.try_run_index(lambda: "x") is None)
    check("索引状态能看到正在跑", syncmod.index_running() is True)
    gate.set()
    th.join(3)
    check("前面的任务正常完成", "slow-done" in done, str(done))
    check("任务结束后锁释放", syncmod.index_running() is False)

    print("\n=== 索引状态与同步状态 ===")
    # 回归：_set_index_state 曾经引用了一个已被删掉的锁（_embedder_lock），
    # 结果任何索引任务一启动就抛 NameError——手动「建立索引」按钮整个是坏的，
    # 而且失败发生在 try 之外，连状态都写不进去，看起来像"点了没反应"。
    from keeper.search import api as apimod
    from keeper.search import sync as syncmod2

    apimod._set_index_state(running=True, error="")
    check("写索引状态不报错", apimod._index_status().get("running") is True)
    apimod._set_index_state(running=False, stats={"indexed": 1}, error="")
    check("索引状态可读回", apimod._index_status().get("stats") == {"indexed": 1})

    s = syncmod2.status()
    check("同步状态可读", "interval" in s and "scans" in s, str(s))
    check("同步默认启用且有间隔",
          config.SYNC_ENABLED is True and config.SYNC_INTERVAL > 0,
          f"{config.SYNC_ENABLED} {config.SYNC_INTERVAL}")

    print("\n=== 索引同步配置（可编辑、立即生效）===")
    from keeper.search import config as cfgmod
    from keeper.setting import user_settings as usmod

    check("默认增量间隔 30 秒", cfgmod.sync_interval() == 30.0, str(cfgmod.sync_interval()))
    check("默认全量间隔 1800 秒",
          cfgmod.sync_full_interval() == 1800.0, str(cfgmod.sync_full_interval()))
    check("默认开着自动同步", cfgmod.sync_enabled() is True)

    old_env = os.environ.get("KEEPER_SETTINGS")
    tmp7 = Path(tempfile.mkdtemp())
    os.environ["KEEPER_SETTINGS"] = str(tmp7 / "settings.yaml")
    try:
        saved = usmod.set_search_config(
            {"sync_interval": 7, "sync_full_interval": 90, "sync_enabled": False}
        )
        check("保存后返回新值",
              saved["sync_interval"] == 7 and saved["sync_full_interval"] == 90
              and saved["sync_enabled"] is False, str(saved))
        check("读回即生效（不用重启）",
              cfgmod.sync_interval() == 7.0
              and cfgmod.sync_full_interval() == 90.0
              and cfgmod.sync_enabled() is False,
              f"{cfgmod.sync_interval()} {cfgmod.sync_full_interval()} {cfgmod.sync_enabled()}")
        text = (tmp7 / "settings.yaml").read_text(encoding="utf-8")
        check("落盘在 settings.yaml 的 search 段", "search:" in text, text[-200:])
        # 手写坏值不该炸
        (tmp7 / "settings.yaml").write_text(
            "search:\n  sync_interval: 不是数字\n", encoding="utf-8"
        )
        check("配置写坏了回落到默认", cfgmod.sync_interval() == 30.0,
              str(cfgmod.sync_interval()))
    finally:
        if old_env is None:
            os.environ.pop("KEEPER_SETTINGS", None)
        else:
            os.environ["KEEPER_SETTINGS"] = old_env

    print("\n=== PDF 切片与索引 ===")
    # PDF 走的是自己的切片器（大纲标题树 + 页级切片），产出的 Chunk 与 Markdown
    # 侧同构 → 索引、存储、检索整条链路复用。
    from keeper.search.ingest import pdf as pdfmod

    try:
        import pypdf  # noqa: F401
        has_pypdf = True
    except ImportError:
        has_pypdf = False

    if not has_pypdf:
        check("PDF 索引", True, "（没装 pypdf，跳过）")
    else:
        # 造一份带大纲的 PDF（pypdf 能写，但写不出书签 → 用真的现成 PDF 更难，
        # 所以这里直接测「无大纲」这条路径 + 用桩验证大纲解析）
        tmp8 = Path(tempfile.mkdtemp())
        conn8 = dbmod.connect(tmp8 / "pdf.db")
        docs8 = tmp8 / "docs"
        docs8.mkdir()

        # 1) 纯文本 PDF（自己造一个最小 PDF）
        pdf_path = docs8 / "note.pdf"
        _write_minimal_pdf(pdf_path, [
            "Cache penetration explained in detail. A request for a key that never "
            "exists in the cache will always hit the database directly.",
            "Bloom filter details. It is a probabilistic data structure which can "
            "tell you definitely not present or maybe present.",
        ])
        chunks8 = pdfmod.chunk_pdf(pdf_path)
        check("无大纲 PDF 也能切出 chunk", len(chunks8) > 0, str(len(chunks8)))
        if chunks8:
            leaves8 = [c for c in chunks8 if not c.is_parent]
            check("叶子 chunk 有正文", any(len(c.text) > 0 for c in leaves8))
            check("标题路径含文档标题",
                  leaves8[0].title_path[0] == "note",
                  str(leaves8[0].title_path))
            check("有定位串（页码）", bool(leaves8[0].metadata.get("locator")),
                  str(leaves8[0].metadata.get("locator")))
            check("embed_text 带标题（标题参与向量检索）",
                  leaves8[0].embed_text.startswith("task: search result | text: /note"),
                  leaves8[0].embed_text[:60])

        # 2) 索引：PDF 能进库，且带 source_kind / page_count
        s8 = ir(conn8, None, roots=[docs8])
        check("PDF 被索引", s8["indexed"] == 1, str(s8))
        doc8 = dbmod.get_document_by_path(conn8, str(pdf_path))
        check("入库带 source_kind=pdf", doc8 and doc8["source_kind"] == "pdf",
              str(dict(doc8) if doc8 else None))
        check("入库带页数", doc8 and doc8["page_count"] == 2,
              str(doc8["page_count"] if doc8 else None))

        # 3) 检索能命中，定位串是页码
        from keeper.search.service import SearchService
        svc8 = SearchService(db_path=tmp8 / "pdf.db")
        res8 = svc8.search("penetration", mode="keyword")
        check("关键词检索命中 PDF", len(res8) > 0, str(res8)[:200])
        if res8:
            hit8 = res8[0]
            check("命中结果带 locator（页码）", bool(hit8.get("locator")),
                  str(hit8.get("locator")))
            check("命中结果带 source_kind", hit8.get("source_kind") == "pdf",
                  str(hit8.get("source_kind")))
        svc8.close()

        # 4) 扫描件（几乎没文本）→ 跳过
        empty_pdf = docs8 / "scan.pdf"
        _write_minimal_pdf(empty_pdf, [""])
        s8b = ir(conn8, None, roots=[docs8])
        check("扫描件被跳过（不索引）", s8b.get("empty", 0) == 1, str(s8b))
        conn8.close()

        # 5) 大纲：父章节自己那几页（章节引子 / 封面）原来不属于任何章节
        #    → 整段没进索引 → 搜不到。现在父章节也成段。
        class _StubNode:
            def __init__(self, title, page, level, children=None):
                self.title = title
                self.page = page
                self.level = level
                self.children = children or []

        tree = [
            _StubNode("第一章", 1, 1, [
                _StubNode("1.1 节", 3, 2),
                _StubNode("1.2 节", 5, 2),
            ]),
            _StubNode("第二章", 7, 1),
        ]
        secs = pdfmod._leaf_sections(tree, "书名", 10)
        covered = set()
        for s in secs:
            covered.update(range(s.start_page, s.end_page + 1))
        check("PDF：父章节的引子页进了章节（原来整段丢失）",
              2 in covered, str(sorted(covered)))
        check("PDF：封面页（首个章节之前）也进了索引",
              0 in covered, str(sorted(covered)))
        check("PDF：页全覆盖，没有漏", covered == set(range(10)),
              str(sorted(set(range(10)) - covered)))

    print("\n=== 文件名参与索引与检索 ===")
    # 用户最可能记得的是**文件名**，所以它必须进索引，而且权重要高。
    # 现在的分工：
    # - 关键词侧：进**独立的 name_tokens 列**（bm25 权重 8.0；正文 tokens 只有 1.0，
    #   标题 title_tokens 3.0）→ 文件名走**字面匹配**，不跟正文混在一列。
    # - 向量侧：**不进** embed_text（向量通道只管语义）。
    # chunkmod 已在文件顶部导入（这里不要再 import，否则会把它变成局部变量，
    # 让前面 line 124 处的用法报 UnboundLocalError）
    from keeper.search.service import SearchService

    tmp9 = Path(tempfile.mkdtemp())
    space9 = tmp9 / "space"
    space9.mkdir()
    body9 = ("本文讨论缓存穿透与布隆过滤器的实现细节：缓存穿透是指查询一个数据库中"
             "不存在的键时，每次请求都会绕过缓存直接打到数据库上，压力大时会把数据库"
             "压垮；布隆过滤器是一种概率型数据结构，用若干位哈希来判断某个键是否可能"
             "存在，用一点误判率换取极低的内存占用，是缓存穿透的常见解法之一。\n\n")
    body9b = ("这一节讨论缓存一致性与失效策略：先写缓存还是先写数据库、缓存更新用"
              "消息队列还是定时任务、双删与延迟双删的差别，以及为什么强一致与最终一致"
              "之间总是要做一个取舍，这些问题在实际落地时比算法本身更容易踩坑。"
              "工程上常见的做法是：写库成功后再删缓存，删除失败就记日志重试，"
              "同时给缓存设置一个合理的过期时间作为兜底，这样即使重试链路出问题，"
              "最坏的结果也只是数据短暂不一致，而不是永久读到脏数据。"
              "另外还要注意缓存穿透与缓存击穿的区别：前者打的是不存在的键，"
              "后者打的是热点键，两者的应对手段完全不同，不能混为一谈。\n")
    (space9 / "vectorsearch-notes.md").write_text(
        "# 缓存设计笔记\n\n" + body9 + "\n## 第二节\n\n" + body9b,
        encoding="utf-8")
    # 再放一个别的文件：用来验证「每个文档只挂一块文件名」
    (space9 / "other-file.md").write_text(
        "# 另一篇\n\n" + body9b, encoding="utf-8")

    cs9 = chunkmod.chunk_markdown(
        "# 缓存设计笔记\n\n" + body9 + "\n## 第二节\n\n" + body9b,
        file_path=str(space9 / "vectorsearch-notes.md"),
    )
    leaf9 = [c for c in cs9 if not c.is_parent][0]
    check("embed_text 不含文件名（文件名不走向量通道）",
          "vectorsearch-notes" not in leaf9.embed_text, leaf9.embed_text[:90])

    svc9 = SearchService(db_path=tmp9 / "s.db")
    svc9.index(roots=[str(space9)])
    conn9 = dbmod.connect(tmp9 / "s.db")
    fts_cols9 = [r[1] for r in conn9.execute("PRAGMA table_info(chunks_fts)")]
    check("FTS5 四列（正文 / 章节路径 / 文件名 / 文档标题）",
          fts_cols9 == ["tokens", "title_tokens", "name_tokens", "doc_title_tokens"],
          str(fts_cols9))
    row9 = conn9.execute(
        "SELECT c.name_tokens FROM chunks c JOIN documents d ON d.id=c.doc_id "
        "WHERE d.file_path LIKE '%vectorsearch-notes.md' LIMIT 1").fetchone()
    check("name_tokens 存了文件名的整体规范化形式",
          bool(row9) and "vectorsearchnotes" in row9[0], str(row9))
    n9 = conn9.execute(
        "SELECT COUNT(*) FROM chunks WHERE name_tokens IS NOT NULL").fetchone()[0]
    all9 = conn9.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    check("文件名每块都存（召回用；打分时按文档只算一次，不重复计数）",
          n9 == all9 and all9 > 0, f"{n9}/{all9}")
    conn9.close()

    hits9 = svc9.search("vectorsearch", mode="keyword")
    check("关键词检索能按文件名命中（此前完全搜不到）", len(hits9) > 0,
          str(hits9)[:160])
    if hits9:
        check("命中的是那个文件", "vectorsearch-notes" in hits9[0]["path"],
              hits9[0]["path"])
    hits9b = svc9.search("vectorsearch-notes", mode="keyword")
    check("搜完整文件名（含连字符）也能字面命中", len(hits9b) > 0,
          str(hits9b)[:160])
    svc9.close()

    # 文件名权重要高于正文：同一个 BM25 里，命中文件名列 > 命中正文列
    tmp9b = Path(tempfile.mkdtemp())
    space9b = tmp9b / "space"
    space9b.mkdir()
    # 名字里有这个词，正文里没有
    (space9b / "季度总结.md").write_text(
        "# 第一部分\n\n这一节讲的是别的内容，跟季度这个词没关系。\n", encoding="utf-8")
    # 正文里有这个词，名字里没有
    (space9b / "notes.md").write_text(
        "# 第二部分\n\n这里提到布隆过滤器的原理与实现。\n", encoding="utf-8")
    conn9b = dbmod.connect(tmp9b / "w.db")
    ir(conn9b, None, roots=[space9b])
    name_hit = dbmod.search_keyword(conn9b, "季度总结", top_k=5)
    body_hit = dbmod.search_keyword(conn9b, "布隆过滤器", top_k=5)
    check("文件名能命中（name_tokens 列）", len(name_hit) > 0, str(name_hit)[:120])
    check("正文能命中（tokens 列）", len(body_hit) > 0, str(body_hit)[:120])
    if name_hit and body_hit:
        check("文件名权重高于正文（字段权重 8.0 vs 1.0）",
              name_hit[0]["score"] > body_hit[0]["score"],
              f"文件名 {name_hit[0]['score']:.3e} vs 正文 {body_hit[0]['score']:.3e}")
    conn9b.close()

    # **大文档不再失效**：文件名的 df 按**文档**算（不是按 chunk），
    # 所以 20 块的大文档搜文件名，分数仍然是"正常量级"（不是 1e-6）。
    tmp9c = Path(tempfile.mkdtemp())
    space9c = tmp9c / "space"
    space9c.mkdir()
    for i in range(6):
        (space9c / f"章节{i}.md").write_text(
            f"# 第{i}章\n\n" + ("这一段是正文内容，讲的是别的事情。" * 12) + "\n",
            encoding="utf-8")
    (space9c / "深度seek内部研讨.md").write_text(
        "# 研讨\n\n" + ("这里是另一篇文档的正文内容。" * 20) + "\n", encoding="utf-8")
    conn9c = dbmod.connect(tmp9c / "big.db")
    ir(conn9c, None, roots=[space9c])
    nchunk9c = conn9c.execute(
        "SELECT COUNT(*) FROM chunks WHERE is_parent=0").fetchone()[0]
    name_hit9c = dbmod.search_keyword(conn9c, "深度seek", top_k=5)
    check("大文档按文件名命中（idf 用文档级 df，不受块数影响）",
          len(name_hit9c) > 0 and name_hit9c[0]["score"] > 1e-3,
          f"chunks={nchunk9c} " + str([(h["path"].split("/")[-1], h["score"])
                                       for h in name_hit9c])[:160])
    if name_hit9c:
        check("文件名命中只给该文档贡献一条（不刷屏）",
              len([h for h in name_hit9c
                   if h["path"].endswith("深度seek内部研讨.md")]) == 1,
              str(len(name_hit9c)))
    conn9c.close()

    # 嵌入格式版本变了 → 必须重嵌（向量复用的判据 content_hash 只算正文，
    # 看不到 embed_text 的变化，不校验版本就等于「改了格式等于没改」）
    if has_np:
        conn9 = dbmod.connect(tmp9 / "s.db")
        old_v = config.EMBED_FORMAT_VERSION
        try:
            e1 = _CountEmbedder()
            ir2(conn9, e1, roots=[space9], prescreen=True)
            check("版本没变 → prescreen 跳过（不重嵌）", e1.embedded == 0,
                  str(e1.embedded))
            config.EMBED_FORMAT_VERSION = old_v + 1
            e2 = _CountEmbedder()
            ir2(conn9, e2, roots=[space9], prescreen=True)
            check("版本 +1 → 重新切片并重嵌", e2.embedded > 0, str(e2.embedded))
        finally:
            config.EMBED_FORMAT_VERSION = old_v
            conn9.close()

    print("\n=== 向量通道（回归：批量点积那行曾经被改坏过）===")
    # 向量召回**不在**常规回归的依赖里（要几百 MB 的模型），但打分那段是纯 numpy，
    # 用假 embedder（4 维）就能覆盖 —— 不测的话，一行笔误就能让整条向量路静默失效。
    if not has_np:
        check("向量召回", True, "（没装 numpy，跳过）")
    else:
        tmp9d = Path(tempfile.mkdtemp())
        space9d = tmp9d / "space"
        space9d.mkdir()
        (space9d / "v.md").write_text(
            "# 向量测试\n\n" + ("这一段是向量检索的测试正文。" * 20) + "\n",
            encoding="utf-8")
        conn9d = dbmod.connect(tmp9d / "v.db")
        emb9d = _CountEmbedder()
        ir(conn9d, emb9d, roots=[space9d])
        vhits = dbmod.search_similar(conn9d, [0.1, 0.2, 0.3, 0.4], top_k=3)
        check("向量召回能返回结果", len(vhits) > 0, str(len(vhits)))
        if vhits:
            check("向量召回带分数", isinstance(vhits[0]["score"], float),
                  str(vhits[0]["score"]))
        conn9d.close()

    print("\n=== 切片：标题自己的正文不再丢 ===")
    # 回归：以前只把「没有子标题的标题」当叶子 → 有子标题的标题，它自己的
    # 正文（H1 的引子 / H2 的引子）要么彻底丢失（H1 不建父块），
    # 要么只在父块里（父块 is_parent=1 不参与检索）→ **搜不到**。
    # 现在每个标题的正文都是一个内容单元。
    md9 = ("# 甲标题\n\n甲标题自己的引子段落，说的是甲内容。\n\n"
           "## 乙标题\n\n乙标题自己的引子段落，说的是乙内容。\n\n"
           "### 丙标题\n\n丙标题的正文段落，说的是丙内容。\n")
    cs9 = chunkmod.chunk_markdown(md9, file_path="/tmp/引子测试.md")
    leaves9 = [c for c in cs9 if not c.is_parent]
    alltext9 = " ".join(c.text for c in leaves9)
    check("H1 自己的引子进了叶子块（原来彻底丢）",
          "甲标题自己的引子段落" in alltext9, alltext9[:80])
    check("H2 自己的引子进了叶子块（原来只在父块里）",
          "乙标题自己的引子段落" in alltext9, alltext9[:140])
    check("引子块保留了自己的章节路径",
          any(c.title_path == ["甲标题"] for c in leaves9)
          and any(c.title_path == ["甲标题", "乙标题"] for c in leaves9),
          str([c.titlepath for c in leaves9]))
    # 检索：引子里的词必须能搜到（父块不参与检索 → 之前必然 0 结果）
    tmp9e = Path(tempfile.mkdtemp())
    (tmp9e / "引子测试.md").write_text(md9, encoding="utf-8")
    conn9e = dbmod.connect(tmp9e / "t.db")
    ir(conn9e, None, roots=[tmp9e])
    for kw in ["甲内容", "乙内容", "丙内容"]:
        hits9e = dbmod.search_keyword(conn9e, kw, top_k=3)
        check(f"引子里的词能搜到（{kw}）", len(hits9e) > 0, str(hits9e)[:120])
    conn9e.close()

    print("\n=== 最终分（0~1）与分数下限 ===")
    # 这一段守的是「召回的确实是有用的信息」：
    #   1. 最终分必须落在 0~1（能当"有用程度"看）；
    #   2. **罕见词**命中 → 分数高；**到处都有的词**命中 → 分数低（被门槛挡掉）；
    #   3. min_score 真的能过滤（UI 0.5 / agent 0.8 两个场景）；
    #   4. 融合是 noisy-OR：两路都命中比只命中一路高。
    from keeper.search.indexer import index_roots as ir10
    from keeper.search.query import (_fuse_noisy_or, _kw_unit, _vec_unit,
                                     search as qsearch10)

    tmp10 = Path(tempfile.mkdtemp())
    space10 = tmp10 / "space"
    space10.mkdir()
    # 5 篇文档：1 篇讲罕见词「布隆过滤器」，4 篇都是"到处都有的词"。
    (space10 / "布隆过滤器笔记.md").write_text(
        "# 布隆过滤器\n\n布隆过滤器是一种概率型数据结构，用位数组判断某个键一定不存在。\n",
        encoding="utf-8")
    for i in range(4):
        (space10 / f"日常记录{i}.md").write_text(
            f"# 日常记录{i}\n\n今天系统的运行很正常，服务的实现方式还是那一套，"
            f"没有什么特别的改动，只是把一些细节又确认了一遍。\n",
            encoding="utf-8")
    conn10 = dbmod.connect(tmp10 / "t.db")
    ir10(conn10, None, roots=[space10])

    rare = qsearch10(conn10, None, "布隆过滤器", top_k=5, mode="keyword",
                      min_score=0)
    common = qsearch10(conn10, None, "系统", top_k=5, mode="keyword",
                       min_score=0)
    check("罕见词能搜到", len(rare) > 0, str(len(rare)))
    check("高频词也能搜到（只是分数低）", len(common) > 0, str(len(common)))
    check("最终分落在 0~1",
          all(0.0 <= r["score"] <= 1.0 for r in rare + common),
          str([r["score"] for r in rare + common]))
    check("罕见词命中 → 分数高（能过 agent 的 0.8）",
          bool(rare) and rare[0]["score"] >= config.MIN_SCORE_AGENT,
          str(rare[0]["score"] if rare else None))
    check("到处都有的词 → 分数低（过不了 UI 的 0.5）",
          bool(common) and common[0]["score"] < config.MIN_SCORE_UI,
          str(common[0]["score"] if common else None))
    check("分数高的排前面",
          all(rare[i]["score"] >= rare[i + 1]["score"]
              for i in range(len(rare) - 1)))
    # 命中词：前端要拿它做关键词高亮。用户输入整串「布隆过滤器」，
    # 但正文里可能只出现其中一段——前端拿整串去高亮会一个都标不出来。
    check("结果带命中词（前端高亮用）",
          all("matched_terms" in r for r in rare + common),
          str([r.get("matched_terms") for r in rare + common]))
    check("命中词非空且确实出现在正文里",
          all(r["matched_terms"] and
              any(t in r.get("text", "") or t in r.get("titlepath", "")
                  for t in r["matched_terms"])
              for r in rare),
          str([(r.get("matched_terms"), r.get("text", "")[:30]) for r in rare]))

    # 门槛：UI 0.5 / agent 0.8 两个场景
    ui10 = qsearch10(conn10, None, "布隆过滤器", top_k=5, mode="keyword",
                      min_score=config.MIN_SCORE_UI)
    agent10 = qsearch10(conn10, None, "布隆过滤器", top_k=5, mode="keyword",
                         min_score=config.MIN_SCORE_AGENT)
    noise10 = qsearch10(conn10, None, "系统", top_k=5, mode="keyword",
                        min_score=config.MIN_SCORE_UI)
    check("UI 门槛 0.5：罕见词能过", len(ui10) > 0, str(len(ui10)))
    check("agent 门槛 0.8：罕见词能过", len(agent10) > 0, str(len(agent10)))
    check("UI 门槛 0.5：高频词被挡掉", len(noise10) == 0, str(len(noise10)))
    check("min_score=0 → 不过滤",
          len(qsearch10(conn10, None, "系统", top_k=5, mode="keyword",
                         min_score=0)) > 0)
    check("min_score 越大结果越少",
          len(agent10) <= len(ui10), f"{len(agent10)} vs {len(ui10)}")
    conn10.close()

    # 融合：noisy-OR（两路都命中 → 比只命中一路高）
    merged10 = _fuse_noisy_or(
        [{"chunk_id": "c1", "score": 0.77}],           # 向量：余弦 0.77 → 0.8
        [{"chunk_id": "c1", "score": 9.0, "evidence": 0.5}],  # 关键词：证据 0.5
    )
    check("noisy-OR：两路都命中 → 0.9",
          abs(merged10[0]["score"] - 0.9) < 1e-6,
          str(merged10[0]["score"]))
    only_vec10 = _fuse_noisy_or([{"chunk_id": "c1", "score": 0.80}], [])
    check("noisy-OR：只有向量 → 就是向量分",
          abs(only_vec10[0]["score"] - 1.0) < 1e-6, str(only_vec10[0]["score"]))
    only_kw10 = _fuse_noisy_or([], [{"chunk_id": "c1", "score": 1.0,
                                     "evidence": 0.5}])
    check("noisy-OR：只有关键词 → 就是证据分",
          abs(only_kw10[0]["score"] - 0.5) < 1e-6, str(only_kw10[0]["score"]))

    # 两路的分都要被夹在 0~1（不能因为余弦或原始分超出范围而越界）
    check("向量分夹在 0~1",
          _vec_unit([{"chunk_id": "c1", "score": 0.9}])["c1"] == 1.0
          and _vec_unit([{"chunk_id": "c2", "score": 0.1}])["c2"] == 0.0)
    check("关键词证据分夹在 0~1",
          _kw_unit([{"chunk_id": "c1", "score": 1.0,
                     "evidence": 5.0}])["c1"] == 1.0
          and _kw_unit([{"chunk_id": "c2", "score": 1.0,
                         "evidence": -1.0}])["c2"] == 0.0)

    # 词频饱和：同一个词出现 24 次，不该等于罕见词出现 1 次
    from keeper.search.rank import _tf_sat
    ratio10 = _tf_sat(24, 1.0, config.KEYWORD_K1) / _tf_sat(1, 1.0, config.KEYWORD_K1)
    check("词频饱和：出现 24 次 ≈ 出现 1 次的 3 倍以内（不是 24 倍）",
          ratio10 < 3.0, f"{ratio10:.2f} 倍")

    # PDF 的页码：locator 是「第 12 页」这种中文串，但前端要的是数值
    # （拿它跳到那一页）。解析集中在 db._page_of 一处。
    from keeper.search.store.db import _page_of
    check("locator → 数值页码",
          _page_of("第 12 页") == 12
          and _page_of("第 12-13 页") == 12
          and _page_of("第 3 页") == 3
          and _page_of(None) is None
          and _page_of("") is None,
          str([_page_of("第 12 页"), _page_of("第 12-13 页"), _page_of(None)]))

    print("\n=== 配置 ===")
    check("默认索引目录在 ~/.keeper 下",
          str(config.DB_PATH).startswith(str(Path.home() / ".keeper")),
          str(config.DB_PATH))
    check("模型目录在 ~/.keeper 下",
          str(config.MODEL_DIR).startswith(str(Path.home() / ".keeper")),
          str(config.MODEL_DIR))
    old = config.REPO_DIRS
    try:
        config.REPO_DIRS = ["/tmp/explicit"]
        check("显式配置优先",
              [str(p) for p in config.default_roots()] == ["/tmp/explicit"])
    finally:
        config.REPO_DIRS = old
    check("未配置「我的空间」-> 空列表（不是报错）",
          isinstance(config.default_roots(), list))

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
