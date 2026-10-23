# keeper

> **本地知识库 agent**：把「我的空间」变成 agent 的知识库——读你的文件、
> 找到它、改它、把结果交付给你，并把过程记录下来
> （用量、耗时、产物、评测）。

keeper 是一个**跑在自己机器上**的 agent 内核 + 桌面应用，围绕**本地知识库**构建：

- **知识来源**：「我的空间」——你自己的文档、代码、表格、PDF、图片；
  加上**关于你的记忆**。
- **能力闭环**：**读**（多格式预览）→ **找**（浏览 + 语义检索）→ **改**（编辑并保存）
  → **交**（带类型的交付块）。
- **组成**：
  - **内核（Python）**：会话、ReAct 执行、工具与插件、MCP、记忆、可观测、评测。
  - **桌面端（Electron）**：只做窗口与进程托管，业务逻辑一个都不碰；
    「我的空间」的文件操作走桌面原生能力（Node `fs` + 系统对话框 + 系统剪贴板）。
  - **前端（React + Vite）**：由内核同源托管——**一个进程 = API + 页面**。

数据全部留在本地：文件是你的文件，记忆是你的记忆，会话与可观测数据也只在本机。

> **「找」这一层正在补**：现在是目录树浏览 + agent 的 `grep` / `find`；
> **向量化 + 跨模态的语义检索**与**长期事实库**在建，见 [Roadmap](#roadmap)。

完整设计见 [`doc/architecture.md`](doc/architecture.md)。

---

## 在线体验

🌐 **http://122.51.49.109/**

一个跑在云端的演示实例（网页形态）。可以试：配一个模型、把文件放进「我的空间」、
让 agent 读它并交付结果、看可观测数据。

> ⚠️ 演示实例**无入站认证**（keeper 现在是本机单用户形态，内核只绑回环）。
> 公网开放意味着任何人都能驱动这个实例上的 agent——**别在上面放真实凭据或敏感项目**，
> 数据也可能随部署重置。想认真用请在本地跑桌面版。

---

## 功能一览

界面分三个主区（**对话** / **智能体** / **我的空间**），加侧栏底部的配置与运维入口
（**设置** / **可观测** / **回收站** / **模型管理**）。

其中「**我的空间**」是知识库的物理形态（你的文件），「**对话**」是 agent 用它工作的场所，
「**智能体**」决定它有哪些能力与用哪个模型。

> 代码侧的模块地图见 [`doc/architecture.md`](doc/architecture.md) 的「模块逐个说」。

---

### 💬 对话

左侧会话抽屉 + 中间消息流 + 底部输入框。右侧在「我的空间」页还有一个常驻对话面板。

#### 消息流里能看到什么

| 元素 | 说明 |
|---|---|
| **自主规划过程** | 折叠块「Agent 自主规划过程（N 步）」，逐步显示「思考 / 调用 / 结果」，每步带用量标签；调用与结果可二级展开看完整 JSON |
| **流式进度** | 「正在思考…」「已执行 N 步，正在整理回答…」 |
| **状态标记** | 「AI 生成」「已停止生成」「暂无回答」、错误红框 |
| **附件缩略图** | 64×64，hover 放大，点击新标签页预览 |
| **消息动作条** | hover 助手消息浮现「复制回答」「重新生成」 |
| **用量标签** | 输入/输出 token、缓存命中率、LLM 耗时、成本、上下文水位、失败次数 → 点开「用量详情」 |
| **产物卡片** | 图表 / 流程图 / 表格就地渲染；`[[art:ref]]` 引用处内联渲染，未被引用的沉到消息底部 |
| **模型追问区** | 「轮到我回答」：候选选项 + 输入框，答完接上刚才的上下文（用下方输入框则是开启新话题） |
| **断线恢复** | 刷新后仍能看到「进行中」气泡，必要时自动重连回放该轮 |

#### 输入框

| 控件 | 说明 |
|---|---|
| **模式** | 普通（完整 ReAct + 记忆 + 工具）/ 快捷（一次性任务）/ 程序化（写代码执行）；会话创建后即固定 |
| **思考档位** | 关闭 / 低 / 高 / 最高 |
| **模型** | 显示当前绑定（失效显示「模型已失效」）；改的是 **agent 级绑定**，可选「未绑定（纯索引模式）」，切换后自动重装配 |
| **`+` 附件** | 选图片（png/jpg/gif/webp）；也可直接把文件**拖进面板**或**粘贴截图** |
| **`@` 上下文** | 唤出快捷项（默认「我的空间」「当前会话目录」，可在设置里增删改）；**发送时**才展开成绝对路径 |
| **`/` 插件** | 唤出**未加载插件**，选中即激活到本会话（仅本对话有效），变成可 ✕ 停用的 chip |
| **停止 / 发送** | 流式中右侧变「停止生成」；Enter 发送、Shift+Enter 换行 |
| **高度把手** | 拖拽调整输入框高度（64–320px） |

`@` 与 `/` 的区别：`@` 留在正文里参与推理；`/` 会把输入片段摘掉、变成 chip。

#### 会话管理

- 抽屉里「会话 / 任务」分段切换；搜索按标题过滤；「新对话」/「新建任务」（标题必填）
- hover 行露出**重命名**与**删除**；删除是**软删除** → 回收站可恢复
- 记住上次会话（按 agent + 类别分桶），刷新自动回到原处
- 任务状态标签：草稿 / 规划中 / 待审计划 / 执行中 / 等待输入 / 待审结果 / 已完成 / 失败

#### 任务视图：计划 → 审批 → 执行 → 验收

| 阶段 | 能做什么 |
|---|---|
| 产出计划 | 草稿态点「启动并产出计划」 |
| 审批 | 「通过计划」（可附意见）/「打回修改」（必填反馈）/「重新规划」（升版本 vN） |
| 执行 | 「执行」连续推进 /「暂停」跑完当前步 /「停止生成」立刻断流；执行中也能重新规划 |
| 计划点 | 绿勾=已完成、灰叉+删除线=已作废、空心圈=待做；可「标记完成」「跳过」，显示 agent 写的结论；能手动补计划点 |
| 验收 | 「验收通过」（可附备注）/「验收打回」（必填反馈，agent 在原会话继续改） |
| 空转护栏 | 某点连续多轮未登记完成 → 弹窗「再试一轮 / 标记为已完成 / 跳过这一步」 |
| 工具统计 | 任务标题行点开：每工具的调用数、失败数、耗时、返回体大小、被截断次数，每列可排序 |
| 最终交付 | 只列任务级登记过的产物，过程性临时文件仍留在对话里 |

#### 产物与交付块

| kind | 渲染方式 |
|---|---|
| `chart` | vega-lite / echarts 渲染，容器可缩放平移；失败可下载 spec |
| `mermaid` | 流程图，支持 `[seq]` 占位自动替换成步骤序号 |
| `table` | CSV / JSON 解析成可排序表格，数字列按数值排 |
| `file` | 文件卡片：Markdown 可切预览/源码，代码有 diff 视图，html / pdf / 视频走新窗口，图片内联缩放平移 |
| `map` / `audio` | 提示「下载后查看」 |

Markdown 正文里的 ` ```mermaid ` / ` ```vega-lite ` / ` ```echarts ` 代码块也直接渲染成图。

#### 调试

- **「查看 dump」** 仅在设置里开了 prompt 落盘时出现：按调用次数分页签（第 N 步 / 最终总结 / 记忆摘要），可展开 system 提示词、完整 messages、模型响应、工具入参
- **用量详情弹窗**：逐步时间线（耗时、LLM/工具拆分、token、工具返回大小）+ 耗时与 token 分解比例条；异常高亮（空转步 / 重取同一块 / 上下文压缩）；重复调用与空转率；子 agent 调用汇总

---

### 🤖 智能体

两个 tab：**我的智能体** / **我的插件**。

#### 我的智能体

| 列 | 说明 |
|---|---|
| 名称 / 说明 | 缺失的插件标红「· 缺失」 |
| 插件 | 以标签展示已绑插件 |
| 启用 | 开关 + 状态（运行中 / 已启用 / 已停用）；缺省开，停用不删任何东西 |
| 操作 | 「管理插件」「删除」（二次确认） |

- **管理插件**：勾选库内插件，保存即建/删符号链接；运行中的 agent 会重建实例
- 页头：**刷新**、**创建 Agent**

#### 创建 Agent

字段：名称（本机唯一，必填）、说明、状态、画像 persona（每次对话都带上）、工作区（用户空间下拉）、能力绑定（插件勾选，显示组件类型与环境要求）。创建页**只做创建**，其余在配置抽屉改。

#### Agent 配置抽屉（对话页顶栏进入，5 个 tab）

| Tab | 能做什么 |
|---|---|
| **概览** | 当前 provider / 模型 / 是否已配 Key；「绑定模型预设」下拉；未绑定时提示「不会调用模型（纯索引模式）」 |
| **内置工具** | 逐项开关，显示状态（已装配 / 已关闭 / 只读跳过 / 依赖缺失 / 未绑定 / 已停用），可展开看该插件挂的 MCP 工具清单 |
| **插件** | 本机插件库全部插件，开关直接绑定 / 解绑 |
| **A2A 对端** | 新增（进程内 agent 下拉选 / 外部手填地址）、代理或直连、移除 |
| **生效工具** | 只读展示最终生效的工具清单 |

#### 我的插件（插件库，只读）

- 列：名称（非 keeper 格式带来源徽标）、版本、能力类型标签、环境要求（最低/最高版本）、体积、说明、目录名
- 行展开：插件实际路径（可复制）或清单错误信息
- 库根路径一键复制 —— **加插件就是往这个目录里放文件**（`keeper-plugin.json` 清单 / plugin-authoring 导出 / Claude 布局包），放进去即生效，刷新可见

---

### 📁 我的空间

**知识库的物理形态**——你的文件在这里。左侧目录树 + 右预览两栏，
页面右侧常驻一个可拖宽、可收起的对话面板。

- **未配置态**：引导卡片 +「选择文件夹」（原生目录对话框，**仅桌面端**）
- **树操作**：展开/收起（子目录懒加载，外部新拷入的文件立即出现）、新建子文件夹、复制（同名自动改名）、在文件管理器中打开、重命名、删除（二次确认，递归）、右键菜单（打开/复制/重命名/删除/新建/粘贴）、`Ctrl/Cmd+V` 从系统剪贴板粘贴导入、工具栏「上传」多选拷入、刷新
- 树宽度可拖拽（180–640px），整栏可折叠成细条

#### 文件预览支持哪些类型

| 类型 | 交互 |
|---|---|
| **Markdown** | GFM 渲染（表格、代码高亮），可切「编辑 / 预览」并**保存回原文件**；可框选文字「添加」进对话 |
| **纯文本 / 日志 / 配置** | Monaco 高亮，可编辑保存 |
| **代码** | 按语言映射高亮，可编辑保存 |
| **JSON / YAML / XML / TOML / CSV / TSV** | Monaco 高亮，可编辑保存 |
| **Excel / 表格**（xlsx / xls / csv / tsv） | SheetJS 表格预览 —— 详见下表 |
| **PDF** | pdf.js 渲染到 canvas，纵向滚动翻页，最多预览前 200 页并提示下载 |
| **图片**（png/jpg/gif/webp/svg） | 内联预览，滚轮/捏合缩放、拖拽平移、双击放大/复位 |
| **HTML** | 不内嵌，给「在浏览器中打开」（可缩放/全屏/另存） |
| **视频** | 浏览器原生播放器 |
| **音频 / 其它二进制 / 超 2MB** | 降级为信息卡，提示下载后查看 |

#### Excel / 表格预览细节

| 能力 | 说明 |
|---|---|
| 多 sheet 切换 | 顶部 tab，>1 个 sheet 才显示 |
| sheet 重命名 | 双击 tab 改名，重名报错 |
| 单元格编辑 | 双击进入输入，Enter 提交 / Esc 取消 / 失焦提交 |
| 公式与格式 | 只改目标单元格，其余公式、格式、列宽原样保留；改到数据区外自动扩 range |
| 保存回原文件 | csv/tsv 走文本写，xlsx/xls 走二进制写；600ms 防抖合并，切文件/卸载前强制落盘；底部显示「保存中… / 已保存 + 时间 / 失败可重试」 |
| 网格体验 | Excel 式行号列 + A/B/C 列头（sticky）、单击选中、拖拽框选、Esc 清除选区 |
| 空白延伸 | 数据区外多渲染 5 行 5 列空白格，可双击直接填新数据 |
| 预览上限 | 最多前 500 行 / 50 列，超出提示下载或用 AI 编辑 |
| 编码兼容 | CSV/TSV 先按 UTF-8 解码，出现替换符再按 GBK 兜底（避免中文乱码） |

#### 从预览到对话

- **文本**：Markdown / 文本预览里选中文字 → 浮出「添加」→ 以 chip（`文件名 · 引用 N 字`）挂进输入框
- **表格**：拖选单元格区域 → 选区右下角出现「添加到对话」，区域用 Excel 语法（`A1:C5`，多 sheet 时带 `Sheet2!A1:C5`）
- 发送时拼成「绝对路径 + 内容」

---

### ⚙️ 设置

6 个分段，一次只显示一段；底部「保存」**立即生效、无需重启**，「重新加载」可放弃改动。

| 分段 | 能配什么 |
|---|---|
| **核心 agent** | 画像（名字 + 一句话说明，写 `agent.yaml`）、灵魂（`soul.md` 人格全文，每次对话都带上）；保存即重新装配 |
| **上下文** | 治理总开关；外部化阈值与豁免名单；单条输出内联上限、每轮最大步数、token 粗估系数；read 展开预算、recall 上限、追回保活条数；工具组折叠门槛、工具并行级别与并发上限、技能预加载策略与「技能少」判定线；外部块保留天数/预览头尾/目录；自动压缩全套参数（模型窗口、压缩水位、步数阈值与水位下限、最小压缩间隔、保留最近步数、骨架摘要上限） |
| **协作（A2A）** | 请求超时、任务超时、轮询间隔、超时是否取消对端任务；熔断开关 / 连续失败阈值 / 冷却时长；运行时熔断状态列表与一键重置 |
| **调试** | prompt 落盘开关、落盘目录、最小输入 token、保留天数 |
| **子 agent** | 每个子 agent 单独设：启用与否、绑定模型、可用插件白名单（留空=不限制）；逐项保存、下一轮即生效 |
| **输入 @ 快捷项** | 工作空间根路径 + 快捷项昵称/插入短语的新增改删（前端即时生效） |

---

### 📊 可观测

- **筛选**：智能体下拉（核心 agent 标注）+ 时间范围（今天 / 近 7 天 / 近 30 天）
- **指标卡**：模型输入、模型输出、缓存命中率、LLM 调用数、LLM 耗时、成本、追问率；另标失败次数与错误率
- **趋势图**：Token（输入/输出堆叠）/ 成本 / 调用次数可切；今天按小时、其余按天，hover 看明细
- **工具统计**：每工具的调用数、失败数、总/平均耗时、平均/最大返回体、被截断次数，每列可排序
- **重复调用（空转）**：同工具同参数的重复率与明细；`read` 追回的重复单独一档（提示上下文抖动）
- **能力加载**：总次数与来源分布（系统预加载 / 模型主动 / 自动补载）、抖动次数
- **最慢调用 Top10**：耗时、首字延迟、输入输出 token、是否流式
- **错误**：LLM 与工具的失败数/失败率 + 按错误键分组的明细
- **解析结构**：解析步数、多动作步占比、被丢弃动作与丢弃率、格式错误
- **效果评估基线**：跑一轮回归、对比上一次或指定报告、看 Markdown 报告并可复制

---

### 🗑 回收站

- 列出全部**已删除**会话（跨智能体，含已卸载的）：标题与 ID、所属智能体、类型（对话/任务）、消息条数、最后更新、删除时间
- 「恢复」→ 回到列表可继续聊（软删除，**消息与用量都没删**）
- 每页 50 条，「显示更多」追加加载
- 彻底清理由保留策略负责，不在删除那一刻发生

---

### 🗄 模型管理

- **列表**：名称、Provider、模型、Base URL、温度、操作
- **行操作**：「测试」连通性（回显模型回复或失败原因）、「单价」、编辑、删除
- **可配字段**：名称、Provider（15 个：openai / deepseek / qwen / dashscope / moonshot / zhipu / anthropic / google / ollama / baichuan / yi / minimax / stepfun / localai / custom）、模型名（带该 provider 常用模型建议）、API Key（**只写不回显**，只显示「已配置 / 未配置」）、Base URL、温度、超时秒、最大重试、上下文上限 token（配了才有上下文水位告警）
- **改预设即重装**：保存后自动重装绑着它的 agent，并列出重载成功/失败的 agent 名
- **单价**：一个模型可配多段价（错峰计费）—— 生效自、适用星期、适用时段、输入 / 缓存命中 / 输出的每百万 token 单价、币种
- **绑定关系**：在「智能体 → Agent 配置 → 概览」或对话输入框的模型选择器里改；未绑定 = 纯索引模式（不调用模型）

---

## Roadmap

接下来主要做三件事，完整设计见 [`doc/roadmap.md`](doc/roadmap.md)：

### ① 我的空间：文件展示与编辑

让「我的空间」从「能看」变成「能改」——每种文件类型都有自己的展示与编辑方式。

| 要做的 | 现状 |
|---|---|
| **docx / pptx 预览与编辑** | 根本打不开（办公场景的默认格式） |
| **PDF 批注、圈选、裁页合并** | 只能看 |
| **图片编辑**（裁剪 / 旋转 / 标注） | 只能看 |
| **音频转写 / 视频抽帧字幕** | 没有处理 |
| **统一编辑协议**（各格式 → 统一文档模型 → 回写） | 每种格式各写一套 |
| **大文件分级降级** | 超 2MB 直接只给元数据 |
| **版本与撤销** | 改错了只能靠 agent 再改回来 |
| **任意文件 diff** | 只有 git 仓库内的文件有 |

→ 详细设计：[`doc/user-space.md`](doc/user-space.md) 第三节

### ② 我的空间：向量跨模态搜索

给空间里的文件建索引，让 agent 能按**语义**找到它们，而不只是按文件名。

| 要做的 | 现状 |
|---|---|
| **索引器**（监听变更 → 分块 → 嵌入） | 空间**没有任何索引** |
| **跨模态嵌入**（文本 / 图片 / 表格 / PDF / 音视频） | — |
| **混合检索**（向量 + 关键词 + 结构化过滤，RRF 融合） | 只有 `ls` + `grep` |
| **`search` 工具** | 没有 |
| **`@我的空间` 候选变成语义匹配结果** | 固定两项（我的空间 / 当前会话目录） |

技术倾向**纯本地**（`sqlite-vec` + 本地嵌入模型）：文件内容不外传，不多一个服务。

→ 详细设计：[`doc/user-space.md`](doc/user-space.md) 第四节

### ③ 长期记忆库

记忆从「今天」跨到「永远」。

| 要做的 | 现状 |
|---|---|
| **L2 长期事实**（跨会话复用，带来源与置信度） | 记忆是**按天**作用域的，换天即换记忆 |
| **L3 用户画像** | 没有 |
| **巩固与衰减**（间隔重复 + 时间衰减） | 没有 |
| **混合检索**（向量 + 关键词 + 权重） | 只有 FTS5 关键词 |
| **冲突裁决**（覆盖 / 并存 / 待确认） | 没有 |
| **可见可改**（用户能看、改、删、导出） | 没有 |

→ 详细设计：[`doc/memory.md`](doc/memory.md) 第八节

### 其余待办（分散在各模块文档里）

| 事项 | 文档 |
|---|---|
| 危险工具二次确认、prompt dump 脱敏 | [`doc/tools.md`](doc/tools.md) |
| LLM 永久错误不重试、连接池 | [`doc/llm.md`](doc/llm.md) |
| 插件安装器、来源校验 | [`doc/plugin-design.md`](doc/plugin-design.md) |
| 跨团队角色编排 | [`doc/collaboration-contract.md`](doc/collaboration-contract.md) |
| 入站认证的风险与恢复路径 | [`doc/security.md`](doc/security.md) |
| 桌面端打包分发、托盘、自启 | [`desktop/README.md`](desktop/README.md) |

**明确不做**：多租户 / 账号体系 / 云端同步 —— keeper 定位是**本机单用户**应用，
数据主权在用户本地。

---

## 目录

- [在线体验](#在线体验)
- [功能一览](#功能一览)
- [Roadmap](#roadmap)
- [环境与依赖](#环境与依赖)
- [快速开始（首次启动）](#快速开始首次启动)
- [配置模型（必做）](#配置模型必做)
- [配置「我的空间」](#配置我的空间)
- [启动桌面端](#启动桌面端)
- [命令行形态](#命令行形态)
- [前端开发模式](#前端开发模式)
- [配置：两层](#配置两层)
- [数据目录](#数据目录)
- [HTTP 接口](#http-接口)
- [插件](#插件)
- [回归测试](#回归测试)
- [部署](#部署)
- [文档](#文档)
- [排错](#排错)

---

## 环境与依赖

| 依赖 | 版本 | 用途 | 必须？ |
|---|---|---|---|
| Python | ≥ 3.10 | 内核（会话 / 工具 / 模型 / MCP / 记忆） | ✅ 必须 |
| pip | 随 Python | 装 `requirements.txt` | ✅ 必须 |
| Node.js | ≥ 18 | 构建前端产物、运行 Electron 壳 | 桌面端必须；只用内核可不装 |
| npm | 随 Node | 装前端 / Electron 依赖 | 同上 |
| git / ssh | 任意 | 只有手动从 git 拉插件源码时才需要 | 可选 |

**Python 依赖**全部列在 [`requirements.txt`](requirements.txt)，`start_keeper.sh` 会自动建
`.venv` 并安装（仅首次或 `requirements.txt` 变化时）。

**Node 依赖**分两处，互不干扰：

| 目录 | 装什么 | 什么时候装 |
|---|---|---|
| `web/` | 前端依赖（React / Vite / Tailwind / Monaco / ECharts…） | `web/build_web.sh` 或 `cd web && npm install` |
| `desktop/` | Electron（唯一的依赖） | `desktop/start_desktop.sh` 首次自动装 |

> 前端产物（`web/dist`）不存在时，内核**只提供 API**，页面是空白 / 404。
> 这是桌面端最常见的「起不来」，启动脚本已自动帮你构建。

---

## 快速开始（首次启动）

按顺序做完这 5 步就能用起来。

### 1. 装依赖

```bash
cd keeper
./start_keeper.sh          # 首次会建 .venv、装依赖
```

嫌它顺手把服务也起了？先 Ctrl+C，然后按下面「命令行形态」单独起也行。

手动装等价命令：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 改 `config.yaml`（元配置，只在部署时手改）

仓库里的 [`config.yaml`](config.yaml) 是出厂模板，**只有一处必须确认**：

```yaml
server:
  host: 127.0.0.1        # 必须绑回环：这是现在唯一的网络边界
  port: 8080
```

- **keeper 是本机单用户应用，没有入站认证**。所以 `host` 保持 `127.0.0.1` 就不只是
  "建议"，而是**唯一**的防线——改成 `0.0.0.0` 就等于把一个「能让 agent 执行本机命令」
  的接口暴露给整个局域网。
- 内核里有能让 agent 执行命令的对话接口，而 CORS 只挡"读响应"、不挡"发请求"：
  你打开的任意网页都能向回环端口发请求。**别在有不可信代码的机器上跑演示形态。**
  详见 [`doc/security.md`](doc/security.md)。
- 桌面形态**什么都不用配**。

> 以前这里有 `server.auth.token`（入站 token）和 `platform.token`（平台凭据），
> 两者**都已随平台一起移除**。如果你的旧 `config.yaml` 里还有它们，可以直接删掉。

### 3. 起内核

```bash
./start_keeper.sh
```

首次启动会自动铺好这些（都可以不管，但知道它们在哪有用）：

| 位置 | 内容 |
|---|---|
| `~/.keeper/core-agent/agent.yaml` | 核心 agent（主脑）的名字 |
| `~/.keeper/core-agent/soul.md` | 核心 agent 的人格 / 语气（每次对话都带上） |
| `~/.keeper/settings.yaml` | 用户设置（可观测 / A2A / 上下文 / 我的空间） |
| `data/keeper.db` | SQLite 库（建表幂等） |

### 4. 配置模型 ← **必做，否则 agent 不调模型**

见下一节。

### 5. 打开页面

桌面形态：壳会自动开窗，不用管。

命令行形态（演示用）：直接开 `http://127.0.0.1:8080`——**没有入站认证**，
不需要 token。

---

## 配置模型（必做）

keeper 的模型语义只有一条：**模型预设存在本地库，agent 通过绑定表显式引用，没有兜底**。

> 没有绑定 = 纯索引模式（只用知识库和工具，**不调用模型**）。日志里会打
> `agent[core]：未绑定模型预设，将以纯索引模式运行`。

### 步骤 1：建一个模型预设

界面：「模型管理」页 → 新增。

或用 API：

```bash
curl -X POST http://127.0.0.1:8080/manage/models \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "deepseek-chat",
    "provider": "deepseek",
    "model_name": "deepseek-chat",
    "api_key": "sk-xxxx",
    "temperature": 0.2,
    "timeout": 120,
    "max_retries": 3
  }'
```

支持的 provider（见 `llm/config.py`）：

| provider | 说明 | 默认模型 |
|---|---|---|
| `openai` | OpenAI 官方 | `gpt-4o-mini` |
| `deepseek` | DeepSeek 官方（OpenAI 兼容） | `deepseek-chat` |
| `siliconflow` | 硅基流动（OpenAI 兼容） | `Qwen/Qwen2.5-7B-Instruct` |
| `anthropic` / `claude` | Anthropic 官方 | `claude-3-5-sonnet-latest` |
| `ollama` | 本地 Ollama / vLLM | `qwen2.5` |

> `api_key` 是**只写**字段：接口永不回显明文，只回 `has_api_key: true/false`。
> 编辑时留空表示「不改」，要换 Key 就填新值。

### 步骤 2：连通性测试

界面点「测试」，或：

```bash
curl -X POST http://127.0.0.1:8080/manage/models/<model_id>/test
```

### 步骤 3：绑定给 agent

界面：agent 详情抽屉 → 「大模型」下拉选择。或：

```bash
curl -X POST http://127.0.0.1:8080/agents/core/model \
  -H 'Content-Type: application/json' \
  -d '{"llm_profile_id": "<model_id>"}'
```

`core` 是**核心 agent** 的 id（见 `config.yaml` 的 `core_agent.id`，默认 `core`）。
绑定 `null` / 空串 = 解绑。

**改完立刻生效**：模型实例是在 agent 装配期固化的，改预设会触发绑着它的 agent
**自动重装配**，不需要重启服务（重启会中断正在跑的会话）。

---

## 配置「我的空间」

「我的空间」= 用户自管的一个目录。它既是 agent 干活的**安全根**（bash 等工具可以
把它里的路径当作合法根，输入框里 `@我的空间` 也会展开成它的绝对路径），
也是 keeper 作为**本地知识库 agent** 的**知识库本体**。

（注意区分：**每一轮真正的工作目录**是会话空间
`~/.keeper/workspace/session/<session_id>/`；「我的空间」是你指定的、跨会话共享的
那个用户目录。）

界面：「我的空间」页 → 选一个文件夹。落盘位置：

```yaml
# ~/.keeper/settings.yaml
user_space:
  path: /Users/you/Projects
```

桌面端该页走**原生实现**（系统文件对话框、Finder 访达、剪贴板），见
[`desktop/spaceFs.js`](desktop/spaceFs.js) 与 [`desktop/preload.js`](desktop/preload.js)。

没配时的行为：agent 回落到内置默认目录 `~/.keeper/workspace/user/default`
（可写），日志里会提示「未配置我的空间」。

---

## 启动桌面端

```bash
cd keeper/desktop

./start_desktop.sh    # 前台运行：Ctrl+C 停止
./stop_desktop.sh     # 停止（窗口被强杀后残留的内核也一并收掉）
```

脚本按顺序做：

1. 检查 `node` / `npm`
2. 首次运行 `npm install` 装 Electron
3. 没有 `web/dist` 就自动构建（`SKIP_BUILD=1` 可跳过）
4. 拉起 Electron → 主进程占一个空闲端口 → spawn Python 内核 → 轮询 `/health`
   直到就绪 → 开窗加载 `http://127.0.0.1:<动态端口>`

不用脚本的等价命令：

```bash
cd keeper/web && ./build_web.sh     # 1) 构建前端产物
cd keeper/desktop && npm install    # 2) 装 Electron
npm start                           # 3) 启动
```

设计细节见 [`desktop/README.md`](desktop/README.md)。

---

## 命令行形态

```bash
./start_keeper.sh                          # 前台，默认 :8080
PORT=9090 ./start_keeper.sh                # 换端口
LOG_LEVEL=DEBUG ./start_keeper.sh          # 更详细的日志
LOG_FILE=/tmp/keeper.log ./start_keeper.sh # 额外落一份日志（Rotating 10MB × 5）
./start_keeper.sh --daemon                 # 后台守护（日志 → keeper.log）
./stop_keeper.sh                           # 停止
```

也可以直接跑（**要在 `keeper/` 的上一级目录**——`keeper` 是那个目录里的包）：

```bash
cd ..                                  # 即 /path/to/parent
python -m keeper.main --config keeper/config.yaml 8080
```

`Ctrl+C` 停止（会一并关闭 MCP 会话与子进程）。

---

## 前端开发模式

改前端时用 vite dev server 热更新，内核仍在 8080：

```bash
cd keeper/web
npm install
npm run dev          # http://localhost:5273
```

`vite.config.ts` 里配了 `/api` 反向代理到 `http://localhost:8080`，
所以**不需要处理跨域**。内核端口不是 8080 时，用环境变量告诉 vite：

```bash
KEEPER_PORT=9090 npm run dev
```

生产模式下前端由内核同源托管，同样不需要 CORS。

---

## 配置：两层

| 文件 | 谁改 | 生效方式 |
|---|---|---|
| `config.yaml` | **人手动改** | 重启 |
| `~/.keeper/settings.yaml` | 设置页写入 | **立即生效**，不需重启 |

`config.yaml` 的段：`server` / `agent` / `core_agent` / `framework` /
`llm` / `workspace` / `agent_overrides` / `plugins` / `observability` / `a2a` / `context`。

`settings.yaml` 只放「用的时候调」的五段：`observability` / `a2a` / `context` /
`user_space`。
读取时与代码默认值合并，**缺项也能跑**。

---

## 数据目录

```
~/.keeper/
├── core-agent/            核心 agent（主脑）：agent.yaml（名字）+ soul.md（人格）
├── agents/<id>/plugins/   该 agent 的插件绑定（符号链接指向插件库）
├── plugins/               插件库（~/.keeper/plugins，仓库里的 plugins/ 只作参考）
├── workspace/
│   ├── user/<space>/      用户空间（含 default）
│   ├── agent/<agent_id>/  agent 级私有产物（插件索引 / 缓存）
│   └── session/<sid>/     会话空间（这一轮的工作目录）
├── memory/days/<日期>/    记忆：turns.jsonl + FTS5 索引
├── evals/                 评测产物（基线 / 运行记录）
├── prompt-dump/           prompt 落盘（调试，默认关）
├── context-store/         上下文外部化的块
├── logs/                  桌面形态的内核日志
├── framework/             本机运行时（python / node）
└── settings.yaml          用户设置
```

数据库：`data/keeper.db`（SQLite，随项目目录走）。

---

## HTTP 接口

| 前缀 | 内容 |
|---|---|
| `/manage/agents/*` | agent 装载 / 卸载 / 本地创建、插件绑定、模型绑定 |
| `/manage/models/*` | 模型预设 CRUD + 连通性测试 + 分段单价 |
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

**没有入站认证**：所有接口直接开放。防线只有「绑回环 + 本机单用户」
（见 [`doc/security.md`](doc/security.md)）。

前端与 API **同源**，不需要 CORS。

---

## 插件

插件是「一个包 + 一份清单」，统一承载三类组件：

| 组件 | 形态 | 说明 |
|---|---|---|
| `mcpServers` | MCP | 常驻子进程，有跨调用状态（如索引缓存） |
| `bin` | 可执行文件 | 一次性进程，语言无关，stdin/stdout JSON 协议 |
| `skill` / `skills` | SKILL.md | 纯知识，只注入 prompt |

清单格式有两种（自动识别）：`keeper-plugin.json` 或 `.claude-plugin/plugin.json`。
路径用 `${PLUGIN_ROOT}` / `${AGENT_SPACE}` 变量，**不要硬编码绝对路径**。

仓库里带了 5 个参考插件（`plugins/`，只作参考，运行时读的是 `~/.keeper/plugins`）：
`bash`（执行 shell，文件读写都走它）、`excel`、`pdf`、`plugin-installer`、
`plugin-authoring`。

插件走**子进程**而不是 import：第三方代码进主进程等于把 keeper 的全部权限交给它。

---

## 回归测试

能直接 `python` 跑完出结论的脚本，不引测试框架：

```bash
python tests/test_plugin_lib.py      # 插件库：清单解析、链接增删、幂等
python tests/test_guard.py           # 安全守卫：白名单放行、绕过拦截
python tests/test_planner_parse.py   # ReAct 文本协议解析
python tests/test_hallucination_guard.py
python tests/test_protocol.py        # ReAct 标签协议
```

---

## 部署

```bash
./deploy.sh              # 打服务端包 → dist/keeper-server-*.zip
                         # 排除 data / web / dist / 日志 / 测试
cd web && ./deploy.sh    # 构建前端 → dist/keeper-web-*.zip
```

服务端包解压后目录结构不变，`start_keeper.sh` 可直接用。
在宝塔 / systemd 下用**前台模式**（进程管理器自己保活），不要加 `--daemon`。

**日志**默认只到控制台；设 `LOG_FILE` 会额外落一份（Rotating，默认 10MB × 5 份）。
装配期会打印每个插件、每个工具的装配结果与跳过原因——查「工具为什么没出现」先看这里。

---

## 文档

全部在 [`doc/`](doc/)：

| 想了解 | 看 |
|---|---|
| **全貌 + 模块↔文档对照** | [`architecture.md`](doc/architecture.md) |
| **怎么跑起来 / 怎么配** | 本文 README + [`desktop/README.md`](desktop/README.md) |
| **未来要做什么** | [`roadmap.md`](doc/roadmap.md)（三个方向） |
| 产品定位 | [`positioning.md`](doc/positioning.md) |
| 安全边界与已知缺口 | [`security.md`](doc/security.md) |
| 会话存储 | [`session-design.md`](doc/session-design.md) |
| 任务模式 | [`task-design.md`](doc/task-design.md) |
| 能力按需加载 | [`capability-loading.md`](doc/capability-loading.md) |
| 上下文治理（外部化 + 压缩） | [`context.md`](doc/context.md) |
| 记忆与检索 + 长期记忆库规划 | [`memory.md`](doc/memory.md) |
| 产物与交付块 | [`artifact-design.md`](doc/artifact-design.md) |
| 工具与安全守卫 | [`tools.md`](doc/tools.md) |
| 模型预设 / 绑定 / 计价 | [`llm.md`](doc/llm.md) |
| 插件库与绑定 | [`plugin-design.md`](doc/plugin-design.md) |
| **我的空间**（展示编辑 + 向量搜索） | [`user-space.md`](doc/user-space.md) |
| 跨端协作 | [`collaboration-contract.md`](doc/collaboration-contract.md) |
| 可观测 | [`observability-design.md`](doc/observability-design.md) |
| 评测 | [`evals.md`](doc/evals.md) |

---

## 排错

| 现象 | 先看 |
|---|---|
| 桌面端窗口空白 / 404 | 有没有 `web/dist`（`start_desktop.sh` 会自动构建；手动跑 `web/build_web.sh`） |
| 桌面端报「内核 xx ms 内未就绪」 | 单独跑 `./start_keeper.sh`，确认内核本身能起 |
| 对话没反应、日志里说「未绑定模型预设」 | 去「模型管理」配模型并绑定给 agent（见 [配置模型](#配置模型必做)） |
| 工具没出现 | 启动日志里的「装配汇总 / 装配跳过」段落说明了原因 |
| 关窗后进程还在、下次启动撞数据库 | `desktop/stop_desktop.sh`；再 `ps -A -o pid=,ppid=,command=` 找 PPID 为 1 的 `keeper.main` |
| 端口被占 | 桌面端用动态端口不会冲突；命令行形态用 `PORT=9090 ./start_keeper.sh` |
