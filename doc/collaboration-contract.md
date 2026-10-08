# keeper 协作契约（跨端）

> 本文描述**已实现**的跨端协作机制。落点：`peer/`、`a2a/`、`store/models.py`
> 的 `threads` / `agent_messages` / `a2a_tasks` / `a2a_outbound_tasks`。

配套：`session-design.md`（本地存储）、`positioning.md`（为什么用 A2A）。

---

## 一、核心洞察：挂起只发生在发起方

跨端协作最容易做错的地方，是把「等待」记在两边。

**事实**：等待只发生在**发起方**。对端收到消息、处理、返回，它自己不等待——
它是被调用的那一方。

所以：

```
等待状态 → 只记在 react_steps（发起方那一侧）
threads    → 只回答「这条链是谁」，不设 status
agent_messages → 用 in_reply_to 串联，不设 status
```

**避免同一事实两处写**——这是这张表设计里最关键的一条。

---

## 二、信封：四个 id

| id | 含义 | 谁生成 |
|---|---|---|
| `chat_session_id` | 我这侧的会话 | 我 |
| `thread_id` | 本地会话 ↔ 某个对端的那条链 | **发起方生成，对端拿它当自己那侧的 session_id** |
| `message_id` | 一条跨端消息 | 发送方 |
| `in_reply_to` | 回复的是哪条 | 回复方 |

`thread_id` 两边**同值**，所以不需要映射表。

定位方式 `(chat_session_id, peer)`，唯一约束。**LLM 不需要记 thread_id**。

---

## 三、两侧对称的三条链路

```
chat  = 请求方 → 我      （chat_sessions + session_messages）
peer  = 我 → 对端        （threads + agent_messages）
a2a   = 对端经 A2A 调我   （a2a_tasks + build_agent_card）
```

### 3.1 `peer/` — 我调对端

`PeerService` 封装一次「我问它答」：

```python
peer = PeerService(chat_session_id, "product")
tid = await peer.ensure_thread()
out_id = await peer.append(tid, "库存变更影响哪些订单？", "out",
                           in_reply_to=await peer.last_reply_to(tid))
# ... 调对端的 send ...
await peer.append(tid, answer, "in", message_id=对端返回 id, in_reply_to=out_id)
```

### 3.2 `a2a/` — 协议层

| 模块 | 职责 |
|---|---|
| `client.py` | `A2AClient`（本端去调对端） |
| `server/` | A2A 服务端（对端经 A2A 调本端）：`build_a2a_app` / `build_a2a_router` / `build_agent_card` |

对外协议面：

- `POST /a2a`：JSON-RPC（`SendMessage` / `GetTask` / `CancelTask`）
- `GET /.well-known/agent.json`：暴露本 agent 的 AgentCard

---

## 四、能力等级：对端门槛越低越好

| 层 | 接入方式 | 对端改造量 |
|---|---|---|
| **层 1** | HTTP 直连 | 零 |
| **层 2** | MCP 访问 | 零或接近零 |
| **层 3** | 框架一致（keeper ↔ keeper） | 需要实现必备 MCP 工具 |

**层 1 / 层 2 不需要额外工具**：用普通 HTTP 或 MCP 工具即可，只有层 3 需要
参与者实现约定的动词。

### 4.1 层 3 必备工具

（见代码中 peer 工具的注册，命名 `{peer}__{skill_id}`）

- 发送并等待结果
- 查询状态

### 4.2 层 3 可选

变更通知、自检等场景的工具。

### 4.3 幂等约定

同一 `message_id` 重复提交**不产生副作用**——网络重试时不会重复执行。

---

## 五、对端能力动态发现（discovery）

本端**不写死**对端工具清单：

1. 从 `config.peers` 取对端 `a2a_url`
2. 拉它的 AgentCard
3. 按 `skills[]` 当场生成 `{peer}__{skill_id}` 工具
4. description 直接取自对端自己声明的 `skills[].description`

对端新增能力 **只需在自己 AgentCard 多声明一个 skill**，本端自动可见——
**不再经 MCP 拉工具清单**。

意义：对端只要说 A2A 就能协作，不要求它是 keeper 造的、也不要求进任何框架。

---

## 六、超时与熔断（A2A 出站）

对端是**别人的进程**，快慢与可用性都不由我们控制。所以 `A2ASection` 提供：

| 参数 | 作用 |
|---|---|
| `request_timeout` | 单次 HTTP/RPC 超时（发消息、查状态、拉 AgentCard 都用它） |
| `task_timeout` | 等一个 task 走到终态的**总时长**；0 = 不限（不推荐） |
| `poll_interval` | 轮询间隔（等终态时查 `GetTask` 的频率） |
| `cancel_on_timeout` | 超时后主动 `CancelTask`，**别让对端继续白跑** |
| `breaker_*` | 连续失败达到阈值就短期熔断，冷却期内快速失败 |

两条依据是实测得来的：

- 没有超时 → 一次调用能把整轮对话挂死（遇到过「请求一直不返回」）
- 没有熔断 → 对端挂了之后每轮还老老实实去拨，白白拖慢每一轮

---

## 七、挂起与恢复（编排侧）

```
react_steps.status    = waiting
react_steps.wait_kind = peer_reply | user_input | ...
react_steps.wait_ref  = 对端 thread_id / 消息 id
```

恢复：按 `wait_ref` 找到对应记录，把结果注入该步，继续 ReAct 循环。
**不新建 session、不新建 step**——原地续跑。

重连（对端中途变动）：按 `thread_id` 重新定位链路；对端若已丢失上下文，
按层 1 降级处理。

---

## 八、降级矩阵

| 情况 | 行为 |
|---|---|
| 对端只支持 HTTP | 降级到层 1，用普通 HTTP 调用 |
| 对端提供 MCP | 降级到层 2，走 MCP 工具 |
| 对端 framework 一致 | 层 3，完整 Task 生命周期 |
| 对端超时 | 按失败处理并记 `wait_kind`，**不无限等待** |
| 对端连续失败 | 熔断，冷却期内快速失败 |
| 长作业 | 交任务模式，不在对话里等 |

---

## 九、与现有实现的关系

| 概念 | 表 | 模块 |
|---|---|---|
| 对端链 | `threads` | `peer/service.py` |
| 跨端往来 | `agent_messages` | `peer/service.py` |
| 入站 A2A 任务 | `a2a_tasks` | `a2a/server/` |
| 出站 A2A 任务 | `a2a_outbound_tasks` | `a2a/client.py` |
| 对端配置 | `config.peers` / `agent_peers` | `config.py` |

---

## 十、已知限制

- **层 3 的必备工具尚未形成书面清单**：当前实现在代码里，契约文档化待补。
- **熔断状态不持久化**：进程重启后熔断计数清零。
- **对端 AgentCard 变更不会主动推送**：靠下次调用时重新拉取发现。
