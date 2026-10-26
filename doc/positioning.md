# keeper 定位（Positioning）

> 一句话：**keeper 是「本地知识库 agent」**——把「我的空间」变成 agent 的知识库：
> 读你的文件、按语义检索、改你的文件、把结果交付给你，并记住关于你的事。
> 本机单用户，模型、插件、能力开关**全部在本机维护**；
> 本地 agent 之间经 **A2A 协议**协同。

配套阅读：`user-space.md`（知识库本体）、`architecture.md`（模块地图）、
`collaboration-contract.md`（跨端契约）、`session-design.md`（本地存储模型）。

---

## 一、我们是什么

- 一个**本地知识库 agent**：知识来自「我的空间」——你自己的文档、代码、表格、PDF、
  图片，加上关于你的记忆。
- 能力闭环是**读 → 找 → 改 → 交**：
  - **读**：多格式预览（md / 代码 / 表格 / PDF / 图片 / HTML / 视频）
  - **找**：浏览 + 语义检索（向量化**在建**，见 [`roadmap.md`](roadmap.md)）
  - **改**：编辑并保存回原文件（md / 代码 / 表格已支持）
  - **交**：带类型的交付块（图表 / 流程图 / 表格 / 地图 / 音频 / 文件）
- agent 之间用 **A2A 协议（v1.0.0）** 协作：一个 agent 经 `SendMessage` 派任务给另一个，
  后者可反问（`INPUT_REQUIRED`）、交付产物（Artifact）、汇报状态（Task）。
- **无头内核**：核心是 A2A-native 的运行时，不绑定特定 UI。前端（`web/`）是可替换的
  一层，构建产物由 keeper 同源托管。

### 1.1 单平面：本机运行时【已落地】

```
┌── 本机运行时（keeper，桌面壳 / :8080）─────────────────┐
│  zonefilesystem/ 我的空间：知识库的物理形态             │
│  agent/       装配实例（工具表在装配期固化）             │
│  llm/         模型预设 + 绑定（本机维护）                 │
│  plugin/      插件库（本机维护）+ 每-agent 绑定           │
│  memory/      短期记忆（长期事实库在建）                  │
│  chat/        会话、步骤、产物全部留在本地                 │
│  peer/ a2a/   经 A2A 协同其他 agent                      │
│  store/       SQLite：画像 / 会话 / 事实表                │
└─────────────────────────────────────────────────────────┘
```

- **没有外部配置源**：keeper 是**本机单用户**应用，agent 画像、模型、插件、
  能力开关**全部在本机维护**。没有"平台"、没有"市场"、没有"同步"。
- **运行时与数据都在用户机器上**：不投递消息、不存会话到别处。
- **agentId 本地生成**（ULID），历史按它归拢。
- **模型、插件、能力开关在本机**：模型预设存本地 `llm_profiles`，agent 通过
  `agent_llm_binding` **显式绑定、无兜底**；插件库在 `~/.keeper/plugins`；
  能力开关存 `agent_capability_overrides`。
- **没有入站认证**：信任边界是「本机只有你一个人用 keeper」，详见
  [`security.md`](security.md)。

对应代码：

| 概念 | 落点 |
|---|---|
| 本地创建 agent | `agent/api.py` → `POST /manage/agents` |
| 启动 / 停止 agent | `agent/api.py` → `POST /manage/agents/{id}/start` / `unload` |
| 模型预设与绑定 | `llm/api.py`（`/manage/models`）、`agent/api.py`（`POST /agents/{id}/model`） |
| 插件库与绑定 | `plugin/lib.py`、`agent/api.py`（`PUT /manage/agents/{id}/plugins`） |

---

## 二、协议层：采用 A2A，不自造

- **不复现 A2A**。A2A 已开放标准（Google 发起、Linux Foundation 托管），提供
  AgentCard 发现、Task 生命周期、`input-required` 反问、Artifact 产物。
