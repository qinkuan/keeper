"""表说明：每张表「是干什么用的」的唯一真源。

SQLite 没有 ``COMMENT ON TABLE``，表结构上挂不住说明。而"这张表为什么存在、
谁写它、数据多久留一次"这类信息恰恰最容易丢——代码删了就没了，文档没人更新就
成了骗人的东西。所以把说明放进库（``schema_doc`` 表），于是：

- 任何程序都能读（:func:`list_schema_docs`）；
- 文档由它**生成**（:func:`render_markdown` → ``doc/database.md``），不会脱节；
- 说明与表同生共死——表删了，这里的种子数据也一起删。

本模块只放**说明**（元数据），不放业务逻辑。写操作在
``store/database.py::_seed_schema_docs``（启动时播种，内容变了才更新）。

生成文档：``python -m keeper.store.schema_doc --write``
"""
from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

# 一条说明：表名 / 归属库 / 用途 / 谁写 / 谁读 / 生命周期 / 注意
Doc = Dict[str, Any]


# ---------------------------------------------------------------------------
# 主库 ~/.keeper/data/keeper.db 的表
# ---------------------------------------------------------------------------
KEEPER_DOCS: List[Doc] = [
    {
        "table_name": "chat_sessions",
        "purpose": "会话本体：一个「对话框」一行，是所有对话内容的容器。",
        "owner": "chat/service.py, chat/api.py",
        "readers": "chat/api.py（列表/详情）, agent/keeper.py（装配）",
        "lifecycle": "随会话创建；删除走**软删除**（status='deleted' + deleted_at），"
                     "回收站可恢复；硬删除由后台清理任务做。",
        "notes": "agent_id 用来隔离会话（同一个人在不同 agent 下的历史不串台）。"
                 "**模型不在本表**——模型绑定在 agent 上（agent_llm_binding）。"
                 "thinking / reasoning_effort / mode 建会话时定，之后沿用库里这一行。"
                 "parent_id 把子 agent 的子会话挂成树，聚合费用时按它递归收集。",
    },
    {
        "table_name": "session_messages",
        "purpose": "会话内的消息：用户提问一条、模型回答一条，按 seq 递增。",
        "owner": "chat/service.py",
        "readers": "chat/api.py（气泡渲染）, agent/process.py（回填上下文）",
        "lifecycle": "随会话；消息不单独删（删会话即随之失效）。",
        "notes": "(chat_session_id, seq) 唯一。artifacts 只挂 **assistant** 消息（agent "
                 "交付的文件）；attachments 只挂 **user** 消息（用户塞进来的输入，指向"
                 "厂商侧 file_id）——两者语义不同，不要混用。"
                 "duration_ms 是端到端墙钟耗时，与 llm_calls 的 SUM(duration_ms) 不是"
                 "一个口径（后者不含工具执行与挂起等待）。",
    },
    {
        "table_name": "react_steps",
        "purpose": "一次 ReAct 的步骤：模型的一轮「思考 → 动作 → 观察」。",
        "owner": "agent/planner.py, chat/service.py",
        "readers": "chat/api.py（步骤展示）, agent/process.py（恢复执行）",
        "lifecycle": "随会话消息产生；挂起的步骤留在库里等恢复，"
                     "恢复后同一行更新状态（不新增行）。",
        "notes": "**挂起与恢复的唯一事实来源**是 status + wait_kind + wait_ref，"
                 "threads / agent_messages 都不再重复记录等待状态。"
                 "(session_message_id, step) 唯一。",
    },
    {
        "table_name": "threads",
        "purpose": "对端链：本地会话 ↔ 某个对端（外部 agent / 进程内 agent）之间的那条链。",
        "owner": "chat/peer.py（PeerService）",
        "readers": "chat/peer.py, agent/process.py（跨端调用）",
        "lifecycle": "首次跨端调用时建；随会话。",
        "notes": "thread_id 由发起方生成，两边用同一个值标识同一条链，不需要映射表。"
                 "定位靠 (chat_session_id, peer)，LLM 不需要记 thread_id。"
                 "**不设 status**：等待状态在 react_steps，避免同一事实两处写。"
                 "last_task_id / last_task_state / context_id 是**遗留兼容列**，已停写。",
    },
    {
        "table_name": "agent_messages",
        "purpose": "跨端消息：我与某个对端之间的往来记录。",
        "owner": "chat/peer.py（PeerService）",
        "readers": "chat/peer.py, agent/process.py",
        "lifecycle": "随对端链；随会话。",
        "notes": "同一条 message_id 在两边各存一份，靠 direction 区分归属："
                 "我发出的记 out（对端存进它自己的 session_messages），"
                 "对端发来的记 in。in_reply_to 串联多轮；存在指向它的记录 = 已被回复，"
                 "所以本表也不设 status。",
    },
    {
        "table_name": "tasks",
        "purpose": "任务（任务模式）本体：一条计划 + 一条执行状态机。",
        "owner": "chat/api.py（任务接口）, chat/service.py",
        "readers": "chat/api.py, agent/process.py",
        "lifecycle": "随任务创建；走到终态（done/failed/cancelled）后留档。",
        "notes": "**一个任务绑定一个会话**（D1）：产出计划、逐点执行、打回修补全程都在"
                 "那一个会话里对话，因此不建 task_runs 表——执行过程本身就是该会话的"
                 "消息，天然留痕。plan_md 只是由 task_items 渲染出的 markdown 视图"
                 "（不是事实来源）。status 机见 doc/task-design.md 第四节。",
    },
    {
        "table_name": "task_items",
        "purpose": "任务的计划点：勾选态、过程产物、结论都落在这一行上。",
        "owner": "chat/api.py（计划审批/推进）, chat/service.py",
        "readers": "chat/api.py, agent/process.py",
        "lifecycle": "随任务；plan_version 保留历史版本的点，仅作留痕。",
        "notes": "本表是计划的**事实来源**，tasks.plan_md 由它按 seq 渲染（done 渲染成"
                 " `- [x]`），避免 markdown 与结构化状态双写不一致。"
                 "plan_version 服务「重新规划」：升版本后已完成的点原样保留，未完成的置 "
                 "skipped，执行只推进当前版本。",
    },
    {
        "table_name": "a2a_tasks",
        "purpose": "入站 A2A 任务：对端发给我的 task 的**协议门面**。",
        "owner": "a2a/（协议层）",
        "readers": "a2a/（协议层）, chat/api.py",
        "lifecycle": "入站 task 到达时建；由对端 GetTask 轮询或我方完成时更新。",
        "notes": "只存**协议态**：state / artifacts / 挂起问题 / history 摘要 / 指回本地"
                 " session 的 metadata。ReAct 执行仍在 chat_sessions / react_steps，"
                 "本表不承载执行。context_id 入站 = chat_sessions.id，出站 = threads.id。",
    },
    {
        "table_name": "a2a_outbound_tasks",
        "purpose": "出站 A2A 任务的**本地索引**：我发起、跑在对端的 task。",
        "owner": "a2a/（协议层）, chat/peer.py",
        "readers": "a2a/（协议层）",
        "lifecycle": "发起时建；状态由对端掌握，本地只记「最后已知状态」。",
        "notes": "与入站的 a2a_tasks **分开建表**：入站 task 由我拥有（完整协议态），"
                 "出站 task 状态在对端，本表只是索引——查「有什么 task 在对端跑」"
                 "不必每次去拉。详情可凭 id（对端 taskId）调对端 GetTask 刷新。"
                 "snapshot 存对端返回的完整 task（JSON）。",
    },
    {
        "table_name": "agent_capability_overrides",
        "purpose": "某 agent 对某一件能力的**本地开关**（覆盖平台 binding）。",
        "owner": "chat/api.py（能力开关接口）, tools/builtin/registry.py",
        "readers": "chat/api.py, agent/config.py（装配期读）",
        "lifecycle": "用户点开关时写；长期保留。",
        "notes": "为什么单独一张表而不是改 binding.enabled：平台同步是**先删光再按平台数据"
                 "重建**，写在那里的开关会被冲掉。分工——kind=builtin 时内置工具只归 "
                 "keeper，开关**只看本表**（无记录 = 默认启用）；kind=tool/mcp/skill 时"
                 "平台决定「有哪些能力」，本表决定「本地开不开」，覆盖 binding.enabled。",
    },
    {
        "table_name": "agent_peers",
        "purpose": "某 agent 要经 A2A 调用的对端（外部 agent，或本进程内另一个 agent）。",
        "owner": "chat/api.py, agent/keeper.py",
        "readers": "agent/keeper.py（装配期 load 进内存）, chat/peer.py",
        "lifecycle": "运行时增删写回本表；随 agent。",
        "notes": "运行时生效层在 KeeperAgent.peers（内存 dict），本表是**持久层**："
                 "启动时 load 进内存，重启不丢。name = 对端标识，同时是工具名前缀"
                 "（{name}__{skill}）。source 为空表示按 a2a_url 自动判定来源。",
    },
    {
        "table_name": "agent",
        "purpose": "用户创建的 agent：身份（name/description）+ 人设（persona）+ 生命周期状态。",
        "owner": "agent/api.py",
        "readers": "agent/keeper.py（装配）, chat/api.py",
        "lifecycle": "创建时建；archived 后留档不再参与装配。",
        "notes": "能力不写在这张表里（靠 binding 挂），模型也不写（靠 agent_llm_binding）"
                 "——加一件能力只是多一行，不用改 agent 本身。"
                 "**没有工作区相关列**：工作空间是会话级属性。"
                 "origin 是历史字段（platform/local），现在只保留给旧数据看。",
    },
    {
        "table_name": "llm_profiles",
        "purpose": "可复用的模型预设：provider / 模型名 / 密钥 / 采样参数 / 上下文上限。",
        "owner": "agent/api.py（「模型管理」页）",
        "readers": "agent/config.py（装配）, observability（计价按 profile_id 查）",
        "lifecycle": "用户配置时建；删预设会让引用它的绑定变成「无模型」。",
        "notes": "api_key **明文存储**（本地运行时，模型归属客户端、平台不维护）。"
                 "context_limit 用于上下文水位告警：ReAct 多轮很容易把 prompt_tokens "
                 "顶到上限，逼近时提前告警。价格挂在 profile 上——同一个 model_name "
                 "可以有多个预设，价格也可能不同。",
    },
    {
        "table_name": "agent_llm_binding",
        "purpose": "agent → 模型预设 的绑定（一个 agent 用哪个模型）。",
        "owner": "agent/api.py",
        "readers": "agent/config.py（装配）",
        "lifecycle": "随 agent；解绑时置 NULL（=纯索引模式，不调用模型）。",
        "notes": "放独立表而非 agent 的一列：模型归属是**本地决定**，平台同步不应覆盖它"
                 "（否则改好的绑定会被同步冲掉）。",
    },
    {
        "table_name": "llm_calls",
        "purpose": "一次 LLM 调用的用量记录——可观测的**事实表**。",
        "owner": "llm 封装层（每次调用后打点）",
        "readers": "observability/（面板与聚合统计）",
        "lifecycle": "每次调用落一行；随会话保留，无自动清理。",
        "notes": "step / 消息 / 任务 / agent 各级用量**全部由本表聚合得出**，"
                 "不在各表冗余 token 列——避免多份数据不一致。"
                 "是「每次调用一行」而非「累计值做差」：LLM 是 agent 级单例，"
                 "其 usage 跨会话累加，做差在并发下会把别的会话算进来。"
                 "cached_tokens 可空是有意：None = provider 未上报，0 = 上报了但没命中。"
                 "cost 在**调用返回时**按当时的单价算好落库，之后改价不影响历史。",
    },
    {
        "table_name": "delegatee_configs",
        "purpose": "子 agent（delegatee）的全局配置：每个 delegatee（code / video…）各自一套模型与工具白名单。",
        "owner": "agent/delegatee（prepare 时读）",
        "readers": "agent/delegatee",
        "lifecycle": "全局配置，长期保留。",
        "notes": "与主 agent 的 AgentLLMBinding 同构但**全局共享**（不按主 agent 区分）"
                 "——所有会话的 code 子 agent 都用同一套模型 + 插件。"
                 "plugins / tools 存 JSON 字符串（白名单）：tools 是工具名精确匹配，"
                 "plugins 是插件名前缀匹配（工具名形如 插件__server__tool）。",
    },
    {
        "table_name": "model_prices",
        "purpose": "模型单价，**按生效时间段**存储（可有多段，覆盖调价历史）。",
        "owner": "observability/（计价）",
        "readers": "observability/pricing",
        "lifecycle": "人工维护；调价时新增一段并把上一段的 effective_to 封口。",
        "notes": "为什么必须有时间段：模型会调价。若只存「当前价」，三个月前那次调用会"
                 "按今天的价重算——历史成本全错。成本换算必须按**调用发生时**生效的那段价。"
                 "三个维度对应 DeepSeek 计费语义：input（未命中缓存）/ cached（命中）/ output。"
                 "匹配优先级：profile_id > model_name+provider > 仅 model_name。"
                 "还支持错峰时段（time_from_minute / time_to_minute）与星期限定（weekdays）。",
    },
    {
        "table_name": "tool_calls",
        "purpose": "一次**工具调用**的执行记录——可观测的工具维度事实表。",
        "owner": "工具执行层（执行后打点）",
        "readers": "observability/（工具统计）",
        "lifecycle": "每次调用落一行；随会话保留。",
        "notes": "与 llm_calls 平行：那张记 LLM，这张记工具。工具维度的价值在于定位"
                 "「哪个工具最慢 / 最常失败 / **返回最大**」——返回大的工具往往就是把上下文"
                 "撑爆的元凶。output_size 记的是**真正进入上下文**的长度，raw_output_size "
                 "是工具原本返回的长度；两者不等即说明给模型的文本被缩减过。",
    },
    {
        "table_name": "capability_loads",
        "purpose": "一次「能力加载」事件：某份正文 / 某组工具定义进入了上下文。",
        "owner": "能力加载层（三条加载路径都打点）",
        "readers": "observability/（上下文占用分析）",
        "lifecycle": "每次加载落一行；随会话保留。",
        "notes": "不复用 tool_calls：三条加载路径里**只有一条**是模型主动发起的工具调用，"
                 "另外两条是 preload（轮开始时系统替模型取技能正文）与 auto_disclose"
                 "（模型只见过工具名，执行前自动补定义）。但三者消耗的是同一份上下文，"
                 "必须一起统计才能回答「这个能力到底被取进来几次」。",
    },
    {
        "table_name": "react_parse_stats",
        "purpose": "一次 LLM 响应的**结构**统计：解析出了几个 ACTION、有没有幻觉跑飞。",
        "owner": "agent/planner.py（解析器）",
        "readers": "observability/（解析质量分析）",
        "lifecycle": "每次解析落一行；随会话保留。",
        "notes": "回答两个问题：① 模型到底想并行吗（action_count > 1 时第二个被丢弃）"
                 "——multi_rate 高说明并行化有真实收益；② 哪个模型爱自己编工具回执"
                 "（kind=hallucination）——**幻觉率必须按 model 看**，聚合在一起会把"
                 "「换模型能根治」的结论抹平。",
    },
    {
        "table_name": "schema_doc",
        "purpose": "表说明元数据：每张表「是干什么用的」写在库里，而不是只躺在文档里。",
        "owner": "store/schema_doc.py（种子数据）, store/database.py（启动时播种）",
        "readers": "store/schema_doc.py, doc 生成脚本",
        "lifecycle": "启动时按需补种；内容变了才更新（不无条件写）。",
        "notes": "SQLite 不支持 COMMENT ON TABLE，表结构上挂不住说明，所以用这张元数据表"
                 "承载。db 字段区分归属库（keeper / search）。"
                 "doc/database.md 由本表**生成**，不手写——所以不会与代码脱节。",
    },
]


