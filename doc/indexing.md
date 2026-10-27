# 文档切片规则与数据库设计

本文档描述 keeper 检索层（`search/`）当前的切片策略（Markdown 结构化切片 + PDF
按页切片）、索引流程与查询策略。

> **出处**：这一层最初是独立项目 k-search，2026-10 整体迁入 `keeper/search/`。
> 迁入时按 keeper 的约定改了三处：**运行时数据落到 `~/.keeper/search/`**、
> **被索引目录默认取「我的空间」**、**环境变量覆盖打开**（`KEEPER_SEARCH_*`）。
> 切片 / 索引 / 检索的算法与参数**未改动**。

实现位置：

- 切片：`search/ingest/chunk.py`（Markdown）、`search/ingest/pdf.py`（PDF）
- 索引编排：`search/indexer.py`
- 向量化：`search/ingest/embed.py`
- 存储：`search/store/db.py`
- 查询：`search/query.py` + `search/store/db.py:search_similar`
- 装配（连接 + 模型 + 索引 + 检索的组合入口）：`search/service.py`

> **谁在用它**：界面侧的「搜索」页（索引管理 + 查询）与 agent 侧的 `kb.*` 工具
> （见 [`kb-pdf-pages.md`](kb-pdf-pages.md)）都走这一层。

---

## 一、切片规则

### 1. 解析

- 识别 ATX 标题（`#` ~ `######`），构建标题树，记录每个标题的 level、文本、起始行号。
- **忽略围栏代码块和 YAML front matter 内的 `#`**，不误判为标题。
- 优先在标题边界断开，不按固定字符硬切。

### 2. 块级元素识别

在每个标题节点的内容范围内，按 Markdown 语法识别块级元素：

| 块类型 | 规则 |
| :--- | :--- |
| 围栏代码块 | ` ``` ` 或 ` ~~~ ` 开头到匹配结束，整体一块 |
| 表格 | 连续的以 `|` 分隔的行，整体一块 |
| 连续列表 | 连续的 `-` / `*` / `+` / 数字. 开头行，整体一块 |
| 普通段落 | 空行分隔，一块 |

**代码块和表格是不可分割单元**：无论多长，都不从中间切断。

### 3. 内容单元：每个标题**自己的正文**各算一个单元

> **这里踩过一个大坑（已修）**：原来只把「没有子标题的标题」当叶子，于是
> **有子标题的标题，它自己的正文（章节引子）就没人要了**——
> - **H1 的正文**：H1 不建父块 → 这段文字进不了任何块 → **彻底丢失**；
> - **H2+ 的正文**：只进了父块 → 而父块 `is_parent=1` **不参与检索** → **搜不到**。
>
> 实测：`# 第一部分` 下面那段引子里的词，搜出来是 **0 条**。
>
> 现在：**每个标题节点自己的正文都是一个内容单元**（它可能是章节引子，
> 也可能是一个没有子标题的标题的全部内容）。

对每个内容单元，按其内容总长度分三种情况：

**3.1 内容 < `CHUNK_MIN_CHARS`（过短）**
- **有子标题的标题**：**单独成块**（保留自己的 `titlepath`）。
  - 不与同级合并：它的后面紧跟的是**子标题的内容**，合并会跳过中间内容；
  - 不向上合并：向上合并会把这段文字记到父章节名下，丢掉它自己的章节身份。
- **无子标题的标题**（普通叶子）：优先与同级相邻叶子合并，合并后不超过 `CHUNK_MAX_CHARS`；
  - 同级无法合并时，向上合并到父节点，父节点作为一个 chunk；
  - 合并后的 chunk 使用父节点的 `titlepath` 和 `title_level`。

**3.2 `CHUNK_MIN_CHARS` ≤ 内容 ≤ `CHUNK_MAX_CHARS`（正常）**
- 叶子内容整体作为一个 chunk。

**3.3 内容 > `CHUNK_MAX_CHARS`（过长）**
1. 将叶子内容按块级元素切分。
2. **贪心合并**：从前往后依次装入当前 chunk，再加一块就超 `CHUNK_MAX_CHARS` 时封口，从下一块开始新 chunk。
3. **代码块/表格特权**：自身超过 `CHUNK_MAX_CHARS` 的代码块或表格，整体作为一个超长 chunk，不切。
4. **普通段落兜底**：单个普通段落自身超过 `CHUNK_MAX_CHARS` 时，按句子边界切（中文 `。！？；`，英文 `.?!`），切点落在标点之后。
5. **不使用 overlap**：相邻 chunk 之间不复制任何文本。语义连续性由句子边界 + `titlepath` + 检索侧相邻扩展/父块回溯保障。

### 4. titlepath 拼进嵌入文本（**文件名不进向量**）

仅叶子 chunk 生成 `embed_text`：

```
task: search result | text: {titlepath}
[标题：{文档标题}]
{text}
```

示例：`task: search result | text: /项目复盘/问题分析\n具体原因是……`

- `titlepath` 为斜杠分隔的标题路径；无标题文档用文件名（`/readme`）。
- **文件名不进嵌入文本**：文件名走关键词通道的**独立高权重列**（`name_tokens`，
  BM25 权重 8.0）——那是「字面匹配」的事，向量通道只管语义。
  （曾经把文件名拼进 `embed_text`，实测语义噪声大、收益小，已移除。）
- 「标题」行只在**文档标题与文件名不同**时出现（文档标题来自 frontmatter `title`
  或 PDF 元数据 `/Title`；文件名是文件去掉扩展名）。

> **改了这段的构造方式，必须把 `config.EMBED_FORMAT_VERSION` +1。**
> 向量复用的判据是 `content_hash`（只算正文），**看不到 `embed_text` 的变化**——
> 不换版本号就会「改了格式等于没改」（旧向量被原样复用）。
> 版本号变了的后果：prescreen 不再跳过这些文件、旧向量不再复用 → 全部重新嵌入。

### 5. 父子链（建到 H2 为止）

**5.1 建链范围**
- **H2 及以下标题建父块；H1 不建可回溯父块。**
- H2 父块的 `parent_id = NULL`，是回溯止点。
- H3 父块指向 H2，H4 指向 H3，以此类推。
- 叶子 chunk 的 `parent_id` 指向其直接父标题对应的父块；若直接父标题是 H1，则 `parent_id = NULL`。
- **有子标题的标题，它自己的正文（章节引子）单独成块** → `parent_id` 指向
  **它自己**的父块（它属于「它这一节」）；无子标题的标题按**叶子**处理，
  指向父标题的父块。
