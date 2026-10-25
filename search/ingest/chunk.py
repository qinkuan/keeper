"""Markdown 结构化切片：标题树 -> 块级元素 -> 叶子 chunk -> 父子链父块。

对应《Markdown 切片规则与数据库设计》：

1. 解析 ATX 标题（# ~ ######）构建标题树，记录 level / 文本 / 行号；
   忽略围栏代码块与 YAML front matter 内的 '#'，不在固定字符处硬切。
2. 在每个标题节点的内容范围内识别块级元素：
   围栏代码块 / 表格 / 连续列表 / 普通段落；代码块与表格是不可分割单元。
3. **内容单元**：**每个标题节点自己的正文**各算一个单元（不只是"没有子标题的"
   标题）——否则有子标题的标题，它自己的正文（章节引子）就没人要了：
   H1 的正文会彻底丢失，H2+ 的正文只进父块（父块不参与检索 → 搜不到）。
   单元三档处理：
   - 有子标题的标题：**单独成块**（保留自己的 titlepath），不与兄弟合并
     （后面紧跟的是子标题的内容）、不向上合并（会丢掉自己的章节身份）。
   - 无子标题的标题（普通叶子）且 < min_chars：优先与同级相邻叶子合并；
     同级无法合并则向上合并到父节点（父节点作为一个 chunk，沿用父节点的
     titlepath 与 title_level）
   - min_chars ~ max_chars：整体作为一个 chunk
   - > max_chars：按块级元素切分 + 贪心合并；代码块/表格特权不切；
     单个普通段落超长时按句子边界兜底切分；不使用 overlap。
4. 仅叶子 chunk 生成 embed_text："task: search result | text: {titlepath}\\n{text}"。
5. 父子链：H2 及以下标题建父块（H1 不建），H2 父块 parent_id 为 NULL；
   叶子 chunk 的 parent_id 指向直接父标题对应的父块；
   有子标题的标题，它自己的正文块指向**它自己**的父块。
"""
import hashlib
import re
from dataclasses import dataclass, field

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
TABLE_RE = re.compile(r"^\s*\|")
LIST_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
# 句子边界：中文标点后；英文句点后需跟空白（避免切碎 "3.14"）
SENTENCE_RE = re.compile(r"(?<=[。！？；!?])\s*|(?<=\.)\s+")

DOC_PREFIX = "task: search result | text: "


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def leaf_chunk_id(file_path: str, titlepath: str, idx: int) -> str:
    """叶子 chunk 的 id：由「文件路径 + 标题路径 + 序号」派生。

    抽成公共函数的原因：文件移动时要按新路径重算 chunk_id（见 store/db.move_document），
    派生规则必须只有一处定义，否则两处一旦漂移就会静默错配。
    """
    return _sha(f"{file_path}{titlepath}{idx}")


def parent_chunk_id(file_path: str, titlepath: str) -> str:
    """父块 chunk 的 id：路径 + 标题路径 + "parent" 后缀。"""
    return _sha(f"{file_path}{titlepath}parent")


@dataclass
class Chunk:
    """一个切片：叶子（参与向量检索）或父块（仅用于回溯）。"""
    chunk_id: str = ""
    parent_id: str = None
    is_parent: bool = False
    is_doc_root: bool = False
    title_level: int = 0
    title_path: list = field(default_factory=list)   # 标题文本列表，如 ["项目复盘", "问题分析"]
    text: str = ""
    embed_text: str = None                          # 仅叶子有值
    start_line: int = 0
    end_line: int = 0
    content_hash: str = ""
    chunk_index: int = -1                           # 仅叶子编号；父块为 -1
    metadata: dict = field(default_factory=dict)    # 来自 frontmatter 的元信息

    @property
    def titlepath(self) -> str:
        """斜杠形式的标题路径，如 "/项目复盘/问题分析"。"""
        return "/" + "/".join(self.title_path) if self.title_path else ""


@dataclass
class Block:
    """块级元素：code / table / list / para。"""
    type: str
    text: str
    start_line: int
    end_line: int


@dataclass
class Heading:
    """标题树节点。"""
    level: int
    text: str
    line: int                     # 标题所在行号(1-based)
    body_start: int               # 正文起始行号(1-based)
    body_end: int                 # 正文结束行号(1-based)
    children: list = field(default_factory=list)
    parent: object = None
    blocks: list = field(default_factory=list)     # 自身正文的块级元素


# ---------------------------------------------------------------- front matter


