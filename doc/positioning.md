# keeper 定位（Positioning）

> 一句话：**keeper 是「中心配置平台 + 本地 agent 运行时」**——平台管 MCP / Skill /
> Tool / Agent 的**定义与市场**，本地应用带 token 拉取「我已添加」的 agent 就地运行；
> 本地 agent 之间经 **A2A 协议**协同。

配套阅读：`architecture.md`（模块地图）、`collaboration-contract.md`（跨端契约）、
`session-design.md`（本地存储模型）。

---

## 一、我们是什么

- 一个**研发领域的 agent 运行时**：预置研发资产（角色模板、研发技能、研发工具、
  隔离工作区、质量门评估）。
- agent 之间用 **A2A 协议（v1.0.0）** 协作：一个 agent 经 `SendMessage` 派任务给另一个，
  后者可反问（`INPUT_REQUIRED`）、交付产物（Artifact）、汇报状态（Task）。
- **无头内核**：核心是 A2A-native 的运行时，不绑定特定 UI。前端（`web/`）是可替换的
  一层，构建产物由 keeper 同源托管。

### 1.1 两平面：配置平台 / 本地运行时【已落地】

```
┌── 配置平台（keeperplatform，:9095）──────────┐
│  管理 agent / 插件 / 模型 的定义与市场         │
│  用户「我已添加」关系；token 鉴权             │
│  只下发配置，不参与投递、不存会话             │
└──────────────┬────────────────────────────────┘
               │  keeper 带 token 拉「我已添加」
               ▼
┌── 本地运行时（keeper，:8080）─────────────────┐
│  plat/        拉配置 → 落库 → 下载插件资源     │
│  agent/       装配实例（工具表在装配期固化）   │
│  chat/        会话、步骤、产物全部留在本地     │
│  peer/ a2a/   经 A2A 协同其他 agent            │
└───────────────────────────────────────────────┘
```

- **平台只管定义**：资产增删改查、市场、「已添加」关系、token 签发。
- **运行时在用户机器上**：本地拉配置后就地跑，**平台不参与投递、不存会话**。
- **agentId 两端一致**：平台分配稳定 id，本地原样使用（`Agent.id`），历史按它归拢。
- **离线可用**：平台只做配置分发，非运行时必需——不连平台也能跑已装载的 agent。

对应代码：

| 概念 | 落点 |
|---|---|
| 拉「我已添加」 | `plat/client.py` → `fetch_my_agents()` |
| 配置落库 | `plat/sync.py` → `sync_agent()` |
| 插件资源下载 | `plat/fetcher.py` → `fetch_resource()` |
| 装载 / 卸载 | `api/agents.py` → `POST /manage/agents/{id}/load` / `DELETE` |

---

## 二、协议层：采用 A2A，不自造

- **不复现 A2A**。A2A 已开放标准（Google 发起、Linux Foundation 托管），提供
  AgentCard 发现、Task 生命周期、`input-required` 反问、Artifact 产物。
- keeper 的对外协议面 = A2A：
  - `POST /a2a`：JSON-RPC（`SendMessage` / `GetTask` / `CancelTask`）
  - `GET /.well-known/agent.json`：暴露本 agent 的 AgentCard
- **对端能力动态发现**：本端**不写死**对端工具清单。从 `config.peers` 取对端 `a2a_url`，
  拉它的 AgentCard，按 `skills[]` 当场生成 `{peer}__{skill_id}` 工具，description 直接
  取自对端自己声明的 `skills[].description`。对端新增能力只需在 AgentCard 多声明一个
  skill，本端自动可见——**不再经 MCP 拉工具清单**。
- 对端只要说 A2A 就能协作，不要求它是 keeper 造的、也不要求进任何框架。

---

## 三、与 AgentScope / A2A 官方 / 垂直 coding 产品的分界

| | AgentScope | A2A 官方 SDK | 通用 coding 产品 | **keeper** |
|---|---|---|---|---|
| 领域 | 水平空壳 | 协议，无领域 | 写代码（通用） | **研发领域（专属资产）** |
| 创建 agent | YAML + 可插拔 | — | — | 平台配置 + 本地装配 |
| 多 agent | 框架内 | 协议 | 各厂封闭 | **A2A-native 异构编排** |
| 对端前提 | 进框架 / 注册中心 | 说 A2A | 自家的 | **任意框架 / 零改造** |
| 平台 / UI | Studio | 无 | 有 | 无头内核 + 可替换前端 |
| 数据 | 视部署而定 | — | 云端 | **全部留在本地** |

- 我们**不是**另一个 AgentScope：它给空机床，我们给「研发 agent 的整条产线」。
- 我们**不是**另一个 coding agent：不做通用 coding（巨头死亡区），做「多 agent 经
  开放标准协作的研发编排」。

---

## 四、首发 agent 与协作流

- **咨询型**（如 keeper）：接代码库 RAG + 对话，不动代码。
- **写代码型**（如 code-reviewer / 助手）：在隔离工作区读文件、改文件、跑测试。

协作示例：用户问「把登录改成 jwt」→ 本端经 A2A 派 Task 给对端 → 对端
`INPUT_REQUIRED` 反问「用哪个分支」→ 本端答 → 对端干完回 Artifact → 本端交回用户。

---

## 五、我们不做（刻意边界）

- 不自造通信协议（直接采用 A2A）。
- 不碰通用 agent 框架的红海（不跟 AgentScope 卷广度）。
- 不做通用 coding agent。
- **平台不进运行时链路**：只管配置与分发，不投递消息、不存会话 / 任务数据
  （数据主权在用户本地）。

---

## 六、差异化落点（诚实版）

技术零件（编排循环 / MCP 调用 / 记忆 / YAML 组装）都是 commodity，护城河在：

1. **研发领域深度**：领域 prompt / skill / tool（代码库 RAG、测试、lint、git、执行）/
   质量门评估——靠真实使用数据迭代。
2. **零改造异构接入 + 企业不锁定**：对端任意框架、A2A 互通；**平台只做配置分发**，
   本地可离线跑；会话、任务、可观测数据全留本地。
3. **可观测是内建的，不是外挂**：「一次 LLM 调用一行」的事实表，各级用量由它聚合
   得出，不靠各表冗余 token 列（见 `observability-design.md`）。

---

## 七、当前实现状态

| 能力 | 状态 | 主要落点 |
|---|---|---|
| 装载 / 卸载平台 agent | ✅ | `plat/` + `api/agents.py` |
| 能力装配（内置 + 插件） | ✅ | `agent/config.py` |
| 两种组件形态（MCP / bin） | ✅ | `mcp/` + `tools/subprocess_tool.py` |
| A2A 协议面（`/a2a` + AgentCard） | ✅ | `a2a/` |
| 对端 AgentCard discovery | ✅ | `peer/service.py` |
| 会话 / 步骤 / 产物落库 | ✅ | `chat/service.py`、`store/models.py` |
| 任务模式（计划 + 审批 + 验收） | ✅ | `tasks` / `task_items`、任务模式 API |
| 可观测（用量 / 成本 / 耗时） | ✅ | `observability/` |
| 记忆（短期 + FTS5 检索） | ✅ | `memory/` |
| 用户工作区（可写 / 只读） | ✅ | `user_space` 表 + `chat/service.py` |
| 评测闭环 | ✅ | `evals/` |

---

## 八、一句话总结

**keeper 把「快速造 agent」收敛到研发这一个领域，并让造出来的 agent 用开放的 A2A
协议互相协作——预置研发资产 + 异构零改造 + 数据留在本地，是我们和空壳框架、通用
coding 产品、纯协议 SDK 的分界。**
