# keeper 会话存储设计

> 本文描述**已实现**的会话存储模型。数据模型与代码一致，不再含待办 / 分期。

配套：`task-design.md`（任务模式是在会话之上的一层）、
`collaboration-contract.md`（跨端链路）。

---

## 一、目标

- **能接着聊**：重开页面、重启进程后历史仍在。
- **多 agent 不串台**：一个进程装多个 agent，同一个人的会话按 agent 隔离。
- **会话与任务不打架**：任务会话与普通会话同表、用一个 `kind` 区分，不必两套 UI。
- **历史可追溯**：一轮对话由「一条消息 + 若干步骤」构成，逐步落库，可回放。

---

## 二、存储选型

**SQLite + SQLAlchemy 异步**（`store/`）。

理由：本地单用户、单机部署；不需要独立数据库进程；异步接口与 FastAPI 一致。
数据库位置 `keeper/data/keeper.db`。

**不做的**：不做跨设备同步、不做多写副本——数据主权在本地，一台机器一份库。

---

## 三、对象模型

```
chat_sessions ──1:N──> session_messages ──1:N──> react_steps
     │                        │
     │                        └── artifacts (JSON)  ← 本轮产物引用
     │
     ├──1:N──> threads ──1:N──> agent_messages      ← 对端链路
     │
     └── kind: chat | task  → tasks（任务模式，一对一）
```

三张主表 + 两张跨端表：

| 表 | 承载 |
|---|---|
| `chat_sessions` | 对话框 |
| `session_messages` | 入站对话（一轮 = 一条 assistant 消息） |
| `react_steps` | 一轮内部的 ReAct 步骤 |
| `threads` | 本地会话 ↔ 某个对端的那条链 |
| `agent_messages` | 跨端往来的消息 |

---

## 四、表结构

### 4.1 `chat_sessions` — 对话框

| 字段 | 说明 |
|---|---|
| `id` | ULID 风格 26 位字符串 |
| `agent_id` | **所属 agent**。单进程多 agent 下按它隔离会话（没有这列会串台） |
| `initiator_id` | 发起人 id（人或外部 agent），区分谁发起的 |
| `context_id` | 协议层上下文锚（A2A contextId），与内部主键**解耦** |
| `user_space_id` | 绑定的用户空间；为空则回落默认空间 |
| `kind` | `chat`（普通） / `task`（任务绑定会话） |
| `status` / `title` / `summary` | 状态、标题、摘要 |

`context_id` 与主键分离的意义：换协议时只换映射，不动 session 主键。

### 4.2 `session_messages` — 入站对话

一个对话框固定两个角色，故只留 `role`，不设 peer / peer_type：

- 对端由 `chat_session_id` → `chat_sessions.initiator_id` 得到
- 工具结果记在 `react_steps.output`

| 字段 | 说明 |
|---|---|
| `chat_session_id` / `seq` | 会话内递增，唯一约束 `(chat_session_id, seq)` |
| `role` | `user` / `assistant` |
| `content` | 消息正文 |
| `artifacts` | 产物引用 JSON：`[{"id","name","path","mime","size"}]` |
| `duration_ms` | **本轮端到端墙钟耗时** |

`duration_ms` 与 `llm_calls` 的 `SUM(duration_ms)` 是**两个口径**：后者只是「算力花了
多久」，不含工具执行、等待与挂起恢复，**别混用**。

### 4.3 `react_steps` — 一次 ReAct 的步骤

| 字段 | 说明 |
|---|---|
| `session_message_id` / `step` | 轮内序号，唯一约束 |
| `kind` / `input` / `output` | 步骤类型与输入输出 |
| `status` | `running` / 完成 / 失败 / 等待 |
| `wait_kind` / `wait_ref` | **挂起与恢复的唯一事实来源** |
| `duration_ms` | 一步总耗时（思考 + 工具执行） |

两个刻意的设计：

- **等待状态只在 `react_steps`**：`threads` 与 `agent_messages` 都不重复记录，
  避免同一事实两处写。
- **`duration_ms` 单独一列**，不能拿 `updated_at - created_at` 顶替——`updated_at`
  带 `onupdate`，会被后续写操作刷新。

### 4.4 `threads` — 对端链（不设 status）

定位方式 `(chat_session_id, peer)`，唯一约束。**LLM 不需要记 thread_id**。

`thread_id` 由发起方生成，**对端拿它当自己那侧的 session_id**——两边用同一个值标识
同一条链，不需要映射表。

> 不设 `status`：等待状态在 `react_steps`，这里只回答「这条链是谁」。

### 4.5 `agent_messages` — 跨端往来

同一条 `message_id` 在两边各存一份，归属不同：

- 我发出的：我这边 `direction=out`，对端存进它自己的 `session_messages`
- 对端发来的：对端存它自己的出站记录，我这边 `direction=in`

`in_reply_to` 串联多轮：存在指向它的记录 = 已被回复，**故不设 status**。

### 4.6 索引

按实际查询路径建：`chat_sessions.agent_id`、`chat_sessions.context_id`、
`session_messages.chat_session_id`、`react_steps.session_message_id`、
`agent_messages.in_reply_to`。

---

## 五、ID 体系

统一的 26 位字符串主键（`store.new_id()`），不用自增整数：

- 跨库合并不会冲突
- URL 里不暴露规模
- 与对端交换时不需要映射

**两端 id 一致**：平台分配的 agentId 本地原样使用；`thread_id` 两边同值。

---

## 六、跨端链路

### 6.1 同步优先

一次派发默认同步等待结果。理由：异步会让「何时算完成」变得不确定，而对话里
用户期待的是**这次回答**。

### 6.2 超时即失败（不挂起）

超时按失败处理，并在 `react_steps` 记 `wait_kind`，不无限等待。

### 6.3 长作业不在对话里做

长时间运行的工作交给任务模式（`tasks`），对话只负责发起与查看结果。

### 6.4 两侧对称

```
chat  = 请求方 → 我     （chat_sessions + session_messages）
peer  = 我 → 对端       （threads + agent_messages）
a2a   = 对端经 A2A 调我  （a2a_tasks + build_agent_card）
```

---

## 七、挂起与恢复

**挂起只发生在发起方**（见 `collaboration-contract.md`）：

```
react_steps.status = waiting
react_steps.wait_kind = peer_reply | user_input | ...
react_steps.wait_ref  = 对端 thread_id / 消息 id
```

恢复时按 `wait_ref` 找到对应记录，把结果注入该步，继续 ReAct 循环。
**不新建 session、不新建 step**——原地续跑。

---

## 八、展示顺序

前端按 `seq` 排消息、按 `step` 排步骤。产物按所属层展示：

- 本轮产物 → `session_messages.artifacts`
- 任务过程产物 → `task_items.artifacts`
- 任务结果产物 → `tasks.artifacts`

三者**同构**（JSON 数组，`path` 带 scheme），取数统一走 `chat/artifacts.py`。

---

## 九、已确认的设计决策

| 编号 | 决策 | 依据 |
|---|---|---|
| D1 | 一个任务绑定一个 session | 上下文天然连续，不存在跨 run 失忆；故不建 `task_runs` 表 |
| D2 | 执行过程本身就是 session 里的消息 | 天然留痕，不需要额外表 |
| D3 | `task_items` 是计划的事实来源，`plan_md` 只是渲染视图 | 避免 markdown 与结构化状态双写不一致 |
| — | 等待状态只记在 `react_steps` | 避免同一事实两处写 |
| — | `duration_ms` 显式成列 | `updated_at` 带 onupdate，不能拿来算耗时 |
