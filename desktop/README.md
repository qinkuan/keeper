# keeper 桌面壳（阶段 0）

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
  读写 `~/.keeper`。

## 还没做（阶段 0 之外）

- **打包成 .app / .exe**（electron-builder）——现在依赖系统里的 Python 和
  `keeper/.venv`，还不是可分发的单文件应用。要做的话下一步是 PyInstaller 把内核
  打进 `resources/`，再加 `electron-builder` 配置。
- 应用图标（现在用 Electron 默认图标）
- 托盘、开机自启、自动更新

## 排错

| 现象 | 先看 |
|---|---|
| 启动报"内核 xx ms 内未就绪" | 单独跑 `cd keeper && ./start_keeper.sh` 确认内核本身能起 |
| 窗口是空白 / 404 | 有没有 `web/dist`（`start_desktop.sh` 会自动构建；手动跑见 `../web/build_web.sh`） |
| 端口相关异常 | 主进程日志里的 `内核就绪：http://127.0.0.1:<端口>` |
| 关窗后进程还在 / 下次启动撞数据库 | `./stop_desktop.sh`（会带走孤儿内核）；再用 `ps -A -o pid=,ppid=,command=` 看有没有 PPID 为 1 的 `keeper.main` |
| 启动时报"已在运行" | 上次的 `desktop.pid` 或残留窗口进程，先 `./stop_desktop.sh` |
