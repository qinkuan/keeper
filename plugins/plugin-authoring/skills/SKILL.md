---
name: plugin-authoring
description: 需要新建或修改 keeper 插件（keeper-plugin.json 与 mcp/ bin/ skills/ 三类组件）时使用；不是某个插件的用法说明——那由插件自己的 SKILL.md 负责；也不是安装现成插件的方法——那由 plugin-installer 负责
always: false
keywords: [插件, 插件包, keeper-plugin, mcp, bin, SKILL.md, 清单, plugin]
tools: [bash__run]
---

# 制作 keeper 插件

一个插件 = **一个包 + 一份清单**。包里按目录约定放三类组件，清单告诉 keeper
「我有什么、怎么启动」。三类组件可任意组合，也可以只有其中一类。

> 本技能只管**制作**。从 GitHub / 直链**安装**现成插件（含 Claude 布局的外部包）
> 用 `plugin-installer`——那是另一个插件的事，这里不展开。

## 一、先选组件

| 你要做的事 | 放哪 | 协议 |
|---|---|---|
| 执行代码，工具由 server 运行时上报，可能常驻 | `mcp/` | 子进程，stdio JSON-RPC，**清单不写 schema** |
| 执行代码，一次调用一次进程，语言任意 | `bin/` | 子进程，stdin/stdout JSON，**schema 必须在清单静态声明** |
| 只教模型怎么做事，不执行任何代码 | `skills/` | 不执行，正文给 LLM 看 |

判断口诀：**MCP 是「服务」，bin 是「脚本」**。编译好的 MCP server 放 `mcp/`；
普通脚本 / CLI 放 `bin/`。同一个可执行文件不会因为放错目录而报错，
但放错会导致它的能力描述方式不匹配（MCP 自描述 vs 清单声明）。

## 二、目录结构

```
my-plugin/
├── keeper-plugin.json     【必需】清单，唯一入口说明书
├── mcp/                   【可选】MCP server 可执行文件
│   └── my-server
├── bin/                   【可选】脚本 / CLI
│   └── my-tool
└── skills/                【可选】知识
    └── SKILL.md
```

`skills/` 是**自动发现**的，命名规则（名字必须唯一，否则互相覆盖）：

| 文件位置 | 生成的名字 |
|---|---|
| `skills/SKILL.md` | 插件名本身（主知识） |
| `skills/<name>/SKILL.md` | `{plugin}__{name}` |
| `skills/<name>.md` | `{plugin}__{name}` |

清单里的 `skill`（单个）/`skills`（列表）只用于指向**目录以外**的文件。

> 坑：`mcp/`、`bin/` 目录里有内容但清单没声明对应字段 → **该目录不生效**，只打告警。

## 三、使用外部技能包（Claude 布局）

市面上的技能包（Claude 的 Agent Skills 生态）布局是：

```
external-skill/
├── .claude-plugin/plugin.json    ← 清单（name/version/description）
└── skills/
    └── pdf-processing/
        ├── SKILL.md              ← 正文，frontmatter 同第七节
        ├── references/           ← 配套资料
        └── scripts/              ← 配套脚本
```

keeper **原生兼容、零改装**：这样的目录原样丢进插件库即生效。装载按**特征标志**
分派（`.claude-plugin/plugin.json` → Claude 格式；`keeper-plugin.json` → keeper 格式），
`skills/` 目录约定两边相同，自动发现成 `{插件名}__{技能名}`。

| 外部布局 | keeper 侧发生什么 |
|---|---|
| `.claude-plugin/plugin.json` | 当清单读（取 name / version / description） |
| `skills/<名字>/SKILL.md` | 自动发现，命名 `{插件名}__{名字}` |
| `references/`、`scripts/` 等配套文件 | 装配时把所在目录的**绝对路径**注入技能正文，模型经 bash 可读可执行 |

差异与限制：

- Claude frontmatter 的 `allowed-tools`（**硬限制**）被忽略——keeper 的 `tools` 是
  软标注（不控制可用性），语义不同，不做映射；
- `commands/`、`agents/`、`hooks/` 是 Claude 的另一套机制，一期不装配（装载时告警）；
- 外部包的 `scripts/` 会被模型通过 bash 执行，**只装信任来源的包**；
- 散装技能（没有 plugin.json 的 `skills/<名字>/SKILL.md`）：套一个目录、补一份
  `keeper-plugin.json` 壳即可入库。

