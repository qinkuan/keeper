# keeper 架构总览

> 本文是 keeper **模块地图**：有哪些包、各自负责什么、关键的取舍是什么。
> 某个子系统的深入设计见本文末尾「文档索引」里的专题文档。

---

## 一、定位

keeper 是**本地 agent 运行时**：负责「把一个 agent 定义装配成能跑的实例，并把它执行的过程记录下来」。

分工边界很明确：

| 谁 | 负责 |
|---|---|
| **平台**（keeperplatform） | agent / 插件 / 模型 / 市场的**定义与增删改** |
| **keeper** | **运行**——装载配置、装配实例、执行对话、记录可观测数据 |

所以 keeper 里没有「创建插件」「编辑 agent」这类编辑态逻辑，只有「装载 / 卸载 / 装配 / 执行 / 记录」。

---

## 二、分层

```
┌─────────────────────────────────────────────────────────────┐
│ 入口层    main.py          启动、装配所有实例、挂路由         │
├─────────────────────────────────────────────────────────────┤
│ 接口层    api/      管理接口（装载/卸载/插件/模型/设置）      │
│           chat/api  会话接口（对话/产物/工作区/时间线）       │
│           framework/api、api/metrics、api/evals…             │
├─────────────────────────────────────────────────────────────┤
│ 内核层    agent/    装配（keeper.py）+ 执行（executor/        │
│                     process/planner）                        │
│           tools/    工具注册表、内置工具、子进程工具、守卫     │
│           skill/    声明式 Skill（数据描述，非 Python）       │
│           llm/      模型工厂与调用                            │
│           mcp/      MCP 多 server 连接                        │
│           memory/   记忆存储 / 检索 / 摘要 / 召回工具          │
│           peer/ a2a/  对端协作（我→对端 / 对端→我）           │
├─────────────────────────────────────────────────────────────┤
│ 支撑层    store/    数据库与 ORM 模型                         │
│           config.py + setting/   配置与用户设置               │
│           plat/     平台对接（拉配置 / 落库 / 下载资源）       │
│           plugin/   插件库（扫描 / 清单 / 每-agent 链接）      │
│           sync→plat/fetcher     资源下载                      │
│           observability/        可观测（记账/统计/定价/额度）  │
│           evals/    评测闭环                                  │
├─────────────────────────────────────────────────────────────┤
│ 交付      web/      React 前端（构建产物由 keeper 同源托管）   │
│           deploy.sh / start_keeper.sh   打包与启动            │
└─────────────────────────────────────────────────────────────┘
```

---

## 三、模块逐个说

### 顶层

| 文件 | 职责 |
|---|---|
| `main.py` | 启动入口：建 DB、装配所有 agent 实例、挂路由、托管 `web/dist` 静态站点 |
| `config.py` | 元配置：**全部代码默认值都在这里**，并按段从 `settings.yaml` 覆盖 |

### `agent/` —— 内核

- `keeper.py`：**装配**。配置 → 实例 → 可执行体，以及资源生命周期
- `executor.py`：**执行**。一轮对话怎么跑完（就绪守卫、逐步落库、挂起恢复）
- `process.py`：`Process` 多步 ReAct 循环
- `planner.py`：`ReActPlanner`，把工具清单 + 问题喂给 LLM 并解析其决策
- `context_store.py`：跨轮上下文的持久化

与会话/页面无关的逻辑才在这里 —— 会话落库在 `chat/`。

### `tools/` —— 工具

- `base.py`：`ProcessorTool` + `ToolRegistry`，工具出错**不会让 agent 崩溃**（兜成错误文本回给模型）
- `builtin/`：keeper 自带工具（`fs.list_dir` / `fs.read_file` / `fs.write_file` / `fs.edit_file` / `fs.find` / `fs.publish` / `task.mark_item_done`）
- `subprocess_tool.py`：插件的可执行文件（bin 组件）通道
- `external.py` / `collect.py`：外部工具与聚合工具
- `guard.py`：**安全守卫**。只读工作区按读命令白名单放行，可写工作区只查路径越界

**工具的两个来源**：内置（代码写死，不查平台、只受本地开关控制）+ 插件（平台下发，MCP 组件或 bin 组件）。

### `plugin/` —— 插件库

扫描 `~/.keeper/plugins` 与 `~/.keeper/agents/<id>/plugins`，解析 `keeper-plugin.json`，管理每-agent 的链接。

关键设计：
- 本机插件建**相对符号链接**（`~/.keeper` 搬家不断）
- 平台下载的插件是**独立副本**（固定版本，已有实体目录就不再覆盖）
- **只读是用户空间的属性**（`user_space.read_only`），不是 agent 画像上的字段

