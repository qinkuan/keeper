# 可观测性设计（Observability）

> 本文描述**已实现**的可观测体系。落点：`keeper/observability/` 包。

模块地图见 `architecture.md`。

---

## 一、要回答的问题

- 这次对话花了多少 token、多少钱？
- 慢在哪一步？哪个工具最耗时？
- 有没有重复调用（同一件事问了两遍）？
- 有没有超出预算？

---

## 二、核心决策：落库粒度是「一次 LLM 调用一行」

**事实表只有一张：`llm_calls`**。step / 消息 / 任务 / agent 各级用量全部由它
**聚合**得出，**不在各表冗余 token 列**。

为什么不用「累计值做差」：`LLM` 是 agent 级单例，`usage` 跨会话累加——并发下会把
别的会话的消耗算进来，且无法归属到具体 step / 消息。

配套三张辅助表：

| 表 | 内容 |
|---|---|
| `llm_calls` | **事实表**：每次 LLM 调用一行（tokens、耗时、缓存命中、归属） |
| `tool_calls` | 工具调用（耗时、成功 / 失败） |
| `capability_loads` | 能力加载（哪个插件 / MCP 加载了多久） |
| `react_parse_stats` | ReAct 文本协议解析统计 |

辅助表是**补充维度**，不是用量来源——用量永远从 `llm_calls` 聚合。

### 2.1 归属怎么拿到

从 `chat/context.py` 的 **ContextVar** 读（trace / agent / session / step），
**不用参数层层透传**——调用链很深（process → llm），透参会污染一堆签名。

### 2.2 任何统计失败都不影响主流程

所有写库一律 `try/except` 只记 debug。可观测是旁挂能力，**不能让统计出错拖垮对话**。

---

## 三、模块划分

原来是一个 1856 行的单体 `observability.py`，已按职责拆包：

| 模块 | 职责 |
|---|---|
| `recording.py` | 记账：`record_llm_call` / `record_tool_call` / `record_capability_load` / `record_react_parse_stat` / `backfill_step_id` |
| `timeline.py` | 时间线：`message_timeline` / `step_metrics_for_message` / `set_message_duration` |
| `stats.py` | 统计：`usage_by_messages` / `usage_timeseries` / `slowest_calls` / `error_breakdown` / `ask_rate` / `duplicate_calls` / `tool_stats` / `react_parse_stats` / `capability_stats` |
| `pricing.py` | 定价：`_price_for` / `_context_limit_for` / `_cost_of`（支持时段 / 工作日折扣） |
| `budget.py` | 额度与聚合：`budget_limits` / `check_budget` / `aggregate` |
| `dump.py` | 完整 prompt 落盘（调试用，需在设置里开 `prompt_dump`） |
| `_context.py` | 归属上下文：`_ctx` / `_resolve_round_anchor` |

依赖方向单向：`recording`/`timeline`/`dump` → `_context`；`stats` → `pricing`/`_context`；
`budget` → `pricing`。反向没有引用，故无循环导入。

---

## 四、采集点

| 位置 | 记什么 |
|---|---|
| `llm/client.py` | **最关键**：每次调用的 input / output / 缓存 tokens 与耗时 |
| `agent/process.py` | ReAct 每步的工具调用、解析统计 |
| `chat/service.py` | 消息级耗时（`set_message_duration`）、产物登记 |
| `chat/context.py` | 归属上下文（ContextVar） |
| 任务侧 | `task_id` 归属，可单独聚合任务维度用量 |

### 4.1 DeepSeek 缓存字段

`llm_calls` 单独存缓存相关字段（命中 / 未命中的 input tokens）。DeepSeek 的
上下文硬盘缓存会显著改变成本结构，**不拆出来算不清真实花费**。

### 4.2 两个耗时口径别混用

- `session_messages.duration_ms`：**一轮端到端墙钟耗时**（含工具、等待、挂起恢复）
- `SUM(llm_calls.duration_ms)`：只是**算力花了多久**

---

## 五、成本换算

价格挂在 `llm_profiles`（不单独建 `model_prices` 表也能用，但已支持按
profile / model / provider 三级匹配）：

```
_price_for(profile, model, provider) → 单价（支持时段 / 工作日折扣）
_context_limit_for(model)            → 上下文上限（用于「接近上限」提示）
_cost_of(...)                        → 算出一次调用的花费
```

匹配顺序从具体到宽泛，找不到就按 provider 默认价，**绝不因为算不出价钱而报错**。

---

## 六、聚合口径

| 指标 | 说明 |
|---|---|
| `usage_by_messages` | 按消息聚合用量与成本 |
| `usage_timeseries` | 按时间桶（天 / 小时）看趋势 |
| `slowest_calls` | 最慢的调用（定位性能问题） |
| `error_breakdown` | 错误归类 |
| `ask_rate` | 提问率（多少轮在追问，反映「一次没答对」） |
| `duplicate_calls` | **重复调用**（同一件事问了两遍 = 浪费） |
| `tool_stats` | 工具维度：调用次数、耗时、失败率 |
| `react_parse_stats` | 解析失败统计（模型输出格式不对的频率） |
| `capability_stats` | 能力加载耗时 |

`duplicate_calls` 是这套体系里最有诊断价值的指标之一——它直接暴露「模型在做无用功」。

---

## 七、API 与前端

| 端点 | 内容 |
|---|---|
| `GET /metrics/*` | 概览、用时序列、最慢调用、错误归类、提问率、重复调用、工具统计、时间线 |

前端「可观测」页按这些维度展示，支持按 agent / session / 任务 过滤。

---

## 八、调试辅助：prompt 落盘

`dump.py` 提供完整 prompt 落盘（开关 `config.observability.prompt_dump`，默认关）：

```
~/.keeper/prompt-dump/<sessionId>/<messageId>/
```

用途：追查「模型到底看到了什么」。默认关是因为**体积大且含敏感内容**。

---

## 九、风险与注意

- **统计失败不影响主流程**：已用 try/except 兜住，但因此也可能**静默丢数据**——
  日志里搜 debug 级记录才能发现。
- **价格靠人工维护**：`llm_profiles` / `model_prices` 里的价格不会自动更新，
  价格变了成本就算错。
- **聚合是实时的**：数据量大时 `/metrics` 查询会变慢，当前没有预聚合表。
- **私有部署的缓存折扣**：`pricing.py` 支持时段 / 工作日折扣，但规则需要按实际
  服务商填。
