# 工具与安全守卫

> 本文讲 `tools/`：工具怎么注册、怎么被 agent 调用、出错了怎么办、
> 以及**安全边界在哪**。落点：`tools/base.py`、`tools/builtin/`、
> `tools/subprocess_tool.py`、`tools/guard.py`。

---

## 一、工具是怎么来的

工具只有两个来源，**都是本地的**：

| 来源 | 形态 | 落点 |
|---|---|---|
| **内置** | Python 工厂 | `tools/builtin/` |
| **插件** | MCP 组件 / bin 可执行文件 | `tools/subprocess_tool.py` |

内置工具只有两个：

| 工具 | 作用 |
|---|---|
| `fs.deliver` | 交付产物（带类型的块：file / chart / mermaid / table / map / audio） |
| `task.mark_item_done` | 标记任务计划点完成 |

**读文件、写文件、列目录、搜索、跑命令一律走 `bash` 插件**——
不设内置的文件工具。理由：内置工具每加一种文件格式就要写一套解析与预览，
而插件（`bash` + `excel` + `pdf`）能覆盖得更宽。

### 记忆与知识库工具（随内置注册）

`tools/collect.py` 除内置工具外，还注册两组**内核自带**的工具：

| 工具 | 作用 | 落点 |
|---|---|---|
| `recall` / `read` | agentic retrieval：召回本会话的记忆块 / 惰性展开原文 | `memory/tools.py` |
| `kb.search` | 在「我的空间」里检索，召回并定位（Markdown 给行号、PDF 给页码） | `knowledge/tools.py` |
| `kb.read_pdf` | 按路径 + 页范围读 PDF，返回那几页的**截图** | `knowledge/tools.py` |
| `kb.read_markdown` | 按路径 + 起始行读 Markdown 原文 | `knowledge/tools.py` |

`kb.*` 的设计见 [`kb-pdf-pages.md`](kb-pdf-pages.md)：**召回只定位，阅读才给内容**。
它也是目前唯一能返回**多模态块**的工具——`tools/base.py` 的 `ToolResult` 让工具结果
带 `text` + `blocks`，图片走厂商 Files API 进上下文（不进文本预算、不被外部化）。

---

## 二、注册表与容错

```python
# tools/base.py
ProcessorTool + ToolRegistry
```

**工具出错不会让 agent 崩溃**——异常被兜成错误文本回给模型，让它自己决定重试还是换路。
这是刻意的：一个工具失败不该终止整轮对话（模型看到「命令找不到」可以自己改）。

工具在**装配期**固化进实例（见 [`capability-loading.md`](capability-loading.md)）：
改能力要重建实例（界面会自动提示「已重建实例」）。

---

## 三、子进程工具通道（bin 组件）

插件里的可执行文件走 `tools/subprocess_tool.py`：

- **子进程**而不是 import：第三方代码进主进程等于把 keeper 的全部权限交给它
- 协议：stdin / stdout 传 JSON，语言无关
- 插件清单里的 `bin[].tools[]` 声明有哪些工具、参数是什么

MCP 组件则是**常驻子进程**（有跨调用状态，比如索引缓存），落点 `mcp/`。
MCP 不可用时进入降级模式并后台重试，不阻塞启动。

---

## 四、安全守卫（`tools/guard.py`）

### 4.1 路径越界

可写工作区下，工具访问路径必须落在工作区根之下，否则拦截。

> 早期只读判断有两个来源打架（agent 画像上的 `workspace_read_only` vs
> `user_space.read_only`），后果是「工具在界面上凭空消失且无任何报错」。
> 现在工作区是**会话级属性**（`~/.keeper/workspace/session/<sid>/`），
> 不存在 agent 画像上的只读开关；「我的空间」是用户指定的**合法根**，与工作区不是一回事。

### 4.2 读命令白名单

只读工作区下，按命令白名单放行。

### 4.3 危险工具二次确认 —— ❌ **未实现**

**这是当前最大的安全缺口。**

现状：

- `bash` 执行前**没有任何确认**。模型判断错了、或被诱导执行了危险命令，
  用户在界面上来不及拦
- `observability/dump.py` 会把完整 prompt 落盘，**含用户代码与可能的密钥**，没有脱敏

要做什么：

| 项 | 设计要点 |
|---|---|
| **危险命令识别** | 在 `tools/guard.py` 里加一层：命令匹配危险模式（`rm -rf`、`sudo`、`chmod 777`、写系统目录、删库、网络外传…）时**不直接执行** |
| **确认流程** | 工具调用挂起 → 界面弹确认框（显示完整命令 + 所在目录）→ 用户批准/拒绝 → 恢复执行。这是**挂起-恢复**机制，`react_steps` 已经有 `waiting` 状态可用 |
| **粒度** | 默认「每次都问」；可按会话记住「本会话都允许这条命令」 |
| **拒绝的反馈** | 拒绝要作为工具结果回给模型（"用户拒绝了这条命令"），让它换路，而不是静默失败 |
| **prompt dump 脱敏** | 落盘前过一遍正则：API Key / token / 私钥 / 密码字段替换成 `***`；提供开关与白名单 |

风险评估：本机单人使用问题不大；**一旦部署到服务器或多人共用，这两项是硬缺口**。

---

## 五、待办清单

| 项 | 状态 | 说明 |
|---|---|---|
| 危险工具二次确认 | ❌ 未做 | 见 4.3 |
| prompt dump 脱敏 | ❌ 未做 | 见 4.3；另见 [`security.md`](security.md) |
| 工具可用性对用户可见 | ⚠️ 部分 | 装配期跳过的工具，界面仍显示「已装配」——用户看不到「这个工具其实没装上」 |
| 插件工具的 schema 校验 | ⚠️ 部分 | bin 组件的参数声明来自清单，缺字段时靠运行时兜底 |