# ---------------------------------------------------------------------------
# 检索索引库 ~/.keeper/search/search.db 的表（独立的 SQLite 文件，不在 ORM 里）
# ---------------------------------------------------------------------------
SEARCH_DOCS: List[Doc] = [
    {
        "table_name": "documents",
        "purpose": "被索引的文件：一行一个文件（路径、大小、mtime、内容哈希、向量状态）。",
        "owner": "search/ingest/, search/store/db.py",
        "readers": "search/store/, search/query.py",
        "lifecycle": "索引时 upsert；文件删除后同步删（否则会搜到不存在的文件，很伤信任）。",
        "notes": "内容哈希用于去重：同内容不同路径不重复嵌入。"
                 "向量状态用于「模型就绪但向量没建」的检测与补建。",
    },
    {
        "table_name": "chunks",
        "purpose": "文件切片：一行一个块，带所属文档与**向量**（embedding）。",
        "owner": "search/ingest/（切块 + 向量化）",
        "readers": "search/query.py（向量召回）",
        "lifecycle": "文档重索引时先删旧块再写新块。",
        "notes": "向量化失败时**只写关键词、不写向量**，不让整批索引失败。"
                 "重新索引会保留已有向量（不抹成 NULL），避免白白重算。",
    },
    {
        "table_name": "chunks_fts",
        "purpose": "FTS5 全文索引（虚表）：关键词召回通道，与向量召回**互相独立**。",
        "owner": "search/store/db.py（建表）, search/ingest/（写）",
        "readers": "search/query.py（关键词召回）",
        "lifecycle": "随 chunks 同步；可整表重建。",
        "notes": "没有向量模型也能建（关键词索引独立可用）。"
                 "chunks_fts_* 那几张是 FTS5 的内部影子表，由 SQLite 自己维护，别手改。",
    },
    {
        "table_name": "chunks_fts_config",
        "purpose": "FTS5 内部影子表：全文索引的配置（分词器等）。**不要手改**。",
        "owner": "SQLite FTS5 内部维护",
        "readers": "SQLite FTS5 内部",
        "lifecycle": "随 chunks_fts 自动维护。",
        "notes": "下面四张（config / data / docsize / idx）都是 FTS5 的影子表，"
                 "由 SQLite 自己管理。它们在运维时看得到，所以也登记说明，"
                 "以免有人以为是什么业务表。",
    },
    {
        "table_name": "chunks_fts_data",
        "purpose": "FTS5 内部影子表：索引正文数据。**不要手改**。",
        "owner": "SQLite FTS5 内部维护",
        "readers": "SQLite FTS5 内部",
        "lifecycle": "随 chunks_fts 自动维护。",
        "notes": "见 chunks_fts_config 的说明。",
    },
    {
        "table_name": "chunks_fts_docsize",
        "purpose": "FTS5 内部影子表：每条索引记录的 token 数。**不要手改**。",
        "owner": "SQLite FTS5 内部维护",
        "readers": "SQLite FTS5 内部",
        "lifecycle": "随 chunks_fts 自动维护。",
        "notes": "见 chunks_fts_config 的说明。",
    },
    {
        "table_name": "chunks_fts_idx",
        "purpose": "FTS5 内部影子表：倒排索引。**不要手改**。",
        "owner": "SQLite FTS5 内部维护",
        "readers": "SQLite FTS5 内部",
        "lifecycle": "随 chunks_fts 自动维护。",
        "notes": "见 chunks_fts_config 的说明。",
    },
    {
        "table_name": "idx_meta",
        "purpose": "索引元信息：索引了哪些类型、用的哪个 embedding 模型、维度、上次建向量索引的时间。",
        "owner": "search/store/db.py（写）, search/ingest/model.py（模型就绪时更新）",
        "readers": "search/api.py（/search/status）, search/ingest/",
        "lifecycle": "随索引变化更新。",
        "notes": "「模型已就绪但向量索引没建」这个状态就是靠它检测出来的"
                 "（embed_model 有值 + last_vector_at 为空），前端据此提示补建向量索引。",
    },
    {
        "table_name": "sqlite_sequence",
        "purpose": "SQLite 内部表：AUTOINCREMENT 计数器。**不要手改**。",
        "owner": "SQLite 内部维护",
        "readers": "SQLite 内部",
        "lifecycle": "由 SQLite 自动维护。",
        "notes": "出现在库里是因为某张表用了 AUTOINCREMENT。这是系统表，不是业务表。",
    },
]