- 无子标题的标题不再建父块（避免内容重复）。

**5.2 父块内容**
- 默认：父块内容 = 该标题的完整子树（自身正文 + 所有子标题内容）。
- 降级：子树总长 > `PARENT_MAX_CHARS` 时，父块内容降级为：
  - 该标题自身正文（若有）
  - 各直接子标题的 `titlepath` 列表
  - 每个直接子标题下首段的摘要（首 200 字符）

**5.3 父块存储**
- `is_parent = 1`，`vector = NULL`，`embed_text = NULL`。
- 父块不参与向量检索，仅用于命中叶子后按 `parent_id` 回溯。

### 6. 两类文档的差异

| `doc_type` | 父子链 | 说明 |
| :--- | :--- | :--- |
| `user_document` | 建多级父块到 H2 | 笔记、文档、资料，需要章节级上下文 |
| `user_profile` | 建多级父块到 H2（与 user_document 一致） | 偏好、习惯、简介同样保留章节上下文 |

### 7. 行号与顺序

- 每个 chunk 记录原始 Markdown 的 `start_line` / `end_line`（1-based）。
- 叶子 chunk 按在原文中出现的顺序分配 `chunk_index`（从 0 开始），用于检索后相邻扩展。
- 父块不参与 `chunk_index` 编号（固定为 -1）。

### 8. 边界情况

| 情况 | 处理 |
| :--- | :--- |
| 无标题的 Markdown | 整个文档作为一个叶子 chunk；超长按 3.3 处理；`titlepath` 用文件名 |
| 只有 H1 无下级标题 | H1 内容作为叶子 chunk，不建父块 |
| 有子标题的标题 | 它自己的正文（章节引子）**单独成块**（见 3.1），保留自己的 `titlepath` |
| 标题层级跳跃（H2 → H4） | 按实际层级建链，H4 父块指向最近的上级父块（H2） |
| 空标题（标题下无内容） | 跳过，不生成空 chunk |
| 单个句子超 `CHUNK_MAX_CHARS` | 允许该块超长，不强行切 |

### 9. PDF 的切片（`search/ingest/pdf.py`）

PDF 没有 Markdown 那样的显式标题树，但**大纲（书签 / outline）就是它**——而且是带层级的树。
实测用户空间里的真实文件：87 页的技术 PDF 有 86 条书签（几乎每页一个标题），
275 页的书有 123 条。**所以章节标题直接读书签就行，不用靠字号猜**。

于是 PDF 的切法与 Markdown **完全同构**：

```
文档标题（元数据 /Title → 缺失时用文件名）
  └─ 章节标题（大纲的层级，原样保留嵌套）
       └─ 章节标题（子节点）
```

`titlepath` 一填好，**标题就自动参与检索了**——嵌入文本（`DOC_PREFIX` + titlepath）
与 BM25（`chunks.title_tokens`）本来就吃 titlepath，**这部分一行都不用改**。
文件名走**独立的 `name_tokens` 列**（高权重，见「二、数据库设计」），所以按文件名搜得到。

| 环节 | 处理 |
| :--- | :--- |
| 提取 | `pypdf` 逐页 `extract_text()`（约 25ms/页） |
| 章节划分 | 大纲树摊平成「章节 → 页区间」；**每个大纲节点都成一段**（父章节自己那几页 = 本页 → 第一个子章节的前一页），第一个章节之前的封面页也补一段 |
| 页眉页脚 | 每页首尾 2 行，若同一文本出现在 ≥60% 的页上 → 判定为页眉页脚并剔除 |
| 跨页段落 | 上页末行不是句末标点 → 与下页首行接成同一段 |
| 行 → 段 | PDF 的「行」是排版产物：一句话会被拆成好几行 → 段内硬换行合并（中文直接接，西文补空格） |
| 中文字距 | PPT 常把字距拉开（提取成"摘 要"）→ 压掉**中文**单字之间的空格。**英文不压**：带空格的英文 jieba 仍能切成完整词，压掉反而搜不到 |
| 定位 | 每个 chunk 带 `locator`（"第 12 页" / "第 12-13 页"）+ 数值列 `page_start`/`page_end`（精确页码，1-based 闭区间） |
| 扫描件 | **跳过**：全文文本 < 100 字 → 视为图片型 PDF，不索引（只索引章节标题等于"能搜到名字、搜不到内容"，反而误导） |

> **定位必须是「精确页」，不是「章节页区间」**（2026-10 修正）。
> 原来叶子块的 `locator` 写的是**它所属章节的页区间**（"第 5-12 页"），
> 于是「命中一个块 → 拿区间去渲染页面」可能渲染出一整页都不含命中内容的图
> —— agent 看着图编答案。
> 现在：`_join_pages` 额外返回每页在正文里的字符区间，`_paragraphs_spans`
> 给每个段落带上它在原文里的字符区间，两者求交即得段落落在哪几页 →
> 块的 `page_start`/`page_end`。
> **正文一个字都没变** → `content_hash` 不变 → 旧向量全部复用（不重嵌不花钱）。
> 配套：新增 `config.CHUNK_FORMAT_VERSION` + `chunks.chunk_version` 列，
> 让 prescreen 不再跳过这些文件（详见 [kb-pdf-pages.md](kb-pdf-pages.md)）。

**已知短板**（纯文本方案的固有边界）：表格会被拉平成数字和竖线（搜不到）、
双栏排版会串行、图里的文字丢失。要解决得换提取实现（`pymupdf` / `pdfplumber`），
本模块接口按「可替换」设计。

> 这条短板在「把 PDF 页面给 agent」这件事上有解：不用文字层，改用**整页渲染的
> 截图**（版面信息全在）。见 [kb-pdf-pages.md](kb-pdf-pages.md)。

**成本**（1000 份 × 平均 50 页）：提取约 21 分钟、嵌入约 25～50 分钟，首次全量约 1 小时
（后台跑）；之后只有改动过的 PDF 才重做（stat 预筛直接跳过）。

---

## 二、数据库设计（两个表）

### `documents`：文件级元数据

```sql
CREATE TABLE documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path     TEXT NOT NULL UNIQUE,
    doc_type      TEXT NOT NULL,          -- user_profile / user_document
    title         TEXT,
    mtime         REAL NOT NULL,
    size          INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,          -- 文件级 hash，快速判断是否变更
    index_status  TEXT NOT NULL DEFAULT 'pending',  -- pending/indexing/done/failed
    indexed_at    REAL,
    error         TEXT,
    page_count    INTEGER,                       -- PDF 的总页数（Markdown 为 NULL）
    source_kind   TEXT,                          -- markdown / pdf
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
```

