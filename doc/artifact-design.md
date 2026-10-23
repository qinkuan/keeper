# keeper 产物（Artifact）设计

> 本文描述**已实现**的产物机制。核心落点 `chat/artifacts.py`、
> `chat/api.py` 的文件端点、前端 `ArtifactCard.tsx`。

配套：`session-design.md`（产物挂在哪）、`task-design.md`（过程产物 vs 结果产物）。

---

## 一、目标

agent 干活会产出文件（报告、代码、图表、PDF）。需要：

1. **用户能看见、能点开、能下载**，而不是只看到一段文字说「已生成」。
2. **来源可追溯**：知道这个文件是哪一轮 / 哪个计划点产出的。
3. **取数安全**：agent 写的路径不能越出工作区，也不能拿服务去探测内网。

---

## 二、数据模型：产物不是独立表

产物是**挂在消息 / 任务上的 JSON 字段**，不是独立表：

| 位置 | 内容 |
|---|---|
| `session_messages.artifacts` | 本轮对话的产物 |
| `task_items.artifacts` | 计划点的**过程产物** |
| `tasks.artifacts` | 任务的**结果产物** |

三者**同构**，都是 JSON 数组：

```json
[{"id": "01H...", "name": "report.md", "path": "file:///abs/path",
  "mime": "text/markdown", "size": 12345}]
```

为什么**不建独立表**：产物天然属于某条消息 / 某个计划点，跟着它们走即可；
建表反而要维护外键与生命周期（消息删了产物怎么办）。

---

## 三、`path` 是带 scheme 的 URI（不是裸路径）

```
file:///abs/path   （或裸绝对路径，默认按 file 处理）→ 本机文件系统
http(s)://host/... （或 s3:// 等）                  → 远程 / 云存储
```

前端**只凭 `artifact_id` 拼 URL**，真正的取数逻辑全在 `chat/artifacts.py`：

- 扩展新存储后端只需注册一个 scheme handler
- **安全策略统一在一处**（本机越界校验、远程 SSRF 防护）

### 3.1 安全红线

| 场景 | 防护 |
|---|---|
| 本机越界 | `resolve()` 后仍要求文件真实存在；软链被替换跳出工作空间会 404 |
| 远程代取 | **默认拒绝任何远程代取** |
| 主机白名单 | 仅 `KEEPER_ARTIFACT_HTTP_ALLOW_HOSTS` 内的 host 允许 |
| SSRF | 解析出的 IP 不得为私有 / 回环 / 链路本地 / 保留段 |
| 体积与超时 | 限制大小与超时 |
| 重定向 | **不跟随**——避免绕过白名单打到云元数据 / 内网 |

---

## 四、API 契约

| 端点 | 用途 |
|---|---|
| `GET /agents/{agent_id}/files/{message_id}/{artifact_id}` | 取产物内容 |
| 同上 `?dl=1` | 作为附件下载（不含 `dl` 为内联预览） |

**内联 vs 附件**（`is_previewable` 判定）：

```
可预览（内联）：image/*  text/*  video/*  audio/*  application/pdf
               application/json  application/javascript
其它：          attachment（下载）
```

HTML 产物额外加 CSP 兜底（`default-src 'none'`、`connect-src 'none'`），
因为 agent 生成的 HTML 属低可信内容。

---

## 五、捕获逻辑（写入侧）

产物由 `fs.deliver` 工具显式登记（**唯一正式入口**）：

- agent 用 bash 写完文件后调用 `fs.deliver([{kind, path|file, ...}])` → 挂到当前消息
- **兼容旧写法** `paths`（路径字符串列表），会规整成 `kind="file"` 的块
- **仅在本轮零产物时**，`chat/service.py` 才会按本轮 mtime 扫描工作区**兜底**补登记
  （最多 5 个），避免「文件写了但没展示」；已经有正式产物时不再兜底，免得重复

工具描述里明确写了这条规则，模型知道要自己交付。

### 5.1 交付块（Block）模型

`fs.deliver` 交付的不是「文件路径」，而是「带类型的块」（`viewer/block.py`）：

| kind | 含义 | 必须引用文件 |
|---|---|---|
| `file` | 普通文件 | 是 |
| `chart` | 图表（ECharts / Vega-Lite spec） | 是 |
| `mermaid` | 流程图（可内联 `src`） | 否 |
| `table` | 表格 | 是 |
| `map` | 地图 | 是 |
| `audio` | 音频 | 是 |

每个块还可以带：

- `render`：`inline` / `preview` / `run` / `download` / `folder`（渲染提示）
- `ref`：语义化引用名——agent 在 `fs.deliver` 里给块起 `ref`，之后在最终回答的文字里
  用 `[[art:ref]]` 把这块产物插到指定位置（`chat/service._capture_artifacts`）

重内容（图表 spec、数据）由 agent 写进文件，块只描述「怎么渲染这个文件」——
几行 JSON 即可，不把巨型 spec 塞进工具参数。

---

## 六、前端展示：按类型分派

展示器判定在**前端**（`web/src/components/BlockView.tsx`），按块的 `kind` 选渲染器：

| kind | 展示 |
|---|---|
| `file` | 按扩展名 / MIME 决定：图片内联预览、文本用 Monaco 高亮、PDF 走 pdf.js、其它给下载 |
| `chart` | ECharts / Vega-Lite 渲染 |
| `mermaid` | mermaid 渲染 |
| `table` | 表格渲染（可下载） |
| `map` | 地图渲染 |
| `audio` | 音频播放器 |

**为什么按块而不是按文件类型**：交付的是「意图」（这是一张图表 / 一段流程图），
不是「扩展名」。同一份 `.html` 既可能是要预览的页面，也可能是要下载的产物，
由 agent 在交付时声明 `render` 提示最准。

**二进制不硬塞进编辑器**：判为二进制就只给下载 + 文件信息，避免显示一堆
`\x00` 和替换字符。判定两种信号：扩展名黑名单 + 内容探测（含 NUL、
或 U+FFFD 占比 >10%）。

---

## 七、已知限制

- **HTML 里的相对引用会 404**：产物是单文件端点，没有「同目录」概念。建议 agent
  生成**单文件内联 HTML**（CSS/JS 内联进 `<style>`/`<script>`）。
- **产物列表不分页**：一轮消息产物很多时没有限制。
- **无版本**：同名文件覆盖后，旧的产物引用指向新内容。

---

## 八、设计取舍记录

| 决策 | 选择 | 理由 |
|---|---|---|
| 产物存哪 | JSON 字段，不建表 | 天然属于消息 / 计划点，避免外键与生命周期维护 |
| path 格式 | 带 scheme 的 URI | 可扩展后端，安全策略集中 |
| 远程代取 | 默认拒绝 + 白名单 | SSRF 风险远大于便利 |
| 展示器判定 | 前端 | 后端不该关心渲染形态；与工作台共用一张表 |
| 大文件 | 超过 512KB 不进编辑器 | Monaco 渲染几 MB 文本会卡死浏览器 |
