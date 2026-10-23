# 插件

> 本文讲 `plugin/` 与 `mcp/`：插件库怎么组织、清单怎么写、怎么绑给 agent。
> 按需**加载**机制（哪些进上下文、什么时候进）见
> [`capability-loading.md`](capability-loading.md)。

---

## 一、插件库

插件库是**一个目录**，不是一个中心化的注册表：

```
~/.keeper/plugins/          ← 库根（config.plugins.root 可改）
├── bash/                   ← 本机插件
├── excel/
├── pdf/
└── plugin-installer/
```

**加插件 = 往这个目录里放文件**。放进去即生效，刷新页面就能看到。
这比「在界面上点安装」更符合本机单用户应用的定位——插件是本机文件，不是网络资源。

仓库根的 `plugins/` 只是**参考副本**，运行时读的是 `~/.keeper/plugins`。

---

## 二、清单格式

两种格式**自动识别**（`plugin/formats.py::detect_format`）：

| 格式 | 路径 | 特点 |
|---|---|---|
| keeper | `keeper-plugin.json` | 原生格式，字段最全 |
| Claude | `.claude-plugin/plugin.json` | 兼容 Claude 插件布局，零改装 |

示例（keeper 格式）：

```json
{
  "name": "bash",
  "version": "1.0.0",
  "description": "执行 shell 命令；文件读写都走它",
  "mcpServers": {
    "shell": {
      "summary": "在本机执行命令并返回输出",
      "command": "${PLUGIN_ROOT}/bin/bash-mcp"
    }
  },
  "bin": [
    {
      "summary": "一次性执行命令",
      "command": "${PLUGIN_ROOT}/bin/run",
      "tools": [{ "name": "run", "description": "执行一条命令" }]
    }
  ],
  "skill": "skills/SKILL.md",
  "skills": ["skills/other.md"]
}
```

### 路径变量

| 变量 | 指向 |
|---|---|
| `${PLUGIN_ROOT}` | 插件自己的目录 |
| `${AGENT_SPACE}` | 该 agent 的私有空间（`~/.keeper/workspace/agent/<id>/`） |

**不要硬编码绝对路径**——库搬个家就全废了。

---

## 三、三种组件形态

| 形态 | 是什么 | 落点 |
|---|---|---|
| **MCP 组件** | 常驻子进程，有跨调用状态（如索引缓存） | `mcp/` |
| **bin 组件** | 一次性进程，语言无关，stdin/stdout 传 JSON | `tools/subprocess_tool.py` |
| **知识（SKILL.md）** | 纯知识，只注入 prompt，不提供工具 | `skill/` |

知识组件的 L1/L2 拆分（摘要常驻 / 正文按需加载）见
[`capability-loading.md`](capability-loading.md)。

---

## 四、绑定：链接目录

**绑定关系由链接目录本身表达，不落表**——列目录就是读绑定，不需要查表。

```
~/.keeper/agents/<agent_id>/plugins/
├── bash -> ../../plugins/bash      （相对符号链接）
└── excel -> ../../plugins/excel
```

- 用**相对**符号链接：`~/.keeper` 搬家不断
- 勾选插件 = 建链接，取消 = 删链接
- 库里目录已不存在的**悬空链接**会标「· 缺失」，保存一次即可清掉

> 早期有「从平台下载的插件是独立副本」这回事（固定版本、已有实体目录就不覆盖）。
> 平台整体移除后**插件只有一种来源**：本机库里的目录，绑定即链接。

---

## 五、插件走子进程

插件是**你本机上的第三方代码**，它通过 MCP / bin 子进程拿到接近 shell 的能力。

插件走子进程而不是 import：import 等于把 keeper 的全部权限（数据库、文件、网络）
交给第三方代码。子进程至少还隔着一层，可以被 kill、被限权。

插件库页是**只读**的：展示名称、版本、能力类型、环境要求、体积、说明、目录名，
行展开可见实际路径或清单错误。加插件的方式就是往目录里放文件。

---

## 六、待办清单

| 项 | 状态 | 说明 |
|---|---|---|
| 插件安装器（图形化） | ⚠️ 部分 | 有 `plugin-installer` 插件，但**没有中心化的安装/卸载界面** |
| 插件依赖检查 | ⚠️ 部分 | 清单里的 `envDependencies`（如 python ≥ 3.10）不满足时创建 agent 会被拒，但插件页只显示要求、不预检 |
| 插件可用性对用户可见 | ⚠️ 部分 | 装配期跳过的工具，界面仍显示「已装配」 |
| 插件签名 / 来源校验 | ❌ 未做 | 装插件等于在本机装了它的全部权限，目前没有来源可信度校验 |