### `plat/` —— 平台对接

- `client.py`：HTTP 拉取 agent 配置（`fetch_my_agents`）
- `sync.py`：把拉到的配置写进本地库（`sync_agent`）
- `fetcher.py`：下载平台下发的 source（git / http / zip）

三个文件合起来是完整的「平台 → 本地」链路。

### `observability/` —— 可观测

| 模块 | 职责 |
|---|---|
| `recording.py` | 记账：LLM 调用 / 工具调用 / 能力加载 / ReAct 解析统计 |
| `timeline.py` | 时间线：消息的 step 明细、每步耗时、单消息完整时间线 |
| `stats.py` | 统计：用量成本多维聚合、耗时分位、错误归类、提问率、去重调用 |
| `pricing.py` | 定价：按 (profile, model, provider) 匹配价格，支持时段 / 工作日折扣 |
| `budget.py` | 额度与聚合：预算上限检查、按维度聚合 |
| `dump.py` | 完整 prompt 落盘（调试用，需在设置里开启） |
| `_context.py` | 归属上下文：从 `chat/context` 的 ContextVar 读 trace/agent/session |

三条设计原则：
1. **落库粒度是「一次 LLM 调用一行」**，各级用量由该表聚合得出，不在各表冗余 token 列
2. **归属从 ContextVar 读**，不层层透传参数（调用链很深，透参会污染一堆签名）
3. **任何统计失败都不影响主流程**，写库一律 try/except 只记 debug

### `setting/` 与 `config.py` —— 两层配置

- `config.yaml` —— **元配置**，只由人手动改：server / agent / platform / llm / workspace
- `~/.keeper/settings.yaml` —— **用户设置**，设置页写入、**立即生效**不需重启：observability / a2a / context

读取时按段覆盖，所以 settings.yaml 缺项也能跑（与代码默认值合并）。

### `memory/` —— 记忆

- `retriever.py`：jieba 预分词 + SQLite FTS5 检索
- `store.py`：会话级短期记忆（JSONL + 增量索引）
- `extractor.py`：异步记忆块摘要（LLM 解耦）
- `tools.py`：agentic retrieval 工具（`recall` / `read`）

### `evals/` —— 评测闭环

四层：**用例**（YAML）→ **隔离环境**（独立进程 + 独立 DB + 独立工作空间）→ **断言**（程序化，不引入第二个模型的偏差）→ **报告**（可 diff 的 JSON + Markdown）。

评测**绝不污染**真实会话与可观测面板数据。

### `a2a/` 与 `peer/` —— 对外协作

对称的两条链路：

```
chat = 请求方 → 我    （存 chat_sessions + session_messages）
peer = 我 → 对端      （存 threads + agent_messages）
a2a  = 对端经 A2A 调我（build_a2a_app / build_agent_card）
```

### `skill/` —— 声明式 Skill

以**数据**（prompt 模板 + tool 列表 + 可选子图）描述能力，而非 Python 代码。这样其它语言运行时也能解释同一份定义，agent 按名动态加载。

`always: true` 的技能正文常驻 system prompt（如 `plugin-authoring` 就只有 SKILL.md、不提供任何工具）。

### `store/` —— 存储

SQLite + SQLAlchemy 异步。除会话外也存**能力市场的资源定义**（现在只有插件）。

### `tests/` —— 回归脚本

能直接 `python keeper/tests/xxx.py` 跑完出结论的脚本，**不引测试框架**（这样"跑一遍"不需要额外依赖）：

```
test_plugin_lib.py    插件库：清单解析、链接增删、幂等
test_guard.py         安全守卫：白名单放行、绕过拦截
test_planner_parse.py ReAct 文本协议解析
```

---

## 四、几个关键取舍

### 1. 能力装配一次、运行期不再变

工具表在**装配期固化**（`build_agent` 时）。所以：

- 换插件 / 换模型 → 自动触发重建实例
- **换工作区也要重建** —— 工作区根与只读标志是装配期读的
- 已经打开的会话持有旧工具表，需新发一轮对话才看到变化

### 2. 只读判断：唯一真源是用户空间

历史教训：agent 画像上曾有 `workspace_read_only`，而运行期读的是 `user_space.read_only` —— 两个来源打架，后果是「工作区明明可写，装配期却按 agent 画像上那个没人改过的默认值把写类工具全丢了」，工具在界面上**凭空消失且无任何报错**。

现在：

- 只读的真源是 `user_space.read_only`，agent 表上那一列已删除
- 插件的写类工具**不在装配期按只读丢弃**，与内置工具一致（运行时拒绝），避免「工具消失」这种静默失败
- agent 绑定的工作区必须在「用户空间」登记过，否则创建时报错

