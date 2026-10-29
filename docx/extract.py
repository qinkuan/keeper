"""docx(zip+OOXML) → IR 的抽取器。

**明确不做的事**（第一版只读渲染器的边界）：
- 浮动定位（w:anchor 图片、w:framePr 段落）——不做锚点布局
- 文本框（w:txbxContent）、SmartArt / 图表 / 公式（OMML）
- 修订与批注（w:ins / w:del / w:commentRangeStart）
- 页眉页脚、脚注尾注、分栏（w:cols）、宏
这些不影响"读懂文档"，但影响"版面还原"。遇到时记进 ``unsupported``，
前端据此提示「此文档含暂不支持的特性，请用系统应用查看原样」——宁可说实话，
也不要渲染出一个看着像那么回事、其实丢了东西的版本。

**为什么自己解析 XML 而不用 python-docx**：python-docx 覆盖的是「文档对象模型」
（段落、表格、样式名），拿不到样式继承链的**解析后值**（w:ind/w:spacing 的
有效值）、编号规则、页面设置；而这些正是渲染要的东西。python-docx 底层也是
lxml，我们直接用 lxml 读原始 XML，反而更短。
"""
from __future__ import annotations

import base64
import re
import zipfile
from io import BytesIO
from typing import Any, Optional

from lxml import etree

from .ir import (
    Border,
    Document,
    Image,
    Numbering,
    Page,
    Paragraph,
    Run,
    Table,
    TableCell,
    Tab,
    emu_to_pt,
    half_pt_to_pt,
    twips_to_pt,
)

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "v": "urn:schemas-microsoft-com:vml",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}


def _q(tag: str) -> str:
    """'w:p' → '{namespace}p'"""
    pfx, _, local = tag.partition(":")
    return "{%s}%s" % (NS[pfx], local)


def _attr(el: Optional[etree._Element], name: str) -> Optional[str]:
    if el is None:
        return None
    return el.get(_q(name))


def _int(el: Optional[etree._Element], name: str) -> Optional[int]:
    v = _attr(el, name)
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _int_plain(el: Optional[etree._Element], name: str) -> Optional[int]:
    """读**无命名空间**属性（drawingml 的 cx/cy 就是这样的裸属性）。"""
    if el is None:
        return None
    v = el.get(name)
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _on(el: Optional[etree._Element]) -> bool:
    """<w:b/> → True；<w:b w:val="0"/> → False。"""
    if el is None:
        return False
    return _attr(el, "w:val") not in ("0", "false", "off")


# 单图超过这个大小就不内联（base64 会让 IR 体积爆炸，收益却很小）
_MAX_IMAGE_BYTES = 4 * 1024 * 1024
# 整个文档内联图片总量上限
_MAX_TOTAL_IMAGE_BYTES = 16 * 1024 * 1024

# Word 用符号字体（Symbol/Wingdings）的私有区码位表示项目符号，
# 这里映射到通用字符，避免前端拿到不可见字符。
_BULLET_MAP = {
    "": "•", "·": "•", "o": "◦", "": "•", "": "▪",
    "": "◦", "": "–", "": "➢", "": "▪",
}

_MIME_BY_EXT = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "bmp": "image/bmp", "svg": "image/svg+xml",
    "webp": "image/webp", "tiff": "image/tiff", "emf": "image/emf",
    "wmf": "image/wmf",
}

# 边框的四个边 + 两条内部线（w:tblBorders 用 insideH/insideV）
_BORDER_SIDES = ("top", "left", "bottom", "right", "insideH", "insideV")


