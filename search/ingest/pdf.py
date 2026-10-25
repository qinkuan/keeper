"""PDF 提取与切片：**大纲标题树 + 页级切片**。

为什么 PDF 要单独一个切片器
--------------------------
Markdown 的结构是显式的（`#` 标题树），所以切片器照着标题树走就行。PDF 没有这棵树
的现成版本，但**大纲（书签 / outline）就是它**——而且是带层级的树。

实测（用户空间里的真实文件）：87 页的技术 PDF 有 86 条书签（几乎每页一个标题），
275 页的书有 123 条。**所以章节标题直接读书签就行，不用靠字号猜**——准确率满分。

于是 PDF 的切法与 Markdown 完全同构：

    文档标题（元数据 /Title → 缺失时用文件名）
      └─ 章节标题（大纲的层级，原样保留嵌套）
           └─ 章节标题（子节点）

``titlepath`` 一填好，**标题就自动参与检索了**（嵌入文本与 BM25 都吃 titlepath，
见 ingest/chunk.py 的 DOC_PREFIX 与 store/db.py 的 title_tokens）——这部分一行都不用改。

**每个大纲节点都成一段**（不只叶子）：父章节自己那几页（章节引子 / 封面）原来
不属于任何章节 → 整段没进索引 → 搜不到。现在父章节也成段（页区间 = 本页 →
第一个子章节的前一页），第一个章节之前的封面页也会补一段。

只支持**电子版**（有文本层）
----------------------------
扫描件（图片型 PDF）没有文本层，提取出来是空的 → 整个文件切不出 chunk → 被跳过。
这是刻意的：扫描件的内容根本进不了索引，只索引章节标题等于"能搜到名字、搜不到内容"，
反而误导用户。

已知短板（纯文本方案的固有边界）
--------------------------------
- **表格会被拉平**成一堆数字和竖线 → 表格内容基本搜不到（pypdf 抽不了表格）；
- **双栏排版会串行**（左右栏文字交替）→ 切片内容乱，语义质量下降；
- **图里的文字搜不到**（纯文本方案丢弃图片）。

要解决这三条得换提取实现（pymupdf / pdfplumber），本模块的接口按「可替换」设计。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .chunk import (
    DOC_PREFIX,
    Chunk,
    leaf_chunk_id,
    parent_chunk_id,
    split_by_sentence,
)

#: 句末标点：判断一个段落是不是结束在这里（用于跨页续接）。
_SENTENCE_END = ("。", "！", "？", "；", ".", "!", "?", ";", "：", ":", "”", '"')

#: 全文文本少于这个字数 → 当作扫描件（图片型 PDF）跳过。
#:
#: 判据不是「有没有文本层」而是「文本少得没有意义」：扫描件常只有封面一行字，
#: 把它索引进去等于制造一个只能搜到文件名的僵尸条目。100 字是保守阈值——
#: 真正的短文档（一页备忘）也远超这个数。
_MIN_TEXT_CHARS = 100

#: 被字距拆开的中文：「摘 要」→ 零宽匹配，可以连续替换
_CJK_SPACED_RE = re.compile(r"(?<=[\u4e00-\u9fff]) (?=[\u4e00-\u9fff])")


def _fix_letter_spacing(text: str) -> str:
    """修复 PDF 里**中文字符间距**造成的假空格。

    PPT / 排版过的 PDF 常把标题做成「每个字之间加一点间距」，pypdf 提取出来就是
    "摘 要"。这不只是难看——它会**直接毁掉关键词检索**：jieba 会把"摘 要"切成
    "摘"和"要"，用户搜"摘要"就命中不了。

    **只修中文，不修英文**（这是个刻意的取舍）：
    英文被字距拆开时是 "A n a l y s i s"，看着更糟，但 jieba 仍会把它切成
    "Analysis"、"of"、"the" 这些完整的词 → 用户搜 "Analysis" **能命中**。
    而如果把空格全压掉，"Reasons for" 会变成 "Reasonsfor"，英文词边界就彻底
    没了，搜 "Reasons" 反而命中不了。带空格的英文对检索更有利。
    """
    if not text:
        return text
    return _CJK_SPACED_RE.sub("", text)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_cjk(ch: str) -> bool:
    """是不是中文字符（判断换行处要不要插空格）。"""
    if not ch:
        return False
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3000 <= code <= 0x303F
        or 0xFF00 <= code <= 0xFFEF
    )


def _join_lines(lines: List[str]) -> str:
    """把 PDF 的硬换行合并成一行。

    PDF 里的「行」是排版产物，不是语义段落：一句话会被拆成好几行。中文换行处
    直接接上；西文换行处补一个空格（不然 "trans-\\nform" 会粘成 "transform"）。
    """
    out = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if not out:
            out = line
        elif _is_cjk(out[-1]) and _is_cjk(line[0]):
            out += line
        else:
            out += " " + line
    return _fix_letter_spacing(out)


def _paragraphs(text: str) -> List[str]:
    """把页面文本重组成段落（空行分段，段内硬换行合并）。"""
    return [p for p, _s, _e in _paragraphs_spans(text)]


def _paragraphs_spans(text: str) -> List[Tuple[str, int, int]]:
    """把页面文本重组成段落，**并带上每段在原文里的字符区间**。

    段落文本与 :func:`_paragraphs` 逐字一致（同一套 :func:`_join_lines` 合并规则），
    区间则是「首行起始偏移 → 末行结束偏移」——用来把段落**映射回它的来源页**。

    为什么两者要分开记
    ------------------
    段落文本是**重组**过的（每行 strip、行间按中英文字 rules 拼接），与原文区间
    里的字符并不逐字对应（缩进没了、空格被规范化了）。但我们只需要知道
    「这段话大致落在原文的哪一段里」——区间只要**包住**它就够了，不必逐字对齐。

    于是「这一段来自第几页」这个问题就变成了区间求交，精确且不需要改动
    任何文本内容（content_hash 不变 → 旧向量全部可复用）。
    """
    out: List[Tuple[str, int, int]] = []
    cur: List[str] = []
    cur_start = 0
    offset = 0
    for raw in text.split("\n"):
        line_start = offset
        offset += len(raw) + 1        # +1 是这一行后面的换行符
        if not raw.strip():
            if cur:
                p = _join_lines(cur)
                if p:
                    out.append((p, cur_start, line_start - 1))
                cur = []
            continue
        if not cur:
            cur_start = line_start
        cur.append(raw)
    if cur:
        p = _join_lines(cur)
        if p:
            out.append((p, cur_start, offset - 1))
    return out


def _join_pages(texts: List[str], offset: int = 0) -> Tuple[str, List[Tuple[int, int, int]]]:
    """把若干页的文本接成一段连续的正文，**并记录每页在结果里的字符区间**。

    跨页段落：上一页最后一行**不是句末标点** → 下一页开头很可能是它的续接，
    用「单换行」连接（视作同一段）；否则用空行断开（是两个段落）。

    返回 ``(正文, spans)``，spans 的每项是 ``(起始字符, 结束字符, 页号)``。
    页号是 0-based 且**已加上 offset**（调用方传章节起始页，得到全局页号）。

    正文内容与旧版**逐字相同**（只是多返回了区间），所以 chunk 的 content_hash
    不变 → 旧向量全部可复用，不需要重新嵌入。
    """
    out = ""
    spans: List[Tuple[int, int, int]] = []
    for idx, t in enumerate(texts):
        t = t.strip()
        if not t:
            continue
        if not out:
            out = t
            spans.append((0, len(out), idx + offset))
            continue
        sep = "\n\n" if out.endswith(_SENTENCE_END) else "\n"
        out += sep + t
        spans.append((len(out) - len(t), len(out), idx + offset))
    return out, spans


def _pages_of_span(span: Tuple[int, int], spans: List[Tuple[int, int, int]]) -> List[int]:
    """一个字符区间覆盖了哪些页（0-based，去重且有序）。

    区间求交：段落区间与页区间有重叠就算命中这一页。一个段落横跨两页时
    （段落被跨页合并规则接起来了），两页都会算进去——这正是我们要的：
    **这一段确实出现在这两页上**。
    """
    s, e = span
    pages = {p for (a, b, p) in spans if a < e and s < b}
    return sorted(pages)


def _strip_repeated_edges(pages: List[str]) -> List[str]:
    """剔除每页重复出现的页眉 / 页脚。

    做法：统计每页**首 2 行与末 2 行**的文本，出现在 ≥60% 页面上的就判定为
    页眉页脚（页码、书名、版权行）。这些行每页都有一份，不剔掉会污染**每一个**
    chunk 的内容（还会稀释向量表达的意思）。
    """
    if len(pages) < 5:
        return pages
    counter: Dict[str, int] = {}
    for t in pages:
        lines = t.split("\n")
        edges = [l.strip() for l in lines[:2]] + [l.strip() for l in lines[-2:]]
        for l in edges:
            if l:
                counter[l] = counter.get(l, 0) + 1
    threshold = max(3, int(len(pages) * 0.6))
    repeated = {l for l, c in counter.items() if c >= threshold}
    if not repeated:
        return pages

    out = []
    for t in pages:
        lines = t.split("\n")
        kept = []
        for i, line in enumerate(lines):
            is_edge = i < 2 or i >= len(lines) - 2
            if is_edge and line.strip() in repeated:
                continue
            kept.append(line)
        out.append("\n".join(kept))
    return out


@dataclass
class _Node:
    """大纲里的一个节点。"""
    title: str
    page: int              # 起始页（0-based）
    level: int             # 层级（1 起）
    children: List["_Node"] = field(default_factory=list)


@dataclass
class _Section:
    """一个章节（大纲的一个叶子节点 + 它覆盖的页区间）。"""
    title: str
    title_path: List[str]
    level: int
    start_page: int        # 0-based
    end_page: int          # 0-based（含）
    parent_id: Optional[str] = None


def _open(path):
    """打开 PDF（pypdf 是可选依赖，没装要说清楚）。"""
    try:
        from pypdf import PdfReader
    except ImportError as e:  # noqa: BLE001
        raise RuntimeError(
            "PDF 索引需要 pypdf（pip install pypdf）。"
            "没装时 PDF 会被跳过，Markdown 检索不受影响。"
        ) from e
    try:
        return PdfReader(str(path))
    except Exception as e:  # noqa: BLE001 损坏 / 加密的 PDF
        raise RuntimeError(f"PDF 打开失败：{e}") from e


def _doc_title(reader, path: Path) -> str:
    """文档标题：元数据 /Title → 缺失时用文件名（去扩展名）。"""
    try:
        meta = reader.metadata or {}
        title = str(meta.get("/Title") or "").strip()
    except Exception:  # noqa: BLE001 元数据可能坏掉
        title = ""
    if not title:
        title = path.stem.strip()
    return title or "document"


def _outline_tree(reader) -> List[_Node]:
    """把 PDF 大纲（书签）解析成树。

    pypdf 的 outline 是**深度优先的混合列表**：节点后面紧跟它的子节点列表，
    例如 ``[A, [A1, A2], B]``。这里还原成树。
    """
    try:
        raw = reader.outline
    except Exception:  # noqa: BLE001 没有大纲 / 大纲损坏
        return []
    return _to_nodes(raw, reader, 1)


def _to_nodes(items, reader, level: int) -> List[_Node]:
    nodes: List[_Node] = []
    for item in items or []:
        if isinstance(item, list):
            # 子节点列表：挂到上一个节点上
            if nodes:
                nodes[-1].children = _to_nodes(item, reader, level + 1)
            continue
        title = str(getattr(item, "title", "") or "").strip()
        try:
            page = int(reader.get_destination_page_number(item))
        except Exception:  # noqa: BLE001 目标页取不到
            page = 0
        nodes.append(_Node(title=title, page=max(0, page), level=level))
    return nodes


def _leaf_sections(nodes: List[_Node], doc_title: str, npages: int) -> List[_Section]:
    """把大纲树摊成「章节列表」：**每个大纲节点覆盖一段页区间**（不只是叶子）。

    为什么父章节也要单独成段：父章节自己那几页（章节引子 / 封面）在原来那版里
    **不属于任何 section** → 整段没进索引 → 搜不到（和 Markdown 侧「H1 正文丢失」
    是同一类洞）。现在父章节也成段，页区间是「本页 → 第一个子章节的前一页」。

    没有大纲时，整份文件当成一个章节。
    """
    raw = []       # (title, 标题路径, level, 起始页, 结束页 或 None)

    def walk(node: _Node, path: List[str]) -> None:
        cur = path + ([node.title] if node.title else [])
        if node.children:
            # 自己那几页：本节点起始页 → 第一个子章节的前一页
            child_start = min(c.page for c in node.children)
            raw.append((node.title, cur, node.level, node.page, child_start - 1))
            for c in node.children:
                walk(c, cur)
        else:
            raw.append((node.title, cur, node.level, node.page, None))

    for n in nodes:
        walk(n, [])

    if not raw:
        return [_Section(title=doc_title, title_path=[doc_title], level=1,
                         start_page=0, end_page=max(0, npages - 1))]

    raw.sort(key=lambda x: x[3])
    sections: List[_Section] = []
    for i, (title, path, level, page, end) in enumerate(raw):
        start = max(0, min(page, npages - 1))
        if end is None:
            # 叶子章节：一直盖到下一个章节开始的前一页
            end = (raw[i + 1][3] - 1) if i + 1 < len(raw) else npages - 1
        end = max(start, min(end, npages - 1))
        if end < start:
            continue        # 空区间（父章节与第一个子章节在同一页）
        sections.append(_Section(
            title=title or doc_title,
            title_path=[doc_title] + path,
            level=level,
            start_page=start,
            end_page=end,
        ))

    # 第一个章节之前的内容（封面 / 摘要 / 目录）也要进索引
    if sections and sections[0].start_page > 0:
        sections.insert(0, _Section(
            title=doc_title, title_path=[doc_title], level=1,
            start_page=0, end_page=sections[0].start_page - 1,
        ))
    return sections


def _split_section(
    text: str,
    min_chars: int,
    max_chars: int,
    spans: Optional[List[Tuple[int, int, int]]] = None,
) -> List[Tuple[str, int, int]]:
    """章节内按段落贪心切分（超长段落按句子边界兜底）。

    返回 ``[(片段文本, 起始页, 结束页)]``，页码 0-based。

    片段**文本**与旧版逐字一致（同样的贪心规则、同样的拼接方式），多出来的只是
    页码：片段由若干段落组成 → 它的页范围 = 这些段落覆盖的页的并集的最小/最大。
    """
    paras = _paragraphs_spans(text)
    out: List[Tuple[str, int, int]] = []
    cur: List[str] = []
    cur_pages: List[int] = []
    cur_len = 0

    def _flush() -> None:
        nonlocal cur, cur_len, cur_pages
        if cur:
            out.append((
                "\n\n".join(cur),
                min(cur_pages) if cur_pages else 0,
                max(cur_pages) if cur_pages else 0,
            ))
        cur, cur_len, cur_pages = [], 0, []

    for (p, s, e) in paras:
        pages = _pages_of_span((s, e), spans) if spans else []
        if len(p) > max_chars:
            # 超长段落：按句子边界兜底切分。每一刀都在这一段里 → 整段共用同一页范围。
            _flush()
            lo = min(pages) if pages else 0
            hi = max(pages) if pages else 0
            for piece in split_by_sentence(p, max_chars):
                out.append((piece, lo, hi))
            continue
        add = len(p) + (2 if cur else 0)
        if cur and cur_len + add > max_chars:
            _flush()
            cur, cur_len, cur_pages = [p], len(p), list(pages)
        else:
            cur.append(p)
            cur_len += add
            cur_pages.extend(pages)
    _flush()
    return out


def _locator(start_page: int, end_page: int) -> str:
    """定位串：「第 12 页」/「第 12-13 页」（页码 1-based）。"""
    if start_page == end_page:
        return f"第 {start_page + 1} 页"
    return f"第 {start_page + 1}-{end_page + 1} 页"


def chunk_pdf(
    path,
    file_path: Optional[str] = None,
    doc_type: str = "user_document",
    min_chars: int = 200,
    max_chars: int = 1000,
    parent_max_chars: int = 2000,
    metadata: Optional[Dict[str, Any]] = None,
) -> List[Chunk]:
    """把 PDF 切成叶子 chunk 与父块。

    返回的 Chunk 列表与 :func:`chunk.chunk_markdown` **同构**（父块在前、
    叶子在后），所以索引、存储、检索、混合召回那一整条链路一行都不用改。

    扫描件（无文本层）→ 返回空列表（被索引器当成 empty 跳过）。
    """
    path = Path(path)
    file_path = file_path or str(path)
    reader = _open(path)

    try:
        npages = len(reader.pages)
    except Exception:  # noqa: BLE001
        npages = 0
    if npages == 0:
        return []

    doc_title = _doc_title(reader, path)

    # 逐页提取文本（扫描件这里全是空的 → 后面自然切不出 chunk）
    pages: List[str] = []
    for i in range(npages):
        try:
            pages.append(reader.pages[i].extract_text() or "")
        except Exception:  # noqa: BLE001 单页坏了不该让整份文件失败
            pages.append("")
    pages = _strip_repeated_edges(pages)

    sections = _leaf_sections(_outline_tree(reader), doc_title, npages)

    parent_chunks: List[Chunk] = []
    leaf_chunks: List[Chunk] = []
    idx = 0                       # 叶子序号（全局唯一 → chunk_id 不撞）
    total_text_len = 0

    # **文件名不进嵌入文本**：文件名走关键词通道的独立高权重列（name_tokens，
    # bm25 权重 8.0），向量通道只管语义。PDF 的 title_path 第一级就是文档标题
    # （元数据 /Title，缺失时用文件名），标题天然已经在嵌入文本里了。

    for sec in sections:
        # offset 把页号从「章节内相对」换算成「整份文档的绝对页号」
        sec_text, sec_spans = _join_pages(
            pages[sec.start_page:sec.end_page + 1], offset=sec.start_page
        )
        if not sec_text.strip():
            continue
        total_text_len += len(sec_text)

        tp = "/" + "/".join(sec.title_path)
        parent_id = parent_chunk_id(file_path, tp)
        body = sec_text[:parent_max_chars].rstrip()
        if len(sec_text) > parent_max_chars:
            body += "…"
        parent_chunks.append(Chunk(
            chunk_id=parent_id,
            parent_id=None,
            is_parent=True,
            is_doc_root=False,
            title_level=sec.level,
            title_path=list(sec.title_path),
            text=body,
            embed_text=None,
            start_line=sec.start_page + 1,
            end_line=sec.end_page + 1,
            content_hash=_sha(sec_text),
            chunk_index=-1,
            metadata={**(metadata or {}), "title": doc_title,
                      "page_count": npages, "source_kind": "pdf",
                      "locator": _locator(sec.start_page, sec.end_page),
                      # 父块是章节级：页范围就是整个章节
                      "page_start": sec.start_page + 1,
                      "page_end": sec.end_page + 1},
        ))

        for piece, p_from, p_to in _split_section(sec_text, min_chars, max_chars, sec_spans):
            cid = leaf_chunk_id(file_path, tp, idx)
            embed_text = f"{DOC_PREFIX}{tp}\n{piece}"
            leaf_chunks.append(Chunk(
                chunk_id=cid,
                parent_id=parent_id,
                is_parent=False,
                is_doc_root=(idx == 0),
                title_level=sec.level,
                title_path=list(sec.title_path),
                text=piece,
                embed_text=embed_text,
                # **叶子块的行号 = 它内容的精确页码**（1-based）。
                # 以前这里写的是章节页区间：命中一个块却拿到「第 5-12 页」，
                # 拿这个区间去渲染页面时可能整页都不含命中内容 —— agent 看着图
                # 编答案。现在写精确页，渲染出来的就是命中内容所在的那一页。
                start_line=p_from + 1,
                end_line=p_to + 1,
                content_hash=_sha(piece),
                chunk_index=idx,
                metadata={**(metadata or {}), "title": doc_title,
                          "page_count": npages, "source_kind": "pdf",
                          "locator": _locator(p_from, p_to),
                          "page_start": p_from + 1,
                          "page_end": p_to + 1},
            ))
            idx += 1

    if not leaf_chunks or total_text_len < _MIN_TEXT_CHARS:
        # 扫描件 / 纯图片 PDF：没有正文可索引 → 返回空（索引器会跳过它）
        return []
    return parent_chunks + leaf_chunks
