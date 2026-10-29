"""Word(.docx) 只读渲染：内部模型（IR）。

为什么要有 IR：docx 是 OOXML（zip + XML），里面既有「内容」（段落、文字）也有
「版面」（样式继承、编号、表格网格、页面尺寸）。前端要画的是版面，所以这里把
两件事都抽出来，合成一棵**与 OOXML 解耦**的树：

    docx(zip+xml) ──extract──▶ IR(JSON) ──前端──▶ 页面

这么分的好处有三个：
1. 解析只做一次。前端不用解 zip、不用碰 XML-namespaces。
2. IR 是 AI 侧也能用的形态（结构 + 选区定位），将来做「AI 改文档」不必再写一遍解析。
3. 渲染层可以换：现在用 HTML/CSS 画，将来换 canvas 自绘，IR 不用动。

**这不是高保真内核**：IR 只表达「能画出来的那些东西」。浮动定位、文本框、
SmartArt、动画、修订、宏、公式、批注一律不抽（前端会在文档级给出提示），
详见 extract.py 顶部的「明确不做的事」。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# ── 单位约定 ────────────────────────────────────────────────────────────────
# OOXML 里长度有两种单位，全在这两个函数里换算，别在别处写魔法数字：
#   twips（w:sz / w:ind / w:spacing）= 1/20 磅
#   EMU（wp:extent）= 1/12700 英寸
TWIPS_PER_PT = 20.0
EMU_PER_PT = 12700.0


def twips_to_pt(v: float | int | None) -> Optional[float]:
    """twips → 磅。"""
    if v is None:
        return None
    try:
        return round(float(v) / TWIPS_PER_PT, 2)
    except (TypeError, ValueError):
        return None


def emu_to_pt(v: float | int | None) -> Optional[float]:
    """EMU → 磅。"""
    if v is None:
        return None
    try:
        return round(float(v) / EMU_PER_PT, 2)
    except (TypeError, ValueError):
        return None


def half_pt_to_pt(v: float | int | None) -> Optional[float]:
    """w:sz 是「半磅」（值 21 = 10.5pt）。"""
    if v is None:
        return None
    try:
        return round(float(v) / 2.0, 2)
    except (TypeError, ValueError):
        return None


# ── 叶子节点 ────────────────────────────────────────────────────────────────


@dataclass
class Run:
    """一段连续的文字（同一套格式）。

    注意：docx 里 run 的切分是「格式变化处」，不是「语义段」。渲染时相邻同格式
    的 run 可以合并，但保留原切分更安全（某些 run 边界带着书签/批注锚点）。
    """

    text: str = ""
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    # 字号（磅）。None = 继承。
    size: Optional[float] = None
    # 西文/等宽字体名。
    font: Optional[str] = None
    # 中日韩字体名（w:eastAsia）——中文文档里真正生效的那个。
    font_east_asia: Optional[str] = None
    # 字色 #RRGGBB。
    color: Optional[str] = None
    # 高亮底色 #RRGGBB。
    highlight: Optional[str] = None
    # 上下标：1=上标 2=下标（w:vertAlign）。
    vert_align: Optional[int] = None
    # 段内换行（<w:br/>）。
    line_break: bool = False
    # 制表符（<w:tab/>）。
    tab: bool = False
    # 外链 URL（w:hyperlink 的目标）。
    link: Optional[str] = None


@dataclass
class Numbering:
    """段落级编号（w:numPr）。

    ``marker`` 是**已经算好的标记文本**（如 "1." / "·" / "一、")：多级编号的
    序号要在遍历全文时累计，前端不该也不必知道 w:num/w:lvl 的规则。
    """

    level: int = 0
    marker: str = ""
    # 编号 id（同 numId 的段落共用一套计数）。前端据此分组渲染。
    num_id: Optional[int] = None


@dataclass
class Border:
    """一条边（w:tblBorders / w:tcBorders 的一个子元素）。

    ``style`` 是 w:val（single / dashed / double / none…）；``size`` 是磅
    （OOXML 里 w:sz 是 1/8 磅，抽取时已换算）；``color`` 是 #RRGGBB。
    """

    style: Optional[str] = None
    size: Optional[float] = None
    color: Optional[str] = None


@dataclass
class Tab:
    """制表位（w:tabs/w:tab）。"""

    # 位置（磅），从左边距算起。
    pos: Optional[float] = None
    # 对齐：left / right / center / decimal。
    val: Optional[str] = None
    # 前导符：none / dot / hyphen / underscore / middleDot。
    leader: Optional[str] = None


@dataclass
class Paragraph:
    """一个段落（w:p）。"""

    runs: list[Run] = field(default_factory=list)
    # 段落样式 id（w:pStyle@w:val），如 "Heading1"。
    style_id: Optional[str] = None
    # 样式名（人看的，如 "标题 1"）。
    style_name: Optional[str] = None
    # 大纲级别（0=一级标题）。用于大纲导航与 H 标签映射。
    outline_level: Optional[int] = None
    """ 对齐：left / center / right / both / distribute。 """
    align: Optional[str] = None
    indent_left: Optional[float] = None
    indent_right: Optional[float] = None
    indent_first_line: Optional[float] = None
    space_before: Optional[float] = None
    space_after: Optional[float] = None
    # 行距（倍数，如 1.5）。
    line_height: Optional[float] = None
    numbering: Optional[Numbering] = None
    """ 段落里内嵌的图片（行内图）。真正的行内混排需要 layout 引擎，
    第一版按「段落级图片」处理：渲染在该段落末尾。 """
    images: list["Image"] = field(default_factory=list)
    """ 制表位（w:tabs）。目录、条款对照表这类靠制表位对齐的排版靠它还原。 """
    tabs: Optional[list[Tab]] = None


@dataclass
class TableCell:
    """表格单元格（w:tc）。cell 里可以再嵌段落与表格。"""

    blocks: list[dict[str, Any]] = field(default_factory=list)
    colspan: int = 1
    rowspan: int = 1
    # 单元格宽度（磅）。"""
    width: Optional[float] = None
    """ 底纹色 #RRGGBB。 """
    shading: Optional[str] = None
    """ 垂直对齐：top / center / bottom。 """
    valign: Optional[str] = None
    """ 单元格边框（w:tcBorders）：top/left/bottom/right → Border。
    抽取时已把**表格级默认边框**与**单元格覆盖**合并好，前端直接用。 """
    borders: Optional[dict[str, "Border"]] = None