def extract_frontmatter(lines):
    """解析 markdown 开头的 YAML frontmatter，返回 (metadata_dict, body_start_index)。

    只有当首个 '---' 之后的内容确实像 YAML（空行/注释/含 ':' 的键值）时才认定为
    front matter，避免把"以 --- 开头的正文 + 后面的分隔线"误判而丢掉正文。
    """
    if not lines or lines[0].strip() != "---":
        return {}, 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            block = lines[1:i]
            if _looks_like_yaml(block):
                return _parse_simple_yaml("\n".join(block)), i + 1
            return {}, 0
    return {}, 0


def _looks_like_yaml(lines):
    """粗略判断一段文本是否是 YAML front matter 内容。"""
    seen = False
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if ":" not in s:
            return False
        seen = True
    return seen


def _is_decorative(block):
    """装饰性块（如 '---' 分隔线）没有语义，不单独成块、不进向量。"""
    if block.type != "para":
        return False
    t = block.text.strip()
    if not t:
        return True
    return bool(re.fullmatch(r"[-*_]{3,}", t))


def _parse_simple_yaml(text):
    """轻量 YAML 解析，覆盖 key: value / "value" / 'value'。"""
    meta = {}
    for line in text.splitlines():
        if ":" in line and not line.strip().startswith("#"):
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta


# ---------------------------------------------------------------- 解析


def _parse_headings(lines, body_start):
    """扫描正文，构建标题树。返回根节点列表（虚拟根 level=0）。"""
    root = Heading(level=0, text="", line=0, body_start=body_start, body_end=len(lines))
    stack = [root]
    in_code = False
    fence = None

    for i in range(body_start, len(lines)):
        line = lines[i]
        m_fence = FENCE_RE.match(line)
        if in_code:
            if m_fence and line.strip().startswith(fence):
                in_code = False
                fence = None
            continue
        if m_fence:
            in_code = True
            fence = m_fence.group(1)
            continue
        m = HEADING_RE.match(line)
        if not m:
            continue
        level = len(m.group(1))
        text = m.group(2).strip()
        if not text:
            continue
        while len(stack) > 1 and stack[-1].level >= level:
            stack.pop()
        node = Heading(
            level=level,
            text=text,
            line=i + 1,
            body_start=i + 2,
            body_end=len(lines),
        )
        node.parent = stack[-1]
        stack[-1].children.append(node)
        stack.append(node)

    # 填充每个节点正文的行号区间与块级元素
    # 正文边界 = 文档顺序中"下一个标题"的前一行（兄弟标题同样算边界）
    all_heads = sorted((n for n in _iter_all(root) if n.level > 0), key=lambda n: n.line)
    for idx, node in enumerate(all_heads):
        node.body_end = (all_heads[idx + 1].line - 1) if idx + 1 < len(all_heads) else len(lines)
        node.blocks = _split_blocks(lines, node.body_start - 1, node.body_end)
    return root


def _iter_all(node):
    yield node
    for c in node.children:
        yield from _iter_all(c)


def _split_blocks(lines, start, end):
    """把 [start, end) 的行（0-based）切成块级元素。"""
    blocks = []
    i = start
    while i < end and i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        # 围栏代码块：整体一块，不从中间切断
        m_fence = FENCE_RE.match(line)
        if m_fence:
            fence = m_fence.group(1)
            j = i + 1
            while j < end:
                lj = lines[j]
                if FENCE_RE.match(lj) and lj.strip().startswith(fence):
                    break
                j += 1
            last = min(j, end - 1)
            blocks.append(Block("code", "\n".join(lines[i:last + 1]), i + 1, last + 1))
            i = last + 1
            continue

        # 表格：连续的以 | 分隔的行，整体一块
        if TABLE_RE.match(line):
            j = i
            while j < end and TABLE_RE.match(lines[j]):
                j += 1
            blocks.append(Block("table", "\n".join(lines[i:j]), i + 1, j))
            i = j
            continue

        # 连续列表：连续的列表行，整体一块
        if LIST_RE.match(line):
            j = i
            while j < end and LIST_RE.match(lines[j]):
                j += 1
            blocks.append(Block("list", "\n".join(lines[i:j]), i + 1, j))
            i = j
            continue

        # 普通段落：空行分隔
        j = i
        while j < end:
            lj = lines[j]
            if not lj.strip():
                break
            if FENCE_RE.match(lj) or TABLE_RE.match(lj) or LIST_RE.match(lj):
                break
            j += 1
        blocks.append(Block("para", "\n".join(lines[i:j]), i + 1, j))
        i = j
    return blocks


