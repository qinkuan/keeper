# keeper 架构总览

> 本文是 keeper **模块地图**：有哪些包、各自负责什么、关键的取舍是什么。
> 某个子系统的深入设计见本文末尾「文档索引」里的专题文档。

---

## 一、定位

keeper 是**本地知识库 agent**：把「我的空间」变成 agent 的知识库，在本机完成
**读 → 找 → 改 → 交**，并记住关于用户的事。

本文讲它**怎么做到**——有哪些包、各自负责什么、关键的取舍是什么。

它是**本机单用户**应用：没有外部配置源、没有入站认证，**所有东西都在本机维护**。

| 谁 | 负责 |
|---|---|
| **keeper（本机）** | **全部**——创建 agent、配模型、装插件、装配实例、执行对话、记录可观测数据 |

四件事都在本机，**没有任何外部来源**：

- **模型**：模型预设在本地 `llm_profiles` 表，agent 通过 `agent_llm_binding` 显式绑定，
  **没有任何兜底**（未绑定 = 纯索引模式，不调用模型）。
- **插件**：插件库在本机（`~/.keeper/plugins`），通过符号链接绑给 agent。
- **能力开关**：`agent_capability_overrides` 只写本地。
- **agent 画像**：本机创建（`POST /manage/agents`），不存在"从别处同步"这回事。

所以 keeper 里有「创建 agent」「配模型」「装插件」这类**本地编辑态**逻辑，
也有「启动 / 停止 / 装配 / 执行 / 记录」这类运行态逻辑。

知识库这条主线的落点：

| 环节 | 落点 | 文档 |
|---|---|---|
| **知识来源**（「我的空间」） | `zonefilesystem/` + `desktop/spaceFs.js` | [`user-space.md`](user-space.md) |
| **读 / 改** | 前端 `UserSpacePage` + `components/` | 同上 |
| **找** | 目录树 + `bash` 插件；**向量检索在建** | [`roadmap.md`](roadmap.md) ② |
| **交**（交付块） | `viewer/block.py` + `tools/builtin/` | [`artifact-design.md`](artifact-design.md) |
| **记** | `memory/`；**长期事实库在建** | [`memory.md`](memory.md)、[`roadmap.md`](roadmap.md) ③ |

**信任边界**：「本机只有你一个人用 keeper」。内核只绑回环地址，没有入站认证
（`auth/` 包已随平台一起移除）——详见 [`security.md`](security.md)。

---

## 二、分层

