# keeper

> **本地 agent 运行时**：把平台下发的 agent 定义装配成能跑的实例，执行对话，
> 并把过程记录下来（用量、耗时、产物、评测）。

平台（keeperplatform）管**定义与市场**，keeper 管**运行**。会话、任务、产物、
可观测数据全部留在本地。

完整设计见 [`doc/architecture.md`](doc/architecture.md)。

---

## 目录结构

```
keeper/
├── main.py            启动入口：建库、装配实例、挂路由、托管前端
├── config.py          元配置（所有代码默认值都在这里）
│
├── agent/             内核：装配（keeper.py）+ 执行（executor/process/planner）
├── chat/              会话：对话、步骤落库、产物、工作区
├── tools/             工具：注册表、内置工具、子进程工具、安全守卫
├── skill/             声明式 Skill（数据描述，非代码）
├── llm/               模型工厂与调用
├── mcp/               MCP 多 server 连接
├── memory/            记忆：短期记忆 + jieba/FTS5 检索 + 召回工具
├── peer/  a2a/        对外协作（我→对端 / 对端→我）
│
├── store/             存储：SQLite + ORM 模型
├── setting/           用户设置读写（~/.keeper/settings.yaml）
├── plat/              平台对接：拉配置 / 落库 / 下载资源
├── plugin/            插件库：扫描、清单解析、每-agent 链接
├── observability/     可观测：记账 / 时间线 / 统计 / 定价 / 额度 / prompt 落盘
├── framework/         本机运行时检测（python / node）
├── evals/             评测闭环
├── api/               管理接口（/manage/*）与可观测接口（/metrics/*）
│
├── web/               前端（React，构建产物由 keeper 同源托管）
├── tests/             回归脚本（直接 python 跑，不引测试框架）
└── doc/               设计文档
```

---

## 快速开始

```bash
# 依赖装在 keeper 目录的 .venv（脚本会自动创建）
./start_keeper.sh                 # 前台运行，默认 :8080

PORT=9090 ./start_keeper.sh       # 换端口
LOG_LEVEL=DEBUG ./start_keeper.sh # 更详细的日志
```

脚本做的事：建 `.venv` → 装依赖（仅首次或 `requirements.txt` 变化时）→ 启动。
按 `Ctrl+C` 停止（会一并关闭 MCP 会话）。

也可以直接跑：

```bash
python -m keeper.main --config keeper/config.yaml 8080
```

启动后访问 `http://localhost:8080`。

---

## 配置：两层

| 文件 | 谁改 | 生效方式 |
|---|---|---|
| `keeper/config.yaml` | **人手动改** | 重启 |
| `~/.keeper/settings.yaml` | 设置页写入 | **立即生效**，不需重启 |

`config.yaml` 的段：`server` / `agent` / `platform` / `llm` / `workspace` / `agent_overrides`。

`settings.yaml` 只放「用的时候调」的三段：`observability` / `a2a` / `context`。
读取时与代码默认值合并，**缺项也能跑**。

---

## 数据目录

```
~/.keeper/
├── agents/<id>/plugins/    该 agent 的插件（本机插件是链接，平台下载的为副本）
├── plugins/                插件库
├── workspace/user/<space>/ 用户工作区（default / 我的仓库 …）
├── sessions/               会话记忆
├── memory/                 记忆索引
├── evals/                  评测产物（基线 / 运行记录）
├── prompt-dump/            prompt 落盘（调试，默认关）
├── context-store/          上下文外部化的块
├── settings.yaml           用户设置
```

数据库：`keeper/data/keeper.db`（SQLite）。

---

## HTTP 接口

| 前缀 | 内容 |
|---|---|
| `/manage/*` | 管理：agent 装载/卸载、插件、模型、设置 |
| `/agents/{agent_id}/*` | 会话：对话、能力开关、工作区文件、时间线 |
| `/metrics/*` | 可观测：用量、耗时、错误、提问率、重复调用 |
| `/user-spaces/*` | 用户工作区 |
| `/framework/*` | 本机运行时检测 |
| `/evals/*` | 评测 |
| `/a2a`、`/.well-known/agent.json` | A2A 协议面 |

前端与 API **同源**，不需要 CORS。

---

## 插件

插件是「一个包 + 一份 `keeper-plugin.json`」，统一承载三类组件：

| 组件 | 形态 | 说明 |
|---|---|---|
| `mcpServers` | MCP | 常驻子进程，有跨调用状态（如索引缓存） |
| `bin` | 可执行文件 | 一次性进程，语言无关，stdin/stdout JSON 协议 |
| `skill` / `skills` | SKILL.md | 纯知识，只注入 prompt（如 `plugin-authoring`） |

清单里的路径用 `${PLUGIN_ROOT}` / `${AGENT_SPACE}` 变量，**不要硬编码绝对路径**。

插件走**子进程**而不是 import：第三方代码进主进程等于把 keeper 的全部权限交给它。

---

## 回归测试

能直接 `python` 跑完出结论的脚本，不引测试框架：

```bash
python keeper/tests/test_plugin_lib.py      # 插件库：清单解析、链接增删、幂等
python keeper/tests/test_guard.py           # 安全守卫：白名单放行、绕过拦截
python keeper/tests/test_planner_parse.py   # ReAct 文本协议解析
```

---

## 部署

```bash
./deploy.sh        # 打服务端包 → dist/keeper-server-*.zip
                   # 排除 data / web / dist / 日志 / 测试
cd web && ./deploy.sh   # 构建前端 → dist/keeper-web-*.zip
```

服务端包解压后目录结构不变，`start_keeper.sh` 可直接用。
在宝塔 / systemd 下用**前台模式**（进程管理器自己保活），不要加 `--daemon`。

**日志**默认只到控制台；设 `LOG_FILE` 会额外落一份（Rotating，默认 10MB × 5 份）。

装配期会打印每个插件、每个工具的装配结果与跳过原因——
查「工具为什么没出现」先看这里。

---

## 文档

全部在 [`doc/`](doc/)：

| 想了解 | 看 |
|---|---|
| **全貌** | [`architecture.md`](doc/architecture.md) |
| 产品定位 | `positioning.md` |
| 会话存储 | `session-design.md` |
| 任务模式 | `task-design.md` |
| 能力按需加载 | `capability-loading.md` |
| 上下文治理（外部化 + 压缩） | `context.md` |
| 记忆与检索 | `memory.md` |
| 产物 | `artifact-design.md` |
| 跨端协作 | `collaboration-contract.md` |
| 可观测 | `observability-design.md` |
| 评测 | `evals.md` |
| 未完成事项 / 技术债 | `roadmap.md` |