# ---------------------------------------------------------------- 标题路径


def _ancestor_titles(node):
    """祖先标题文本列表（自上而下）。"""
    out = []
    p = node.parent
    while p is not None and p.level > 0:
        out.append(p.text)
        p = p.parent
    return list(reversed(out))


def _title_path(node):
    """节点自身的标题路径（含自身标题）。"""
    return _ancestor_titles(node) + [node.text]


# ---------------------------------------------------------------- 叶子切分


def _node_text(node):
    """节点自身正文（块级元素以空行拼接；装饰性分隔线不计）。"""
    parts = [b.text for b in node.blocks if not _is_decorative(b)]
    return "\n\n".join(parts).strip()


def _first_paragraph(node):
    """节点下首段文本（用于父块降级摘要）。"""
    for b in node.blocks:
        if b.type == "para":
            return b.text
    for b in node.blocks:
        return b.text
    return ""


def _split_by_sentence(text, max_chars):
    """超长普通段落：按句子边界切分，切点落在标点之后；单句超长则允许超长。"""
    parts = [p for p in SENTENCE_RE.split(text) if p and p.strip()]
    chunks = []
    cur = ""
    for p in parts:
        cand = (cur + p) if cur else p
        if cur and len(cand.strip()) > max_chars:
            chunks.append(cur.strip())
            cur = p
        else:
            cur = cand
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


def split_by_sentence(text, max_chars):
    """按句子边界切分超长段落（公开给 PDF 切片器复用）。"""
    return _split_by_sentence(text, max_chars)


def _split_long_leaf(node, max_chars, min_chars=200):
    """叶子内容过长时的切分：块级元素切分 + 贪心合并（代码块/表格特权、段落兜底）。

    另外做一次"碎片回收"：长度不足 min_chars 的碎块并入前一个普通块，
    避免出现只有几个字符（例如 '---' 分隔线）的垃圾片段干扰检索。
    """
    blocks = [b for b in node.blocks if not _is_decorative(b)]
    if not blocks:
        return []
    total = len(_node_text(node))
    if total <= max_chars:
        return [_node_text(node)]

    out = []
    cur = []
    cur_len = 0
    cur_special = False    # 当前 chunk 是否由特权块（超长代码块/表格）开启

    def flush_cur():
        nonlocal cur, cur_len, cur_special
        if cur:
            out.append("\n\n".join(cur))
        cur, cur_len, cur_special = [], 0, False

    for b in blocks:
        # 普通段落兜底：单段超长按句子边界切，独立成块
        if b.type == "para" and len(b.text) > max_chars:
            flush_cur()
            out.extend(_split_by_sentence(b.text, max_chars))
            continue

        # 代码块/表格特权：自身超长也整体保留，先封口当前块，自己开启新块
        if b.type in ("code", "table") and len(b.text) > max_chars:
            flush_cur()
            cur = [b.text]
            cur_len = len(b.text)
            cur_special = True
            continue

        add = len(b.text) + (2 if cur else 0)
        # 特权块开启的 chunk 允许吸收后续短块，避免产生孤立碎片
        if cur and cur_len + add > max_chars and not (cur_special and len(cur) == 1):
            flush_cur()
            add = len(b.text)
        cur.append(b.text)
        cur_len += add
        cur_special = False
    flush_cur()

    # 碎片回收：短碎块并入前一个未超长的块
    merged = []
    for text in out:
        if merged and len(text) < min_chars and len(merged[-1]) <= max_chars \
                and len(merged[-1]) + len(text) + 2 <= max_chars:
            merged[-1] = merged[-1] + "\n\n" + text
            continue
        merged.append(text)
    return merged


def _collect_units(root):
    """按原文顺序收集「内容单元」：**每个标题节点自己的正文**各算一个单元。

    为什么不是只取「没有子标题的标题」（旧的 ``_collect_leaves``）：
    那样的话，**有子标题的标题，它自己的正文（章节引子）就没人要了**——

    - H1 的正文：H1 不建父块 → 这段文字进不了任何块 → **彻底丢失**；
    - H2+ 的正文：只进了父块 → 而父块 ``is_parent=1`` 不参与检索 → **搜不到**。

    实测：``# 第一部分`` 下面那段引子里的词，搜出来是 0 条。

    所以这里把「每个标题的正文」都当成一个单元（它可能是章节引子，
    也可能是一个没有子标题的标题的全部内容）。
    """
    out = []

    def walk(node):
        for child in node.children:
            out.append(child)
            walk(child)

    walk(root)
    return out