```
┌─────────────────────────────────────────────────────────────┐
│ 入口层    main.py          启动、装配所有实例、挂路由、托管前端 │
│           desktop/         Electron 壳（窗口 + sidecar 托管） │
├─────────────────────────────────────────────────────────────┤
│ 接口层    agent/api.py          agent 增删改、启停、插件/模型绑定   │
│           chat/api.py           会话接口（对话/产物/我的空间/时间线）│
│           llm/api.py            模型预设 CRUD + 单价 + 连通测试   │
│           plugin/api.py         插件库（只读）                  │
│           setting/api.py        用户设置读写                    │
│           observability/api.py  可观测查询（/metrics）          │
│           evals/api.py          评测                            │
│           framework/api.py      本机运行时检测                  │
├─────────────────────────────────────────────────────────────┤
│ 内核层    agent/    装配（keeper.py/core.py）+ 执行（executor/  │
│                     process/planner/protocol）+ 子 agent      │
│                     （delegatee.py、code_delegatee.py）        │
│                     + 沙箱（ptc_sandbox.py）                   │
│           tools/    工具注册表、内置工具、子进程工具、安全守卫     │
│           skill/    声明式 Skill（数据描述，非代码）             │
│           llm/      模型工厂与调用                             │
│           mcp/      MCP 多 server 连接                         │
│           memory/   记忆存储 / 检索 / 摘要 / 召回工具            │
│           peer/ a2a/  对外协作（我→对端 / 对端→我）             │
├─────────────────────────────────────────────────────────────┤
│ 支撑层    store/    数据库与 ORM 模型                          │
│           config.py + setting/   配置与用户设置                │
│           plugin/   插件库（扫描 / 清单 / 每-agent 链接）       │
│           zonefilesystem/ 「我的空间」文件操作                 │
│           viewer/   交付块（block）模型                        │
│           observability/        可观测（记账/统计/定价/额度）   │
│           evals/    评测闭环                                   │
├─────────────────────────────────────────────────────────────┤
│ 交付      web/      React 前端（构建产物由 keeper 同源托管）    │
│           plugins/  参考插件（bash/excel/pdf/plugin-*）         │
│           deploy.sh / start_keeper.sh   打包与启动             │
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
- `core.py`：核心 agent（主脑）的**目录定义**（`~/.keeper/core-agent/`），不进数据库
- `executor.py`：**执行**。一轮对话怎么跑完（就绪守卫、逐步落库、挂起恢复）
- `process.py`：`Process` 多步 ReAct 循环
- `planner.py`：`ReActPlanner`，把工具清单 + 问题喂给 LLM 并解析其决策
- `protocol.py`：**ReAct 标签协议的唯一真源**（`<THOUGHT>` / `<ACTION>` / `<FINAL>` 的解析与生成）
- `simple_process.py`：快捷模式（不走 ReAct 多步循环）
- `delegatee.py` / `code_delegatee.py` / `delegatee_types.py`：**子 agent**（各自独立模型与插件）
- `ptc_sandbox.py`：代码执行的沙箱
- `context_store.py`：跨轮上下文的持久化

与会话/页面无关的逻辑才在这里 —— 会话落库在 `chat/`。

### `tools/` —— 工具

- `base.py`：`ProcessorTool` + `ToolRegistry`，工具出错**不会让 agent 崩溃**（兜成错误文本回给模型）
- `builtin/`：keeper 自带工具（**只有 2 个**：`fs.deliver` 交付块、`task.mark_item_done` 任务完成标记）
  —— 读 / 写 / 列目录 / 编辑 / 搜索文件**一律走 `bash` 插件**，不走内置工具
- `subprocess_tool.py`：插件的可执行文件（bin 组件）通道
- `collect.py`：工具聚合
- `guard.py`：**安全守卫**。只读工作区按读命令白名单放行，可写工作区只查路径越界

**工具的两个来源**：内置（代码写死，只受本地开关控制）+ 插件（**本地插件库**，
MCP 组件或 bin 组件）。

### `plugin/` —— 插件库

扫描 `~/.keeper/plugins`（库根，`config.plugins.root` 可改）与 `~/.keeper/agents/<id>/plugins`
（每 agent 的绑定目录），解析清单（`keeper-plugin.json` 或 `.claude-plugin/plugin.json`），
管理每-agent 的链接。

关键设计：
- 本机插件建**相对符号链接**（`~/.keeper` 搬家不断）
- 仓库根的 `plugins/` 只是**参考副本**，运行时读的是 `~/.keeper/plugins`
- 绑定关系由链接目录本身表达，列目录就是读绑定，不需要查表

### `observability/` —— 可观测

| 模块 | 职责 |
|---|---|
| `recording.py` | 记账：LLM 调用 / 工具调用 / 能力加载 / ReAct 解析统计 |
| `timeline.py` | 时间线：消息的 step 明细、每步耗时、单消息完整时间线 |
| `stats.py` | 统计：用量成本多维聚合、耗时分位、错误归类、提问率、去重调用 |
| `pricing.py` | 定价：按 (profile, model, provider) 匹配价格，支持时段 / 工作日折扣 |
| `budget.py` | 额度与聚合：预算上限检查、按维度聚合 |
| `dump.py` | 完整 prompt 落盘（调试用，需在设置里开启） |
| `api.py` | 查询接口（`/metrics/*`） |
| `_guard.py` | 埋点守卫：避免重复 / 无效调用进统计 |
| `_context.py` | 归属上下文：从 `chat/context` 的 ContextVar 读 trace/agent/session |

三条设计原则：
1. **落库粒度是「一次 LLM 调用一行」**，各级用量由该表聚合得出，不在各表冗余 token 列
2. **归属从 ContextVar 读**，不层层透传参数（调用链很深，透参会污染一堆签名）
3. **任何统计失败都不影响主流程**，写库一律 try/except 只记 debug

### `setting/` 与 `config.py` —— 两层配置

- `config.yaml` —— **元配置**，只由人手动改：server / agent / core_agent /
  framework / llm / workspace / agent_overrides / plugins / observability / a2a / context
- `~/.keeper/settings.yaml` —— **用户设置**，设置页写入、**立即生效**不需重启：
  observability / a2a / context / user_space

读取时按段覆盖，所以 settings.yaml 缺项也能跑（与代码默认值合并）。

### `zonefilesystem/` 与 `viewer/` —— 我的空间与交付块

- `zonefilesystem/`：「我的空间」的文件操作（浏览、读写、重命名、删除、导入导出），
  从 `chat/api.py` 里收归到独立包，Web 层只负责解析请求
- `viewer/block.py`：交付块的模型（`file` / `chart` / `mermaid` / `table` / `map` / `audio`），
  前端按 kind 渲染（`web/src/components/BlockView.tsx`）

### `memory/` —— 记忆

- `retriever.py`：jieba 预分词 + SQLite FTS5 检索
- `store.py`：**按天作用域**的短期记忆（`~/.keeper/memory/days/<日期>/turns.jsonl` + 索引）
  ——同一天开的多个会话共享记忆
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
a2a  = 对端经 A2A 调我（按 agent 路由：/agents/{agent_id}/a2a）
```

### `skill/` —— 声明式 Skill

以**数据**（prompt 模板 + tool 列表 + 可选子图）描述能力，而非 Python 代码。这样其它语言运行时也能解释同一份定义，agent 按名动态加载。

`always: true` 的技能正文常驻 system prompt（如 `plugin-authoring` 就只有 SKILL.md、不提供任何工具）。

### `store/` —— 存储

SQLite + SQLAlchemy 异步。存 agent 画像、模型预设与绑定、会话与消息、任务、
可观测事实表、插件绑定等。

### `tests/` —— 回归脚本

能直接 `python tests/xxx.py` 跑完出结论的脚本，**不引测试框架**（这样"跑一遍"不需要额外依赖）：

```
test_plugin_lib.py       插件库：清单解析、链接增删、幂等
test_guard.py            安全守卫：白名单放行、绕过拦截
test_planner_parse.py    ReAct 文本协议解析
test_hallucination_guard.py  幻觉检测
test_protocol.py         ReAct 标签协议
```

---

## 四、几个关键取舍

### 1. 能力装配一次、运行期不再变

工具表在**装配期固化**（`build_agent` 时）。所以：

- 换插件 / 换模型 → 自动触发重建实例
- **换工作区也要重建** —— 工作区根与只读标志是装配期读的
- 已经打开的会话持有旧工具表，需新发一轮对话才看到变化

### 2. 工作区 = 会话空间，「我的空间」是另一件事

历史教训：agent 画像上曾有 `workspace_read_only`，而运行期读的是 `user_space.read_only`
—— 两个来源打架，后果是「工作区明明可写，装配期却按 agent 画像上那个没人改过的默认值
把写类工具全丢了」，工具在界面上**凭空消失且无任何报错**。

现在的语义：

- **会话空间**（`~/.keeper/workspace/session/<session_id>/`）才是这一轮真正的工作目录，
  `root` 与 `agent_space` 指向同一份会话目录，默认可写；
- **「我的空间」**（路径存 `~/.keeper/settings.yaml` 的 `user_space.path`）是**用户自管的目录**，
  是文件浏览 / 交付的落点，与会话空间不是一回事；
- agent 表上曾经那两列工作区列已被删除（`_drop_agent_legacy_columns`），
  工作区是**会话级属性**，不属于 agent 画像；
- 插件的写类工具**不在装配期按只读丢弃**（与内置工具一致，改为运行时拒绝），
  避免「工具消失」这种静默失败。

### 3. 插件走子进程，不进主进程

`tools/subprocess_tool.py` 的注释说得很直白：第三方代码进主进程等于把 keeper 的全部权限交给它 —— 能读所有 agent 的会话，依赖还可能跟 keeper 打架。

代价是每次调用付一次进程启动，换来**语言无关 + 崩了只影响这一次调用**。

协议极简：stdin 一行 JSON / stdout 最后一行 JSON / stderr 随便打。

### 4. 内置工具与插件的两条来源

| | 内置 | 插件 |
|---|---|---|
| 定义位置 | keeper 代码 | 本地插件库（`~/.keeper/plugins`） |
| 开关 | `AgentCapabilityOverride`，无记录 = 启用 | 同左 |
| 形态 | Python 工厂 | MCP 组件 / bin 组件 / 知识（SKILL.md） |
| 需要外部来源 | 否 | 否（只读本地库） |

### 5. 模型没有兜底

模型配置只有一个来源：`agent_llm_binding` → `llm_profiles`。

历史上这里有过三层兜底（inline 配置被同步灌全局 `llm.yaml`、全局 `llm.yaml`、
建 agent 时自动绑第一个预设），每一层都制造过「改了配置却不生效、又查不出原因」的问题。
现在语义唯一：**先在模型管理页配好模型，再显式绑定给 agent**；未绑定就是纯索引模式
（不调用模型），并在日志里明确告警。

改预设 / 删预设都会触发绑着它的 agent **自动重装配**——LLM 实例是在装配期固化的，
不重载就得重启整个服务（而重启会中断正在跑的会话）。

### 6. 前端同源托管

`web/dist` 由 keeper 自己挂成站点根（或由 nginx 反代）。前端生产包 `BASE=""`，请求不带 `/api` 前缀，与 API **同源**，因此不需要 CORS。

---

## 五、一次对话的完整链路

```
用户发消息
  → chat/api.py          建/取会话，解析工作区（resolve_workspace）
  → agent/executor.py    就绪守卫 → 交给 Process
  → agent/process.py     多步 ReAct 循环：
      planner.py  把工具清单 + 问题 → LLM → 解析出 <THOUGHT>/<ACTION>
      tools/guard.py   执行前的安全守卫（只读白名单 / 路径越界）
      执行工具（内置 / MCP / 插件子进程）
      observability/recording.py   记账（LLM 调用、工具调用）
  → chat/service.py      逐步落库（消息 / 步骤 / 产物）
  → observability/       时间线、统计、额度检查
```

### 唯一入口，一份编排：`run_one_ask`

所有会触发一轮执行的入口（页面 SSE、任务模式、评测）**共用一份编排**——
`chat/service.py::run_one_ask`：

```
入口（页面 SSE / 任务 / 评测）      ┐
                                   ├─→ run_one_ask()
                                   ↓
                          ChatService.ask()
                       （执行层，所有入口共用）
```

其余全部共用：`ensure_run` → `ensure_session_id` → `register_run` → `ask` →
`hub_finish` → `finally` 注销 + 清停止标记。

**为什么必须共用**：这份编排曾经被写了两遍，并因此出过事——其中一份把
`chat.hub.finish` 的导入名写错（`from ..chat.hub import hub_finish`，真名是 `finish`），
ImportError 落在所有 `try` 之外，而另一侧安然无恙。**两份代码只有一份会坏，是最坏的
组合**：它不报错，只是那条路不通。

更麻烦的是 `pyflakes` 查不出 `from X import Y` 里的 Y 不存在（它只管"导入了没用到"），
静态检查全绿、运行时才炸。所以回归脚本会**真的把入口各跑一遍**，
其中一条断言直接比对 `chat.api.run_one_ask is service.run_one_ask`——
挡住"哪天又有人 copy 一份过去"。

**新增入口时**：不要复制这段编排，调 `run_one_ask` 并传入自己的回调
（`on_session` / `on_step` / `on_delta`）即可。

---

## 六、数据与目录

```
~/.keeper/
├── core-agent/                 核心 agent（主脑）：agent.yaml + soul.md
├── agents/<agent_id>/plugins/  该 agent 的插件绑定（链接或下载副本）
├── plugins/                    插件库（~/.keeper/plugins）
├── workspace/
│   ├── user/<space>/           用户空间（含 default）
│   ├── agent/<agent_id>/       agent 级私有产物（插件索引 / 缓存）
│   └── session/<session_id>/   会话空间（这一轮的工作目录）
├── memory/days/<日期>/         记忆：turns.jsonl + FTS5 索引
├── evals/                      评测产物（基线 / 运行记录）
├── prompt-dump/                prompt 落盘（调试，默认关）
├── context-store/              跨轮上下文
├── logs/                       桌面形态的内核日志
├── framework/                  本机运行时（python / node）
└── settings.yaml               用户设置
```

数据库在 `data/keeper.db`（SQLite，随项目目录走）。

---

## 七、接口分组

| 前缀 | 内容 |
|---|---|
| `/manage/agents/*` | agent 装载 / 卸载 / 本地创建、插件绑定、模型绑定 |
| `/manage/models/*` | 模型预设 CRUD、连通性测试、分段单价 |
| `/manage/plugins/*` | 插件库（只读：库根 + 库里有哪些插件） |
| `/manage/delegatees/*` | 子 agent（各自独立模型 + 插件） |
| `/manage/settings/*` | 用户设置读写（写回 `~/.keeper/settings.yaml`） |
| `/manage/sessions/trash` | 会话回收站（软删除会话的列表与恢复） |
| `/agents/{agent_id}/*` | 会话：对话、能力开关、我的空间文件、时间线、附件 |
| `/user-spaces/*` | 我的空间：浏览 / 读写 / 重命名 / 删除 / 导入导出 |
| `/metrics/*` | 可观测：用量、耗时、错误、提问率、重复调用 |
| `/framework/*` | 本机运行时（python / node）检测 |
| `/evals/*` | 评测 |
| `/agents/{id}/a2a`、`/agents/{id}/.well-known/agent.json` | A2A 协议面（按 agent 路由） |
| `/health` | 健康检查（只返回 ready 标志，桌面壳靠它判断内核就绪） |

**没有入站认证**：所有接口直接开放，只靠「绑回环 + 本机单用户」这条边界
（见 [`security.md`](security.md)）。

---

## 八、启动与部署

### 首次配置（按顺序）

1. **依赖**：Python ≥ 3.10；`./start_keeper.sh` 会自动建 `.venv` 并装 `requirements.txt`。
   桌面端另需 Node.js ≥ 18（`web/` 构建 + `desktop/` 的 Electron）。
2. **`config.yaml`**：确认 `server.host` 是回环地址（`127.0.0.1`）——这是现在
   **唯一的网络边界**（没有入站认证）。
3. **模型**：在「模型管理」建预设 → 测试连通性 → 绑定给 agent
   （核心 agent 的 id 是 `core`）。**不配模型 = agent 不调用模型**（纯索引模式）。
4. **「我的空间」**：在设置里选一个目录（路径存 `~/.keeper/settings.yaml` 的 `user_space.path`）。
5. 起服务，打开 `http://127.0.0.1:8080`（桌面形态由壳自动打开）。

完整步骤见项目根 [`README.md`](../README.md)。

### 脚本

- `start_keeper.sh` —— 建 venv、装依赖（仅首次或 `requirements.txt` hash 变化时）、前台启动。宝塔等无 `HOME` 的环境有 `KEEPER_HOME` 回退
- `stop_keeper.sh` —— 停止
- `desktop/start_desktop.sh` —— 桌面版（Electron 窗口 + Python sidecar）；自动装 Electron、自动构建 `web/dist`
- `desktop/stop_desktop.sh` —— 停止桌面版（顺带收掉残留内核）
- `web/build_web.sh` —— 构建前端产物
- `deploy.sh` —— 打包服务端（排除 data / web / 日志 / 测试）
- `web/deploy.sh` —— 构建前端并打 zip

**日志**：默认只到控制台；设 `LOG_FILE` 会额外落一份（Rotating，默认 10MB × 5 份）。装配期会打印每个插件 / 每个工具的装配结果与跳过原因，定位「工具为什么没出现」看这里。

---

## 九、代码模块 ↔ 文档

**每个代码模块都有对应文档**（下表是双向索引）：

| 代码模块 | 文档 | 讲什么 |
|---|---|---|
| `agent/` | [`architecture.md`](architecture.md) 第三节 + [`capability-loading.md`](capability-loading.md) | 装配、执行、ReAct 协议、按需加载 |
| `chat/` | [`session-design.md`](session-design.md)、[`task-design.md`](task-design.md) | 会话存储、任务模式 |
| `tools/` | [`tools.md`](tools.md) | 工具注册、子进程通道、安全守卫 |
| `plugin/` + `mcp/` | [`plugin-design.md`](plugin-design.md) | 插件库、清单格式、绑定机制 |
| `skill/` | [`capability-loading.md`](capability-loading.md) | SKILL.md 声明式技能 |
| `llm/` | [`llm.md`](llm.md) | 模型预设、显式绑定、计价 |
| `memory/` | [`memory.md`](memory.md) | 短期记忆、检索、**长期记忆库规划** |
| `agent/context_store.py` | [`context.md`](context.md) | 外部化、压缩、水位 |
| `zonefilesystem/` | [`user-space.md`](user-space.md) | 我的空间、文件展示与编辑、**向量跨模态搜索规划** |
| `viewer/` | [`artifact-design.md`](artifact-design.md) | 交付块模型与渲染 |
| `observability/` | [`observability-design.md`](observability-design.md) | 事实表、指标、定价 |
| `evals/` | [`evals.md`](evals.md) | 用例集、基线、报告 |
| `peer/` + `a2a/` | [`collaboration-contract.md`](collaboration-contract.md) | 跨端契约、AgentCard、熔断 |
| `store/` + `setting/` + `config.py` | [`session-design.md`](session-design.md) + 本文第八节 | 存储模型、两层配置 |
| `framework/` | 本文 | 本机运行时自举（镜像 / 版本 / 校验） |
| `desktop/` | [`../desktop/README.md`](../desktop/README.md) | Electron 壳、sidecar、原生能力 |
| `web/` | 项目根 [`README.md`](../README.md) | 界面能力清单（按菜单组织） |

### 文档索引

| 文档 | 内容 |
|---|---|
| **`architecture.md`**（本文） | **模块地图与设计取舍**——想了解全貌从这里开始 |
| `positioning.md` | 产品定位：本机单用户形态、A2A、与同类产品的分界 |
| `security.md` | **威胁模型与已知缺口**——哪些能信、哪些不能信、哪里还没堵上 |
| — 运行机制 — | |
| `session-design.md` | 会话存储模型（表结构、跨端链路、挂起恢复） |
| `task-design.md` | 任务模式（计划 → 审批 → 执行 → 验收） |
| `capability-loading.md` | 能力按需加载（L1 摘要 / L2 正文 / `load_capabilities`） |
| `context.md` | 上下文治理（工具输出外部化 + 水位压缩） |
| `memory.md` | 记忆与检索（短期记忆 + jieba/FTS5）+ **长期记忆库规划** |
| `artifact-design.md` | 产物机制（交付块模型、安全策略、按类型展示） |
| `tools.md` | 工具注册、子进程通道、**安全守卫与危险工具确认** |
| `llm.md` | 模型预设、显式绑定、计价、调用链技术债 |
| `plugin-design.md` | 插件库、清单格式、绑定机制 |
| `user-space.md` | **我的空间**：文件展示与编辑、**向量跨模态搜索** |
| — 外部与质量 — | |
| `collaboration-contract.md` | 跨端协作契约（thread / message、超时熔断、降级） |
| `observability-design.md` | 可观测（一次 LLM 调用一行的选型依据） |
| `evals.md` | 评测闭环（隔离、断言、基线） |
| `roadmap.md` | **未来要做的功能**（三个方向） |