### `chunks`：切片与向量

```sql
CREATE TABLE chunks (
    chunk_id      TEXT PRIMARY KEY,       -- 叶子: sha256(file_path+titlepath+chunk_index)
                                          -- 父块: sha256(file_path+titlepath+"parent")
    doc_id        INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    parent_id     TEXT,                   -- 叶子指向直接父块；H2父块为 NULL
    is_parent     INTEGER NOT NULL DEFAULT 0,
    is_doc_root   INTEGER NOT NULL DEFAULT 0,
    title_level   INTEGER NOT NULL,       -- 1=H1, 2=H2 ...
    titlepath     TEXT NOT NULL,
    text          TEXT NOT NULL,
    embed_text    TEXT,                   -- 仅叶子有值
    vector        BLOB,                   -- 仅叶子有值，float32；父块为 NULL
    start_line    INTEGER NOT NULL,
    end_line      INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,
    chunk_index   INTEGER NOT NULL,
    tokens        TEXT,                       -- jieba 预分词后的正文（空格分隔）
    title_tokens  TEXT,                       -- jieba 预分词后的「章节路径」（块级字段）
    name_tokens   TEXT,                       -- 文件名 token（整体规范化形式 + 分词形式）
    doc_title_tokens TEXT,                    -- 文档级标题 token（路径第一级 + 文档标题）
    body_len      INTEGER,                   -- tokens 的词数（长度归一用）
    title_len     INTEGER,                   -- title_tokens 的词数（长度归一用）
    locator       TEXT,                       -- 定位串：PDF 形如"第 12 页"；Markdown 为 NULL
    page_start    INTEGER,                    -- PDF 叶子块的精确起始页（1-based）；Markdown 为 NULL
    page_end      INTEGER,                    -- PDF 叶子块的精确结束页（1-based）
    embed_version INTEGER,                    -- 嵌入文本格式版本（config.EMBED_FORMAT_VERSION）
    chunk_version INTEGER                     -- 切片格式版本（config.CHUNK_FORMAT_VERSION）
);

CREATE INDEX idx_chunks_doc    ON chunks(doc_id);
CREATE INDEX idx_chunks_parent ON chunks(parent_id);
CREATE INDEX idx_chunks_leaf   ON chunks(doc_id, is_parent, chunk_index);
```

> 库中只有 `documents` / `chunks` 两张业务表 + `chunks_fts` 关键词索引
> + `idx_meta`（只存一个关键词字段格式版本号）。

### `chunks_fts`：FTS5 关键词索引（外部内容表）

```sql
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    tokens, title_tokens, name_tokens, doc_title_tokens,
    content='chunks', content_rowid='rowid',
    tokenize='unicode61'
);
-- 三个触发器保证 chunks 与 chunks_fts 同步（ai / ad / au）
```

**FTS5 只做召回，不做排序**：它回答「谁含这些词」，排序由 `search/rank.py`
自己算 BM25F（原因见下）。

**四列的分工**（按「字段属于哪一层」分，不是按「哪个更重要」分）：

| 列 | 内容 | 层级 | 权重 |
| :--- | :--- | :--- | :--- |
| `tokens` | 正文 | 块级 | 1.0 |
| `title_tokens` | 章节路径（标题路径**去掉第一级**） | 块级 | 3.0 |
| `name_tokens` | **文件名** | 文档级 | **8.0** |
| `doc_title_tokens` | 文档标题（标题路径第一级 + 文档标题） | 文档级 | 3.0 |

> **标题为什么也走同一套逻辑、却不能塞在同一层**：
> 标题路径的第一级（H1 / PDF 的文档标题 / 无标题文档的文件名）会出现在该文档的
> **每一个** chunk 里——它和文件名是同一类东西：**文档级字段**。
> 所以它必须单独成列、单独按文档算 tf，而不是当成块级 tf 重复计入每一块。

> FTS5 虚表的列在 `CREATE` 时就定死了、**不能 ALTER**；列数变了只能删了重建
> （`drop` + `recreate` + `rebuild`，**不需要重新嵌入向量**）。
> `store/db.py::_ensure_fts` 会在连库时自动检测列数变化并重建。

**为什么索引的是「预分词文本」而不是原文**（实测踩坑）：

| 做法 | 查询「任务」 | 查询「接口分组」 | 结论 |
| :--- | :--- | :--- | :--- |
| `tokenize='trigram'` 索引原文 | ❌ 0 行（trigram 要求 ≥3 字符） | ✅ | 2 字中文词直接失效 |
| `tokenize='unicode61'` 索引原文 | ❌ 0 行（连续中文被当成 1 个 token） | ❌ | 中文基本不可用 |
| **unicode61 + jieba 预分词文本** | ✅ 命中 | ✅（拆成 `"接口" OR "分组"`） | **采用** |

所以索引时用 jieba 把正文与标题路径切成词、空格连接后写入各列，
查询时用同样的规则切查询词，拼成 FTS5 的 MATCH 表达式（`"任务"`，多词用 `OR` 连接）。

**文件名匹配为什么要有专门的列**：

- 文件名里有标点（`00【大纲】2026版_v2.md`），jieba 一分就碎 → **搜完整文件名匹配不上**。
  所以 `name_tokens` 额外存一份**整体规范化形式**（全角转半角、去标点空白、转小写、
  去下划线——unicode61 把下划线当分隔符），unicode61 会把连续汉字/字母/数字当成
  **一个 token** → 搜完整文件名能**字面精确命中**。
- 同时保留 jieba 分词形式 → 搜「部分词」（如"大纲"）也能命中。
- 查询侧 `match_expression()` 会额外追加「查询词的整体规范化形式」，
  让完整文件名能命中 `name_tokens` 里那个整体 token。

`chunks` 表新增的列在**旧库上会自动回填**（关键词字段可以从 `chunks.text` /
`chunks.titlepath` / `documents` 现算，**不需要重新切片、更不需要重新嵌入向量**），
随后 `INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')` 重建索引。

### 关键约束

| 约束 | 说明 |
| :--- | :--- |
| **文件移动** | 只更新 `documents.file_path`，`chunks.doc_id` 不变，不触发重新嵌入 |
| **父块检索** | 检索时过滤 `is_parent = 0`，父块 `vector` 为 NULL 不参与 |
| **增量更新** | 以 `chunks.content_hash` 为单位比对，只重嵌变化的 chunk |
| **父块 ID 稳定** | 基于 `file_path + titlepath` 哈希，标题路径不变时 ID 稳定 |