### 3. 插件走子进程，不进主进程

`tools/subprocess_tool.py` 的注释说得很直白：第三方代码进主进程等于把 keeper 的全部权限交给它 —— 能读所有 agent 的会话，依赖还可能跟 keeper 打架。

代价是每次调用付一次进程启动，换来**语言无关 + 崩了只影响这一次调用**。

协议极简：stdin 一行 JSON / stdout 最后一行 JSON / stderr 随便打。

### 4. 内置工具与插件的两条来源

| | 内置 | 插件 |
|---|---|---|
| 定义位置 | keeper 代码 | 平台下发 |
| 开关 | `AgentCapabilityOverride`，无记录 = 启用 | 同左 |
| 形态 | Python 工厂 | MCP 组件 / bin 组件 |
| 是否查平台 | 否 | 是 |

### 5. 前端同源托管

`web/dist` 由 keeper 自己挂成站点根（或由 nginx 反代）。前端生产包 `BASE=""`，请求不带 `/api` 前缀，与 API **同源**，因此不需要 CORS。

---

## 五、一次对话的完整链路

```
用户发消息
  → chat/api.py          建/取会话，解析工作区（resolve_workspace）
  → agent/executor.py    就绪守卫 → 交给 Process
  → agent/process.py     多步 ReAct 循环：
      planner.py  把工具清单 + 问题 → LLM → 解析出 THOUGHT/ACTION
      tools/guard.py   执行前的安全守卫（只读白名单 / 路径越界）
      执行工具（内置 / MCP / 插件子进程）
      observability/recording.py   记账（LLM 调用、工具调用）
  → chat/service.py      逐步落库（消息 / 步骤 / 产物）
  → observability/       时间线、统计、额度检查
```

---

## 六、数据与目录

```
~/.keeper/
├── agents/<agent_id>/plugins/   该 agent 的插件（链接或下载副本）
├── plugins/                     插件库（本机插件）
├── workspace/user/<space>/      用户工作区（default / 我的仓库 …）
├── sessions/                    会话记忆
├── memory/                      记忆索引
├── evals/                       评测产物
├── prompt-dump/                 prompt 落盘（调试）
├── settings.yaml                用户设置
└── context-store/               跨轮上下文
```

数据库在 `keeper/data/keeper.db`（SQLite）。

---

## 七、接口分组

| 前缀 | 内容 |
|---|---|
| `/manage/*` | 管理：agent 装载/卸载、插件、模型、设置 |
| `/agents/{agent_id}/*` | 会话：对话、能力开关、工作区文件、时间线 |
| `/metrics/*` | 可观测：用量、耗时、错误、提问率、重复调用 |
| `/user-spaces/*` | 用户工作区 |
| `/framework/*` | 本机运行时（python / node）检测 |
| `/evals/*` | 评测 |

---

## 八、启动与部署

- `start_keeper.sh` —— 建 venv、装依赖（仅首次或 `requirements.txt` hash 变化时）、前台启动。宝塔等无 `HOME` 的环境有 `KEEPER_HOME` 回退
- `deploy.sh` —— 打包服务端（排除 data / web / 日志 / 测试）
- `web/deploy.sh` —— 构建前端并打 zip

**日志**：默认只到控制台；设 `LOG_FILE` 会额外落一份（Rotating，默认 10MB × 5 份）。装配期会打印每个插件 / 每个工具的装配结果与跳过原因，定位「工具为什么没出现」看这里。

---

## 九、文档索引

全部设计文档都在 `keeper/doc/`（项目根已无独立 doc 目录）。

| 文档 | 内容 |
|---|---|
| **`architecture.md`**（本文） | **模块地图与设计取舍**——想了解全貌从这里开始 |
| `positioning.md` | 产品定位：两平面、A2A、与同类产品的分界 |
| — 运行机制 — | |
| `session-design.md` | 会话存储模型（表结构、跨端链路、挂起恢复） |
| `task-design.md` | 任务模式（计划 → 审批 → 执行 → 验收） |
| `capability-loading.md` | 能力按需加载（L1 摘要 / L2 正文 / `load_capabilities`） |
| `context.md` | 上下文治理（工具输出外部化 + 水位压缩） |
| `memory.md` | 记忆与检索（短期记忆 + jieba/FTS5） |
| `artifact-design.md` | 产物机制（URI scheme、安全策略、按类型展示） |
| — 外部与质量 — | |
| `collaboration-contract.md` | 跨端协作契约（thread / message、超时熔断、降级） |
| `observability-design.md` | 可观测（一次 LLM 调用一行的选型依据） |
| `evals.md` | 评测闭环（隔离、断言、基线） |
| `roadmap.md` | 未完成事项与技术债 |
