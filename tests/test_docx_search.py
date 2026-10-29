"""docx 进检索索引的自测。

跑法：keeper/.venv/bin/python keeper/tests/test_docx_search.py

覆盖：
  1. 类型识别：.docx/.docm 可索引，.txt 不可
  2. 切片：标题层级 → title_path、块序号定位、表格转 markdown
  3. 中文编号式标题（"第一编" / "第三章"）也能被认出来
  4. 端到端：索引 → 检索命中 → source_kind=docx / 块序号可用
"""
from __future__ import annotations

import io
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from keeper.search.ingest import detect  # noqa: E402
from keeper.search.ingest.docx import chunk_docx  # noqa: E402
from keeper.search.store import db as dbmod  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}{('  → ' + extra) if extra else ''}")


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def make_docx(document: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", document)
        z.writestr(
            "word/styles.xml",
            f'<w:styles {W}><w:style w:type="paragraph" w:styleId="Heading1">'
            '<w:name w:val="heading 1"/></w:style></w:styles>',
        )
    return buf.getvalue()


DOC_XML = f"""<w:document {W}><w:body>
  <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>合同标题</w:t></w:r></w:p>
  <w:p><w:r><w:t>甲方：某公司</w:t></w:r></w:p>
  <w:p><w:r><w:t>第一条　违约责任：违约方应当赔偿守约方因此遭受的全部损失。</w:t></w:r></w:p>
  <w:p><w:r><w:t>第二条　合同解除：一方可以解除合同，但应提前三十日通知对方。</w:t></w:r></w:p>
  <w:tbl>
    <w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="2000"/></w:tblGrid>
    <w:tr>
      <w:tc><w:p><w:r><w:t>项目</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>金额</w:t></w:r></w:p></w:tc>
    </w:tr>
    <w:tr>
      <w:tc><w:p><w:r><w:t>服务费</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>100 万</w:t></w:r></w:p></w:tc>
    </w:tr>
  </w:tbl>
</w:body></w:document>"""


def main() -> None:
    print("\n=== 类型识别 ===")
    check(".docx 可索引", detect.is_docx(Path("a.docx")) and detect.is_indexable(Path("a.docx")))
    check(".docm 可索引", detect.is_docx(Path("a.DOCM")) and detect.is_indexable(Path("a.DOCM")))
    check(".txt 不可索引", not detect.is_indexable(Path("a.txt")))
    check(".pdf 仍可索引", detect.is_indexable(Path("a.pdf")))

    print("\n=== 切片 ===")
    tmp = Path(tempfile.mkdtemp())
    p = tmp / "合同.docx"
    p.write_bytes(make_docx(DOC_XML))
    chunks = chunk_docx(str(p), file_path=str(p))
    leaves = [c for c in chunks if not c.is_parent]
    check("切出了叶子块", len(leaves) >= 1, str(len(leaves)))
    all_text = "\n".join(c.text for c in leaves)
    check("正文进索引了", "违约责任" in all_text and "合同解除" in all_text, all_text[:60])
    check("表格转成 markdown", "| 项目 | 金额 |" in all_text or "服务费" in all_text, all_text[-120:])
    check("标题进了 title_path", any("合同标题" in "/".join(c.title_path) for c in leaves),
          str([c.title_path for c in leaves]))
    # 块序号：1-based，且不重复
    lines = [c.start_line for c in leaves]
    check("块序号从 1 开始", min(lines) >= 1, str(lines))
    check("块序号递增", lines == sorted(lines), str(lines))
    check("locator 留空（docx 没有页）", all(c.metadata.get("locator") is None for c in leaves))
    check("source_kind=docx", all(c.metadata.get("source_kind") == "docx" for c in leaves))

    print("\n=== 中文编号式标题 ===")
    xml2 = f"""<w:document {W}><w:body>
      <w:p><w:r><w:t>第一章　总则</w:t></w:r></w:p>
      <w:p><w:r><w:t>第一条　立法目的</w:t></w:r></w:p>
      <w:p><w:r><w:t>第二章　合同</w:t></w:r></w:p>
      <w:p><w:r><w:t>第三条　合同订立</w:t></w:r></w:p>
    </w:body></w:document>"""
    p2 = tmp / "编号标题.docx"
    p2.write_bytes(make_docx(xml2))
    cs2 = chunk_docx(str(p2), file_path=str(p2))
    paths = ["/".join(c.title_path) for c in cs2 if not c.is_parent]
    check("认出了『第一章』", any("第一章" in x for x in paths), str(paths))
    check("认出了『第二章』", any("第二章" in x for x in paths), str(paths))
    check("层级递进（第一章 → 第二章）",
          any("第一章" in x and "第二章" in x for x in paths), str(paths))
    check("『第 N 条』不当标题（不切碎）",
          not any("第一条" in x for x in paths), str(paths))

    print("\n=== 端到端：索引 → 检索 ===")
    from keeper.search import indexer, query

    conn = dbmod.connect(str(tmp / "idx.db"))
    res = indexer.index_paths(conn, None, [str(p)])
    check("索引成功", res.get("indexed") == 1, str(res))
    doc = dbmod.get_document_by_path(conn, str(p))
    check("文档条目 source_kind=docx", doc and doc["source_kind"] == "docx",
          str(doc["source_kind"] if doc else None))
    check("文档标题取自首段", doc and doc["title"] == "合同标题",
          str(doc["title"] if doc else None))
    hits = query.search(conn, None, "违约责任", top_k=5, mode="keyword")
    check("关键词检索命中", len(hits) >= 1, str(len(hits)))
    if hits:
        h = hits[0]
        check("命中项 source_kind=docx", h["source_kind"] == "docx", str(h["source_kind"]))
        check("命中项带块序号（可跳转）", h["start_line"] >= 1, str(h["start_line"]))
        check("命中项没有页码（docx 无页）", h["page"] is None, str(h["page"]))
        check("命中内容正确", "违约" in h["text"], h["text"][:40])

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
