# 模型

> 本文讲 `llm/`：模型预设怎么存、怎么绑给 agent、怎么计价、
> 以及调用链上已知的技术债。落点：`llm/api.py`、`llm/config.py`、`llm/client.py`。

---

## 一、核心语义：没有兜底

keeper 的模型语义只有一条：

> **模型预设存在本地库，agent 通过绑定表显式引用，没有兜底。**

| 概念 | 落点 | 说明 |
|---|---|---|
| 预设 | `llm_profiles` 表 | provider、模型名、API Key、Base URL、温度、超时、重试、上下文上限 |
| 绑定 | `agent_llm_binding` 表 | agent → 预设，**显式**一条 |
| 未绑定 | — | **纯索引模式**：只用知识库和工具，**不调用模型**；启动日志会明确告警 |

历史上这里有过三层兜底（inline 配置被同步灌全局 `llm.yaml`、全局 `llm.yaml`、
建 agent 时自动绑第一个预设），每一层都制造过「改了配置却不生效、又查不出原因」的问题。
现在语义唯一：**先配好模型，再显式绑定**。

### 改配置会立刻生效

LLM 实例是在**装配期**固化的，所以改预设 / 删预设都会触发绑着它的 agent
**自动重装配**——不需要重启服务（重启会中断正在跑的会话）。

界面上：模型管理页保存后 toast 会列出「已重载 N 个 agent」以及哪些重载失败；
对话输入框的模型选择器改的是 **agent 级绑定**，同样会自动重装配。

---

## 二、provider

`llm/config.py` 里注册了 15 个 provider：

| provider | 说明 | 默认模型 |
|---|---|---|
| `openai` | OpenAI 官方 | `gpt-4o-mini` |
| `deepseek` | DeepSeek 官方（OpenAI 兼容） | `deepseek-chat` |
| `qwen` / `dashscope` | 阿里通义千问 | — |
| `moonshot` | Moonshot（Kimi） | — |
| `zhipu` | 智谱 GLM | — |
| `anthropic` | Anthropic 官方 | `claude-3-5-sonnet-latest` |
| `google` | Google | — |
| `ollama` | 本地 Ollama / vLLM | `qwen2.5` |
| `baichuan` / `yi` / `minimax` / `stepfun` | 各自官方 | — |
| `localai` / `custom` | 任意 OpenAI 兼容端点 | — |

界面上选 provider 会带出该 provider 的常用模型建议（datalist）。

### API Key 是只写字段

接口**永不回显明文**，只回 `has_api_key: true/false`；界面上显示「已配置 / 未配置」。
编辑时留空表示「不改」，要换 Key 就填新值。

---

## 三、计价

一个模型可以配**多段价**（错峰计费）：

| 字段 | 说明 |
|---|---|
| 生效自 | 起始日期 |
| 适用星期 / 时段 | 不勾 = 每天；时段可跨天 |
| 输入 / 缓存命中 / 输出 | 每百万 token 单价 |
| 币种 | CNY / USD |

没配价时成本显示「—」。计价落点 `observability/`（成本按 `llm_calls` 的
usage + 命中的价段计算）。

---

## 四、调用链上的已知技术债

| 项 | 现状 | 该怎么做 |
|---|---|---|
| **LLM 永久错误仍重试** | 402（余额不足）/ 401（鉴权失败）属于**永久**错误，当前仍走「流式 → 一次性」回退，白等一轮 | 分类错误码：永久错误直接抛给用户，不重试 |
| **连接池未归还** | GC 日志有 `non-checked-in connection` 警告 | SQLite 换 `NullPool` + 取消路径补 rollback |
| **上下文上限未强制** | 模型上下文上限存在预设里（用于水位告警），但**没有截断保护** | 超限时提前提示，而不是让请求失败 |

---

## 五、待办清单

| 项 | 状态 | 说明 |
|---|---|---|
| LLM 永久错误不重试 | ❌ 未做 | 见第四节 |
| 连接池 / rollback | ❌ 未做 | 见第四节 |
| 模型预设的导入导出 | ❌ 未做 | 模型配置是用户资产，应该能带走 |
| 按 provider 分组的批量测试 | ❌ 未做 | 现在只能逐个点「测试」 |