@dataclass
class Table:
    """表格（w:tbl）。"""

    rows: list[list[TableCell]] = field(default_factory=list)
    """ 各列宽度（磅），来自 w:tblGrid。 """
    col_widths: list[float] = field(default_factory=list)
    """ 表格级边框（w:tblBorders，含 insideH/insideV）。 """
    borders: Optional[dict[str, "Border"]] = None


@dataclass
class Image:
    """内嵌图片。"""

    data_uri: str = ""
    width: Optional[float] = None
    height: Optional[float] = None


@dataclass
class Page:
    """页面设置（w:sectPr）。"""

    width: float = 595.28  # A4 纵向（磅）
    height: float = 841.89
    margin_top: float = 72.0
    margin_right: float = 72.0
    margin_bottom: float = 72.0
    margin_left: float = 72.0


@dataclass
class Document:
    """一份文档的 IR 根。"""

    blocks: list[dict[str, Any]] = field(default_factory=list)
    page: Page = field(default_factory=Page)
    # 文档里出现过的样式（id → 名称），给「样式缺失」排查用。
    styles: dict[str, str] = field(default_factory=dict)
    # 抽取过程中遇到的、不支持但**影响阅读**的特性（前端据此提示用户）。
    unsupported: list[str] = field(default_factory=list)
    # 段落总数（前端可显示"共 N 段"）。
    paragraph_count: int = 0


def to_json(doc: Document) -> dict[str, Any]:
    """IR → 可 JSON 序列化的 dict（None 值剔除，前端少判 null）。"""

    def clean(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items() if v is not None}
        if isinstance(o, list):
            return [clean(v) for v in o]
        return o

    return clean(asdict(doc))
