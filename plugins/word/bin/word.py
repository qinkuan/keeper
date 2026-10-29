#!/usr/bin/env python3
"""word 插件：生成 .docx（报告 / 合同 / 方案级排版）与回读文档结构。

协议（keeper 的 bin 组件）：stdin 一行 JSON
    {"tool": "create|read", "args": {...}, "context": {...}}
stdout 最后一行 JSON
    {"ok": true, "result": ...} 或 {"ok": false, "error": ...}
业务级问题（越界 / 只读 / 缺参 / 文件已存在）走 ok:true + result；意外异常才 ok:false。

**为什么不用「给模型一段 Python 代码让它自己写文件」**：
模型写 docx 的代码，成功率取决于它对 python-docx API 的记忆 —— 样式名、单元格
合并、域代码这些细节错一个，产出就是一份排版错乱的文档，而且模型看不见自己写
出来的东西。这里改成**声明式**：模型只描述结构（标题、段落、表格、图片…），
排版由本插件统一负责，产出可预期。

依赖 python-docx（纯 Python + lxml）。打包形态下由 kernel bundle 提供
（packaging/keeper-kernel.spec 收了 docx 与 lxml）——插件 bin 用的是 keeper
内置解释器（KEEPER_PYTHON），它的 sys.path 只能看到 bundle 里的东西，所以
依赖必须在 bundle 里，不能指望插件目录自带。

文件名带 .py 是**必须的**：内核在 Windows 上只对 .py/.pyw 后缀的 bin 用解释器
启动（tools/subprocess_tool.py 的 build_argv），无后缀的脚本在 Windows 上会被
直接 CreateProcess 而失败。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 上限：防止模型一次塞爆内存 / 产出没法看的巨���
MAX_BLOCKS = 500
MAX_TABLE_ROWS = 2000
MAX_TABLE_COLS = 60
MAX_CELL_CHARS = 4000
MAX_READ_CHARS = 30000
MAX_TEXT_CHARS = 200000
# 回写（edit）的限制：一次最多多少个操作、单段最多多少字
MAX_EDIT_OPS = 200
MAX_EDIT_CHARS = 20000

# 默认字体：西文 Calibri，中文微软雅黑（Windows 自带；mac 上装了 Office 也有）
DEFAULT_ASCII_FONT = "Calibri"
DEFAULT_EA_FONT = "Microsoft YaHei"

# --------------------------------------------------------------------------
# 版式预设
# --------------------------------------------------------------------------
#
# 为什么要有预设：同一份结构描述（标题/段落/列表…），排出来给人的气质差很远——
# 公文要三号仿宋、固定行距 28 磅、首行缩进两字符；合同要小四宋体、标题居中；
# 技术文档不要首行缩进、行距紧一点。这些数字模型记不住也不该记，
# 所以把"一整套排版规范"命名成 preset，模型只说"按合同版式"。
#
# **预设只提供默认值**：cfg 里显式写了 font_ea / page / margin_cm / toc.enabled，
# 一律以显式值为准——预设是兜底，不是强制。
#
# 数值参考：
# - 公文：GB/T 9704《党政机关公文格式》（版心 156×225mm、三号仿宋、每页 22 行）
# - 合同/报告：小四宋体、1.5 倍行距、首行缩进两字符
# - 技术文档：无首行缩进（用空行分段）、行距 1.4、等宽代码块
PRESETS: dict[str, dict] = {
    "default": {
        "label": "默认",
        "fonts": {"ascii": DEFAULT_ASCII_FONT, "ea": DEFAULT_EA_FONT},
        "body": {"size": 12, "line": 1.5, "space_after": 6, "indent_cm": 0, "align": "left"},
        "headings": {
            "1": {"size": 18, "bold": True},
            "2": {"size": 16, "bold": True},
            "3": {"size": 14, "bold": True},
            "4": {"size": 12, "bold": True},
        },
        "title": {"size": 22},
        "toc": False,
    },
    "gongwen": {
        "label": "公文",
        "fonts": {"ascii": "Times New Roman", "ea": "仿宋"},
        "page": {
            "size": "A4",
            "margin_cm": {"top": 3.7, "bottom": 3.5, "left": 2.8, "right": 2.6},
        },
        # 三号（16pt）仿宋；固定行距 28 磅（公文每页 22 行的由来）
        "body": {
            "size": 16, "line_pt": 28, "space_after": 0,
            "indent_cm": 1.13, "align": "justify",
        },
        "headings": {
            "1": {"size": 16, "bold": False, "ea": "黑体", "before": 0, "after": 0},
            "2": {"size": 16, "bold": True, "ea": "楷体", "before": 0, "after": 0},
            "3": {"size": 16, "bold": True, "ea": "仿宋", "before": 0, "after": 0},
            "4": {"size": 16, "bold": True, "ea": "仿宋", "before": 0, "after": 0},
        },
        "title": {"size": 16, "ea": "方正小标宋简体", "align": "center"},
        "toc": False,
    },
    "contract": {
        "label": "合同",
        "fonts": {"ascii": "Times New Roman", "ea": "宋体"},
        "page": {"size": "A4", "margin_cm": {"top": 2.54, "bottom": 2.54, "left": 3.17, "right": 3.17}},
        "body": {
            "size": 12, "line": 1.5, "space_after": 6,
            "indent_cm": 0.74, "align": "justify",
        },
        "headings": {
            "1": {"size": 16, "bold": True, "ea": "黑体", "before": 12, "after": 6},
            "2": {"size": 14, "bold": True, "ea": "黑体", "before": 10, "after": 6},
            "3": {"size": 12, "bold": True, "ea": "黑体", "before": 8, "after": 4},
            "4": {"size": 12, "bold": True, "ea": "楷体", "before": 6, "after": 4},
        },
        "title": {"size": 18, "ea": "宋体", "align": "center"},
        "toc": False,
    },
    "report": {
        "label": "报告",
        "fonts": {"ascii": "Times New Roman", "ea": "宋体"},
        "page": {"size": "A4", "margin_cm": {"top": 2.5, "bottom": 2.5, "left": 2.5, "right": 2.5}},
        "body": {
            "size": 12, "line": 1.5, "space_after": 6,
            "indent_cm": 0.74, "align": "justify",
        },
        "headings": {
            "1": {"size": 16, "bold": True, "ea": "黑体", "before": 12, "after": 6},
            "2": {"size": 14, "bold": True, "ea": "黑体", "before": 10, "after": 6},
            "3": {"size": 12, "bold": True, "ea": "黑体", "before": 8, "after": 4},
            "4": {"size": 12, "bold": True, "ea": "楷体", "before": 6, "after": 4},
        },
        "title": {"size": 22, "ea": "黑体", "align": "center"},
        "toc": True,
    },
    "tech": {
        "label": "技术文档",
        "fonts": {"ascii": "Calibri", "ea": "微软雅黑"},
        "page": {"size": "A4", "margin_cm": {"top": 2.5, "bottom": 2.5, "left": 2.5, "right": 2.5}},
        # 技术文档不首行缩进（靠空行分段），行距收紧一点
        "body": {
            "size": 11, "line": 1.4, "space_after": 8,
            "indent_cm": 0, "align": "left",
        },
        "headings": {
            "1": {"size": 18, "bold": True, "ea": "微软雅黑", "before": 16, "after": 8},
            "2": {"size": 15, "bold": True, "ea": "微软雅黑", "before": 12, "after": 6},
            "3": {"size": 13, "bold": True, "ea": "微软雅黑", "before": 10, "after": 4},
            "4": {"size": 11.5, "bold": True, "ea": "微软雅黑", "before": 8, "after": 4},
        },
        "title": {"size": 20, "ea": "微软雅黑", "align": "center"},
        "toc": True,
        "code_size": 10,
    },
}

# 中文名/别名 → 预设 key（模型很可能直接写"按合同版式"）
PRESET_ALIASES = {
    "公文": "gongwen", "党政机关公文": "gongwen", "gongwen": "gongwen",
    "合同": "contract", "协议": "contract", "contract": "contract",
    "报告": "report", "汇报": "report", "report": "report",
    "技术文档": "tech", "技术说明书": "tech", "说明书": "tech",
    "技术": "tech", "tech": "tech", "document": "tech",
    "默认": "default", "default": "default",
}

# 工具描述里给模型看的清单
PRESET_CHOICES = "gongwen（公文）/ contract（合同）/ report（报告）/ tech（技术文档）/ default"

ALIGN_MAP = {
    "left": 0,
    "center": 1,
    "right": 2,
    "justify": 3,
}


class Biz(Exception):
    """业务级问题：作为正常 result 回给模型，不当成崩溃。"""


# --------------------------------------------------------------------------
# 路径解析（与 excel/pdf 插件同口径）
# --------------------------------------------------------------------------
def _resolve(ctx: dict, rel: str) -> Path:
    """把工作区相对路径 / @user / @session 前缀解析成绝对路径，并拦越界。"""
    ws = (ctx.get("workspace") or "").strip()
    ag = (ctx.get("agent_space") or "").strip()
    s = (rel or "").strip()
    if s == "@tmp" or s.startswith("@tmp/") or s.startswith("@session/"):
        base = ag
        if s.startswith("@session/"):
            s = s[len("@session/"):]
        elif s.startswith("@tmp/"):
            s = s[len("@tmp/"):]
        label = "会话工作区"
    else:
        if s.startswith("@user/"):
            s = s[len("@user/"):]
        base = ws
        label = "用户工作区"
    if not base:
        raise Biz(f"{label}不可用（没有工作区上下文）")
    b = Path(base).resolve()
    s = s.lstrip("/")
    if not s:
        raise Biz("空路径")
    p = Path(s)
    target = p.resolve() if p.is_absolute() else (b / s).resolve()
    if not (target == b or b in target.parents):
        raise Biz(f"路径越界（不在{label}内）：{rel}")
    return target


# --------------------------------------------------------------------------
# docx 相关（延迟导入：read 也需要 docx，但缺依赖时要有友好提示）
# --------------------------------------------------------------------------
def _docx():
    try:
        import docx  # noqa: F401
    except Exception as e:  # noqa: BLE001
        raise Biz(
            f"未安装 python-docx：{e}。开发态执行 `pip install python-docx`；"
            "打包形态需在 packaging/keeper-kernel.spec 收 docx 与 lxml。"
        )
    return docx


def _qn(tag: str):
    from docx.oxml.ns import qn

    return qn(tag)


def _el(tag: str, **attrs):
    """造一个 w: 命名空间下的元素。"""
    from docx.oxml import OxmlElement

    e = OxmlElement(tag)
    for k, v in attrs.items():
        e.set(_qn(k), str(v))
    return e


def _set_fonts(rpr, ascii_font: str, ea_font: str) -> None:
    """给 rPr 设字体，**中文必须显式设 eastAsia**。

    只设 style.font.name 的话，Word 会用主题里的「+mn-ea」给中文，中英混排时
    观感割裂（尤其中文用宋体、西文用雅黑）。这里把 ascii/hAnsi/eastAsia 一起钉死。
    """
    rf = rpr.get_or_add_rFonts()
    rf.set(_qn("w:ascii"), ascii_font)
    rf.set(_qn("w:hAnsi"), ascii_font)
    rf.set(_qn("w:eastAsia"), ea_font)
    rf.set(_qn("w:cs"), ascii_font)


def _apply_align(pf, align: str | None) -> None:
    """对齐（Normal / 标题样式上都用得上）。"""
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    m = {
        "left": WD_ALIGN_PARAGRAPH.LEFT,
        "center": WD_ALIGN_PARAGRAPH.CENTER,
        "right": WD_ALIGN_PARAGRAPH.RIGHT,
        "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
    }
    a = (align or "").strip().lower()
    if a in m:
        pf.alignment = m[a]


def _setup_styles(doc, sty: dict) -> None:
    """按版式设置 Normal 与 Heading 1-4。

    正文的一切（字号、行距、段后、首行缩进、对齐）都挂在 Normal 样式上——
    这样每个段落自动继承，不用逐段设；列表/表格里的段落再单独清掉首行缩进。
    """
    styles = doc.styles
    fonts = sty["fonts"]
    body = sty["body"]
    normal = styles["Normal"]
    # 正文字号：之前这里写的是 `size = None`，等于**没设**——正文其实是 Word 默认的
    # 11pt Calibri，跟文档里"小四宋体"的说法对不上。现在按版式明确给字号。
    normal.font.size = _pt(body.get("size", 12))
    _set_fonts(normal.element.get_or_add_rPr(), fonts["ascii"], fonts["ea"])
    pf = normal.paragraph_format
    # 行距：给了磅值就是固定行距（公文的 28 磅），否则是倍数
    pf.line_spacing = _pt(body["line_pt"]) if body.get("line_pt") else body.get("line", 1.5)
    pf.space_after = _pt(body.get("space_after", 6))
    pf.space_before = _pt(body.get("space_before", 0))
    if body.get("indent_cm"):
        pf.first_line_indent = _cm(body["indent_cm"])
    else:
        pf.first_line_indent = None
    _apply_align(pf, body.get("align"))

    # 标题：字号/字体/加粗按版式；去掉默认的蓝色主题色
    for lvl in range(1, 5):
        name = f"Heading {lvl}"
        try:
            st = styles[name]
        except KeyError:
            continue
        h = sty["headings"].get(str(lvl), {})
        st.font.color.rgb = None
        st.font.bold = h.get("bold", True)
        st.font.size = _pt(h.get("size", 12))
        _set_fonts(
            st.element.get_or_add_rPr(),
            h.get("ascii", fonts["ascii"]),
            h.get("ea", fonts["ea"]),
        )
        hpf = st.paragraph_format
        hpf.space_before = _pt(h.get("before", 12))
        hpf.space_after = _pt(h.get("after", 6))
        hpf.line_spacing = body.get("line", 1.5) if not body.get("line_pt") else _pt(body["line_pt"])
        hpf.first_line_indent = None  # 标题不缩进
        _apply_align(hpf, h.get("align"))


def _pt(v):
    from docx.shared import Pt

    return Pt(v)


def _cm(v):
    from docx.shared import Cm

    return Cm(v)


def _shade(el, fill: str) -> None:
    """给 tcPr / pPr 加底纹。"""
    pr = el.get_or_add_tcPr() if hasattr(el, "get_or_add_tcPr") else el.get_or_add_pPr()
    pr.append(_el("w:shd", **{"w:val": "clear", "w:color": "auto", "w:fill": fill}))


def _field(paragraph, instr: str, placeholder: str = "") -> None:
    """往段落里插一个 Word 域（TOC / PAGE / NUMPAGES 都靠它）。

    域是「按需计算」的：Word 打开文档时不会自动算，得按 F9 或右键「更新域」。
    所以域里必须带 placeholder 文字（域未更新时显示这段），并在文档里
    提示使用者更新 —— 否则用户会以为目录是空的。
    """
    r1 = paragraph.add_run()._r
    r1.append(_el("w:fldChar", **{"w:fldCharType": "begin", "w:dirty": "true"}))
    r2 = paragraph.add_run()._r
    it = _el("w:instrText")
    it.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    it.text = f" {instr} "
    r2.append(it)
    r3 = paragraph.add_run()._r
    r3.append(_el("w:fldChar", **{"w:fldCharType": "separate"}))
    if placeholder:
        paragraph.add_run(placeholder)
    r5 = paragraph.add_run()._r
    r5.append(_el("w:fldChar", **{"w:fldCharType": "end"}))


def _bottom_border(paragraph, color: str = "BFBFBF") -> None:
    """给段落加下边框（页眉横线、引用块左边线用）。"""
    ppr = paragraph._p.get_or_add_pPr()
    bdr = _el("w:pBdr")
    bdr.append(_el("w:bottom", **{"w:val": "single", "w:sz": "6", "w:space": "1", "w:color": color}))
    ppr.append(bdr)


def _left_bar(paragraph, color: str = "BFBFBF") -> None:
    ppr = paragraph._p.get_or_add_pPr()
    bdr = _el("w:pBdr")
    bdr.append(_el("w:left", **{"w:val": "single", "w:sz": "18", "w:space": "8", "w:color": color}))
    ppr.append(bdr)


def _no_indent(paragraph) -> None:
    """清掉首行缩进。

    正文的首行缩进来自 Normal 样式（公文/合同/报告要缩进两字符），但列表、
    表格单元格里再缩进就错了——那里一律归零。
    """
    paragraph.paragraph_format.first_line_indent = _cm(0)


def _align(paragraph, align: str) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    a = (align or "").strip().lower()
    if a in ALIGN_MAP:
        paragraph.alignment = WD_ALIGN_PARAGRAPH(ALIGN_MAP[a])


# --------------------------------------------------------------------------
# 生成
# --------------------------------------------------------------------------
def _build(doc, cfg: dict, ctx: dict, sty: dict) -> None:
    """按 cfg（结构描述）+ sty（版式）把文档搭出来。"""
    from docx.shared import Cm

    fonts = sty["fonts"]
    ascii_font, ea_font = fonts["ascii"], fonts["ea"]
    _setup_styles(doc, sty)

    # ---- 页面 ----
    page = cfg.get("page") or {}
    sty_page = sty.get("page") or {}
    sec = doc.sections[0]
    size = (page.get("size") or sty_page.get("size") or "A4").strip().upper()
    if size == "A4":
        sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    elif size == "LETTER":
        sec.page_width, sec.page_height = Cm(21.59), Cm(27.94)
    elif size == "A3":
        sec.page_width, sec.page_height = Cm(29.7), Cm(42.0)
    else:
        raise Biz(f"[page.size 无效] 支持 A4 / LETTER / A3，收到 {size}")
    # 页边距：显式 margin_cm（数字=四边 / 字典=逐边）> 版式预设 > 2.5
    margin = page.get("margin_cm")
    if margin is None:
        margin = sty_page.get("margin_cm")
    if margin is None:
        margin = 2.5
    if isinstance(margin, dict):
        for attr, key in (
            ("left_margin", "left"), ("right_margin", "right"),
            ("top_margin", "top"), ("bottom_margin", "bottom"),
        ):
            if key in margin:
                setattr(sec, attr, Cm(float(margin[key])))
    else:
        for attr in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
            setattr(sec, attr, Cm(float(margin)))

    # ---- 页眉 / 页脚 ----
    header = cfg.get("header") or {}
    if header.get("text"):
        hp = sec.header.paragraphs[0]
        hp.text = ""
        hp.add_run(str(header["text"]))
        _align(hp, header.get("align") or "center")
        for r in hp.runs:
            r.font.size = _pt(9)
            _set_fonts(r._r.get_or_add_rPr(), ascii_font, ea_font)
        if header.get("rule", True):
            _bottom_border(hp)
    footer = cfg.get("footer") or {}
    if footer.get("page_numbers"):
        fp = sec.footer.paragraphs[0]
        fp.text = ""
        _align(fp, footer.get("align") or "center")
        prefix = str(footer.get("prefix") or "")
        if prefix:
            fp.add_run(prefix)
        _field(fp, "PAGE", "1")
        middle = str(footer.get("middle") or "")
        if middle:
            fp.add_run(middle)
        if footer.get("total_pages", True):
            _field(fp, "NUMPAGES", "1")
        for r in fp.runs:
            r.font.size = _pt(9)
            _set_fonts(r._r.get_or_add_rPr(), ascii_font, ea_font)

    # ---- 封面（可选）----
    cover = cfg.get("cover") or {}
    if cover:
        for _ in range(2):
            doc.add_paragraph()
        p = doc.add_paragraph()
        _align(p, "center")
        _no_indent(p)
        r = p.add_run(str(cfg.get("title") or cover.get("title") or ""))
        r.bold = True
        r.font.size = _pt(26)
        _set_fonts(r._r.get_or_add_rPr(), ascii_font, ea_font)
        if cover.get("subtitle"):
            p = doc.add_paragraph()
            _align(p, "center")
            _no_indent(p)
            r = p.add_run(str(cover["subtitle"]))
            r.font.size = _pt(14)
            _set_fonts(r._r.get_or_add_rPr(), ascii_font, ea_font)
        for key, label in (("author", "编制"), ("date", "日期"), ("org", "单位")):
            if cover.get(key):
                p = doc.add_paragraph()
                _align(p, "center")
                _no_indent(p)
                r = p.add_run(f"{label}：{cover[key]}")
                r.font.size = _pt(12)
                _set_fonts(r._r.get_or_add_rPr(), ascii_font, ea_font)
        doc.add_page_break()

    # ---- 标题（无封面时放在正文最前）----
    if not cover and cfg.get("title"):
        t_sty = sty.get("title") or {}
        p = doc.add_paragraph()
        _align(p, cfg.get("title_align") or t_sty.get("align") or "center")
        r = p.add_run(str(cfg["title"]))
        r.bold = True
        r.font.size = _pt(cfg.get("title_size") or t_sty.get("size", 22))
        _set_fonts(r._r.get_or_add_rPr(), ascii_font, t_sty.get("ea", ea_font))
        if cfg.get("subtitle"):
            p = doc.add_paragraph()
            _align(p, "center")
            r = p.add_run(str(cfg["subtitle"]))
            r.font.size = _pt(13)
            _set_fonts(r._r.get_or_add_rPr(), ascii_font, ea_font)
        doc.add_paragraph()

    # ---- 目录 ----
    toc = cfg.get("toc") or {}
    if toc.get("enabled") is None:
        toc = dict(toc)
        toc["enabled"] = bool(sty.get("toc"))
    if toc.get("enabled"):
        if toc.get("title"):
            p = doc.add_paragraph()
            r = p.add_run(str(toc["title"]))
            r.bold = True
            r.font.size = _pt(16)
            _set_fonts(r._r.get_or_add_rPr(), ascii_font, ea_font)
        p = doc.add_paragraph()
        levels = str(toc.get("levels") or "1-3")
        _field(p, f'TOC \\o "{levels}" \\h \\z \\u', "【目录域】在 Word 里按 Ctrl+A 后 F9 更新即可生成")
        doc.add_page_break()

    # ---- 正文块 ----
    blocks = cfg.get("blocks") or []
    if not isinstance(blocks, list):
        raise Biz("[blocks 无效] blocks 必须是数组")
    if len(blocks) > MAX_BLOCKS:
        raise Biz(f"[块太多] 一次最多 {MAX_BLOCKS} 个块，收到 {len(blocks)}")
    for i, b in enumerate(blocks):
        if not isinstance(b, dict):
            raise Biz(f"[blocks[{i}] 无效] 每个块必须是对象")
        _block(doc, b, ctx, ascii_font, ea_font, sty)


def _block(doc, b: dict, ctx: dict, ascii_font: str, ea_font: str, sty: dict | None = None) -> None:
    t = (b.get("type") or "").strip().lower()

    if t in ("heading", "h"):
        lvl = int(b.get("level") or 1)
        if not (1 <= lvl <= 4):
            raise Biz(f"[heading.level 无效] 取 1-4，收到 {lvl}")
        p = doc.add_paragraph(style=f"Heading {lvl}")
        p.add_run(str(b.get("text") or ""))
        if b.get("page_break_before"):
            p.paragraph_format.page_break_before = True
        return

    if t in ("paragraph", "p"):
        p = doc.add_paragraph()
        _align(p, b.get("align") or "left")
        r = p.add_run(str(b.get("text") or ""))
        r.bold = bool(b.get("bold"))
        r.italic = bool(b.get("italic"))
        if b.get("size"):
            r.font.size = _pt(float(b["size"]))
        if b.get("indent"):
            p.paragraph_format.left_indent = _cm(float(b["indent"]))
        if b.get("space_after") is not None:
            p.paragraph_format.space_after = _pt(float(b["space_after"]))
        return

    if t in ("bullets", "bullet", "ul"):
        items = b.get("items") or []
        if not isinstance(items, list):
            raise Biz("[bullets.items 无效] 需要数组")
        for it in items:
            p = doc.add_paragraph(style="List Bullet")
            _align(p, b.get("align") or "left")
            _no_indent(p)
            p.add_run(str(it))
        return

    if t in ("numbers", "numbered", "ol"):
        items = b.get("items") or []
        if not isinstance(items, list):
            raise Biz("[numbers.items 无效] 需要数组")
        for it in items:
            p = doc.add_paragraph(style="List Number")
            _align(p, b.get("align") or "left")
            _no_indent(p)
            p.add_run(str(it))
        return

    if t == "table":
        _table(doc, b)
        return

    if t == "image":
        p = _resolve(ctx, str(b.get("path") or ""))
        if not p.exists():
            raise Biz(f"[图片不存在] {b.get('path')}")
        width = float(b.get("width_cm") or 14.0)
        try:
            doc.add_picture(str(p), width=_cm(width))
        except Exception as e:  # noqa: BLE001
            raise Biz(f"[图片插入失败] {b.get('path')}：{e}")
        if b.get("caption"):
            cp = doc.add_paragraph()
            _align(cp, "center")
            r = cp.add_run(str(b["caption"]))
            r.font.size = _pt(9)
            r.italic = True
            _set_fonts(r._r.get_or_add_rPr(), ascii_font, ea_font)
        return

    if t == "code":
        p = doc.add_paragraph()
        _no_indent(p)
        r = p.add_run(str(b.get("text") or ""))
        code_size = float(b.get("size") or (sty or {}).get("code_size") or 9.5)
        r.font.name = "Consolas"
        r.font.size = _pt(code_size)
        _set_fonts(r._r.get_or_add_rPr(), "Consolas", ea_font)
        _shade(p._p, "F5F5F5")
        return

    if t == "quote":
        p = doc.add_paragraph()
        _align(p, b.get("align") or "left")
        _no_indent(p)
        r = p.add_run(str(b.get("text") or ""))
        r.italic = True
        r.font.color.rgb = _rgb(0x59, 0x59, 0x59)
        _set_fonts(r._r.get_or_add_rPr(), ascii_font, ea_font)
        p.paragraph_format.left_indent = _cm(0.6)
        _left_bar(p)
        return

    if t == "page_break":
        doc.add_page_break()
        return

    if t == "spacer":
        n = int(b.get("count") or 1)
        for _ in range(max(0, min(n, 20))):
            doc.add_paragraph()
        return

    if t == "caption":
        p = doc.add_paragraph()
        _align(p, b.get("align") or "center")
        r = p.add_run(str(b.get("text") or ""))
        r.font.size = _pt(9)
        r.italic = True
        r.font.color.rgb = _rgb(0x59, 0x59, 0x59)
        _set_fonts(r._r.get_or_add_rPr(), ascii_font, ea_font)
        return

    raise Biz(f"[未知块类型] {t}（可用：heading/paragraph/bullets/numbers/table/image/code/quote/page_break/spacer/caption）")


def _rgb(r: int, g: int, b: int):
    from docx.shared import RGBColor

    return RGBColor(r, g, b)


def _table(doc, b: dict) -> None:
    header = b.get("header") or []
    rows = b.get("rows") or []
    if not isinstance(header, list) or not isinstance(rows, list):
        raise Biz("[table 无效] header / rows 必须是数组")
    ncols = len(header) if header else (len(rows[0]) if rows and isinstance(rows[0], list) else 0)
    if ncols == 0:
        raise Biz("[table 无效] 没有列（给 header 或至少一行 rows）")
    if ncols > MAX_TABLE_COLS:
        raise Biz(f"[表格太宽] 最多 {MAX_TABLE_COLS} 列，收到 {ncols}")
    if len(rows) > MAX_TABLE_ROWS:
        raise Biz(f"[表格太长] 最多 {MAX_TABLE_ROWS} 行，收到 {len(rows)}")

    table = doc.add_table(rows=0, cols=ncols)
    try:
        table.style = "Table Grid"
    except KeyError:
        pass
    table.alignment = 1  # center
    # 固定列宽布局：Word 的自动列宽会把中文表格挤得很难看
    table.autofit = False
    tblPr = table._tbl.tblPr
    tblPr.append(_el("w:tblLayout", **{"w:type": "fixed"}))

    widths = b.get("widths_cm")
    if isinstance(widths, list) and len(widths) == ncols:
        for i, w in enumerate(widths):
            table.columns[i].width = _cm(float(w))

    if header:
        cells = table.add_row().cells
        for i, h in enumerate(header):
            cell = cells[i]
            cell.text = ""
            p = cell.paragraphs[0]
            _align(p, b.get("header_align") or "center")
            _no_indent(p)
            r = p.add_run(str(h))
            r.bold = True
            _shade(cell._tc, "F2F2F2")
        # 表头跨页重复
        trPr = table.rows[0]._tr.get_or_add_trPr()
        trPr.append(_el("w:tblHeader"))

    for row in rows:
        if not isinstance(row, list):
            raise Biz("[table.rows 无效] 每行必须是数组")
        cells = table.add_row().cells
        for i in range(ncols):
            val = row[i] if i < len(row) else ""
            cell = cells[i]
            cell.text = ""
            p = cell.paragraphs[0]
            _align(p, b.get("align") or "left")
            _no_indent(p)
            p.add_run(str(val)[:MAX_CELL_CHARS])

    if b.get("caption"):
        cp = doc.add_paragraph()
        _align(cp, "center")
        r = cp.add_run(str(b["caption"]))
        r.font.size = _pt(9)
        r.italic = True
        r.font.color.rgb = _rgb(0x59, 0x59, 0x59)


# --------------------------------------------------------------------------
# 读（回读结构，供模型自检）
# --------------------------------------------------------------------------
def _read(p: Path, max_chars: int) -> str:
    _docx()
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(p))
    out: list[str] = []
    body = doc.element.body
    n_para = n_table = 0
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            para = Paragraph(child, doc)
            text = (para.text or "").strip()
            style = (para.style.name if para.style is not None else "") or ""
            n_para += 1
            if not text:
                continue
            if style.startswith("Heading "):
                try:
                    lvl = int(style.split()[-1])
                except ValueError:
                    lvl = 1
                out.append("#" * max(1, min(lvl, 6)) + " " + text)
            elif style == "List Bullet":
                out.append("- " + text)
            elif style == "List Number":
                out.append("1. " + text)
            else:
                out.append(text)
        elif tag == "tbl":
            tbl = Table(child, doc)
            n_table += 1
            rows = []
            for row in tbl.rows:
                cells = [(c.text or "").strip().replace("\n", " ") for c in row.cells]
                rows.append(cells)
            if not rows:
                continue
            ncols = max(len(r) for r in rows)
            rows = [r + [""] * (ncols - len(r)) for r in rows]
            out.append("| " + " | ".join(rows[0]) + " |")
            out.append("| " + " | ".join(["---"] * ncols) + " |")
            for r in rows[1:]:
                out.append("| " + " | ".join(r) + " |")
            out.append(f"（表 {n_table}：{len(rows) - 1} 行 × {ncols} 列）")

    n_img = len(doc.inline_shapes)
    head = []
    for sec in doc.sections:
        for hp in sec.header.paragraphs:
            if (hp.text or "").strip():
                head.append(hp.text.strip())
        for fp in sec.footer.paragraphs:
            if (fp.text or "").strip():
                head.append("页脚:" + fp.text.strip())
    summary = (
        f"文件 {p.name}｜段落 {n_para}｜表格 {n_table}｜内嵌图片 {n_img}"
        + (f"｜页眉页脚：{' / '.join(head)}" if head else "")
    )
    text = "\n".join(out)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（内容过多已截断）"
    return summary + "\n\n" + (text or "（正文为空）")


# --------------------------------------------------------------------------
# 版式预设的解析
# --------------------------------------------------------------------------
def _resolve_preset(cfg: dict) -> dict:
    """把 cfg.preset 解析成一份版式参数（用户的显式值优先）。

    返回的是**深拷贝**：预设表是模块级常量，不能被就地改掉
    （否则第一次调用改了字体，第二次调用就全乱了）。
    """
    import copy

    raw = str(cfg.get("preset") or "").strip()
    key = PRESET_ALIASES.get(raw.lower(), raw.lower()) if raw else "default"
    base = PRESETS.get(key)
    if base is None:
        raise Biz(
            f"[preset 无效] 可用：{PRESET_CHOICES}；收到 {raw!r}"
        )
    sty = copy.deepcopy(base)

    # 显式值覆盖预设：字体
    fonts = sty["fonts"]
    if cfg.get("font_ascii"):
        fonts["ascii"] = str(cfg["font_ascii"])
    if cfg.get("font_ea"):
        fonts["ea"] = str(cfg["font_ea"])
    # 显式值覆盖预设：标题字号
    if cfg.get("title_size"):
        sty["title"] = dict(sty.get("title") or {})
        sty["title"]["size"] = float(cfg["title_size"])
    return sty


# --------------------------------------------------------------------------
# 改（受限回写：只动段落，**另存副本，绝不覆盖原件**）
# --------------------------------------------------------------------------
#
# 为什么能力要这么窄：
# - docx 是 zip + OOXML，重建一份会丢样式、编号、域、图片、批注……所以这里
#   在**原文件**上做最小修改（python-docx 只重写它认识的部分，其余部件原样保留）。
# - 只支持「段落」这一级：追加一段 / 替换某段的纯文本 / 删掉某段。
#   表格内部、图片、页眉页脚一律不动——动它们的代价（丢格式、丢关系）远大于收益。
# - 输出一律是**新文件**：原件是用户的资产，不许被覆盖。
#
# 段落序号口径：**正文里的第 N 个段落**（1-based，不含表格里的段落、
# 不含文本框里的段落）——与 word__read 读出来的顺序一致，模型能对上号。


def _copy_para_format(src_p, new_p, style: str | None) -> None:
    """把模板段落的 pPr / 首个 run 的 rPr 复制到新段（让新段看起来像原文里的段）。"""
    from copy import deepcopy

    if src_p is not None:
        ppr = src_p._p.find(_qn("w:pPr"))
        if ppr is not None:
            new_p.append(deepcopy(ppr))
    if style:
        ppr = new_p.find(_qn("w:pPr"))
        if ppr is None:
            ppr = _el("w:pPr")
            new_p.insert(0, ppr)
        for old in ppr.findall(_qn("w:pStyle")):
            ppr.remove(old)
        ppr.insert(0, _el("w:pStyle", **{"w:val": style.replace(" ", "")}))


def _make_para(text: str, src_p, style: str | None):
    """造一个新段落（w:p），带模板段落的格式。"""
    from copy import deepcopy

    new_p = _el("w:p")
    _copy_para_format(src_p, new_p, style)
    r = _el("w:r")
    if src_p is not None and src_p.runs:
        rpr = src_p.runs[0]._r.find(_qn("w:rPr"))
        if rpr is not None:
            r.append(deepcopy(rpr))
    t = _el("w:t")
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = text
    r.append(t)
    new_p.append(r)
    return new_p


def _tool_edit(args: dict, ctx: dict) -> str:
    rel = (args.get("path") or "").strip()
    if not rel:
        raise Biz("[缺少 path] 请给出要改的工作区内 .docx 路径")
    p = _resolve(ctx, rel)
    if not p.exists():
        raise Biz(f"[文件不存在] {rel}")
    if bool(ctx.get("read_only")):
        raise Biz("[只读工作区] 当前会话绑定的工作区是只读的，不能写文件")

    ops = args.get("ops") or []
    if not isinstance(ops, list) or not ops:
        raise Biz("[缺少 ops] ops 必填：要做的改动列表（append / replace / delete）")
    if len(ops) > MAX_EDIT_OPS:
        raise Biz(f"[改动太多] 一次最多 {MAX_EDIT_OPS} 个操作，收到 {len(ops)}")

    # 输出路径：默认在原文件名后加 _edited；**永远不是原文件**
    out_rel = (args.get("out_path") or "").strip()
    if not out_rel:
        out_rel = str(Path(rel).with_name(Path(rel).stem + "_edited" + Path(rel).suffix))
    out = _resolve(ctx, out_rel)
    if out.resolve() == p.resolve():
        raise Biz(
            "[拒绝覆盖原件] 回写一律另存副本，不覆盖原文件。"
            f"请把 out_path 设成别的名字（如 {out_rel.replace('.docx', '_v2.docx')}）"
        )
    if out.exists() and not args.get("overwrite"):
        raise Biz(f"[文件已存在] {out_rel}。要覆盖这个副本请加 overwrite:true")

    _docx()
    from docx import Document

    doc = Document(str(p))
    body = doc.element.body
    done: list[str] = []

    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            raise Biz(f"[ops[{i}] 无效] 每个操作必须是对象")
        kind = (op.get("op") or op.get("type") or "").strip().lower()
        paras = doc.paragraphs  # 每次重新取：上一处改动会让序号平移

        if kind == "append":
            text = str(op.get("text") or "")
            if not text:
                raise Biz(f"[ops[{i}] 缺 text] append 必须给 text")
            if len(text) > MAX_EDIT_CHARS:
                raise Biz(f"[ops[{i}] 太长] 单段最多 {MAX_EDIT_CHARS} 字")
            after = op.get("after")
            style = op.get("style")
            if after is None:
                # 追加到正文末尾（sectPr 之前，Word 要求它是最后一个孩子）
                src = paras[-1] if paras else None
                new_p = _make_para(text, src, style)
                sect = body.find(_qn("w:sectPr"))
                if sect is not None:
                    sect.addprevious(new_p)
                else:
                    body.append(new_p)
                done.append(f"在文末追加了 1 段")
            else:
                k = int(after)
                if k < 0 or k > len(paras):
                    raise Biz(f"[ops[{i}] after 越界] after 是第几段（0=最前），收到 {k}，文档共 {len(paras)} 段")
                src = paras[k - 1] if k >= 1 else (paras[0] if paras else None)
                new_p = _make_para(text, src, style)
                if k == 0:
                    first = body.find(_qn("w:p"))
                    if first is not None:
                        first.addprevious(new_p)
                    else:
                        body.insert(0, new_p)
                else:
                    paras[k - 1]._p.addnext(new_p)
                done.append(f"在第 {k} 段后追加了 1 段")
            continue

        if kind == "replace":
            k = int(op.get("index") or op.get("paragraph") or 0)
            if k < 1 or k > len(paras):
                raise Biz(f"[ops[{i}] 段号越界] index 是第几段（1 起），收到 {k}，文档共 {len(paras)} 段")
            text = str(op.get("text") or "")
            if len(text) > MAX_EDIT_CHARS:
                raise Biz(f"[ops[{i}] 太长] 单段最多 {MAX_EDIT_CHARS} 字")
            para = paras[k - 1]
            old = (para.text or "")[:40]
            if para.runs:
                para.runs[0].text = text
                for r in list(para.runs[1:]):
                    r._r.getparent().remove(r._r)
            else:
                para.add_run(text)
            done.append(f"第 {k} 段文本已替换（原：{old!r}）")
            continue

        if kind == "delete":
            k = int(op.get("index") or op.get("paragraph") or 0)
            if k < 1 or k > len(paras):
                raise Biz(f"[ops[{i}] 段号越界] index 是第几段（1 起），收到 {k}，文档共 {len(paras)} 段")
            el = paras[k - 1]._element
            el.getparent().remove(el)
            done.append(f"第 {k} 段已删除")
            continue

        raise Biz(f"[ops[{i}] 未知操作] {kind}（可用：append / replace / delete）")

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    size_kb = out.stat().st_size / 1024
    return (
        f"已改写并另存为 {out_rel}（{size_kb:.1f} KB），原文件 {rel} 未改动。\n"
        + "\n".join(done)
        + f"\n现在共 {len(doc.paragraphs)} 段（不含表格内段落）。"
        + "需要核对内容可用 word__read 回读。"
    )


# --------------------------------------------------------------------------
# 工具入口
# --------------------------------------------------------------------------
def _tool_create(args: dict, ctx: dict) -> str:
    rel = (args.get("path") or "").strip()
    if not rel:
        raise Biz("[缺少 path] 请给出要生成的工作区内 .docx 路径，如 报告/方案.docx")
    if not rel.lower().endswith(".docx"):
        raise Biz(f"[路径后缀不对] 只生成 .docx，收到 {rel}")
    p = _resolve(ctx, rel)
    if p.exists() and not args.get("overwrite"):
        raise Biz(
            f"[文件已存在] {rel}。要覆盖就加 overwrite:true；"
            "不想覆盖就换个文件名（避免把用户已有文档冲掉）"
        )
    if bool(ctx.get("read_only")):
        raise Biz("[只读工作区] 当前会话绑定的工作区是只读的，不能写文件")

    cfg = args.get("doc") or {}
    if not isinstance(cfg, dict):
        raise Biz("[doc 无效] doc 必须是对象（结构描述）")
    # preset 可以写在顶层（args.preset）或 doc 里，两个都认，顶层只是兜底
    if args.get("preset") and not cfg.get("preset"):
        cfg = {**cfg, "preset": args["preset"]}
    blocks = cfg.get("blocks")
    if not blocks:
        raise Biz("[缺少 blocks] doc.blocks 是必填：文档的段落/表格/图片等结构")

    _docx()
    from docx import Document

    doc = Document()
    sty = _resolve_preset(cfg)
    _build(doc, cfg, ctx, sty)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(p))
    size_kb = p.stat().st_size / 1024
    preset_name = str(cfg.get("preset") or "").strip()
    preset_label = PRESETS.get(
        PRESET_ALIASES.get(preset_name.lower(), preset_name.lower()), PRESETS["default"]
    )["label"]
    return (
        f"已生成 {rel}（{size_kb:.1f} KB），版式：{preset_label}。"
        "注意：目录/页码是 Word 域，首次打开需在 Word 里按 Ctrl+A 再按 F9 更新；"
        "需要核对内容可用 word__read 回读。"
    )


def _tool_read(args: dict, ctx: dict) -> str:
    rel = (args.get("path") or "").strip()
    if not rel:
        raise Biz("[缺少 path] 请给出要读的工作区内 .docx 路径")
    p = _resolve(ctx, rel)
    if not p.exists():
        raise Biz(f"[文件不存在] {rel}")
    max_chars = int(args.get("max_chars") or MAX_READ_CHARS)
    return _read(p, max(500, min(max_chars, MAX_READ_CHARS)))


def handle(tool: str, args: dict, ctx: dict) -> str:
    if tool == "create":
        return _tool_create(args, ctx)
    if tool == "read":
        return _tool_read(args, ctx)
    if tool == "edit":
        return _tool_edit(args, ctx)
    raise Biz(f"未知工具 {tool}（本插件提供 create / read / edit）")


def main() -> None:
    line = sys.stdin.readline()
    try:
        req = json.loads(line)
    except Exception as e:  # noqa: BLE001
        sys.stdout.write(json.dumps({"ok": False, "error": f"stdin 不是合法 JSON：{e}"}, ensure_ascii=False) + "\n")
        return
    tool = req.get("tool")
    args = req.get("args") or {}
    ctx = req.get("context") or {}
    try:
        result = handle(tool, args, ctx)
        sys.stdout.write(json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n")
    except Biz as e:
        sys.stdout.write(json.dumps({"ok": True, "result": str(e)}, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        sys.stdout.write(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
