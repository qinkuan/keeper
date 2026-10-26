# keeper 桌面壳

Electron 只做**窗口和进程托管**，业务逻辑一个都不碰。

页面是 keeper 内核自带的（内核把 `web/dist` 作为站点根同源托管），所以这里只要
等内核就绪、把窗口指过去 —— **前端代码零改动**。桌面版和命令行版跑的是同一套
东西：数据目录（`~/.keeper`）、数据库、配置完全一致，切换形态没有迁移成本。

## 跑起来

```bash
cd keeper/desktop

./start_desktop.sh   # 前台运行：Ctrl+C 停止
./stop_desktop.sh    # 停止（窗口被强杀后残留的内核也一并收掉）
```

启动脚本会按顺序做：装 Electron（首次）→ 检查/构建 `web/dist`（`SKIP_BUILD=1` 可跳过）
→ 拉起 Electron → 由 Electron 拉起 Python 内核并等它就绪。

不用脚本的话等价命令是：

```bash
cd ../web && ./build_web.sh     # 1) 先构建前端产物（没有 dist 就只能拿到 API，页面 404）
cd ../desktop && npm install    # 2) 装 Electron 依赖（首次）
npm start                       # 3) 启动
```

启动流程：主进程占一个空闲端口 → 拉起 Python 内核 → 轮询 `/health` 直到就绪 →
开窗加载 `http://127.0.0.1:<动态端口>`。

## 「我的空间」走桌面原生

「我的空间」的文件操作**不走 Python HTTP 端点**，走 Electron 的原生能力：

| 能力 | 落点 |
|---|---|
| 列目录 / 读 / 写 / 重命名 / 删除 / 复制 | `spaceFs.js`（Node `fs`） |
| 系统文件对话框（选目录 / 选文件） | `dialog`（`main.js`） |
| 访达 / 资源管理器定位文件 | `shell.showItemInFolder` |
| 系统剪贴板 | `clipboard` |
| 前端调用通道 | `preload.js` 暴露的 `window.keeperSpace` |

理由：这些操作**只有桌面端做得到**（系统对话框、访达、剪贴板），
放在 Python 侧既做不出来、也违背「桌面专属」的形态定位。

路径安全由 `spaceFs.js` 的 `resolveSpacePath` 兜住：所有路径都必须在
「我的空间」根之下，越界直接拒绝。

## 窗口：macOS 隐藏标题栏，其他平台保留

| 平台 | 窗口形态 |
|---|---|
| **macOS** | `titleBarStyle: 'hidden'`——**没有系统标题栏**，红绿灯浮在页面左上角（`trafficLightPosition: {x:12, y:12}`） |
| **Windows / Linux** | 保留系统标题栏，标题文字缩短为 `Keeper` |

前端配套（`web/src/App.tsx`）：

- macOS 下渲染一条横跨整窗的**自绘拖拽条**（`.kp-dragbar`，`-webkit-app-region: drag`），
  侧栏与内容区都从它下面开始；纯 Web 版没有红绿灯，所以不渲染这条。
- **双击标题栏最大化 / 还原**由主进程代劳（`keeper:toggle-maximize`）：
  drag 区域的指针事件不会派发给渲染进程，前端收不到 `dblclick`。

为什么 Windows / Linux 不动原生标题栏：Electron 30 的 `titleBarOverlay` 能把系统按钮
浮在右上角，但那是**盖在内容上**的——知识库、设置、我的空间这些页面右上角都有控件，
会被压住。为这点观感去改一堆页面的布局不划算。

## 为什么是 sidecar 而不是搬进 Node

keeper 的不可替代资产都在 Python 侧：jieba 中文分词（FTS5 检索依赖它）、ReAct
解析与幻觉检测、工具沙箱（子进程 bin 协议）、A2A 实现、任务系统、可观测埋点。
重写成 Node 要付出极高代价，换来的只是"少一跳进程间通信"——本地回环是毫秒级，
相对 LLM 调用的秒级延迟可以忽略。

这条路 LM Studio / Ollama / AnythingLLM 都走过，是标准做法。

## 几个刻意的设计

- **动态端口**：先占一个空闲端口再交给 Python，避免和"你已经手动
  `./start_keeper.sh` 起过的那个 8080"撞车。
- **等就绪再开窗**：Python 要建表、装载所有 agent，慢的时候几十秒。不等就先白屏
  再刷新。
- **单实例 + 启动去重**：`requestSingleInstanceLock()` 挡住第二个 Electron；
  内核启动被收敛成一个 Promise，因为就绪前 `state` 还是 null，而 macOS 的
  `activate` 在这段时间里会反复触发——不去重的话一次启动会 spawn 多个 Python、
  开出多个窗口（都抢同一个 `~/.keeper` 数据库）。
- **退出必须收干净**：`before-quit` 里 SIGTERM（给 uvicorn 优雅关闭 MCP 的机会），
  5 秒后仍不退再 SIGKILL。漏掉这步的话，关窗后 Python 变孤儿进程，下次启动撞
  数据库。
- **不给页面 node 权限**：`nodeIntegration: false`。第三方插件的 Web 内容不该能
  读写 `~/.keeper`；原生能力只经 preload 白名单暴露。

## 还没做

| 项 | 说明 |
|---|---|
| **打包成 .app / .exe** | 现在依赖系统里的 Python 和 `keeper/.venv`，还不是可分发的单文件应用。下一步是 PyInstaller 把内核打进 `resources/`，再加 `electron-builder` 配置 |
| **应用图标** | 现在用 Electron 默认图标 |
| **托盘** | 常驻菜单（最小化到托盘、开机项入口、退出） |
| **开机自启** | 登录后自动拉起 |
| **自动更新** | 版本检查与替换 |
| **窗口状态记忆** | 尺寸/位置不跨启动保留 |
| **多窗口** | 目前只开一个窗口；多实例由 `requestSingleInstanceLock` 挡住 |

## 排错

| 现象 | 先看 |
|---|---|
| 启动报"内核 xx ms 内未就绪" | 单独跑 `cd keeper && ./start_keeper.sh` 确认内核本身能起 |
| 窗口是空白 / 404 | 有没有 `web/dist`（`start_desktop.sh` 会自动构建；手动跑见 `../web/build_web.sh`） |
| 端口相关异常 | 主进程日志里的 `内核就绪：http://127.0.0.1:<端口>` |
| 关窗后进程还在 / 下次启动撞数据库 | `./stop_desktop.sh`（会带走孤儿内核）；再用 `ps -A -o pid=,ppid=,command=` 看有没有 PPID 为 1 的 `keeper.main` |
| 启动时报"已在运行" | 上次的 `desktop.pid` 或残留窗口进程，先 `./stop_desktop.sh` |
| 「我的空间」操作报错 | 看 `spaceFs.js` 的路径校验——越界的路径会被直接拒绝 |