- keeper 的对外协议面 = A2A（**按 agent 路由**）：
  - `POST /agents/{agent_id}/a2a`：JSON-RPC（`SendMessage` / `GetTask` / `CancelTask`）
  - `GET /agents/{agent_id}/.well-known/agent.json`：暴露该 agent 的 AgentCard
- **对端能力动态发现**：本端**不写死**对端工具清单。从对端配置（`AgentConfig.peers`
  / `agent_peers` 表）取对端 `a2a_url`，
  拉它的 AgentCard，按 `skills[]` 当场生成 `{peer}__{skill_id}` 工具，description 直接
  取自对端自己声明的 `skills[].description`。对端新增能力只需在 AgentCard 多声明一个
  skill，本端自动可见——**不再经 MCP 拉工具清单**。
- 对端只要说 A2A 就能协作，不要求它是 keeper 造的、也不要求进任何框架。

---

## 三、与 AgentScope / A2A 官方 / 通用 coding 产品的分界

| | AgentScope | A2A 官方 SDK | 通用 coding 产品 | **keeper** |
|---|---|---|---|---|
| 领域 | 水平空壳 | 协议，无领域 | 写代码（通用） | **本地知识库** |
| 知识库 | 需自建 | — | 上下文窗口 | **本地文件 + 向量索引（在建）** |
| 创建 agent | YAML + 可插拔 | — | — | 本地创建 + 本地装配 |
| 多 agent | 框架内 | 协议 | 各厂封闭 | **A2A-native 异构编排** |
| 对端前提 | 进框架 / 注册中心 | 说 A2A | 自家的 | **任意框架 / 零改造** |
| UI | Studio | 无 | 有 | 桌面壳（Electron）+ 可替换前端 |
| 数据 | 视部署而定 | — | 云端 | **全部留在本地** |

- 我们**不是**另一个 AgentScope：它给空机床，我们给「知识库 agent 的整条产线」。
- 我们**不是**另一个 coding agent：不做通用 coding（巨头死亡区），做「本地知识库上的
  读 → 找 → 改 → 交」。
- 我们**不是**又一个云端 RAG 工具：知识库是**你本机的文件**——不搬家、不上传、
  不按量计费，也不需要为「知识在别人服务器上」付费。

---

## 四、知识库 agent 的工作流

- **读**：把「我的空间」里的文件按类型渲染出来（md / 代码 / 表格 / PDF / 图片 /
  HTML / 视频），用户与 agent 看到的是同一份内容。
- **找**：目录树浏览 + 关键词；**语义检索**（本地向量 + 关键词混合）**已可用**，
  跨模态（图片 / 音视频）**在建**。
- **改**：在预览里直接编辑并保存回原文件（md / 代码 / 表格已支持；更多格式在建）。
- **交**：agent 产出带类型的交付块（图表 / 流程图 / 表格 / 地图 / 音频 / 文件），
  直接嵌在回答里，正文用 `[[art:ref]]` 定位。
- **记**：按会话的短期记忆已可用；**长期事实库**（跨会话、跨天）**在建**。

协作示例：用户问「我们上次定的缓存方案是什么」→ 本端经 A2A 派 Task 给对端 →
对端 `INPUT_REQUIRED` 反问「哪份文档？」→ 本端答 → 对端从知识库检索并回 Artifact →
本端交回用户。

---

## 五、我们不做（刻意边界）

- 不自造通信协议（直接采用 A2A）。
- 不碰通用 agent 框架的红海（不跟 AgentScope 卷广度）。
- 不做通用 coding agent。
- **不做多端 / 多租户形态**：只服务本机单用户，不做账号体系、不做云端同步、
  不做跨设备访问（数据主权与使用范围都在用户本地）。

---

## 六、差异化落点（诚实版）

技术零件（编排循环 / MCP 调用 / 记忆 / YAML 组装）都是 commodity，护城河在：