### 向量检索方式

向量检索目前是**numpy 全量点积**（向量写入时已 L2 归一化，点积即余弦）：
`search_similar` 一次取出全部叶子向量 → `np.frombuffer` 成矩阵 → 一次矩阵乘。

没有 ANN 索引（sqlite-vec 等）。按实测：1000 片段时全量扫描 ~15 ms，1 万片段 ~85 ms
（其中约一半时间花在把每行 ~1KB 的正文也一起读出来）。若语料再上一个数量级，
再考虑 ANN 或进程内向量缓存。

---

## 三、索引流程（`search/indexer.py`）

1. **发现文件**：`detect.list_markdown_files` 扫描待索引目录（`.md/.markdown/.mdown/.mkd`，递归）。
   默认目录是「我的空间」（`config.default_roots()`），可用 `KEEPER_SEARCH_REPO` 覆盖。
2. **文件移动识别**（先于删除同步）：若某文件路径未入库，但库中存在 `content_hash` 相同、路径不同的文档，则只更新 `documents.file_path`，`chunks` 不动，不重新嵌入。由于 `chunk_id` 是「路径 + 标题路径 + 序号」的派生值，移动时会按新路径**原子重算 `chunk_id` 并同步 `parent_id` 引用**（否则文件一移动、下次一改内容，旧向量就全作废、整篇重嵌）。`force=True` 时跳过此步（强制重建的语义是"这个文件必须重切+重嵌"，先判成 moved 就等于没重建）。
3. **变更检测**：`content_hash` 与库中一致且 `index_status='done'` 则 `skipped`；`force=True` 强制重建。
   开启 `prescreen`（默认开）时，**先比 `mtime + size`**：两者都没变就直接 `skipped`，
   连文件都不打开（内容没变、只是 stat 变了时，把 stat 更新到最新，下轮就能直接跳过）。
4. **切片**：按文件类型分派——Markdown 走 `chunk.chunk_markdown`（标题树），
   PDF 走 `pdf.chunk_pdf`（大纲标题树 + 页级切片，见「1.9」）。两者产出的
   `Chunk` 结构完全同构，所以后面的落库、向量化、检索整条链路不用分叉。
5. **增量向量化**：对比库中 `chunks.content_hash`，只对新增/变化的叶子调用模型；父块不向量化（`vector` 保持 NULL）。`force=True` 时**不复用任何旧向量**，全部叶子重新嵌入（维度仍需与当前模型一致，否则本就要重嵌）。
6. **落库**：`sync_chunks` 以 `chunk_id` 为主键 upsert，并删除本次不存在的旧 chunk。
7. **删除同步**：数据库中存在但磁盘已不存在的文件，其文档与 chunk 一并删除。

---

## 四、查询策略

入口：`POST /search`（见 `api.py`）。

1. **向量化**：`embedder.embed_query(query)` 加前缀 `task: search query | text: ` 后取向量（与索引同维、同归一化）。
2. **相似度检索**：`search_similar` 只扫描 `is_parent = 0` 的叶子 chunk，与查询向量点积（= 余弦），按 `score` 降序取 `top_k`（默认 10）。
3. **混合重排（默认，`mode="hybrid"`）**：见下方「混合检索」。
4. **过滤**：`path_prefix`（`file_path LIKE prefix%`）、`title`（`title LIKE %title%`）、`doc_type`。
5. **父块回溯**：命中叶子后按 `parent_id` 取回父块（章节级上下文），随结果一并返回 `parent` 字段。
6. **返回**：每条结果含 `chunk_id`、`parent_id`、`titlepath`（斜杠路径）、`title_level`、`text`/`content`、`start_line`/`end_line`、`chunk_index`、`path`、`title`、`score`、`vec_score`、`kw_score`、`matched_terms`（实际命中的查询词，前端高亮用）、`page`（PDF 起始页码 1-based，Markdown 为 null；前端用它「跳到那一页」）、`parent`、`locator`（PDF 是"第 12 页"，Markdown 为 null）、`source_kind`（markdown / pdf）。

### 混合检索（向量召回 + FTS5 关键词召回）

**为什么需要**：740M 级的向量模型在**短中文查询**上区分度有限——实测查询「任务」时，
全部候选的余弦得分挤在 `0.70~0.73`（差值 0.019，属噪声级排序），排在第一的片段可能
**完全不含查询词**（曾召回「接口分组」的路由表）。而用户的直觉是"我输入的词就该在结果里"。

**检索通道**（`mode`）：

| mode | 说明 |
| :--- | :--- |
| `hybrid` | 两路各召回 top-N 后融合（默认） |
| `vector` | 只用向量召回 |
| `keyword` | 只用 FTS5/BM25（**不加载模型**，也不需要 embedding） |

**两路召回**：

1. 向量路：`search_similar` → numpy 批量点积（一次 `np.frombuffer` + 一次矩阵乘），取前 `VECTOR_TOP_N`（50）。
2. 关键词路：`search_keyword` → FTS5 `MATCH` 召回 → **自己算 BM25F**（`search/rank.py`），取前 `KEYWORD_TOP_N`（50）。

### 关键词打分：自己算 BM25F（`search/rank.py`）

**为什么不用 FTS5 自带的 `bm25()`**：FTS5 的 df（文档频率）是**按行**算的，
而索引里的行是 **chunk**。文档级字段（文件名、文档标题）会写进该文档的**每一个** chunk：

```
170 块的大 PDF  →  df = 170 / 241
idf = log((241 - 170 + 0.5) / (170 + 0.5)) = -0.87      ← 负数
→ FTS5 把负 idf 钳到 1e-6 → 文件名这一路等于完全失效
```

更糟的是 FTS5 **一个词只算一个 idf**（跨列共用）：文件名里出现过的词，
它正文里的那些命中会被一起"毒化"（df 被文件名撑大 → idf 归零）。

**改成教科书里的 BM25F**：

```
idf(t) = log(1 + (N_docs - df_doc(t) + 0.5) / (df_doc(t) + 0.5))
          ↑ N_docs = 文档数；df_doc(t) = 含这个词的**文档**数（不是 chunk 数）
            用 log(1+x) 变体 → 恒为正，不会出现「负 idf 被钳成 1e-6」

score(chunk) = Σ_t idf(t) · [ 块级：Σ_f w_f · tf_f(chunk,t) / (1 - b + b·len_f/avglen_f) ]
             + 文档级：Σ_t idf(t) · [ w_name·tf_name(doc,t) + w_title·tf_title(doc,t) ]
```