def _unit_parent_id(node, parent_ids):
    """单元的父块（命中后回溯章节上下文用）。

    - 节点**有子标题**时：它自己的正文属于「它这一节」→ 指向**它自己**的父块；
    - 节点**没有子标题**时：它就是普通叶子 → 指向父标题的父块（沿用旧逻辑）。
    """
    if node.children:
        return parent_ids.get(id(node))
    return _parent_id_for(node, parent_ids)


# ---------------------------------------------------------------- 主入口


def chunk_markdown(text, file_path="", doc_type="user_document",
                   min_chars=200, max_chars=1000, parent_max_chars=2000,
                   metadata=None):
    """把 Markdown 文本切成叶子 chunk 与父块。

    返回的 Chunk 列表：父块在前（按标题树顺序），叶子在后（按 chunk_index 顺序）。
    """
    metadata = dict(metadata or {})
    lines = text.split("\n")
    fm, body_start = extract_frontmatter(lines)
    metadata.update(fm)

    root = _parse_headings(lines, body_start)
    has_heading = bool(root.children)

    # ---- 内容单元：每个标题节点**自己的正文**都是一个单元 ----
    units = _collect_units(root)
    units = [n for n in units if _node_text(n)]

    # ---- 叶子：三档处理 ----
    # 先算出各内部节点（level>=2）的父块 id，用于建立父子链
    parent_ids = _parent_block_ids(root, file_path)
    leaf_specs = []   # (title_path, title_level, text, start_line, end_line, parent_id)
    i = 0
    while i < len(units):
        node = units[i]
        node_text = _node_text(node)
        if not node_text:
            i += 1
            continue

        # 有子标题的标题：它自己的正文（章节引子）**单独成块**。
        # 不跟兄弟合并（它的后面紧跟的是子标题的内容，合并会跳过中间内容），
        # 也不向上合并（向上合并会把这段文字记到父章节名下，丢掉它自己的章节身份）。
        if node.children:
            parts = _split_long_leaf(node, max_chars, min_chars)
            for part in parts:
                leaf_specs.append((
                    _title_path(node), node.level, part, node.line, node.body_end,
                    _unit_parent_id(node, parent_ids),
                ))
            i += 1
            continue

        if len(node_text) < min_chars:
            # 优先与同级相邻叶子合并
            nxt = units[i + 1] if i + 1 < len(units) else None
            if nxt is not None and nxt.parent is node.parent:
                nxt_text = _node_text(nxt)
                if nxt_text and len(node_text) + len(nxt_text) + 2 <= max_chars:
                    leaf_specs.append((
                        _title_path(node), node.level,
                        node_text + "\n\n" + nxt_text,
                        node.line, nxt.body_end,
                        _parent_id_for(node, parent_ids),
                    ))
                    i += 2
                    continue
            # 同级无法合并 -> 向上合并到父节点（父节点作为一个 chunk）
            if node.parent is not None and node.parent.level > 0:
                leaf_specs.append((
                    _title_path(node.parent), node.parent.level,
                    node_text, node.line, node.body_end,
                    # 合并后身份是父节点，指向父节点自己的父块，便于回溯章节上下文
                    parent_ids.get(id(node.parent)),
                ))
                i += 1
                continue
            # 无父（无标题文档 / H1）：只能自身成块
            leaf_specs.append((
                _title_path(node), node.level, node_text, node.line, node.body_end,
                None,
            ))
            i += 1
            continue

        # 正常档：过长则按块级元素切分
        parts = _split_long_leaf(node, max_chars, min_chars)
        for part in parts:
            leaf_specs.append((
                _title_path(node), node.level, part, node.line, node.body_end,
                _parent_id_for(node, parent_ids),
            ))
        i += 1

    # ---- 无标题文档：整篇作为一个叶子 chunk，titlepath 用文件名 ----
    if not has_heading:
        stem = _file_stem(file_path)
        whole = "\n\n".join(b.text for b in _split_blocks(lines, body_start, len(lines))).strip()
        if whole:
            parts = _split_text_into_parts(whole, max_chars)
            leaf_specs = [(["" + stem], 0, p, 1, len(lines), None) for p in parts]

    # ---- 生成叶子 Chunk ----
    leaf_chunks = []
    # **文件名不进嵌入文本**（用户明确要求）：文件名走关键词通道的独立高权
    # 重列（name_tokens，bm25 权重 8.0），向量通道只管语义。
    # 文档标题（frontmatter 的 title，且与文件名不同时）仍然进嵌入文本——
    # 它跟文件名不是一回事，是作者给文档起的正式名字。
    doc_title = (metadata.get("title") or "").strip()
    file_stem = _file_stem(file_path)
    for idx, (tp, level, body, start_line, end_line, pid) in enumerate(leaf_specs):
        titlepath = "/" + "/".join(tp) if tp else ""
        cid = leaf_chunk_id(file_path, titlepath, idx)
        title_head = ""
        if doc_title and doc_title != file_stem and doc_title not in tp:
            title_head = f"标题：{doc_title}"
        embed_text = (f"{DOC_PREFIX}{titlepath}\n{title_head}\n{body}" if title_head
                      else f"{DOC_PREFIX}{titlepath}\n{body}")
        leaf_chunks.append(Chunk(
            chunk_id=cid,
            parent_id=pid,
            is_doc_root=(not has_heading and idx == 0),
            title_level=level,
            title_path=tp,
            text=body,
            embed_text=embed_text,
            start_line=start_line,
            end_line=end_line,
            content_hash=_sha(body),
            chunk_index=idx,
            metadata=dict(metadata),
        ))

    # ---- 父块：H2 及以下标题建父块（H1 不建）----
    # user_document 与 user_profile 采用一致的父子链策略
    parent_chunks = _build_parents(root, file_path, parent_max_chars, metadata)

    return parent_chunks + leaf_chunks