def all_docs() -> List[Doc]:
    """全部表说明（主库 + 检索索引库）。"""
    out: List[Doc] = []
    for d in KEEPER_DOCS:
        out.append({**d, "db": "keeper"})
    for d in SEARCH_DOCS:
        out.append({**d, "db": "search"})
    return out


async def list_schema_docs(db: Optional[str] = None) -> List[Dict[str, Any]]:
    """从库里读出全部表说明。

    ``db`` 为 None 时返回两个库的全部说明；否则只返回该库的。
    """
    from .database import get_session_factory
    from sqlalchemy import select

    from .models import SchemaDoc

    async with get_session_factory()() as s:
        stmt = select(SchemaDoc)
        if db:
            stmt = stmt.where(SchemaDoc.db == db)
        rows = (await s.execute(stmt)).scalars().all()
        return [
            {
                "table_name": r.table_name,
                "db": r.db,
                "purpose": r.purpose,
                "owner": r.owner,
                "readers": r.readers,
                "lifecycle": r.lifecycle,
                "notes": r.notes,
            }
            for r in rows
        ]


def render_markdown(docs: Optional[List[Dict[str, Any]]] = None) -> str:
    """把表说明渲染成 markdown（生成 ``doc/database.md`` 用）。

    不传 ``docs`` 时用**种子数据**渲染——这样没起服务也能生成文档。
    """
    if docs is None:
        docs = [
            {**d, "db": "keeper"} for d in KEEPER_DOCS
        ] + [{**d, "db": "search"} for d in SEARCH_DOCS]

    lines: List[str] = []
    lines.append("# 数据库表说明")
    lines.append("")
    lines.append(
        "本文由 `keeper/store/schema_doc.py` 的种子数据**自动生成**"
        "（`python -m keeper.store.schema_doc --write`），不要手改——"
        "说明的真源是那张 `schema_doc` 表，改那里再重新生成。"
    )
    lines.append("")
    lines.append(
        "keeper 有两个 SQLite 文件：主库 `~/.keeper/data/keeper.db`（会话、任务、"
        "agent、可观测）与检索索引库 `~/.keeper/search/search.db`（文件切片、"
        "关键词索引、向量）。下面按库分组。"
    )
    lines.append("")

    for db, title in (
        ("keeper", "主库 `~/.keeper/data/keeper.db`"),
        ("search", "检索索引库 `~/.keeper/search/search.db`"),
    ):
        items = [d for d in docs if d.get("db") == db]
        if not items:
            continue
        lines.append(f"## {title}")
        lines.append("")
        lines.append("| 表名 | 干什么用的 |")
        lines.append("|---|---|")
        for d in items:
            purpose = str(d.get("purpose") or "").replace("\n", " ").replace("|", "\\|")
            lines.append(f"| `{d['table_name']}` | {purpose} |")
        lines.append("")

        for d in items:
            lines.append(f"### `{d['table_name']}`")
            lines.append("")
            lines.append(f"**干什么用的**：{d.get('purpose') or '—'}")
            lines.append("")
            for label, key in (
                ("谁写它", "owner"),
                ("谁读它", "readers"),
                ("数据生命周期", "lifecycle"),
                ("注意事项", "notes"),
            ):
                val = d.get(key)
                if val:
                    lines.append(f"- **{label}**：{val}")
            lines.append("")
    return "\n".join(lines)


def _main(argv: List[str]) -> int:
    """``python -m keeper.store.schema_doc [--write]``。

    默认打印到 stdout；``--write`` 写进 ``doc/database.md``。
    """
    write = "--write" in argv
    md = render_markdown()
    if not write:
        sys.stdout.write(md)
        return 0
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]  # keeper/
    target = root / "doc" / "database.md"
    target.write_text(md, encoding="utf-8")
    print(f"已写入 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