| 字段 | 层级 | tf 怎么算 | 权重 |
| :--- | :--- | :--- | :--- |
| 正文 | 块级 | 该块里出现几次 | 1.0 |
| 章节路径 | 块级 | 该块里出现几次 | 3.0 |
| **文件名** | 文档级 | 该**文档**的名字里出现几次（**只算一次**） | **8.0** |
| 文档标题 | 文档级 | 该文档的标题里出现几次（只算一次） | 3.0 |

三条关键设计：

1. **idf 用文档级 df** —— 文件名出现在 1 个文档里 → df=1 → idf 正常，
   8 倍权重才真的起作用（实测：搜那个 170 块 PDF 的文件名，BM25 分从 `2e-06` 变成 `13.4`）。
2. **文档级字段的 tf 按文档算一次**，加分加在该文档的**最优块**上
   → 搜文件名命中的是"那个文件"，而不是那个文件的一堆块（不刷屏）。
3. **长度归一按字段分别做**（正文按块长、章节路径按路径长；文件名/标题定长不归一）。

**代价与取舍**：

- 打分从 FTS5 的 C 代码搬到了 Python（`rank.py`）：实测关键词通道 **0.3~1.1 ms/查询**
  （7 文档 / 253 块），量级没变；但语料到万级 chunk 时需要重新评估。
- FTS5 仍负责**召回**（谁含这些词，C 速度）+ 给候选排个序（好截断
  `KEYWORD_CANDIDATE_LIMIT = 400` 个候选），最终名次由 BM25F 决定。
- `KEYWORD_FORMAT_VERSION` 控制关键词字段的构造规则；变了会**自动回填**
  （从 `chunks.text` / `chunks.titlepath` / `documents` 现算，**不用重新嵌入向量**）。

**融合方式**（`fusion`）：

| fusion | 公式 | 特点 |
| :--- | :--- | :--- |
| `noisy_or`（默认） | `final = 1 - (1 - vec_norm) * (1 - kw_norm)` | 证据充分度：一条证据够硬分就够高，两路都命中则更高；**不被另一路的权重压住上限** |
| `weighted` | `final = alpha*vec_norm + (1-alpha)*kw_norm` | 旧方式；`alpha` 越大越偏语义 |
| ~~`rrf`~~ | `Σ 1/(k + rank)` | 只吃名次，分数不是 0~1 的置信度、**没法卡门槛** → 已不支持，传了会记一条日志并回退 `noisy_or` |

- 查询中**没有任何字面命中**时，关键词路为空，融合自动退化为纯向量分（纯语义场景不劣化）。
- 结果附 `vec_score`（向量那一路的 0~1 分）、`kw_score`（关键词那一路的 0~1 分）
  与 `score`（**最终分，0~1**），便于展示与调参。

### 最终分（0~1）与分数下限

**要解决的问题**：召回得太多不一定有用——搜「的」也能搜出一堆东西。
所以最终分必须能回答一个问题：**这条结果有多可信？** 分数要落在 **0~1**，
并且**跟语料大小无关**（否则换个库门槛就失效了）。

**两条通道各自先换算成 0~1 的"证据充分度"，再用 noisy-OR 合并**：

```
最终分 = 1 - (1 - 向量分) * (1 - 关键词分)
```

#### 向量路：余弦 → 0~1

```
向量分 = clamp((余弦 - SCORE_VEC_LO) / (SCORE_VEC_HI - SCORE_VEC_LO), 0, 1)
```

`SCORE_VEC_LO = 0.65`（低于它基本不相关，记 0 分）、`SCORE_VEC_HI = 0.80`（记满分）。
实测（embeddinggemma，7 篇语料）：真命中 `0.74~0.80`，泛用词 `0.71~0.77`，
语料里根本没有的词 `0.64~0.73`。语料越大、区分度越高。

#### 关键词路：证据充分度

**不能用 BM25F 原始分**——它的绝对值会随语料规模漂移，没法跟固定门槛比：

| 查询 | 原始分 | 说明 |
| :--- | :--- | :--- |
| 北京大学 | **13.39** | 罕见词 + 命中在文件名 |
| 向量检索 | 1.36 | |
| 实现 | 0.29 | 到处都是的词 |
| 的 | **0.81** | idf 很小，但 name(8) + path(3) + body(1) 三处叠加后仍然过了 0.5 的线 |

同一个固定基准线（0.5）面前，「的」和「北京大学」无法区分——**必须换一个量纲**。

改用两个跟语料无关的因子相乘：

```
关键词分 = 证据充分度 = 这个词有多稀有 × 命中在多显眼的位置
```

- **有多稀有** = 相对稀有度 = `idf(t) / log(1 + 文档数)`（0~1，见 `rank.py::_idf_norm`）。
  除以 `log(1+N)` 是因为 idf 是对数、绝对值随语料漂移；除完就与语料大小无关了：

  | 词 | 出现在几个文件里 | 相对稀有度 |
  | :--- | :--- | :--- |
  | 的 | 7 / 7 | 0.09（到处都是，说明不了什么） |
  | 实现 | 6 / 7 | 0.10 |
  | 系统、缓存 | 3 / 7 | 0.40 |
  | 北京大学 | 1 / 7 | 0.81（很难得碰上，说服力强） |

- **多显眼的位置**（`config.KEYWORD_EVIDENCE_WEIGHTS`）：
  文件名 `1.0` > 文档标题 `0.9` > 章节路径 `0.8` > 正文 `0.8`。
  命中在文件名里 → 最可能就是用户要找的那个文件；只在正文里出现 → 可能是巧合。

- 各个查询词的证据用 **noisy-OR** 合并（命中两个罕见词 > 只命中一个）：
  `1 - Π(1 - 证据_t)`。

> 证据充分度**不套长度惩罚**：长度归一是排序的事——一段很长的文字里提到
> 「北京大学」，这条证据依然很硬，不该因为段落长就被打折。

#### 分数下限（`min_score`）

| 场景 | 门槛 | 常量 | 含义 |
| :--- | :--- | :--- | :--- |
| **文件目录搜索**（人看） | `0.5` | `config.MIN_SCORE_UI` | 低于它就不展示 |
| **agent 搜索**（模型当答案用） | `0.8` | `config.MIN_SCORE_AGENT` | 宁缺毋滥 |