def _split_text_into_parts(text, max_chars):
    """无标题文档：按段落切分并贪心合并。"""
    blocks = [b for b in text.split("\n\n") if b.strip()]
    if not blocks:
        return []
    out = []
    cur = []
    cur_len = 0
    for b in blocks:
        add = len(b) + (2 if cur else 0)
        if cur and cur_len + add > max_chars:
            out.append("\n\n".join(cur))
            cur, cur_len = [], 0
            add = len(b)
        cur.append(b)
        cur_len += add
    if cur:
        out.append("\n\n".join(cur))
    return out


def _file_stem(file_path):
    if not file_path:
        return "document"
    import os
    return os.path.splitext(os.path.basename(str(file_path)))[0] or "document"


def _parent_block_ids(root, file_path):
    """内部节点（level>=2，即建父块的标题）的父块 chunk_id。"""
    ids = {}
    for node in _iter_all(root):
        if node.level < 2 or not node.children:
            continue
        tp = "/" + "/".join(_title_path(node))
        ids[id(node)] = parent_chunk_id(file_path, tp)
    return ids


def _parent_id_for(node, parent_ids):
    """parent_id：指向直接父标题对应的父块；父为 H1 或不存在则 None。"""
    p = node.parent
    if p is None:
        return None
    return parent_ids.get(id(p))


def _build_parents(root, file_path, parent_max_chars, metadata):
    """构建父块链：H2 及以下标题建父块，parent_id 指向最近的上级父块。"""
    parent_ids = _parent_block_ids(root, file_path)

    chunks = []
    for node in _iter_all(root):
        if node.level < 2 or not node.children:
            continue
        cid = parent_ids[id(node)]
        parent = node.parent
        parent_id = parent_ids.get(id(parent)) if parent is not None else None

        subtree = _subtree_text(node)
        if len(subtree) > parent_max_chars:
            body = _degraded_parent_text(node)
        else:
            body = subtree

        tp = _title_path(node)
        chunks.append(Chunk(
            chunk_id=cid,
            parent_id=parent_id,
            is_parent=True,
            title_level=node.level,
            title_path=tp,
            text=body,
            embed_text=None,
            start_line=node.line,
            end_line=node.body_end,
            content_hash=_sha(body),
            chunk_index=-1,
            metadata=dict(metadata),
        ))
    return chunks


def _subtree_text(node):
    """节点完整子树文本：自身正文 + 所有子标题内容。"""
    parts = []
    own = _node_text(node)
    if own:
        parts.append(own)
    for c in node.children:
        t = _subtree_text(c)
        if t:
            parts.append(t)
    return "\n\n".join(parts)


def _degraded_parent_text(node):
    """父块降级内容：自身正文 + 各直接子标题的 titlepath + 其首段摘要（200 字符）。"""
    parts = []
    own = _node_text(node)
    if own:
        parts.append(own)
    for c in node.children:
        parts.append("/" + "/".join(_title_path(c)))
        first = _first_paragraph(c)
        if first:
            parts.append(first[:200])
    return "\n\n".join(parts)