1. **本地知识库的深度**：不是「接一个向量库就完事」，而是**每种文件类型都有自己的
   展示与编辑方式**（表格保公式格式、PDF 能批注、图片能标注），再叠一层跨模态的
   语义检索——靠真实文件与真实使用数据迭代。
2. **零改造异构接入 + 不锁定**：对端任意框架、A2A 互通；**全程离线**，
   文件、记忆、会话、任务、可观测数据全留本地。
3. **可观测是内建的，不是外挂**：「一次 LLM 调用一行」的事实表，各级用量由它聚合
   得出，不靠各表冗余 token 列（见 `observability-design.md`）。

---

## 七、当前实现状态

| 能力 | 状态 | 主要落点 |
|---|---|---|
| **「我的空间」**（知识库的物理形态） | ✅ | `zonefilesystem/` + `settings.yaml` 的 `user_space.path` |
| **读**：多格式预览（md / 代码 / 表格 / PDF / 图片 / HTML / 视频） | ✅ | `web/src/components/`（`SheetViewer` / `PdfViewer` / `ImageViewer` …） |
| **改**：编辑并保存回原文件（md / 代码 / 表格保公式格式） | ✅ | `UserSpacePage.tsx` + `SheetViewer` |
| **交**：带类型的交付块（file / chart / mermaid / table / map / audio） | ✅ | `viewer/block.py` + `tools/builtin/` |
| **找**：目录树浏览 + 关键词（`grep` / `find`） | ✅ | `zonefilesystem/browse.py` + `bash` 插件 |
| **找**：本地语义检索（向量 + 关键词混合，Markdown / PDF） | ✅ | `search/` + 「搜索」页；见 [`indexing.md`](indexing.md) |
| **找**：agent 侧读本地资料（`kb.*` 工具） | ✅ | `knowledge/`；见 [`kb-pdf-pages.md`](kb-pdf-pages.md) |
| **找**：跨模态向量检索（图片 / 代码 / 表格 / 音视频） | 🚧 在建 | 见 [`roadmap.md`](roadmap.md) 方向 ② |
| **记**：按会话短期记忆 + FTS5 检索 | ✅ | `memory/` |
| **记**：长期事实库（跨会话 + 画像 + 衰减） | 🚧 在建 | 见 [`roadmap.md`](roadmap.md) 方向 ③ |
| **本地创建 agent** | ✅ | `agent/api.py` → `POST /manage/agents` |
| 启动 / 停止 agent | ✅ | `agent/api.py` → `POST /manage/agents/{id}/start` / `unload` |
| 能力装配（内置 + 插件） | ✅ | `agent/config.py` |
| 三种组件形态（MCP / bin / 知识） | ✅ | `mcp/` + `tools/subprocess_tool.py` + `skill/` |
| **模型预设与绑定**（本机维护、无兜底） | ✅ | `llm/api.py` + `agent/api.py` |
| A2A 协议面（按 agent 路由 + AgentCard） | ✅ | `a2a/` |
| 对端 AgentCard discovery | ✅ | `peer/service.py` |
| 会话 / 步骤 / 产物落库 | ✅ | `chat/service.py`、`store/models.py` |
| 任务模式（计划 + 审批 + 验收） | ✅ | `tasks` / `task_items`、任务模式 API |
| 可观测（用量 / 成本 / 耗时） | ✅ | `observability/` |
| 会话空间（每轮的工作目录） | ✅ | `chat/service.py::resolve_workspace` |
| 桌面端（Electron 壳 + sidecar） | ✅ | `desktop/` |
| 评测闭环 | ✅ | `evals/` |

---

## 八、一句话总结

**keeper 是本地知识库 agent：把「我的空间」变成它的知识库，在本机完成
读 → 找 → 改 → 交，并记住关于你的事——文件、记忆、会话、数据全部留在本地，
agent 之间用开放的 A2A 协议协同。**
