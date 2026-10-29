"""docx 只读渲染（IR 抽取 + 端点）的自测。

跑法：keeper/.venv/bin/python keeper/tests/test_docx_ir.py

覆盖：文字级格式、样式继承（basedOn + docDefaults）、编号、表格（合并/底纹）、
图片、超链接、页面设置，以及端点的路径边界（空间内可解析 / 越界 403 / 坏文件 422）。
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from keeper.docx.extract import extract_docx_ir  # noqa: E402
from keeper.docx.ir import to_json  # noqa: E402

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
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
WP = 'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'

# 一张 1x1 的 PNG（最小合法 PNG）
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


def make_docx(document, styles="", numbering="", rels="", media=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", document)
        if styles:
            z.writestr("word/styles.xml", styles)
        if numbering:
            z.writestr("word/numbering.xml", numbering)
        if rels:
            z.writestr("word/_rels/document.xml.rels", rels)
        for name, data in (media or {}).items():
            z.writestr(f"word/{name}", data)
    return buf.getvalue()


def text_of(block: dict) -> str:
    return "".join(r.get("text", "") for r in block.get("runs", []))


def main() -> None:
    print("\n=== 文字级格式 ===")
    doc = make_docx(
        f"""<w:document {W} {R}><w:body>
        <w:p><w:r><w:rPr><w:b/><w:sz w:val="44"/><w:rFonts w:eastAsia="黑体" w:ascii="SimHei"/>
        <w:color w:val="FF0000"/></w:rPr><w:t>标题文字</w:t></w:r></w:p>
        <w:p><w:r><w:t>普通文字</w:t></w:r></w:p>
        </w:body></w:document>"""
    )
    ir = to_json(extract_docx_ir(doc))
    paras = ir["blocks"]
    check("抽出 2 段", len(paras) == 2, str(len(paras)))
    r0 = paras[0]["runs"][0]
    check("文字内容", text_of(paras[0]) == "标题文字")
    check("加粗", r0.get("bold") is True)
    check("字号（半磅→磅）", r0.get("size") == 22.0, str(r0.get("size")))
    check("颜色", r0.get("color") == "#FF0000", str(r0.get("color")))
    check("中文字体", r0.get("font_east_asia") == "黑体", str(r0.get("font_east_asia")))

    print("\n=== 样式继承（basedOn + docDefaults）===")
    doc = make_docx(
        f"""<w:document {W}><w:body>
        <w:p><w:pPr><w:pStyle w:val="H1"/></w:pPr><w:r><w:t>继承来的</w:t></w:r></w:p>
        <w:p><w:pPr><w:pStyle w:val="H1"/><w:jc w:val="center"/></w:pPr><w:r><w:t>直接覆盖</w:t></w:r></w:p>
        </w:body></w:document>""",
        styles=f"""<w:styles {W}>
          <w:docDefaults><w:rPrDefault><w:rPr><w:sz w:val="21"/></w:rPr></w:rPrDefault>
            <w:pPrDefault><w:pPr><w:jc w:val="both"/></w:pPr></w:pPrDefault></w:docDefaults>
          <w:style w:type="paragraph" w:styleId="Base"><w:name w:val="Base"/>
            <w:pPr><w:jc w:val="left"/></w:pPr><w:rPr><w:b/></w:rPr></w:style>
          <w:style w:type="paragraph" w:styleId="H1"><w:name w:val="heading 1"/>
            <w:basedOn w:val="Base"/><w:pPr><w:outlineLvl w:val="0"/></w:pPr>
            <w:rPr><w:sz w:val="32"/></w:rPr></w:style>
        </w:styles>""",
    )
    ir = to_json(extract_docx_ir(doc))
    p0, p1 = ir["blocks"][0], ir["blocks"][1]
    check("基于链的字号生效（32 半磅→16 磅）", p0["runs"][0].get("size") == 16.0, str(p0["runs"][0].get("size")))
    check("基于链的加粗生效", p0["runs"][0].get("bold") is True)
    check("大纲级别（标题识别）", p0.get("outline_level") == 0, str(p0.get("outline_level")))
    check("样式名", p0.get("style_name") == "heading 1", str(p0.get("style_name")))
    check("直接格式覆盖样式", p1.get("align") == "center", str(p1.get("align")))
    check("未覆盖处沿用样式（left）", p0.get("align") == "left", str(p0.get("align")))

    print("\n=== 编号 ===")
    numbering = f"""<w:numbering {W}>
      <w:abstractNum w:abstractNumId="0">
        <w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/><w:start w:val="1"/></w:lvl>
        <w:lvl w:ilvl="1"><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2"/><w:start w:val="1"/></w:lvl>
      </w:abstractNum>
      <w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
    </w:numbering>"""
    doc = make_docx(
        f"""<w:document {W}><w:body>
        <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>A</w:t></w:r></w:p>
        <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>B</w:t></w:r></w:p>
        </w:body></w:document>""",
        numbering=numbering,
    )
    ir = to_json(extract_docx_ir(doc))
    check("单级编号标记递增", [b["numbering"]["marker"] for b in ir["blocks"]] == ["1.", "2."],
          str([b.get("numbering") for b in ir["blocks"]]))

    print("\n=== 表格 ===")
    doc = make_docx(
        f"""<w:document {W}><w:body>
        <w:tbl>
          <w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="3000"/></w:tblGrid>
          <w:tr>
            <w:tc><w:tcPr><w:gridSpan w:val="2"/><w:shd w:fill="FFEE00"/></w:tcPr>
              <w:p><w:r><w:t>合并格</w:t></w:r></w:p></w:tc>
          </w:tr>
          <w:tr>
            <w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>纵向合并</w:t></w:r></w:p></w:tc>
            <w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p><w:r><w:t>延续</w:t></w:r></w:p></w:tc>
          </w:tr>
        </w:tbl>
        </w:body></w:document>"""
    )
    ir = to_json(extract_docx_ir(doc))
    t = ir["blocks"][0]
    check("表格块", t.get("type") == "table")
    check("列宽（twips→磅）", t.get("col_widths") == [100.0, 150.0], str(t.get("col_widths")))
    c0 = t["rows"][0][0]
    check("gridSpan 合并", c0.get("colspan") == 2, str(c0.get("colspan")))
    check("底纹", c0.get("shading") == "#FFEE00", str(c0.get("shading")))
    check("纵向合并起始行 rowspan=1", t["rows"][1][0].get("rowspan") == 1)
    check("纵向合并延续行 rowspan=0", t["rows"][1][1].get("rowspan") == 0)
    check("单元格里的文字", text_of(c0["blocks"][0]) == "合并格")

    print("\n=== 表格边框 ===")
    # 无边框表格：不该凭空多一圈框线
    doc = make_docx(
        f"""<w:document {W}><w:body>
        <w:tbl>
          <w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="2000"/></w:tblGrid>
          <w:tr><w:tc><w:p><w:r><w:t>a</w:t></w:r></w:p></w:tc>
                <w:tc><w:p><w:r><w:t>b</w:t></w:r></w:p></w:tc></w:tr>
        </w:tbl>
        </w:body></w:document>"""
    )
    ir = to_json(extract_docx_ir(doc))
    t = ir["blocks"][0]
    check("无边框表格：单元格没有 borders", not t["rows"][0][0].get("borders"),
          str(t["rows"][0][0].get("borders")))

    # 表格级边框 + 单元格覆盖
    doc = make_docx(
        f"""<w:document {W}><w:body>
        <w:tbl>
          <w:tblPr><w:tblBorders>
            <w:top w:val="single" w:sz="8" w:color="FF0000"/>
            <w:insideH w:val="single" w:sz="4" w:color="000000"/>
            <w:insideV w:val="single" w:sz="4"/>
            <w:bottom w:val="single" w:sz="8"/>
          </w:tblBorders></w:tblPr>
          <w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="2000"/></w:tblGrid>
          <w:tr><w:tc><w:p><w:r><w:t>a</w:t></w:r></w:p></w:tc>
                <w:tc><w:p><w:r><w:t>b</w:t></w:r></w:p></w:tc></w:tr>
          <w:tr><w:tc><w:tcPr><w:tcBorders><w:top w:val="none"/></w:tcBorders></w:tcPr>
                  <w:p><w:r><w:t>c</w:t></w:r></w:p></w:tc>
                <w:tc><w:p><w:r><w:t>d</w:t></w:r></w:p></w:tc></w:tr>
        </w:tbl>
        </w:body></w:document>"""
    )
    ir = to_json(extract_docx_ir(doc))
    t = ir["blocks"][0]
    b00 = t["rows"][0][0]["borders"]
    check("表格外框 top", b00.get("top", {}).get("style") == "single", str(b00.get("top")))
    check("边框粗细（1/8 磅→磅）", b00.get("top", {}).get("size") == 1.0,
          str(b00.get("top", {}).get("size")))
    check("边框颜色", b00.get("top", {}).get("color") == "#FF0000", str(b00.get("top")))
    check("内部竖线来自 insideV", b00.get("right", {}).get("style") == "single",
          str(b00.get("right")))
    check("第二行单元格上边来自 insideH", t["rows"][1][1]["borders"].get("top", {}).get("size") == 0.5,
          str(t["rows"][1][1]["borders"].get("top")))
    check("单元格 w:tcBorders 覆盖表格级",
          t["rows"][1][0]["borders"].get("top", {}).get("style") == "none",
          str(t["rows"][1][0]["borders"].get("top")))

    # 表格样式（Table Grid）里的边框
    doc = make_docx(
        f"""<w:document {W}><w:body>
        <w:tbl>
          <w:tblPr><w:tblStyle w:val="TableGrid"/></w:tblPr>
          <w:tblGrid><w:gridCol w:w="2000"/></w:tblGrid>
          <w:tr><w:tc><w:p><w:r><w:t>a</w:t></w:r></w:p></w:tc></w:tr>
        </w:tbl>
        </w:body></w:document>""",
        styles=f"""<w:styles {W}>
          <w:style w:type="table" w:styleId="TableGrid">
            <w:name w:val="Table Grid"/>
            <w:tblPr><w:tblBorders>
              <w:top w:val="single" w:sz="4"/>
              <w:left w:val="single" w:sz="4"/>
              <w:bottom w:val="single" w:sz="4"/>
              <w:right w:val="single" w:sz="4"/>
            </w:tblBorders></w:tblPr>
          </w:style>
        </w:styles>""",
    )
    ir = to_json(extract_docx_ir(doc))
    b = ir["blocks"][0]["rows"][0][0].get("borders") or {}
    check("表格样式的边框生效", b.get("top", {}).get("style") == "single", str(b))

    print("\n=== 制表位 ===")
    doc = make_docx(
        f"""<w:document {W}><w:body>
        <w:p><w:pPr><w:tabs>
            <w:tab w:val="right" w:pos="8306" w:leader="dot"/>
            <w:tab w:val="clear" w:pos="100"/>
          </w:tabs></w:pPr>
          <w:r><w:t>第一章</w:t></w:r><w:r><w:tab/></w:r><w:r><w:t>1</w:t></w:r>
        </w:p>
        </w:body></w:document>"""
    )
    ir = to_json(extract_docx_ir(doc))
    p0 = ir["blocks"][0]
    tabs = p0.get("tabs") or []
    check("抽出 1 个制表位（clear 被跳过）", len(tabs) == 1, str(tabs))
    if tabs:
        check("制表位位置（twips→磅）", tabs[0].get("pos") == 415.3, str(tabs[0].get("pos")))
        check("制表位对齐方式", tabs[0].get("val") == "right", str(tabs[0].get("val")))
        check("制表位前导符", tabs[0].get("leader") == "dot", str(tabs[0].get("leader")))

    print("\n=== 图片与超链接 ===")
    doc = make_docx(
        f"""<w:document {W} {R} {WP} {A}><w:body>
        <w:p><w:r><w:drawing><wp:inline>
          <wp:extent cx="1270000" cy="635000"/>
          <a:blip r:embed="rId9"/></wp:inline></w:drawing></w:r></w:p>
        <w:p><w:hyperlink r:id="rId8"><w:r><w:t>链接</w:t></w:r></w:hyperlink></w:p>
        </w:body></w:document>""",
        rels="""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/a.png"/>
          <Relationship Id="rId8" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.com" TargetMode="External"/>
        </Relationships>""",
        media={"media/a.png": PNG},
    )
    ir = to_json(extract_docx_ir(doc))
    img = ir["blocks"][0].get("images", [])
    check("抽出 1 张图", len(img) == 1, str(len(img)))
    if img:
        check("图片是 data URI", img[0]["data_uri"].startswith("data:image/png;base64,"))
        check("图片尺寸（EMU→磅）", img[0].get("width") == 100.0 and img[0].get("height") == 50.0,
              f"{img[0].get('width')}x{img[0].get('height')}")
    check("超链接带 URL", ir["blocks"][1]["runs"][0].get("link") == "https://example.com",
          str(ir["blocks"][1]["runs"][0].get("link")))

    print("\n=== 页面设置 ===")
    doc = make_docx(
        f"""<w:document {W}><w:body><w:p><w:r><w:t>x</w:t></w:r></w:p>
        <w:sectPr><w:pgSz w:w="11906" w:h="16838"/>
        <w:pgMar w:top="1440" w:right="1800" w:bottom="1440" w:left="1800"/></w:sectPr>
        </w:body></w:document>"""
    )
    ir = to_json(extract_docx_ir(doc))
    pg = ir["page"]
    check("页宽（A4 纵向）", abs(pg["width"] - 595.3) < 0.5, str(pg["width"]))
    check("页高", abs(pg["height"] - 841.9) < 0.5, str(pg["height"]))
    check("页边距", pg["margin_top"] == 72.0 and pg["margin_left"] == 90.0, str(pg))

    print("\n=== 端点：路径边界 ===")
    from fastapi import HTTPException

    from keeper.docx.api import get_docx_ir

    tmp = Path(tempfile.mkdtemp())
    space = tmp / "space"
    space.mkdir()
    (space / "a.docx").write_bytes(doc)
    home = tmp / "home"
    (home / ".keeper").mkdir(parents=True)
    os.environ["HOME"] = str(home)
    (home / ".keeper" / "settings.yaml").write_text(
        f"user_space:\n  path: {space}\n", encoding="utf-8"
    )

    async def call(p: str):
        try:
            return await get_docx_ir(path=p), None
        except HTTPException as e:
            return None, e

    res, err = asyncio.run(call(str(space / "a.docx")))
    check("空间内的 docx 能解析", res is not None and len(res["blocks"]) == 1, str(err))

    res, err = asyncio.run(call(str(tmp / "outside.docx")))
    check("空间外的文件被拒（403）", err is not None and err.status_code == 403,
          str(err.status_code if err else res))

    (space / "bad.docx").write_bytes(b"not a zip at all")
    res, err = asyncio.run(call(str(space / "bad.docx")))
    check("坏文件报 422（不是 500）", err is not None and err.status_code == 422,
          str(err.status_code if err else res))

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