- `min_score` 不传 = 用 `MIN_SCORE_UI`（0.5）；传 `0` = **不过滤**。
- 接口：`POST /search/query` 的 `min_score` 字段（0~1）。
- Python：`SearchService.search(..., min_score=config.MIN_SCORE_AGENT)`。

**实测（真实索引，7 篇文档）**：

| 查询 | 最终分 | 过 0.5 | 过 0.8 |
| :--- | :--- | :--- | :--- |
| 北京大学 | 0.954 | 20 | 3 |
| 缓存穿透 | 0.886 | 12 | 3 |
| 提示词工程 | 1.000 | 20 | 20 |
| 的 | 0.433 | **0** | 0 |
| 实现 | 0.766 | 20 | **0** |
| 今天天气怎么样 | 0.000 | **0** | 0 |
| 布隆过滤器 | 0.199 | **0** | 0 |

「的」「实现」这类泛用词能过 UI 门槛（人看，扫一眼就知道有没有用），
但过不了 agent 门槛（模型会当答案用，宁缺毋滥）——这正是两个门槛的分工。

> ⚠️ 门槛是**绝对**的，依赖语料规模与模型；语料从 7 篇涨到几千篇后，
> 中频词的相对稀有度会明显上升（同样的 df，占比变小 → 更稀有），
> 届时可按需调 `SCORE_VEC_LO` / `SCORE_VEC_HI` / `KEYWORD_EVIDENCE_WEIGHTS`。

**实测（真实索引，8 条查询的 Top1 命中 / Top3 召回）**：

| 模式 | Top1 命中 | Top3 召回 |
| :--- | :--- | :--- |
| 纯向量 | 3/8 | 5/8 |
| 纯关键词 | 8/8 | 8/8 |
| RRF | 5/8 | 7/8 |
| **加权** | **6/8** | **7/8**（alpha=0.5 时 8/8） |

> 这张表是**迁移时**的对比数据（当时的默认是加权）；现在的默认是 `noisy_or`，
> 它不看名次只看证据充分度，因此不能用"Top1 命中率"这种方式衡量。

典型修正：查询「任务」纯向量 Top1 是「七、接口分组」（**不含该词**），混合后 Top1 变成
「产物设计/一、目标」（含「挂在消息 / 任务上」）。

**性能**：

| 阶段 | 旧（纯 Python BM25，每次查询重建全库分词） | 现（FTS5 召回 + BM25F 打分） |
| :--- | :--- | :--- |
| 关键词通道 | 232.6 ms（且每次重算，内存 106 MB） | **0.3~1.1 ms**（7 文档 / 253 块实测） |
| 混合检索总耗时 | ≈ 243 ms | **16.2 ms** |
| 1 万片段混合检索 | ≈ 2.3 s（内存 ~1 GB） | **136.7 ms** |

> 1 万片段时瓶颈已转移到**向量路的全量扫描**（~85 ms，主要是把每行 ~1 KB 的正文也一起读出），
> 需要时可改成两阶段取数或上 ANN。
> BM25F 的打分在 Python 侧（`rank.py`），候选上限 `KEYWORD_CANDIDATE_LIMIT = 400`，
> 语料到万级 chunk 时要重新评估这一段耗时。

**切换方式**：
- 配置项：`config.SEARCH_MODE` / `config.FUSION_MODE` / `config.HYBRID_ALPHA`。
- 环境变量：`KEEPER_SEARCH_MODE` / `KEEPER_SEARCH_FUSION` / `KEEPER_SEARCH_HYBRID_ALPHA`。
- 调用级：`SearchService.search(..., mode="keyword", fusion="weighted", alpha=0.5)`。

> **融合方式与向量权重不开放给前端调节**，由服务端 config 定死（默认
> `FUSION_MODE="noisy_or"`）。理由：这两个参数直接决定最终分是不是 0~1 的
> 置信度，也就决定了分数门槛还有没有意义——交给用户调，很容易被调成一个
> "看起来能搜到、但分数全都不准"的状态。前端只留「检索通道」这一个开关
> （纯向量 / 纯关键词 / 混合），因为它改变的是**召回范围**，不是**打分口径**。

> 迁移说明：k-search 原本还有 CLI（`cli.py`）、HTTP 端点（`api.py`）与一个搜索前端，
> 这些**不在迁移范围内**——keeper 里检索层的入口是 `search/service.py`。
> 将来接 agent 工具或「我的空间」页的搜索框时，直接用 `SearchService`。

---

## 五、可配置参数（`search/config.py`）

| 参数 | 含义 | 默认 |
| :--- | :--- | :--- |
| `CHUNK_MIN_CHARS` | 叶子内容短于此值时与同级相邻叶子合并 | `200` |
| `CHUNK_MAX_CHARS` | 叶子内容上限 | `1000` |
| `PARENT_MAX_CHARS` | 父块子树内容上限，超出降级 | `2000` |
| `DOC_TYPE` | 默认文档类型 | `"user_document"` |
| `DEFAULT_TOP_K` | 默认返回条数 | `10` |
| `SEARCH_MODE` | 检索模式：`hybrid` / `vector` / `keyword` | `"hybrid"` |
| `FUSION_MODE` | 融合方式：`noisy_or` / `weighted` | `"noisy_or"` |
| `RRF_K` | RRF 常数（`rrf` 已不支持，留着备用） | `60` |
| `VECTOR_TOP_N` | 向量召回候选数 | `50` |
| `KEYWORD_TOP_N` | 关键词召回候选数 | `50` |
| `HYBRID_ALPHA` | weighted 模式下向量权重 0~1，其余给关键词 | `0.65` |
| `MIN_SCORE_UI` | **文件目录搜索**的展示门槛（最终分 0~1） | `0.5` |
| `MIN_SCORE_AGENT` | **agent 搜索**的可信门槛（0~1） | `0.8` |
| `SCORE_VEC_LO` | 向量路余弦 ≤ 它记 0 分 | `0.65` |
| `SCORE_VEC_HI` | 向量路余弦 ≥ 它记满分 | `0.80` |
| `KEYWORD_EVIDENCE_WEIGHTS` | 证据充分度里各位置的贡献：文件名 / 文档标题 / 章节路径 / 正文 | `{1.0, 0.9, 0.8, 0.8}` |
| `KEYWORD_K1` | 词频饱和参数 k1（出现次数越多，边际收益越小） | `1.2` |
| `KEYWORD_WEIGHTS` | BM25F 的字段权重：正文 / 章节路径 / 文件名 / 文档标题 | `{1.0, 3.0, 8.0, 3.0}` |
| `KEYWORD_B` | BM25F 的长度归一强度 b（0 = 不归一） | `0.75` |
| `KEYWORD_CANDIDATE_LIMIT` | 关键词通道的候选上限（FTS5 召回多少个来打分） | `400` |
| `KEYWORD_FORMAT_VERSION` | 关键词字段格式版本。**改了各 token 列的构造方式就必须 +1**（会自动回填，**不用重新嵌入向量**） | `2` |
| `EMBED_BACKEND` | 向量推理后端：`auto`（探测到 GPU 就用）/ `cpu` / `gpu` | `"auto"` |
| `EMBED_FORMAT_VERSION` | 嵌入文本格式版本。**改了 `embed_text` 的构造方式就必须 +1**（否则旧向量被错误复用） | `4` |
| `SYNC_ENABLED` | 是否启用后台定时增量扫描 | `True` |
| `SYNC_INTERVAL` | 定时扫描间隔（秒） | `30.0` |
| `SYNC_FULL_INTERVAL` | 定期全量 hash 校准的间隔（秒） | `1800.0` |

