"""Word(.docx) 提取与切片：**标题层级 + 段落级切片**。

为什么 docx 需要自己的切片器
--------------------------
Markdown 的结构是显式的（`#` 标题树），PDF 有大纲（书签）——两者都有现成的
层级可用。docx 的层级藏在**样式**里（`w:outlineLvl` / 标题样式），但我们已经有
一套自研的 docx 解析（`keeper/docx/`：docx → IR），层级就在 IR 的
`outline_level` 里，直接拿来当标题树用。

所以 docx 的切法与前两者同构：

    文档标题（文件名或首个一级标题）
      └─ 章节标题（IR 的 outline_level）
           └─ 章节标题（子节点）

**段落是 docx 的"页"**
--------------------
PDF 的叶子块定位到页（start_line/end_line = 页码），Markdown 定位到行。
docx 没有页，行也没有意义——它的最小单位是**段落**。于是：

* `start_line` / `end_line` = **块序号**（1-based，表格算一个块）。
  前端渲染时按同一个顺序给每个块编号，跳转直接对号入座。
* `locator` 留空：`store/db._page_of` 会从定位串里抠数字当页码，
  给 docx 塞"第 12 段"会被当成"第 12 页"，反而误导。定位信息放在
  `titlepath`（章节路径）里，那才是 docx 真正有用的定位。

**标题路径同样参与检索**
------------------------
`titlepath` 一填好，标题就自动参与检索（嵌入文本与 BM25 都吃它）。对法律文书
这类文档，"第三编 合同编 › 第二章 合同的效力 › 第 534 条"这样的路径极其关键：
搜"合同效力"能命中条款正文，搜"违约责任"能定位到具体章节。

表格转成 Markdown 表格文本参与检索：合同里的当事人信息表、金额表往往就是答案
所在，纯文本化会丢掉"哪一列是哪一项"的关系。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .chunk import DOC_PREFIX, Chunk, _sha, leaf_chunk_id, parent_chunk_id

logger = logging.getLogger(__name__)

#: 标题样式名里的层级：中文（"标题 2"）与英文（"Heading 2"）都认。
_HEADING_NAME_RE = re.compile(r"^(?:标题|Heading)\s*(\d+)$", re.IGNORECASE)

#: 中文编号式标题（法律/公文里极常见）：
#:   "第一编" / "第二章" / "第三节" ……
#: 这些段落**通常没有大纲级别**（作者直接手打 + 加粗），但它们才是法律文书
#: 真正的章节结构。认出来，title_path 才有"第三编 合同编 › 第二章 合同的效力"
#: 这样的路径，语义检索才能定位到条款。
#:
#: 不认"第 X 条"：那是条文编号，每条都当标题会把文档切成上千段。
_CN_HEADING_RE = re.compile(r"^\s*第[一二三四五六七八九十百千零〇]+(分编|编|篇|章|节)")
#: 层级：编 > 分编 > 章 > 节（法律文书的常见结构）
_CN_HEADING_LEVEL = {"编": 1, "篇": 1, "分编": 2, "章": 3, "节": 4}

#: 编号式标题的长度上限：正文里以"第三章"开头的**句子**不算标题
_CN_HEADING_MAX_LEN = 40

#: 表格转文本时，一行最多保留多少列（防止超宽表把 chunk 撑爆）
_MAX_TABLE_COLS = 12


def _heading_level(block: dict[str, Any]) -> Optional[int]:
    """这个块是不是标题？是的话返回层级（1-based），不是返回 None。

    三个来源，先到先得：
    1. `outline_level`（0-based，来自 `w:outlineLvl`）——最准，
       Word 的"导航窗格"就是用它。
    2. 中文编号式标题（"第一编" / "第二章" / "第三节"）——法律文书的实际结构，
       往往没有大纲级别，只能从文字认。
    3. 样式名像标题（"标题 2" / "Heading 2"）——很多文档没设大纲级别，
       只有样式名；这时按名字里的数字取层级。
    """
    lvl = block.get("outline_level")
    if isinstance(lvl, int) and lvl >= 0:
        return lvl + 1
    text = _para_text(block)
    if text and len(text) <= _CN_HEADING_MAX_LEN:
        m = _CN_HEADING_RE.match(text)
        if m:
            return _CN_HEADING_LEVEL.get(m.group(1), 2)
    name = (block.get("style_name") or "").strip()
    m = _HEADING_NAME_RE.match(name)
    if m:
        return max(1, min(9, int(m.group(1))))
    return None


def _para_text(block: dict[str, Any]) -> str:
    """段落的纯文本（含编号标记——"第一条"这种编号本身就是检索线索）。"""
    runs = block.get("runs") or []
    text = "".join(r.get("text") or "" for r in runs)
    num = block.get("numbering")
    if num and num.get("marker"):
        marker = num["marker"]
        # 列表标记与正文之间补个空格：Word 里标记与文字是分开的
        if marker and not text.startswith(marker):
            text = f"{marker} {text}"
    return text.strip()


def _norm_title(text: str) -> str:
    """标题归一化。

    Word 里为了对齐，标题里常塞全角空格（"第一编　总　　则"）。这些空格进了
    titlepath 会让 BM25 分词多出无意义的词，也会让路径显示得莫名其妙——
    统一折成单个普通空格。
    """
    t = (text or "").replace("\u3000", " ").replace("\t", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _table_text(block: dict[str, Any]) -> str:
    """表格 → Markdown 表格文本（保留行列关系，对检索友好）。"""
    rows: List[List[str]] = []
    for row in block.get("rows") or []:
        cells: List[str] = []
        for cell in row:
            if cell.get("rowspan") == 0:
                continue  # 纵向合并的延续行
            texts: List[str] = []
            for sub in cell.get("blocks") or []:
                if sub.get("type") == "paragraph":
                    t = _para_text(sub)
                    if t:
                        texts.append(t)
                elif sub.get("type") == "table":
                    texts.append(_table_text(sub))
            cells.append(" ".join(texts).replace("|", "｜"))
        if cells:
            rows.append(cells[:_MAX_TABLE_COLS])
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    out = ["| " + " | ".join(r + [""] * (width - len(r))) + " |" for r in rows]
    # 分隔行放在表头之后（Word 表没有真正的表头行，取第一行当表头）
    if len(out) > 1:
        out.insert(1, "|" + "|".join([" --- "] * width) + "|")
    return "\n".join(out)


#: 句子边界：中文标点后；英文句点后需跟空白（避免切碎 "3.14"）
_SENTENCE_RE = re.compile(r"(?<=[。！？；!?])\s*|(?<=\.)\s+")


class _Section:
    """一个章节：标题 + 标题路径 + 覆盖的块区间（块序号 1-based，闭区间）。

    ``body_start`` = 正文第一个块的 0-based 下标。**标题块自身不在正文里**——
    标题文字已经进了 titlepath（那才是它的检索价值所在），再放进正文只会
    制造一堆"只有一行标题"的空块。
    """

    __slots__ = ("level", "title", "title_path", "title_idx", "body_start", "end_idx")

    def __init__(self, level: int, title: str, title_path: List[str],
                 title_idx: int, body_start: int, end_idx: int):
        self.level = level
        self.title = title
        self.title_path = title_path
        self.title_idx = title_idx
        self.body_start = body_start
        self.end_idx = end_idx


def _build_sections(blocks: List[dict[str, Any]], doc_title: str) -> List[_Section]:
    """把块序列切成章节区间。

    与 Markdown 标题树同构：**每个标题节点自己的正文单独成段**（不与子章节
    合并）——否则有子标题的章节，它自己的引言就没人要了。而每个标题节点的
    正文范围是「它自己 → 下一个任何层级的标题之前」。
    """
    headings: List[Tuple[int, int, str]] = []  # (块序号0-based, 层级, 标题文本)
    for i, b in enumerate(blocks):
        if b.get("type") != "paragraph":
            continue
        lvl = _heading_level(b)
        if lvl is None:
            continue
        text = _norm_title(_para_text(b))
        if not text:
            continue
        headings.append((i, lvl, text))

    if not headings:
        # 整篇没有标题（很多合同/通知就是这样）：整份文档作为一段
        return [_Section(1, doc_title, [doc_title], 0, 0, len(blocks) - 1)] if blocks else []

    sections: List[_Section] = []
    # 第一个标题之前的内容（封面 / 前言 / 说明）也要进索引
    if headings[0][0] > 0:
        sections.append(_Section(1, doc_title, [doc_title], 0, 0, headings[0][0] - 1))

    # 标题路径用栈算（层级 <= 栈顶就弹出）
    stack: List[Tuple[int, str]] = []
    for k, (idx, lvl, text) in enumerate(headings):
        while stack and stack[-1][0] >= lvl:
            stack.pop()
        path = [t for _, t in stack] + [text]
        stack.append((lvl, text))
        end_idx = headings[k + 1][0] - 1 if k + 1 < len(headings) else len(blocks) - 1
        # 正文从标题块的**下一个块**开始（标题本身进 titlepath，不进正文）
        sections.append(_Section(lvl, text, path, idx, idx + 1, max(idx, end_idx)))
    return sections


def _split_long(text: str, max_chars: int) -> List[str]:
    """超长段落按句子边界切（没有句号就硬切）。"""
    pieces: List[str] = []
    cur = ""
    for part in _SENTENCE_RE.split(text):
        if not part:
            continue
        if cur and len(cur) + len(part) > max_chars:
            pieces.append(cur)
            cur = part
        else:
            cur += part
    if cur:
        pieces.append(cur)
    return pieces or [text[:max_chars]]


def _split_units(units: List[Tuple[int, str]], min_chars: int,
                 max_chars: int) -> List[Tuple[int, int, str]]:
    """内容单元 → 叶子块。

    units 是 (块序号, 文本)。返回 (起始块序号, 结束块序号, 文本)。
    与 Markdown 的三档处理同 spirit：短的合并、长的切开。
    """
    out: List[Tuple[int, int, str]] = []
    cur: List[Tuple[int, str]] = []
    cur_len = 0

    def flush() -> None:
        nonlocal cur, cur_len
        if cur:
            out.append((cur[0][0], cur[-1][0], "\n".join(t for _, t in cur)))
            cur = []
            cur_len = 0

    for idx, text in units:
        if not text:
            continue
        if len(text) > max_chars:
            flush()
            for piece in _split_long(text, max_chars):
                out.append((idx, idx, piece))
            continue
        if cur and cur_len + len(text) > max_chars:
            flush()
        cur.append((idx, text))
        cur_len += len(text) + 1
    flush()

    # 太短的块并到前一个（别让索引里全是几十字的碎片）
    merged: List[Tuple[int, int, str]] = []
    for s in out:
        if (merged and len(s[2]) < min_chars
                and len(merged[-1][2]) + len(s[2]) <= max_chars * 1.3):
            merged[-1] = (merged[-1][0], s[1], merged[-1][2] + "\n" + s[2])
        else:
            merged.append(s)
    return merged


def chunk_docx(path, file_path: str = "", doc_type: str = "user_document",
               min_chars: int = 200, max_chars: int = 1000,
               parent_max_chars: int = 2000, metadata: Optional[dict] = None) -> List[Chunk]:
    """把 docx 切成叶子 chunk 与父块。

    返回的 Chunk 列表：父块在前（按章节顺序），叶子在后（按 chunk_index 顺序）。
    """
    # 延迟导入：没装 lxml 时不该让 markdown/pdf 的索引也一起失败
    from keeper.docx.extract import extract_docx_ir
    from keeper.docx.ir import to_json

    metadata = dict(metadata or {})
    p = Path(path)
    try:
        # 转成纯 dict（IR 里是 dataclass，切片器按 dict 访问）
        ir = to_json(extract_docx_ir(p.read_bytes()))
    except Exception as e:  # noqa: BLE001 —— 坏文件 / 不是 zip / 缺 document.xml
        logger.warning("docx 解析失败，跳过：%s（%s）", path, e)
        return []

    blocks = list(ir.get("blocks") or [])
    if not blocks:
        return []

    stem = p.stem
    # 文档标题：取**开头几个块**里的第一个非空段落——封面上的书名/合同名就在那儿
    # （"中华人民共和国民法典"）。找不到就用文件名。
    #
    # 为什么不取"第一个一级标题"：法律文档的第一个章节标题（"第一编 总则"）
    # 会被当成文档名，标题就变成了"总则"，检索时显示得很别扭。
    doc_title = stem
    for b in blocks[:10]:
        if b.get("type") != "paragraph":
            continue
        t = _norm_title(_para_text(b))
        if t:
            doc_title = t[:120]
            break

    sections = _build_sections(blocks, doc_title)
    parent_chunks: List[Chunk] = []
    leaf_chunks: List[Chunk] = []
    idx = 0  # 叶子序号（全局唯一 → chunk_id 不撞）

    for sec in sections:
        units: List[Tuple[int, str]] = []
        for i in range(sec.body_start, min(sec.end_idx, len(blocks) - 1) + 1):
            b = blocks[i]
            if b.get("type") == "paragraph":
                t = _para_text(b)
            elif b.get("type") == "table":
                t = _table_text(b)
            else:
                t = ""
            if t:
                units.append((i + 1, t))  # 块序号 1-based（与前端渲染同序）
        if not units:
            continue

        title_path = [doc_title] + [x for x in sec.title_path if x != doc_title]
        tp = "/" + "/".join(title_path)

        # 父块只为 **level>=2** 的标题建（与 Markdown 一致：H1 不建）。
        # 否则一份有 257 个标题的文档会多出 257 个父块——父块不参与向量检索，
        # 建那么多只是让库变大、拖慢写入。
        parent_id = None
        if sec.level >= 2:
            parent_id = parent_chunk_id(file_path, tp)
            body = "\n".join(t for _, t in units)
            parent_body = body[:parent_max_chars].rstrip()
            if len(body) > parent_max_chars:
                parent_body += "…"
            parent_chunks.append(Chunk(
                chunk_id=parent_id,
                parent_id=None,
                is_parent=True,
                is_doc_root=False,
                title_level=sec.level,
                title_path=list(title_path),
                text=parent_body,
                embed_text=None,
                # 父块的行号 = **标题块**的位置（定位到章节开头，不是正文第一段）
                start_line=sec.title_idx + 1,
                end_line=units[-1][0],
                content_hash=_sha(body),
                chunk_index=-1,
                metadata={**metadata, "title": doc_title, "source_kind": "docx"},
            ))

        for s_idx, e_idx, piece in _split_units(units, min_chars, max_chars):
            cid = leaf_chunk_id(file_path, tp, idx)
            embed_text = f"{DOC_PREFIX}{tp}\n{piece}"
            leaf_chunks.append(Chunk(
                chunk_id=cid,
                parent_id=parent_id,
                is_parent=False,
                is_doc_root=(idx == 0),
                title_level=sec.level,
                title_path=list(title_path),
                text=piece,
                embed_text=embed_text,
                # **块序号**（1-based）：docx 没有页，段落/表格块就是最小定位单位。
                # 前端渲染时按同一顺序编号，跳转直接对号入座。
                start_line=s_idx,
                end_line=e_idx,
                content_hash=_sha(piece),
                chunk_index=idx,
                metadata={**metadata, "title": doc_title, "source_kind": "docx"},
            ))
            idx += 1

    if not leaf_chunks:
        return []
    return parent_chunks + leaf_chunks
