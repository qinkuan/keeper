"""知识库 PDF 页图能力的回归脚本（直接 `python keeper/tests/test_kb_pdf.py` 跑）。

覆盖这条链路上最容易坏的六件事：

1. **PDF 精确页码**：切片时每个叶子块要落在**它内容真正所在的那几页**，
   而不是整个章节的页区间（错了 → agent 拿到一页不含命中内容的图 → 编答案）。
2. **页码展开**：命中页 ±1，撞到文档首尾时往另一侧补足，页数恒定。
3. **渲染**：页级光栅图要能渲染出合法的 PNG。
4. **工具契约**：kb.read_pdf / kb.search_pdf 返回「文字说明 + N 个图片块」，
   页数对得上；越界、超页数、路径越界都要有人话报错。
5. **框架贯通**：工具结果里的图片块要能变成消息里的 file 块，并被认出来
   （走直连发送，绕开 langchain 对 file 块的形状改写）。
6. **存储层**：page_start / page_end 要真的写进库、能读回来。

不需要网络、不需要 embedding 模型、不需要厂商 API：上传那一步用假的
uploader 顶替（只验证「我们交给厂商的是什么」，不验证厂商怎么存）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# tests/ -> keeper/ -> 项目根（keeper 包的父目录）
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from keeper.knowledge import tools as kbtools  # noqa: E402
from keeper.pdfpage import render as pdfrender  # noqa: E402
from keeper.search.ingest import pdf as pdfingest  # noqa: E402

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


# ---------------------------------------------------------------- 最小 PDF


def make_text_pdf(path: Path, pages_text: list) -> Path:
    """生成一个每页一行文字的最小 PDF（**有文字层**，能被 pypdf 抽出）。

    为什么自己造而不找现成的 PDF：现成文件要么是扫描件（没文字层，索引不了），
    要么体积大几百 MB。测试要的是「可控的页数 + 可控的文字」，自己造最省事。

    文字只能是 **ASCII**：内容流用的是 Helvetica（latin-1 编码的标准字体），
    要放中文就得嵌一套 TrueType 字体——那是字体子集化的事，不该混进这个测试。
    测试要验的是**页码定位**与**工具契约**，跟文字是什么语言无关。

    每页文字都**以句号结尾**是有意的：切 PDF 时「上一页末尾不是句末标点 →
    下一页开头是它的续接」会被合并成一个段落。不加句号，5 页会被并成 1 个块，
    测不到「多块命中」那件事。
    """
    n = len(pages_text)
    objs: list = []

    def add(body: str) -> int:
        objs.append(body)
        return len(objs)          # 1-based 对象号

    catalog_num = add("")        # 占位，稍后填
    pages_num = add("")
    font_num = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    kids = []
    for text in pages_text:
        # 内容流：BT /F1 14 Tf 72 720 Td (文字) Tj ET
        safe = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 14 Tf 72 720 Td ({safe}) Tj ET"
        content_num = add(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        page_num = add(
            "<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
            "/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_num, font_num, content_num)
        )
        kids.append(page_num)

    objs[catalog_num - 1] = f"<< /Type /Catalog /Pages {pages_num} 0 R >>"
    kids_str = " ".join(f"{k} 0 R" for k in kids)
    objs[pages_num - 1] = f"<< /Type /Pages /Kids [{kids_str}] /Count {n} >>"

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")

    xref_at = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root {catalog_num} 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode("latin-1")
    path.write_bytes(bytes(out))
    return path


# ---------------------------------------------------------------- 假上传器


class _FakeUploaded:
    def __init__(self, file_id: str):
        self.file_id = file_id


class _FakeUploader:
    """顶替厂商上传器的假实现：只验证「我们交给厂商的是什么」。"""

    provider = "fake"

    def __init__(self):
        self.uploads: list = []

    def supports(self, cfg):
        return True, ""

    async def upload(self, cfg, data: bytes, name: str):
        self.uploads.append((name, len(data)))
        return _FakeUploaded(f"file-{len(self.uploads)}")

    def ref_part(self, file_id: str):
        return {"type": "file", "file_id": file_id}


def _patch_uploader(fake_uploader):
    """把 llm.files.uploader_for 换成假的，并让工具拿到一份假配置。"""
    import keeper.llm.files as filesmod

    filesmod._UPLOADERS["fake"] = fake_uploader

    class _Cfg:
        provider = "fake"
        model_name = "fake-model"
        api_key = "sk-test"
        base_url = "https://example.invalid"
        timeout = 30
        max_retries = 1

    async def _cfg():
        return _Cfg()

    kbtools._current_llm_config = _cfg
    return filesmod


# ---------------------------------------------------------------- 各部分测试


def _long_page(topic: str) -> str:
    """一段约 600 字符的正文。

    为什么这么长：切片是**贪心合并**——段落依次并入，直到超过
    ``CHUNK_MAX_CHARS``（1000）。每页只有几十个字符的话，5 页会被并成 1 个块，
    就测不到「多块命中 → block=2」这件事了。每页 ~600 字符 → 每页各自成块。
    """
    base = (
        "The chapter walks through the problem from first principles and then lists "
        "the concrete trade-offs between the simple approach and the elaborate one. "
        "It points out where each one breaks down under heavy production traffic, "
        "and finishes with the practical advice that most teams end up adopting "
        "after a few months of running it in production and watching the graphs."
    )
    return f"{topic}. {base} {base}"


def part_pages() -> None:
    """页码展开规则。"""
    print("\n[1] 页码展开（命中页 ±1，撞边界往回找）")
    cases = [
        ("常规", 5, 20, [4, 5, 6]),
        ("命中首页 → 往下补", 1, 20, [1, 2, 3]),
        ("命中末页 → 往上补", 20, 20, [18, 19, 20]),
        ("只有 3 页", 2, 3, [1, 2, 3]),
        ("只有 2 页", 1, 2, [1, 2]),
        ("只有 1 页", 1, 1, [1]),
    ]
    for label, hit, total, want in cases:
        got = kbtools.expand_pages(hit, total)
        check(f"{label} → {want}", got == want, f"实际 {got}")


def part_exact_page() -> None:
    """切片时的精确页码。"""
    print("\n[2] 切片：叶子块的精确页码")
    tmp = Path(tempfile.mkdtemp())
    pdf = make_text_pdf(tmp / "doc.pdf", [
        "Chapter one: cache penetration. The first page body text.",
        "Chapter two: cache breakdown. A hot key expires under heavy load.",
        "Chapter three: cache avalanche. Many keys expire at the same moment.",
        "Chapter four: bloom filter. A bit array rejects missing keys early.",
        "Chapter five: mutex rebuild. Only one thread rebuilds the value.",
    ])
    chunks = pdfingest.chunk_pdf(pdf, file_path=str(pdf))
    leaves = [c for c in chunks if not c.is_parent]
    check("切出了叶子块", len(leaves) > 0, f"{len(leaves)}")

    from pypdf import PdfReader

    reader = PdfReader(str(pdf))
    per_page = [(reader.pages[i].extract_text() or "") for i in range(len(reader.pages))]

    ok_pages = True
    detail = ""
    for c in leaves:
        ps = c.metadata.get("page_start")
        pe = c.metadata.get("page_end")
        if not ps or not pe:
            ok_pages = False
            detail = "缺 page_start/page_end"
            break
        # 该块的前 12 个字符应出现在它声明的页范围里
        probe = "".join(c.text.split())[:12]
        if not probe:
            continue
        hits = [i + 1 for i, t in enumerate(per_page)
                if probe in "".join(t.split())]
        if hits and not (ps <= min(hits) and max(hits) <= pe):
            ok_pages = False
            detail = f"声明 {ps}-{pe}，实际在 {hits}"
            break
    check("叶子块的页范围覆盖其内容所在页", ok_pages, detail)
    check(
        "locator 用的是精确页而不是章节区间",
        all(c.metadata.get("locator", "").startswith("第 ") for c in leaves),
    )


def part_render() -> None:
    """渲染出的 PNG。"""
    print("\n[3] 渲染：整页 → PNG")
    tmp = Path(tempfile.mkdtemp())
    pdf = make_text_pdf(tmp / "doc.pdf", ["Page one", "Page two", "Page three"])
    check("页数读得到", pdfrender.page_count(str(pdf)) == 3)
    pngs = pdfrender.render_pages(str(pdf), [1, 2, 3])
    check("渲染出 3 页", len(pngs) == 3, f"{len(pngs)}")
    check("是 PNG", all(p[:8] == b"\x89PNG\r\n\x1a\n" for p in pngs))
    check(
        "体积合理（>1KB，<8MB）",
        all(1024 < len(p) < 8 * 1024 * 1024 for p in pngs),
        str([len(p) for p in pngs]),
    )
    check("越界页码被跳过", len(pdfrender.render_pages(str(pdf), [1, 99])) == 1)


def part_tools() -> None:
    """kb.read_pdf 的契约。"""
    print("\n[4] 工具契约：kb.read_pdf")
    tmp = Path(tempfile.mkdtemp())
    pdf = make_text_pdf(tmp / "doc.pdf", [f"Content of page {i}" for i in range(1, 11)])

    up = _FakeUploader()
    _patch_uploader(up)

    import asyncio

    import keeper.setting as settingmod

    old_root = settingmod.get_user_space_path
    settingmod.get_user_space_path = lambda: str(tmp)
    try:
        res = asyncio.run(kbtools._read_pdf({"path": "doc.pdf", "start_page": 1}))
        check("返回 ToolResult", hasattr(res, "text") and hasattr(res, "blocks"))
        check("读 1 页 → 1 个图片块", len(res.blocks) == 1, str(res.blocks))
        check("块是 file 形状", bool(res.blocks) and res.blocks[0].get("type") == "file")
        check(
            "说明里有文件名与页码",
            "doc.pdf" in res.text and "第 1 页" in res.text,
            res.text,
        )

        res3 = asyncio.run(
            kbtools._read_pdf({"path": "doc.pdf", "start_page": 2, "end_page": 4})
        )
        check("读 3 页 → 3 个图片块", len(res3.blocks) == 3, str(len(res3.blocks)))

        res6 = asyncio.run(
            kbtools._read_pdf({"path": "doc.pdf", "start_page": 1, "end_page": 6})
        )
        check("超 5 页被拒", not res6.blocks and "最多" in res6.text, res6.text)

        res_oob = asyncio.run(kbtools._read_pdf({"path": "doc.pdf", "start_page": 99}))
        check("越界页码被拒", not res_oob.blocks and "超出" in res_oob.text, res_oob.text)

        res_miss = asyncio.run(kbtools._read_pdf({"path": "nope.pdf", "start_page": 1}))
        check("文件不存在被拒", not res_miss.blocks and "不存在" in res_miss.text, res_miss.text)

        res_esc = asyncio.run(
            kbtools._read_pdf({"path": "../../etc/hosts", "start_page": 1})
        )
        check("路径越界被拦", not res_esc.blocks and "我的空间" in res_esc.text, res_esc.text)

        # 模型从 bash 的 ls 输出里抄路径，常漏空格/少字/多带引号 → 要能吸收
        res_fuzzy = asyncio.run(kbtools._read_pdf({"path": "doc", "start_page": 1}))
        check("只给文件名的一部分也能命中", len(res_fuzzy.blocks) == 1, res_fuzzy.text)
        res_quote = asyncio.run(kbtools._read_pdf({"path": "'doc.pdf'", "start_page": 1}))
        check("带引号的路径也能命中", len(res_quote.blocks) == 1, res_quote.text)

        check("上传次数对得上（1+3+1+1）", len(up.uploads) == 6, str(up.uploads))
    finally:
        settingmod.get_user_space_path = old_root


def part_search_tool() -> None:
    """kb.search_pdf 的契约（走真实索引）。"""
    print("\n[5] 工具契约：kb.search_pdf（走真实索引）")
    tmp = Path(tempfile.mkdtemp())
    pdf = make_text_pdf(tmp / "cache.pdf", [
        _long_page("Chapter one talks about cache penetration"),
        _long_page("Chapter two talks about cache breakdown"),
        _long_page("Chapter three talks about cache avalanche"),
        _long_page("Chapter four talks about the bloom filter"),
        _long_page("Chapter five talks about the mutex rebuild"),
    ])

    up = _FakeUploader()
    _patch_uploader(up)

    # 用真实索引，但强制走关键词模式（不加载 embedding 模型）
    import keeper.search.ingest.embed as embedmod
    import keeper.search.config as scfgmod

    old_get = embedmod.get_embedder
    old_db = scfgmod.DB_PATH
    embedmod.get_embedder = lambda: None
    # **把索引库指到临时目录**：默认是用户真实的 ~/.keeper/search/search.db。
    # 拿它跑测试有两个问题：① 往用户知识库索引里塞测试文档；② 召回条数取决于
    # 用户库里有什么 → 断言会飘。工具内部自己 new 的 SearchService 也读这个
    # 配置，所以一并被指走。
    scfgmod.DB_PATH = str(tmp / "index.db")
    try:
        import asyncio

        from keeper.search.service import SearchService

        svc = SearchService()
        try:
            svc.index_paths([str(pdf)], force=True)
            hits = svc.search("cache avalanche", top_k=5, mode="keyword")
        finally:
            svc.close()
        check("索引后能召回", len(hits) > 0, f"{len(hits)}")
        check(
            "召回结果带精确页码",
            all(h.get("page_start") for h in hits),
            str([(h.get("locator"), h.get("page_start")) for h in hits]),
        )

        res = asyncio.run(kbtools._search_pdf({"keyword": "cache avalanche"}))
        check("search_pdf 返回 3 个图片块", len(res.blocks) == 3, str(res.blocks))
        check("说明里有命中页", "命中第" in res.text, res.text)
        check("块都是 file 形状", all(b.get("type") == "file" for b in res.blocks))

        # 「cache」出现在好几页 → 能召回多块，这时 block=2 才有意义
        res2 = asyncio.run(kbtools._search_pdf({"keyword": "cache", "block": 2}))
        check("block=2 也能取到 3 页", len(res2.blocks) == 3, str(res2.blocks))
        check("block=2 的说明里标了第 2 块", "第 2/" in res2.text, res2.text)

        res99 = asyncio.run(kbtools._search_pdf({"keyword": "cache", "block": 99}))
        check("block 越界有人话报错", not res99.blocks and "不存在" in res99.text, res99.text)

        res_none = asyncio.run(kbtools._search_pdf({"keyword": "quantum chromodynamics"}))
        check(
            "召回不到时明确说召回不到",
            not res_none.blocks and "没有召回" in res_none.text,
            res_none.text,
        )
    finally:
        embedmod.get_embedder = old_get
        scfgmod.DB_PATH = old_db


def part_pipeline() -> None:
    """框架贯通：工具结果 → 消息 → 厂商 payload。"""
    print("\n[6] 框架贯通：图片块 → 消息 → 直连 payload")
    from keeper.agent.process import _tool_message
    from keeper.llm.client import LangChainLLM, _has_file_blocks

    blocks = [{"type": "file", "file_id": "file-a"}, {"type": "file", "file_id": "file-b"}]
    msg = _tool_message("kb.read_pdf", "doc.pdf 第 1-2 页", blocks)
    check("content 变成数组", isinstance(msg.get("content"), list))
    check("带 _media 标记", msg.get("_media") is True)
    check("文本块在最前", msg["content"][0].get("type") == "text")

    llm = LangChainLLM(model=None, system="系统提示")
    msgs = llm._build_messages([msg], system="系统提示")
    human = [m for m in msgs if getattr(m, "type", "") == "human"]
    check("转成 human 消息（role=user）", len(human) == 1)
    check("content 是数组且带 file 块",
          isinstance(human[0].content, list)
          and any(b.get("type") == "file" for b in human[0].content))
    check("_has_file_blocks 认得它（会走直连）", _has_file_blocks(msgs) is True)

    payload = llm._direct_payload(msgs)
    user_msg = [p for p in payload if p["role"] == "user"][0]
    check("直连 payload 是 user + content 数组",
          user_msg["role"] == "user" and isinstance(user_msg["content"], list))
    check("file 块是厂商要的扁平形状",
          all(b.get("file_id") for b in user_msg["content"] if b.get("type") == "file"))

    # 纯文本的工具结果不受影响
    plain = _tool_message("recall", "纯文本结果")
    check("纯文本工具结果仍是字符串", isinstance(plain["content"], str))


def part_store() -> None:
    """存储层：page_start / page_end 要写进库、能读回来。"""
    print("\n[7] 存储层：精确页码落库")
    from keeper.search.ingest.chunk import Chunk
    from keeper.search.store import db as dbmod

    tmp = Path(tempfile.mkdtemp())
    conn = dbmod.connect(str(tmp / "t.db"))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()}
        check("chunks 表有 page_start 列", "page_start" in cols)
        check("chunks 表有 page_end 列", "page_end" in cols)
        check("chunks 表有 chunk_version 列", "chunk_version" in cols)

        doc_id = dbmod.upsert_document(
            conn, "/x/a.pdf", "user_document", "A", 1.0, 10, "h1",
            index_status="done", indexed_at=1.0, page_count=20, source_kind="pdf",
        )
        chunks = [
            Chunk(
                chunk_id="c2", parent_id=None, is_parent=False, title_level=2,
                title_path=["A", "1.1"], text="叶子正文", embed_text="emb",
                start_line=12, end_line=12, content_hash="h2", chunk_index=0,
                metadata={
                    "title": "A", "page_count": 20, "source_kind": "pdf",
                    "locator": "第 12 页", "page_start": 12, "page_end": 12,
                },
            )
        ]
        dbmod.sync_chunks(conn, doc_id, chunks, {})
        row = conn.execute(
            "SELECT locator, page_start, page_end FROM chunks WHERE chunk_id='c2'"
        ).fetchone()
        check("精确页码写进库了", row == ("第 12 页", 12, 12), str(row))
        check("切片版本号写进库了",
              dbmod.doc_chunk_version(conn, doc_id)
              == dbmod.config.CHUNK_FORMAT_VERSION)
    finally:
        conn.close()


def main() -> int:
    part_pages()
    part_exact_page()
    part_render()
    part_tools()
    part_search_tool()
    part_pipeline()
    part_store()
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