这三个参数**可以在界面上改**：「搜索 → 索引管理 → 索引配置」区块。改完落到
`~/.keeper/settings.yaml` 的 `search` 段，**立即生效**（不用重启内核）——
后台扫描每轮都重新读一次配置，改间隔马上按新的来，改开关立刻起 / 停。
优先级：**用户设置 > 环境变量 > 代码默认**。

### 索引自动同步（`search/sync.py`）

索引是快照，文件是活的。用户改完一个 md、切到搜索页就要能搜到新内容——
所以索引必须**自己**跟上文件变化。三层机制，各管一段：

| 层 | 触发 | 做什么 | 延迟 | 日常成本 |
| :--- | :--- | :--- | :--- | :--- |
| **stat 预筛** | 每次扫描 | 先看 `mtime + size`，没变过的文件**连打开都不打开** | — | 3 万文件 200ms（vs 全量 hash 6～7 秒、读 2.8GB） |
| **定时增量扫描** | 每 `SYNC_INTERVAL` 秒（30 秒） | 扫描「我的空间」，只重切重嵌变化了的文件 | ≤ 30 秒 | 0.6% 单核 |
| **定期全量校准** | 每 `SYNC_FULL_INTERVAL` 秒（30 分钟） | **不信任 stat**，把所有文件重新读一遍算 hash | ≤ 30 分钟 | 每半小时 6～7 秒 |
| **桌面端文件监听** | 文件事件（秒级） | Electron 主进程 `fs.watch` → 通知内核更新那几个文件 | **~1 秒** | 接近 0 |

秒级体验靠的是**桌面端文件监听**；30 秒的定时扫描是兜底（桌面端没开、或事件丢了）。

**为什么 stat 只能预筛、不能当判据**：`mtime` 会漏报（`cp -p` / `rsync --times` / 备份恢复
保留了旧 mtime，内容其实变了），也会误报（`touch`）。只有内容 hash 是事实。所以：
日常走 stat 预筛（便宜），定期做全量 hash 校准（准）。

**并发**：定时扫描、手动「建立索引」、文件监听回调三者共用一把全局锁
（`sync.try_run_index`）。拿不到锁就**跳过这一轮**，不排队——索引关心的是文件的
最新状态，迟到的旧任务没有价值，下一轮会补上。

**文件监听只是「提示」，不是事实**（`desktop/spaceWatch.js`）：
编辑器保存是「写临时文件 + rename」，事件里的文件名可能是临时文件；事件会合并、
延迟、溢出丢失；目录改名时事件给的是目录名。所以监听只负责**把可能变了的路径
告诉内核**，真正「变了没有」仍由内核用内容 hash 判定。漏掉的事件由定时扫描兜底。

### 启动预热（`search/warmup.py`）

应用启动时，如果**索引库里已经有向量**，就在后台把 embedding 模型加载好——
用户第一次搜索就不用等 1～7 秒的模型加载（实测：预热后首次查询 0.04s，
不预热是 1～7s）。

四条前提，缺一不做：

1. **索引库里确实有向量** —— 没建过索引就不加载 462MB 权重（用户可能只聊天）；
2. **模型文件在** —— 没下载或被删了都跳过（页面会提示去下载）；
3. **没装 litert-lm** —— 跳过（关键词检索本来就不需要模型）；
4. **不阻塞启动** —— daemon 线程 + 1.5 秒延迟（把 CPU 让给启动流程），失败只记日志。

预热用的是**共享单例**（`ingest/embed.py::get_embedder`）——预热加载好的模型
就是查询要用的那个，不会出现「预热白做、查询照样慢」。

`GET /search/status` 的 `model.warmed` 会告诉你当前是不是已经预热好了。

### GPU 加速（`search/ingest/embed.py`）

`EMBED_BACKEND="auto"` 时：先**静态探测**（macOS arm64 → Metal；Windows → WebGPU/DX12；
Linux 看 `/dev/dri`、`/dev/nvidia*` 有没有），探测不到就退 CPU；探测得到则真用 GPU，
**加载或推理失败会自动回退 CPU**（回退原因记在 `Embedder.backend_note`，
`GET /search/status` 里能看到，页面上也会提示「已回退到 CPU」）。

实测（macOS M1 Pro / embeddinggemma-2-740m）：

| 后端 | 加载 | 单条查询 | 批量（8 条） |
| :--- | :--- | :--- | :--- |
| CPU | 0.2s | 62ms | 0.50s |
| GPU（Metal） | 6.4s（含 WebGPU kernel 编译） | 30ms（预热后） | 0.25s |

两点因此而来的设计：

- **加载后立刻预热一条**：GPU 第一次推理要编译 kernel（~1s），不预热的话这笔开销
  会落在用户的第一次搜索上——一个 62ms 的查询突然卡 1 秒，用户会以为卡死了。
- **GPU 只快一倍**：所以默认是 `auto` 而不是无条件 GPU；批量索引收益明显，
  单条查询差别不大。

> 换后端：`EMBED_BACKEND=cpu` / `gpu`（`config.py` 或 `KEEPER_SEARCH_*` 环境变量）。
> 换后端后建议**重建索引**：向量维度可能变化，旧向量会被静默跳过。

> 注：本次 schema 变更后，旧版索引库会在首次连接时自动迁移：保留文件级元数据（`path` → `file_path`），旧 chunk 因结构不兼容被清空，文档标记为 `pending`，重新执行一次索引即可。
>
> 迁移到 keeper 后，索引库落在 `~/.keeper/search/search.db`（原 k-search 的
> `data/ksearch.db` 已一并搬过来，旧索引可直接用，无需重建）。