class _Extractor:
    def __init__(self, data: bytes):
        self.data = data
        self.zf = zipfile.ZipFile(BytesIO(data))
        self.styles: dict[str, dict[str, Any]] = {}
        self.style_order: list[str] = []
        self.default_ppr: dict[str, Any] = {}
        self.default_rpr: dict[str, Any] = {}
        self.numbering: dict[int, dict[str, Any]] = {}
        self.abstracts: dict[int, dict[int, dict[str, Any]]] = {}
        self.rels: dict[str, dict[str, str]] = {}
        self.counters: dict[tuple[int, int], int] = {}
        self.unsupported: list[str] = []
        self.image_budget = _MAX_TOTAL_IMAGE_BYTES
        # 表格样式（w:tblStyle）的边框解析结果缓存
        self._style_tbl_borders: dict[str, dict[str, Border]] = {}
        # 文本框里的段落：跟在**当前容器**的段落之后输出（浮动定位第一版不做）。
        # 用栈而不是单个列表：抽取是递归的（文本框里还能有段落），单个列表会在
        # 内层被重新绑定，内容凭空消失（实测简历抽不出一个字）。
        self._pending_stack: list[list[dict[str, Any]]] = []

    # ── 载入各部件 ──────────────────────────────────────────────────────────
    def load(self) -> None:
        self._load_styles()
        self._load_numbering()
        self._load_rels()

    def _xml(self, name: str) -> Optional[etree._Element]:
        try:
            raw = self.zf.read(name)
        except KeyError:
            return None
        try:
            return etree.fromstring(raw)
        except etree.XMLSyntaxError:
            return None

    def _load_styles(self) -> None:
        root = self._xml("word/styles.xml")
        if root is None:
            return
        dd = root.find("w:docDefaults", NS)
        if dd is not None:
            pd = dd.find("w:pPrDefault/w:pPr", NS)
            if pd is not None:
                self.default_ppr = self._parse_ppr(pd)
            rd = dd.find("w:rPrDefault/w:rPr", NS)
            if rd is not None:
                self.default_rpr = self._parse_rpr(rd)
        for st in root.findall("w:style", NS):
            sid = _attr(st, "w:styleId")
            if not sid:
                continue
            self.styles[sid] = {
                "name": _attr(st.find("w:name", NS), "w:val") or sid,
                "basedOn": _attr(st.find("w:basedOn", NS), "w:val"),
                "pPr": self._parse_ppr(st.find("w:pPr", NS)),
                "rPr": self._parse_rpr(st.find("w:rPr", NS)),
                # 表格样式里定义的边框（"Table Grid"/"网格型" 靠它才有框线）
                "tblBorders": self._parse_borders(st.find("w:tblPr/w:tblBorders", NS)),
            }
            self.style_order.append(sid)

    def _load_numbering(self) -> None:
        root = self._xml("word/numbering.xml")
        if root is None:
            return
        for an in root.findall("w:abstractNum", NS):
            aid = _int(an, "w:abstractNumId")
            if aid is None:
                continue
            lvls: dict[int, dict[str, Any]] = {}
            for lvl in an.findall("w:lvl", NS):
                ilvl = _int(lvl, "w:ilvl")
                if ilvl is None:
                    continue
                lvls[ilvl] = {
                    "fmt": _attr(lvl.find("w:numFmt", NS), "w:val") or "decimal",
                    "text": _attr(lvl.find("w:lvlText", NS), "w:val") or "",
                    "start": _int(lvl.find("w:start", NS), "w:val") or 1,
                }
            self.abstracts[aid] = lvls
        for num in root.findall("w:num", NS):
            nid = _int(num, "w:numId")
            if nid is None:
                continue
            aid = _int(num.find("w:abstractNumId", NS), "w:val")
            overrides: dict[int, dict[str, Any]] = {}
            for ov in num.findall("w:lvlOverride", NS):
                ilvl = _int(ov, "w:ilvl")
                if ilvl is None:
                    continue
                ovl = ov.find("w:lvl", NS)
                if ovl is None:
                    continue
                overrides[ilvl] = {
                    "fmt": _attr(ovl.find("w:numFmt", NS), "w:val") or "decimal",
                    "text": _attr(ovl.find("w:lvlText", NS), "w:val") or "",
                    "start": _int(ovl.find("w:start", NS), "w:val") or 1,
                }
            self.numbering[nid] = {"abstract": aid, "overrides": overrides}

    def _load_rels(self) -> None:
        root = self._xml("word/_rels/document.xml.rels")
        if root is None:
            return
        for rel in root:
            rid = rel.get("Id")
            if not rid:
                continue
            self.rels[rid] = {
                "target": rel.get("Target") or "",
                "type": rel.get("Type") or "",
                "mode": rel.get("TargetMode") or "Internal",
            }

    # ── 属性解析 ────────────────────────────────────────────────────────────
    def _parse_ppr(self, ppr: Optional[etree._Element]) -> dict[str, Any]:
        if ppr is None:
            return {}
        out: dict[str, Any] = {}
        jc = _attr(ppr.find("w:jc", NS), "w:val")
        if jc:
            out["align"] = jc
        ind = ppr.find("w:ind", NS)
        if ind is not None:
            left = _int(ind, "w:left")
            if left is None:
                left = _int(ind, "w:start")
            right = _int(ind, "w:right")
            if right is None:
                right = _int(ind, "w:end")
            first = _int(ind, "w:firstLine")
            hanging = _int(ind, "w:hanging")
            if left is not None:
                out["indent_left"] = twips_to_pt(left)
            if right is not None:
                out["indent_right"] = twips_to_pt(right)
            if hanging is not None:
                out["indent_first_line"] = -(twips_to_pt(hanging) or 0.0)
            elif first is not None:
                out["indent_first_line"] = twips_to_pt(first)
        sp = ppr.find("w:spacing", NS)
        if sp is not None:
            before = _int(sp, "w:before")
            after = _int(sp, "w:after")
            line = _int(sp, "w:line")
            rule = _attr(sp, "w:lineRule") or "auto"
            if before is not None:
                out["space_before"] = twips_to_pt(before)
            if after is not None:
                out["space_after"] = twips_to_pt(after)
            if line is not None:
                if rule == "auto":
                    out["line_height"] = round(line / 240.0, 3)  # 240 = 1 倍
                else:
                    out["line_height"] = twips_to_pt(line)
                    out["line_rule"] = rule
        lvl = _int(ppr.find("w:outlineLvl", NS), "w:val")
        if lvl is not None:
            out["outline_level"] = lvl
        tabs = self._parse_tabs(ppr)
        if tabs:
            out["tabs"] = tabs
        return out

    def _parse_rpr(self, rpr: Optional[etree._Element]) -> dict[str, Any]:
        if rpr is None:
            return {}
        out: dict[str, Any] = {}
        b = rpr.find("w:b", NS)
        if b is not None:
            out["bold"] = _on(b)
        i = rpr.find("w:i", NS)
        if i is not None:
            out["italic"] = _on(i)
        strike = rpr.find("w:strike", NS)
        if strike is not None:
            out["strike"] = _on(strike)
        u = rpr.find("w:u", NS)
        if u is not None:
            out["underline"] = _attr(u, "w:val") not in (None, "none")
        sz = _int(rpr.find("w:sz", NS), "w:val")
        if sz is not None:
            out["size"] = half_pt_to_pt(sz)
        rf = rpr.find("w:rFonts", NS)
        if rf is not None:
            f = _attr(rf, "w:ascii")
            ea = _attr(rf, "w:eastAsia")
            if f:
                out["font"] = f
            if ea:
                out["font_east_asia"] = ea
        color = _attr(rpr.find("w:color", NS), "w:val")
        if color and color != "auto":
            out["color"] = "#" + color[-6:]
        hl = _attr(rpr.find("w:highlight", NS), "w:val")
        if hl and hl != "none":
            out["highlight"] = _highlight_color(hl)
        va = _attr(rpr.find("w:vertAlign", NS), "w:val")
        if va == "superscript":
            out["vert_align"] = 1
        elif va == "subscript":
            out["vert_align"] = 2
        return out

    # ── 边框 / 制表位 ──────────────────────────────────────────────────────
    def _parse_borders(self, el: Optional[etree._Element]) -> dict[str, Border]:
        """w:tblBorders / w:tcBorders → {边: Border}。

        OOXML 里 w:sz 是 **1/8 磅**（sz=4 → 0.5pt），这里换算成磅。
        """
        if el is None:
            return {}
        out: dict[str, Border] = {}
        for side in _BORDER_SIDES:
            b = el.find("w:" + side, NS)
            if b is None:
                continue
            val = _attr(b, "w:val")
            sz = _int(b, "w:sz")
            color = _attr(b, "w:color")
            out[side] = Border(
                style=val or "single",
                size=(round(sz / 8.0, 2) if sz is not None else None),
                color=("#" + color[-6:] if color and color != "auto" else None),
            )
        return out

    def _parse_tabs(self, ppr: Optional[etree._Element]) -> list[Tab]:
        """w:tabs → 制表位列表（按位置升序）。

        w:val="clear" 是「清除此处默认制表位」，不是真正的制表位，跳过。
        """
        if ppr is None:
            return []
        tabs_el = ppr.find("w:tabs", NS)
        if tabs_el is None:
            return []
        out: list[Tab] = []
        for t in tabs_el.findall("w:tab", NS):
            pos = _int(t, "w:pos")
            if pos is None:
                continue
            val = _attr(t, "w:val") or "left"
            if val == "clear":
                continue
            out.append(Tab(
                pos=twips_to_pt(pos),
                val=val,
                leader=_attr(t, "w:leader") or "none",
            ))
        out.sort(key=lambda x: x.pos or 0.0)
        return out

    def _style_table_borders(self, style_id: Optional[str]) -> dict[str, Border]:
        """表格样式（w:tblStyle）链上的边框（父样式 → 子样式逐边覆盖）。"""
        if not style_id:
            return {}
        cached = self._style_tbl_borders.get(style_id)
        if cached is not None:
            return cached
        chain: list[dict[str, Any]] = []
        sid: Optional[str] = style_id
        seen: set[str] = set()
        while sid and sid in self.styles and sid not in seen:
            seen.add(sid)
            st = self.styles[sid]
            chain.append(st)
            sid = st.get("basedOn")
        out: dict[str, Border] = {}
        for st in reversed(chain):
            out.update(st.get("tblBorders") or {})
        self._style_tbl_borders[style_id] = out
        return out

    # ── 样式继承 ────────────────────────────────────────────────────────────
    def resolve_style(self, style_id: Optional[str]) -> tuple[dict[str, Any], dict[str, Any]]:
        """返回 (段落属性, 文字属性)，已把 basedOn 链与 docDefaults 合并进去。"""
        chain: list[dict[str, Any]] = []
        sid = style_id
        seen: set[str] = set()
        while sid and sid in self.styles and sid not in seen:
            seen.add(sid)
            st = self.styles[sid]
            chain.append(st)
            sid = st.get("basedOn")
        ppr: dict[str, Any] = dict(self.default_ppr)
        rpr: dict[str, Any] = dict(self.default_rpr)
        for st in reversed(chain):  # 根 → 叶，越靠近段落的越优先
            ppr.update(st["pPr"])
            rpr.update(st["rPr"])
        return ppr, rpr

    # ── 正文遍历 ────────────────────────────────────────────────────────────
    def extract(self) -> Document:
        doc = Document()
        root = self._xml("word/document.xml")
        if root is None:
            doc.unsupported.append("无法解析 document.xml（文件可能已损坏）")
            return doc
        body = root.find("w:body", NS)
        if body is None:
            body = root
        sect = body.find("w:sectPr", NS)
        if sect is not None:
            doc.page = self._page(sect)
        blocks: list[dict[str, Any]] = self._child_blocks(body)
        doc.blocks = blocks
        doc.styles = {sid: self.styles[sid]["name"] for sid in self.style_order}
        doc.unsupported = sorted(set(self.unsupported))
        doc.paragraph_count = sum(1 for b in blocks if b.get("type") == "paragraph")
        return doc

    def _child_blocks(self, container: etree._Element) -> list[dict[str, Any]]:
        """容器**子元素**里的块级元素（段落 / 表格）。

        必须**递归**：Word 把大量内容包在内容控件（w:sdt，可层层嵌套）、
        兼容性分支（mc:AlternateContent）与**文本框**（w:txbxContent）里——
        只认直接子元素会丢掉整份简历的大部分文字（实测一份简历 144 段
        只能抽到 14 段，且段内文字全在文本框中）。

        AlternateContent 的 Choice 与 Fallback 是**同一份内容的两种表示**
        （前者给新渲染器、后者给老渲染器），只能取一份，否则内容会重复。
        """
        out: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        self._pending_stack.append(pending)
        try:
            for el in container:
                tag = etree.QName(el).localname
                if tag == "p":
                    out.append(self._paragraph(el))
                    # 该段落里的文本框内容紧跟其后
                    if pending:
                        out.extend(pending)
                        pending.clear()
                elif tag == "tbl":
                    out.append(self._table(el))
                elif tag == "sdt":
                    content = el.find("w:sdtContent", NS)
                    if content is not None:
                        out.extend(self._child_blocks(content))
                elif tag == "AlternateContent":
                    branch = el.find("mc:Choice", NS)
                    if branch is None:
                        branch = el.find("mc:Fallback", NS)
                    if branch is not None:
                        out.extend(self._child_blocks(branch))
        finally:
            self._pending_stack.pop()
        if pending:
            out.extend(pending)
        return out

    def _page(self, sect: etree._Element) -> Page:
        page = Page()
        sz = sect.find("w:pgSz", NS)
        if sz is not None:
            w = twips_to_pt(_int(sz, "w:w"))
            h = twips_to_pt(_int(sz, "w:h"))
            if w:
                page.width = w
            if h:
                page.height = h
        mar = sect.find("w:pgMar", NS)
        if mar is not None:
            page.margin_top = twips_to_pt(_int(mar, "w:top")) or page.margin_top
            page.margin_right = twips_to_pt(_int(mar, "w:right")) or page.margin_right
            page.margin_bottom = twips_to_pt(_int(mar, "w:bottom")) or page.margin_bottom
            page.margin_left = twips_to_pt(_int(mar, "w:left")) or page.margin_left
        return page

    # ── 段落 ────────────────────────────────────────────────────────────────
    def _paragraph(self, p: etree._Element) -> dict[str, Any]:
        ppr_el = p.find("w:pPr", NS)
        style_id = _attr(ppr_el.find("w:pStyle", NS), "w:val") if ppr_el is not None else None
        style_ppr, style_rpr = self.resolve_style(style_id)
        eff = dict(style_ppr)
        eff.update(self._parse_ppr(ppr_el))

        para = Paragraph()
        para.style_id = style_id
        if style_id and style_id in self.styles:
            para.style_name = self.styles[style_id]["name"]
        if eff.get("outline_level") is not None:
            para.outline_level = int(eff["outline_level"])
        elif style_id and style_id.lower().startswith("heading"):
            m = re.search(r"(\d+)$", style_id)
            if m:
                para.outline_level = max(0, int(m.group(1)) - 1)
        para.align = eff.get("align")
        para.indent_left = eff.get("indent_left")
        para.indent_right = eff.get("indent_right")
        para.indent_first_line = eff.get("indent_first_line")
        para.space_before = eff.get("space_before")
        para.space_after = eff.get("space_after")
        para.line_height = eff.get("line_height")
        tabs = eff.get("tabs")
        if tabs:
            para.tabs = tabs

        images: list[Image] = []
        para.runs = self._runs(p, style_rpr, images)
        para.images = images

        num = self._numbering(ppr_el)
        if num is not None:
            para.numbering = num
        return {"type": "paragraph", **para.__dict__}

    def _numbering(self, ppr_el: Optional[etree._Element]) -> Optional[Numbering]:
        if ppr_el is None:
            return None
        numpr = ppr_el.find("w:numPr", NS)
        if numpr is None:
            return None
        nid = _int(numpr.find("w:numId", NS), "w:val")
        ilvl = _int(numpr.find("w:ilvl", NS), "w:val") or 0
        if nid is None or nid == 0:
            return None
        numinfo = self.numbering.get(nid)
        if numinfo is None:
            return None
        props = self._level_props(numinfo, ilvl)
        return Numbering(level=ilvl, marker=self._marker(nid, ilvl, props), num_id=nid)

    def _level_props(self, numinfo: dict[str, Any], ilvl: int) -> dict[str, Any]:
        ov = numinfo.get("overrides", {}).get(ilvl)
        if ov:
            return ov
        lvls = self.abstracts.get(numinfo.get("abstract") or -1, {})
        return lvls.get(ilvl, {})

    def _marker(self, num_id: int, ilvl: int, props: dict[str, Any]) -> str:
        """算编号标记文本（"1." / "1.2" / "•"）。计数在遍历全文时累计。"""
        fmt = props.get("fmt") or "decimal"
        text = props.get("text") or ""
        if fmt == "bullet":
            return _BULLET_MAP.get(text, text if text and "%" not in text else "•")
        self.counters[(num_id, ilvl)] = self.counters.get((num_id, ilvl), 0) + 1
        for (n, lv) in list(self.counters.keys()):
            if n == num_id and lv > ilvl:
                del self.counters[(n, lv)]
        if not text:
            return f"{self.counters[(num_id, ilvl)]}."
        if text.startswith("%1") and text.count("%") == 1:
            return text.replace("%1", str(self.counters[(num_id, ilvl)]))

        def repl(m: "re.Match[str]") -> str:
            lv = int(m.group(1)) - 1
            return str(self.counters.get((num_id, lv), 1))

        return re.sub(r"%(\d)", repl, text)

    # ── run ─────────────────────────────────────────────────────────────────
    def _runs(self, p: etree._Element, style_rpr: dict[str, Any],
              images: list[Image]) -> list[Run]:
        runs: list[Run] = []
        for child in p:
            tag = etree.QName(child).localname
            if tag == "r":
                runs.extend(self._run(child, style_rpr, None, images))
            elif tag == "hyperlink":
                rid = _attr(child, "r:id")
                anchor = _attr(child, "w:anchor")
                url = None
                if rid and rid in self.rels:
                    url = self.rels[rid].get("target")
                elif anchor:
                    url = "#" + anchor
                for r in child.findall("w:r", NS):
                    runs.extend(self._run(r, style_rpr, url, images))
            elif tag == "ins":  # 修订插入：内容保留
                for r in child.findall("w:r", NS):
                    runs.extend(self._run(r, style_rpr, None, images))
            elif tag == "del":  # 修订删除：正文里看不到
                continue
            elif tag == "AlternateContent":
                # 兼容性分支：Choice 与 Fallback 是同一份内容，只取一份
                branch = child.find("mc:Choice", NS)
                if branch is None:
                    branch = child.find("mc:Fallback", NS)
                if branch is not None:
                    runs.extend(self._runs(branch, style_rpr, images))
            elif tag in ("commentRangeStart", "commentRangeEnd"):
                self.unsupported.append("批注")
        return runs

    def _run(self, r: etree._Element, style_rpr: dict[str, Any],
             link: Optional[str], images: list[Image]) -> list[Run]:
        eff = dict(style_rpr)
        eff.update(self._parse_rpr(r.find("w:rPr", NS)))
        out: list[Run] = []
        for child in r:
            tag = etree.QName(child).localname
            if tag == "t":
                out.append(Run(text=child.text or "", link=link, **eff))
            elif tag == "br":
                out.append(Run(text="", line_break=True, link=link, **eff))
            elif tag == "tab":
                out.append(Run(text="", tab=True, link=link, **eff))
            elif tag in ("drawing", "pict", "object"):
                self._absorb_drawing(child, images)
            elif tag == "AlternateContent":
                # 兼容性分支（Choice 给新渲染器 / Fallback 给老渲染器，
                # 是同一份内容）：只取一份，否则内容重复。
                branch = child.find("mc:Choice", NS)
                if branch is None:
                    branch = child.find("mc:Fallback", NS)
                if branch is not None:
                    for sub in branch:
                        if etree.QName(sub).localname in ("drawing", "pict", "object"):
                            self._absorb_drawing(sub, images)
        return out

    def _absorb_drawing(self, el: etree._Element, images: list[Image]) -> None:
        """处理一个 drawing / pict：取图片，并把文本框里的段落收进块流。"""
        img = self._image(el)
        if img is not None:
            images.append(img)
        # 文本框（w:txbxContent）：Word 里的另一种"段落容器"，
        # 简历/封面这类排版把正文全放里面——不抽就一个字都读不到。
        if not self._pending_stack:
            return
        pending = self._pending_stack[-1]
        for tb in el.findall(".//w:txbxContent", NS):
            pending.extend(self._child_blocks(tb))

    # ── 图片 ────────────────────────────────────────────────────────────────
    def _image(self, el: etree._Element) -> Optional[Image]:
        tag = etree.QName(el).localname
        extent = None
        if tag == "drawing":
            inline = el.find("wp:inline", NS)
            anchor = el.find("wp:anchor", NS)
            if anchor is not None:
                self.unsupported.append("浮动定位的图片")
            holder = inline if inline is not None else anchor
            if holder is None:
                return None
            extent = holder.find("wp:extent", NS)
            blip = holder.find(".//a:blip", NS)
            rid = _attr(blip, "r:embed") if blip is not None else None
        else:  # w:pict（VML 老格式）
            data = el.find(".//v:imagedata", NS)
            rid = _attr(data, "r:id") if data is not None else None
        if not rid or rid not in self.rels:
            return None
        rel = self.rels[rid]
        if rel.get("mode") == "External":
            return None
        target = rel.get("target") or ""
        name = target if target.startswith("word/") else "word/" + target.lstrip("/")
        name = re.sub(r"^\.\./", "", name)
        try:
            raw = self.zf.read(name)
        except KeyError:
            return None
        if len(raw) > _MAX_IMAGE_BYTES or self.image_budget < len(raw):
            self.unsupported.append("过大的图片（已跳过内联）")
            return None
        self.image_budget -= len(raw)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else "png"
        mime = _MIME_BY_EXT.get(ext, "application/octet-stream")
        b64 = base64.b64encode(raw).decode("ascii")
        img = Image(data_uri=f"data:{mime};base64,{b64}")
        if extent is not None:
            img.width = emu_to_pt(_int_plain(extent, "cx"))
            img.height = emu_to_pt(_int_plain(extent, "cy"))
        return img

    # ── 表格 ────────────────────────────────────────────────────────────────
    def _table(self, tbl: etree._Element) -> dict[str, Any]:
        widths: list[float] = []
        grid = tbl.find("w:tblGrid", NS)
        if grid is not None:
            for gc in grid.findall("w:gridCol", NS):
                widths.append(twips_to_pt(_int(gc, "w:w")) or 0.0)

        tblpr = tbl.find("w:tblPr", NS)
        style_id = _attr(tblpr.find("w:tblStyle", NS), "w:val") if tblpr is not None else None
        # 表格级边框：样式链上的默认 ← 表格自身的 w:tblBorders（逐边覆盖）
        table_borders = self._style_table_borders(style_id)
        if tblpr is not None:
            table_borders.update(self._parse_borders(tblpr.find("w:tblBorders", NS)))

        rows: list[list[TableCell]] = []
        trs = tbl.findall("w:tr", NS)
        n_rows = len(trs)
        for ri, tr in enumerate(trs):
            cells: list[TableCell] = []
            col_cursor = 0
            tcs = tr.findall("w:tc", NS)
            for ci, tc in enumerate(tcs):
                tcpr = tc.find("w:tcPr", NS)
                cell = TableCell()
                cell_borders: dict[str, Border] = {}
                if tcpr is not None:
                    gs = _int(tcpr.find("w:gridSpan", NS), "w:val")
                    if gs:
                        cell.colspan = gs
                    vm = tcpr.find("w:vMerge", NS)
                    if vm is not None:
                        # restart = 起始行（占 1 行）；其余为延续（渲染时跳过）
                        cell.rowspan = 1 if _attr(vm, "w:val") == "restart" else 0
                    shd = _attr(tcpr.find("w:shd", NS), "w:fill")
                    if shd and shd != "auto":
                        cell.shading = "#" + shd[-6:]
                    va = _attr(tcpr.find("w:vAlign", NS), "w:val")
                    if va:
                        cell.valign = va
                    tcw = tcpr.find("w:tcW", NS)
                    if tcw is not None:
                        cell.width = twips_to_pt(_int(tcw, "w:w"))
                    cell_borders = self._parse_borders(tcpr.find("w:tcBorders", NS))

                # 单元格有效边框 = 表格级（外边/内线按位置取）← 单元格自身覆盖
                eff: dict[str, Border] = {}

                def _pick(cell_side: str, table_side: str) -> None:
                    """把表格级的某条边放到单元格的某条边上。

                    表格用 insideH/insideV 表示内部线；对单元格来说，
                    内部线就是它的下边/右边——所以这里做一次换名。
                    """
                    b = table_borders.get(table_side)
                    if b is not None:
                        eff[cell_side] = b

                _pick("top", "top" if ri == 0 else "insideH")
                _pick("bottom", "bottom" if ri == n_rows - 1 else "insideH")
                _pick("left", "left" if col_cursor == 0 else "insideV")
                _pick("right", "right" if ci == len(tcs) - 1 else "insideV")
                eff.update(cell_borders)
                if eff:
                    cell.borders = eff

                blocks: list[dict[str, Any]] = []
                blocks.extend(self._child_blocks(tc))
                cell.blocks = blocks
                cells.append(cell)
                col_cursor += max(1, cell.colspan)
            rows.append(cells)
        t = Table(rows=rows, col_widths=widths)
        if table_borders:
            t.borders = table_borders
        return {"type": "table", **t.__dict__}


def _highlight_color(name: str) -> str:
    """w:highlight 的枚举名 → #RRGGBB。"""
    table = {
        "yellow": "#FFFF00", "green": "#00FF00", "cyan": "#00FFFF",
        "magenta": "#FF00FF", "blue": "#0000FF", "red": "#FF0000",
        "darkBlue": "#000080", "darkCyan": "#008080", "darkGreen": "#008000",
        "darkMagenta": "#800080", "darkRed": "#800000", "darkYellow": "#808000",
        "darkGray": "#808080", "lightGray": "#C0C0C0", "black": "#000000",
        "white": "#FFFFFF",
    }
    return table.get(name, "#FFFF00")


def extract_docx_ir(data: bytes) -> Document:
    """docx 字节 → IR。文件损坏时抛异常（调用方转成 4xx/5xx）。"""
    ex = _Extractor(data)
    ex.load()
    return ex.extract()
