# keeper 任务模式（Task Mode）设计

> 本文描述**已实现**的任务模式。数据模型与 `store/models.py` 一致。

配套：`session-design.md`（任务跑在一个会话里）、`artifact-design.md`（产物）。

---

## 一、为什么需要任务模式

对话模式够用，但遇到「要做十几件事」的场景会失控：模型边想边做，做到一半
上下文耗尽，或者顺序乱掉，用户看不到进度也改不了计划。

任务模式补上三件事：

1. **先出计划**：把「要做的事」显式列出来，人能看、能改、能审批。
2. **逐点执行**：一次推进一个计划点，做完打勾，有明确的进度。
3. **验收与打回**：交付前过一道验收，不合格退回去修。

---

## 二、核心决策

### 2.1 一个任务绑定一个会话（D1）

**不建 `task_runs` 表**（D2）。理由：执行过程本身就是该 session 里的消息，
天然留痕，再建一张表只是重复。

好处是**不存在「跨 run 失忆」**——产出计划、逐点执行、打回修补全程都在同一个
会话里对话，上下文连续。

### 2.2 `task_items` 是计划的事实来源（D3）

`Task.plan_md` 只是由 `task_items` 渲染出的 markdown 视图：

- 勾选态 / 过程产物 / 结论都落在 `task_items` 行上
- `plan_md` 按 `seq` 渲染，`done` 渲染成 `- [x]`
- 人在文本里直接改 → 反向 upsert items

**不双写**，避免 markdown 与结构化状态不一致。

### 2.3 会话可复用 vs 必须新建

- 任务会话 `kind='task'`，与普通会话同表，用 `kind` 区分
- 一个任务固定一个 session，任务结束后该会话仍可查看

---

## 三、数据模型

### 3.1 `tasks` — 任务定义 + 计划 + 状态 + 结果

| 字段 | 说明 |
|---|---|
| `agent_id` / `title` / `description_md` | 归属与描述 |
| `session_id` | 绑定的唯一会话（D1） |
| `status` | 状态机（见第四节） |
| `paused` | 暂停开关，按 step 暂停 |
| `plan_md` | 计划 markdown（**视图**，非事实来源） |
| `plan_feedback` | 最新一条审批意见 |
| `plan_reject_count` / `plan_version` | 审批打回次数 / 计划版本 |
| `replan_count` | 重新规划次数 |
| `review_reject_count` / `review_feedback` | 验收打回次数 / 最新验收意见 |
| `result_summary` / `artifacts` | 结果摘要 / **结果产物** |
| `started_at` / `finished_at` | 任务自身的时间戳 |

任务耗时**单独放在本表**而不是按 session 聚合：任务与会话虽一对一，但任务有
自己的生命周期（审批、打回、暂停），用任务自身的时间戳更贴合语义。

### 3.2 `task_items` — 计划点（事实来源）

| 字段 | 说明 |
|---|---|
| `task_id` / `seq` / `plan_version` | 归属、序号、版本 |
| `content_md` | 计划点内容 |
| `status` | `pending` / `doing` / `done` / `skipped` |
| `rounds` | 已执行的对话轮数（配合单点上限防死循环） |
| `conclusion` | 结论 |
| `artifacts` | **过程产物**（与 `session_messages.artifacts` 同构） |
| `started_at` / `finished_at` | 时间 |

### 3.3 产物的两层

- **过程产物** → `task_items.artifacts`（每个计划点自己产生的文件）
- **结果产物** → `tasks.artifacts`（最终交付）

二者与 `session_messages.artifacts` **同构**（JSON 数组，`path` 带 `file://` scheme），
取数统一走 `chat/artifacts.py`。

---

## 四、状态机（`tasks.status`）

```
draft ──> planning ──> plan_review ──> executing ──> waiting_review ──> done
              │             │              │               │
              │             │(打回)        │(暂停)          │(打回)
              │             ▼              ▼               ▼
              └────────> planning      waiting_input    executing
                                                            │
                                                            ▼ (超上限)
                                                         failed
任意状态 ──> cancelled
```

| 状态 | 含义 |
|---|---|
| `draft` | 刚创建，还没生成计划 |
| `planning` | 正在生成计划 |
| `plan_review` | 计划待审批 |
| `executing` | 逐点执行中 |
| `waiting_input` | 暂停等待用户输入 |
| `waiting_review` | 待验收 |
| `done` / `failed` / `cancelled` | 终态 |

**暂停语义**：前端点「暂停」置 `paused=True`；ReAct 每跑完一步就检查它，命中则停
在该步之后——**该步已落库，未开始的下一步丢弃**；下次「执行」从断点续跑。

---

## 五、执行流程

1. 创建任务（`draft`）→ 绑定 session
2. 生成计划（`planning`）→ 写入 `task_items` → 渲染 `plan_md`
3. 计划审批（`plan_review`）：通过 → `executing`；打回 → 带 `plan_feedback` 回到
   `planning`，`plan_reject_count + 1`
4. 逐点执行（`executing`）：取当前 `plan_version` 下第一个非终态点，一轮对话推进它
   （`rounds + 1`），完成则 `done` + 写 `conclusion`
5. 全部完成 → `waiting_review`
6. 验收：通过 → `done`；打回 → `review_reject_count + 1` 并回到 `executing` 修补

### 5.1 单点上限（防失控）

`task_items.rounds` 配合上限：一个计划点超过 N 轮仍未完成，判定失败并转 `failed`。
避免模型在某一点上死循环。

### 5.2 重新规划（replan）

`replan_count + 1`，`plan_version + 1`：

- **已完成的点原样保留**（不丢已完成的工作）
- 未完成的置 `skipped`
- 新点写入新版本
- **执行只推进当前版本的点**，历史版本仅作留痕

---

## 六、与产物 / 记忆的关系

- 计划点产生的文件 → `task_items.artifacts`（过程产物）
- 最终交付 → `tasks.artifacts`（结果产物）
- 执行过程中的对话 → 任务绑定的 session 里的 `session_messages`（天然留痕）
- 可观测：`llm_calls` 按 `task_id` 归属，任务维度的用量成本可单独聚合

---

## 七、上限与保护

| 保护项 | 机制 |
|---|---|
| 单点死循环 | `task_items.rounds` 上限 |
| 计划反复打回 | `plan_reject_count` 上限 |
| 验收反复打回 | `review_reject_count` 上限（超出转 `failed`） |
| 用户中途干预 | `paused` 按 step 暂停 |
| 预算超支 | `observability/budget.py` 的 `check_budget` |

这些上限是**防失控**用的，不是业务规则——正常流程不该碰到。

---

## 八、风险与已知限制

- **计划质量依赖模型**：计划点切分不合理时，执行会反复修补。缓解：审批环节 +
  重新规划。
- **验收标准主观**：当前验收意见由人给（`review_feedback`），没有自动化门禁。
- **任务与会话一对一**：不支持一个任务拆到多个会话（D1 的取舍，换来了上下文连续）。
- **过程产物可能很多**：`task_items.artifacts` 是 JSON 数组，单点产物过多会让行变大。
  当前没有分片。