---

## 六、后端接口（`search/api.py`）

前端「搜索」页（侧边栏 Keeper 菜单 → 搜索，两个 tab：索引管理 / 搜索）直接消费这组接口。

| 端点 | 方法 | 说明 |
| :--- | :--- | :--- |
| `/search/status` | GET | 首屏一次性拉全：模型状态 + 索引统计 + 索引进度 + 参数摘要 |
| `/search/stats` | GET | 索引统计（文档数 / 片段数 / 关键词条目数） |
| `/search/model/status` | GET | 模型状态（是否就绪 / 下载中 / 失败 / litert-lm 装没装） |
| `/search/model/download` | POST | 后台下载模型（`{source: auto\|hf\|modelscope}`） |
| `/search/index` | POST | 建索引（**后台跑**，`{roots?, force?, doc_type?, paths?}`；`paths` = 只索引这几个文件，桌面端文件监听用；进度看 `/search/status`） |
| `/search/config` | GET / POST | 读 / 写索引同步配置（间隔与开关；保存后立即生效） |
| `/search/query` | POST | 检索（`{query, top_k, mode, fusion, alpha, min_score, neighbor_span, ...}`；`min_score` = 最终分下限 0~1，不传用 `MIN_SCORE_UI`） |
| `/search/documents` | GET | 已索引文档列表 |
| `/search/documents/{id}` | GET / DELETE | 文档详情 / 删除该文档及其片段 |
| `/search/documents/{id}/chunks` | DELETE | 只删该文档的片段，保留文件记录 |
| `/search/chunks/{cid}` | DELETE | 删除单个片段 |
| `/search/chunks/{cid}/siblings` | GET | 该片段的相邻兄弟片段 |
| `/search/cache/clear` | POST | 清空索引（`{drop_documents}`） |

**两个实现上的要点**：

1. **端点是同步的（`def` 而不是 `async def`）**。检索是 sqlite3 + numpy 的阻塞调用
   （向量走全量点积），写成 `async def` 会把事件循环卡住，keeper 其它在跑的请求
   （对话流式输出）全被拖死。FastAPI 对 `def` 端点会自动丢到线程池。
2. **embedding 模型跨请求复用，SQLite 连接每次新建**。模型权重 462MB，每个请求
   重载一次不可接受；而 sqlite3 连接不能跨线程共享（线程池会把它甩到别的线程上）。

---

## 六点五、关键词索引与向量索引是**两条独立的路**

向量模型（740M，几百 MB）不是检索的前提：

| 能力 | 依赖 | 没有模型时 |
|---|---|---|
| 关键词检索（FTS5 + BM25F） | 只要有索引库 | **照常可用** |
| 语义检索（向量召回） | 模型 + 向量索引 | 不可用（检索自动降级为关键词） |

因此：

- **建索引不要求模型**。`Embedder` 是懒加载的，以前"有没有 embedder"这个判断是
  假的（永远说可用），结果建索引跑到一半抛异常，**连关键词索引都建不出来**。
  现在改成提前判定（`embed.unavailable_reason()`：模型文件在不在、litert-lm 装没装），
  不可用就**只建关键词索引**，任务照常完成。
- **向量可以后补**。跳过判定现在会看"这个文档的向量齐不齐"（`doc_missing_vectors`），
  所以"先建关键词索引、后来下载模型"的文档不会再被当成"没变过"而永远跳过。
  模型下载完成后会自动触发一次索引同步；界面上也有「补建向量索引」按钮
  （`POST /search/vector/backfill`，只补缺向量的文档，不全量重建）。
- **向量化失败只降级，不丢数据**：写回时会把**已有的向量原样带回**，不会因为
  这次推理失败就把之前算好的向量抹成 NULL。

状态接口 `GET /search/status` 的 `vector` 段：

```json
{ "state": "partial", "leaves": 120, "with_vector": 80, "missing": 40,
  "model": "...", "dim": 768, "kinds": ["keyword"],
  "model_ready": true, "needs_backfill": true }
```

`needs_backfill` = 模型已就绪但还有叶子块没向量 —— 这就是"界面提示用户补建"的依据。

---

## 七、索引维护与回溯接口

> 迁移说明：k-search 原本把这些能力做成了 HTTP 端点（`api.py`）。keeper 里
> **没有那层 HTTP**，对应能力由 `search/service.py` 的方法提供，语义一一对应：

| 原 HTTP 端点 | keeper 侧 | 说明 |
| :--- | :--- | :--- |
| `POST /index` | `SearchService.index(roots, force, doc_type)` | 建索引；默认索引「我的空间」 |
| `POST /search` | `SearchService.search(query, top_k, filters, mode, alpha, fusion, min_score)` | 检索；`neighbor_span>0` 附带兄弟片段，`with_parent=True` 附带父块；`min_score` = 最终分下限（0~1，不传用 `MIN_SCORE_UI`，传 0 = 不过滤，agent 场景传 `MIN_SCORE_AGENT`） |
| `POST /cache/clear` | `SearchService.clear(drop_documents=True)` | 清空索引缓存：**向量索引 + 关键词(BM25/FTS5)索引（含影子表）全清**；`drop_documents=False` 只清索引并保留清单（标记 pending） |
| `DELETE /chunk/{id}` | `store/db.py::delete_chunk(conn, id)` | 删除单个片段 |
| `DELETE /doc/{id}` | `store/db.py::delete_document(conn, id)` | 删除某文档及其全部片段 |
| `DELETE /doc/{id}/chunks` | `store/db.py::delete_chunks_of_document(conn, id)` | 只删除该文档的片段，保留文件记录（`index_status='pending'`） |
| `GET /chunk/{id}/siblings?span=N` | `store/db.py::get_sibling_chunks(conn, doc_id, idx, span)` | 取该片段在同一文档中的相邻兄弟片段（前后各 N 个，含自身） |
| `GET /stats` | `SearchService.stats()` | 索引统计（文档数 / 片段数 / 关键词条目数） |
| `GET /model/status`、`POST /model/download` | `ingest/model.py::get_state / start_download` | 模型状态与后台下载（模型默认在 `~/.keeper/search/models/`） |

回溯规则：

- 命中叶子 → `parent_id` → 父块（章节级上下文）；
- 父块向上可继续回溯（H3 → H2 → 止于 H2）；
- 无父标题或父标题为 H1 时 `parent_id` 为 `NULL`；
- 向上合并到父节点的片段，`parent_id` 指向该父节点自身的父块。