## 四、清单字段

```jsonc
{
  "name": "my-plugin",              // 必需，同时是工具名前缀
  "version": "1.0.0",
  "description": "插件级一句话覆盖",

  "skill": "skills/SKILL.md",       // 或 "skills": ["a.md", "b.md"]

  "mcpServers": {
    "graph": {
      "summary": "代码库知识图谱：索引仓库、查符号定义与调用关系、评估改动影响面",  // 见下文
      "transport": "stdio",         // 默认 stdio
      "command": "${PLUGIN_ROOT}/mcp/my-server",
      "args": ["--verbose"],
      "env": { "CBM_CACHE_DIR": "${AGENT_SPACE}/my-plugin" },
      "timeout": 300                // 秒
      // cwd 默认包根；工具由 server 运行时上报，这里不写 schema
    }
  },

  "bin": [
    {
      "name": "deploy",             // 可选，仅用于日志标识
      "summary": "部署相关：打包、灰度、回滚",   // 可选，覆盖这一组工具的一句话说明
      "command": "${PLUGIN_ROOT}/bin/my-tool",
      "args": [],                   // 可选，固定附加参数
      "cwd": "${PLUGIN_ROOT}",      // 可选，默认包根
      "timeout": 60,
      "tools": [
        {
          "name": "release",        // 必需，工具短名
          "description": "什么时候该用这个工具（写给模型看）",
          "parameters": {           // JSON Schema，静态声明
            "type": "object",
            "properties": { "env": { "type": "string" } },
            "required": ["env"]
          },
          "mutating": true,         // 会改动环境/数据
          "requires_workspace": true // 依赖工作区，没有就不装配
        }
      ]
    }
  ]
}
```

> **`summary` 决定这一组折不折叠。** 工具数达到门槛**且**写了 `summary`，才会折成一行
> 只列工具名（省上下文）；**没写就走老流程**，逐个给完整明细——宁可多花 token，
> 也不拿「模型能不能发现它」去换。
> 想省这个 token 就写一句人话：`"GitHub 仓库操作：PR / issue / 代码搜索"`。
> 不写也完全能跑，只是不省。

### 路径变量（禁止硬编码路径）

| 变量 | 含义 | 放什么 |
|---|---|---|
| `${PLUGIN_ROOT}` | 包解压后的绝对路径 | 引用包内文件 |
| `${AGENT_SPACE}` | 本 agent 的私有产物根 | **索引、缓存、临时文件** |
| `${PLUGIN_DATA}` | 持久数据目录（预留） | 需要跨升级保留的数据 |

> 坑：依赖和缓存**不能放包内**——包会随升级/卸载被整个删掉。

### 工具全名

| 组件 | 全名 | 说明 |
|---|---|---|
| bin | `{plugin}__{tool}` | 传给可执行文件的是**短名** |
| MCP | `{plugin}__{server}__{tool}` | server 名来自清单的 key |

在 SKILL.md 里引用工具时**必须写全名**（如 `bash__run`），模型照抄才不会调错。

## 五、bin 的调用协议（写给插件作者）

- **调用**：keeper 起进程，stdin 写入**一行** JSON
  ```json
  { "tool": "release", "args": { "env": "prod" },
    "context": { "workspace": "/…", "read_only": false } }
  ```
- **返回**：stdout 的**最后一行**必须是 JSON
  ```json
  { "ok": true, "result": "…" }   // 或 { "ok": false, "error": "…" }
  ```
  前面允许打日志（所以取最后一行）。
- 非零退出码 / 超时 / 输出不是 JSON，都算调用失败。

最小可跑的 bin 脚本（Python）：

```python
#!/usr/bin/env python3
import json, sys
req = json.loads(sys.stdin.readline())
args = req.get("args") or {}
if req.get("tool") != "release":
    print(json.dumps({"ok": False, "error": f"未知工具 {req.get('tool')}"}))
    sys.exit(0)
print(json.dumps({"ok": True, "result": f"released {args.get('env')}"}))
```

记得 `chmod +x`——没有可执行权限会在装载期被跳过。

## 六、权限语义

- `mutating: true` 且工作区只读 → **不装配**；
- `requires_workspace: true` 却没有工作区 → **不装配**。

高危能力（删文件、执行 shell）要明确标 `mutating`，并在 SKILL.md 里写清禁区。

## 七、SKILL.md 该怎么写

