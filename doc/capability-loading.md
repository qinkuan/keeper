# 能力按需加载（Capability Loading）

> 本文描述**已实现**的能力装载机制。落点：`agent/process.py`（加载器）、
> `skill/base.py`（L1/L2 声明）、`agent/config.py`（分组摘要）、
> `observability/recording.py`（`record_capability_load`）。

---

## 一、要解决的问题

一个 agent 挂一堆插件后，system prompt 会同时塞进：

- 所有技能的完整做法（SKILL.md 正文）
- 所有工具的完整参数说明

结果是 prompt 几万字符，但实际一轮只用到其中一两个。三个后果：

1. **输入 token 线性膨胀**（每轮都付全量）
2. **前缀缓存被打散**（改一个插件就要重建整个前缀）
3. **模型选择困难**（工具太多时容易选错）

---

## 二、L1 / L2 拆分

**插件是唯一的一等公民**——不管是 MCP 组件还是 bin 组件，都按同一套规则拆：

| 层 | 内容 | 何时进上下文 |
|---|---|---|
| **L1** | **一句话摘要**：这个能力是什么、什么时候用 | 常驻（清单里） |
| **L2** | **完整正文**：SKILL.md 全文 / 工具完整参数 | 模型确定要用时才 `load_capabilities` 拉进来 |

L1 的来源：

- 技能：`skills/SKILL.md` 里的 `description`（或 skills 包的 `default_summary`）
- MCP 组件：插件清单里的 `summary` 字段
- 工具分组：`group_summaries`（`agent/config.py` 装配时收集）

### 2.1 常驻范围：`always: true`

```python
# skill/base.py:25
always: bool = False
```

`always: true` 的技能**正文常驻** system prompt，不走按需加载：

```357:358:keeper/agent/process.py
            if s.system_prompt and bool(getattr(s, "always", False))
```

适用：行为约束型技能（比如 `plugin-authoring` 规定「写完代码必须自己跑」），
它每轮都要生效，不适合"用时才加载"。

---

## 三、统一加载接口：`load_capabilities`

```python
# agent/process.py:60
_CAP_LOADER = "load_capabilities"
```

每轮开头 `install_capability_loader()` 装进工具表（**没有技能时就不装**——白给一个
工具只会增加选择负担）。

模型看到「可用能力」清单（L1）后，确定要用某个就调：

```
ACTION load_capabilities {"keys": ["codebase-memory"]}
```

三条硬规则（`_load_capabilities` 实现）：

| 规则 | 行为 | 为什么 |
|---|---|---|
| **部分成功** | 未知 key 单列 `missing`，不整请求失败 | 否则模型得把全部 key 重发一遍 |
| **幂等** | 已加载的只回 `already`，不重复塞 | 避免同一段正文进两次上下文 |
| **模糊建议** | `missing` 带最接近的名字 | 让模型一次改对，而不是反复试 |

### 3.1 每轮重置

`_reset_capabilities()` 在每轮开始清空加载状态——messages 是新的一批，
旧的加载记录不能留。

---

## 四、索引键：前缀即分组

工具与技能都按 `{插件名}__{组件名}` 命名（`_MCP_SEP = "__"`），
前缀天然构成分组。清单渲染时：

- 同一插件的多个工具折叠成一行分组摘要
- 组内存活工具数达到阈值才展开明细（`_tool_group_min()`）

这样「一个插件 15 个 MCP 工具」在清单里只占一行。

---

## 五、注入位置差别

| 内容 | 位置 |
|---|---|
| `always: true` 的技能正文 | **system prompt** |
| L1 清单（可用能力概要） | system prompt |
| `load_capabilities` 拉回的 L2 正文 | **messages**（后续消息） |

L2 进 messages 而不是 system：system 是每轮固定的前缀，往里塞会**打散前缀缓存**；
messages 侧追加只影响尾部。

---

## 六、可观测

每次加载记一条 `capability_loads`（`record_capability_load`）：
哪个能力、加载了多久、L2 正文多大。

聚合看 `capability_stats`——用来判断「哪些能力总是被加载但 rarely 用到」
（说明 L1 摘要写得不够清楚，模型不敢直接用）。

---

## 七、插件清单字段规范

插件作者需要提供的（缺了就退化成整包常驻）：

```json
{
  "skill": "skills/SKILL.md",
  "skills": {
    "my-skill": {
      "description": "一句话：什么时候用（L1）",
      "always": false
    }
  },
  "mcpServers": {
    "server": {
      "summary": "一句话：这个 server 干嘛的（L1）",
      "command": "${PLUGIN_ROOT}/mcp/xxx"
    }
  },
  "bin": [
    {
      "summary": "一句话：这个可执行文件干嘛的（L1）",
      "command": "${PLUGIN_ROOT}/bin/xxx",
      "tools": [{"name": "run", "description": "..."}]
    }
  ]
}
```

---

## 八、已知限制

- **L1 摘要质量决定效果**：摘要写得含糊，模型就不敢直接用、每轮都去 `load_capabilities`，
  反而更贵。
- **工具级 L2 只到「分组」粒度**：折叠是按插件分的，不能只加载某个插件里的某一个工具。
- **无自动淘汰**：加载过的 L2 在整轮内一直留在 messages（靠上下文压缩回收，
  见 `context.md`）。