```markdown
---
name: my-skill
description: 需要 X 时使用；不用于 Y（写给模型判断是否加载）
always: false          # true = 始终生效的行为规矩，正文常驻
keywords: [PR, 合并, review]   # 可选：给系统做「该不该预加载」用的命中词
tools: [bash__run]     # 可选：本技能擅长的工具（软标注，不控制可用性）
---

# 标题

## 什么时候用
## 流程（按步骤，写清每步用什么工具、全名）
## 禁区（明确禁止的做法）
```

三个字段各管一件事，别混：

- `description` 给**模型**看：判断要不要主动加载；
- `keywords` 给**系统**看：keeper 每轮开头会拿它跟用户问题做匹配，命中就把正文预先取来。
  不写也能用（会从 description 里抽词），但抽出来的命中率远不如手写的准——
  尤其是术语有缩写、或者中英文混用的场景；
- `always` 决定要不要走这套流程：true 的技能永远在上下文里，不用判也不用加载。

`description` 的写法决定模型会不会用对它：**写「何时用」，不要写「是什么」**。

- 差：`处理 GitHub 相关操作`
- 好：`用户要创建/合并 PR、查看 CI 状态、查 issue 时用；不用于读写本地文件`

`always` 的取舍：是**做事的方法**就按需加载（false），是**做事的规矩**
（如「回答必须用中文」「改代码必须补测试」）就该始终生效（true）——
按需加载会让规矩时灵时不灵。

## 八、交付前自检

- [ ] `keeper-plugin.json` 在包根、JSON 合法（解析失败整个插件被跳过）
- [ ] 所有内部路径用 `${PLUGIN_ROOT}`，没有硬编码绝对路径
- [ ] `bin` 工具的可执行文件有 `+x` 权限
- [ ] bin 脚本的 stdout **最后一行**是 JSON，前面没有多余输出
- [ ] 清单 `parameters` 与脚本实际读取的参数名一致
- [ ] `mcp/` 或 `bin/` 目录有内容且清单已声明对应字段
- [ ] 每个 MCP server / bin 条目都写了 `summary`（工具被折叠时就靠它判断要不要用）
- [ ] 提供了 MCP 工具的插件，SKILL.md 里写清了**每个工具的用途与参数**
- [ ] skill 的 `description` 写的是「何时用」而不是「是什么」
- [ ] SKILL.md 里引用的工具名是全名（`{plugin}__{tool}`）
- [ ] 缓存 / 索引写到 `${AGENT_SPACE}`，没写进包内
- [ ] 破坏性能力标了 `mutating`，依赖工作区的标了 `requires_workspace`

## 九、常见坑

| 现象 | 原因 | 修法 |
|---|---|---|
| 工具一个都没出现 | 目录有内容但清单没声明 | 补 `mcpServers` / `bin` |
| 装载期告警「跳过」 | 可执行文件不存在或没有 `+x` | `chmod +x` |
| 调用报「不是 JSON」 | 脚本前面打了日志但最后一行不是 JSON | 把 JSON 放最后一行输出 |
| skill 没生效 | 名字重复被覆盖 / 文件不在 `skills/` 下 | 改目录名或清单显式声明 |
| 模型调错工具 | SKILL.md 里写的是短名 | 改写成全名 |
| 装了插件却像没装 | 工具被折叠成一行，模型看不出这组干嘛、不敢调 | 补 `summary`，并在 SKILL.md 说明用途与参数 |
| 外部技能包里 `scripts/x.py` 跑不起来 | 模型不知道包在哪个绝对路径 | 已自动注入配套目录路径；确认技能用的是 `<名字>/SKILL.md` 布局（散装 `.md` 没有独立资源目录） |
| 丢进库的 Claude 包不显示 | 包根没有 `.claude-plugin/plugin.json`（按特征标志识别格式） | 补标志文件，或套目录加一份 `keeper-plugin.json` 壳 |
| 升级后缓存没了 | 缓存写在了包内 | 改写到 `${AGENT_SPACE}` |

## 十、装载与验证

平台新建插件 → 来源选**本地目录**（填本机绝对路径）→ 绑定给 agent → 装载。
`local` 来源不下载、直接用本机目录，适合开发期；正式分发改 git / http
（二进制要按目标平台分别构建）。

改完插件**必须重新装载**才生效；改的是包内文件也要确认来源是否会被重新拉取。
验证顺序：先看工具是否出现在可用工具清单里 → 再单独调一次 → 最后才跑完整流程。
