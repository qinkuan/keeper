# 打包成 macOS 可安装应用

把 keeper 打成一个能双击安装的 `.app` / `.dmg`：目标机**不需要装 Node，也不需要装 Python**。

```bash
./packaging/build_macos.sh      # 全流程（前端 → 冻结内核 → 出 dmg）
```

产物：

| 路径 | 是什么 |
|---|---|
| `desktop/release/Keeper-<版本>-arm64.dmg` | 分发用：双击 → 拖进「应用程序」 |
| `desktop/release/mac-arm64/Keeper.app` | 未压缩的 app，可直接 `open` 调试 |

## 一、环境是怎么被打进去的

| 运行时 | 怎么办 | 为什么 |
|---|---|---|
| **Node** | 不用管 —— Electron 自带 Node 运行时 | Electron = Chromium + Node，「打包 node 环境」这件事天然就完成了 |
| **Python** | PyInstaller 冻成自包含可执行文件 | `.venv` 里记的是基座解释器的**绝对路径**（本机是 `/opt/anaconda3`），拷到别的机器上 `bin/python` 直接失效；而自带解释器的发行版（python-build-standalone）要从 GitHub 下载。PyInstaller 把解释器本体和全部依赖（langchain / fastapi / greenlet / pypdfium2 …）一起打进产物，目标机什么都不用装 |
| 前端 `web/dist` | 冻进内核 bundle（`_internal/keeper/web/dist`） | 内核自己把它挂成站点根，页面与 API 同源，不需要 CORS、不需要额外静态服务器 |
| 配置 `config.yaml` | 打成 `config.default.yaml`，首次运行复制到 `~/.keeper/config.yaml` | 用户数据（数据库 / 设置 / 插件 / 核心 agent）一律留在 home，卸载 app 不带走数据、换机器也不用重配 |

安装后的结构：

```
Keeper.app/Contents/Resources/
  app.asar              # Electron 主进程（窗口 / IPC / sidecar）
  kernel/
    keeper-kernel       # 冻出来的内核可执行文件（自带 Python）
    _internal/          # 依赖 + web/dist + config.yaml
  config.default.yaml   # 首次运行生成 ~/.keeper/config.yaml 的模板
```

## 二、几个文件各管什么

| 文件 | 职责 |
|---|---|
| `packaging/build_macos.sh` | 一键流程：构建前端 → 跑 PyInstaller → 跑 electron-builder |
| `packaging/keeper-kernel.spec` | 冻结规格：包路径、hiddenimports（provider 集成是运行时动态 import，不点名会炸）、排除掉的胖包 |
| `packaging/launcher.py` | 冻结入口（仓库根**就是** `keeper` 包，`import keeper.x` 要的是它的父目录，spec 里用 `pathex` 解决） |
| `desktop/electron-builder.yml` | 打包配置：appId、extraResources（内核 + 配置模板）、dmg 布局 |
| `desktop/sidecar.js` | 运行时找内核：打包形态找 `Resources/kernel/keeper-kernel`，开发形态找仓库 `.venv` |

## 三、签名与公证

没有开发者证书时产物**未签名**：本机正常打开；给别人用第一次要右键→打开（或
`xattr -dr com.apple.quarantine /Applications/Keeper.app`）。

有 Apple Developer ID 证书时：

```bash
export CSC_LINK="$(base64 -i cert.p12)"     # 或指向 .p12 文件
export CSC_KEY_PASSWORD="你的密码"
./packaging/build_macos.sh
```

electron-builder 会自动签名 + 公证。**注意**：内核那个 `keeper-kernel` 可执行文件
与它旁边的 dylib 也要签 —— electron-builder 只签 app 内部的东西；不签的话公证
过不去。签名顺序：先签 `_internal` 里的 dylib/so，再签 `keeper-kernel`，最后签 `.app`。

## 四、内置运行时：用户机器上没装 Python / Node 也能干活

打包给普通用户时，对方电脑上大概率**既没 Python 也没 Node**。keeper 自己带了两套
运行时，并且都做成了「命令名」，塞进内核进程的 PATH 最前：

| 命令 | 打包形态下指向 | 说明 |
|---|---|---|
| `python3` / `python` | `keeper-kernel --py` | **同一个冻结产物**兼当解释器：解释器本体与 numpy 之类的依赖都在 bundle 里，不用再塞一份 CPython 进安装包（+0MB） |
| `node` | Electron 自带的 node（`ELECTRON_RUN_AS_NODE=1`） | Electron = Chromium + Node，那个 node 本来就在 app 里（Node 20.x），改个环境变量就能当独立 node 用 |

实现要点：

- `packaging/launcher.py`：冻结入口支持 `--py`（`script.py` / `-c CODE` / `-m MODULE` /
  `--version`）。冻结产物**不认 PYTHONPATH**，所以脚本所在目录会被手动加进 `sys.path`
  ——沙箱生成的 `main.py` 靠这个 import 到同目录的 `keeper_tools.py`。
- `desktop/sidecar.js`：启动时在 `~/.keeper/runtime/bin` 生成 `python3`/`python`/`node`
  三个 shim，并把这个目录提到内核进程 PATH 最前（顺带修掉 macOS GUI app 的 PATH 只有
  `/usr/bin:/bin` 的坑：Homebrew 的目录也补回去了）。
- `tools/subprocess_tool.py`：插件 bin 的 `#!/usr/bin/env python3` 要命中**带依赖**的
  解释器，所以优先用 `KEEPER_PYTHON` 所在的目录提 PATH（冻结形态下 `sys.executable`
  是内核自己，不是解释器）。
- `agent/ptc_sandbox.py`：冻结形态下**不建 venv**（内置解释器没法 `-m venv`，那等于把
  内核再跑一遍），直接用它跑代码。代价：沙箱里没有独立 site-packages —— numpy 这类
  bundle 里有的库能用，pandas（要 pip 装）就没有。
- `framework/runtime.py`：解释器探测的候选里加上内置运行时（`KEEPER_PYTHON` /
  `KEEPER_NODE`），所以「环境」接口能报出内置的那份。

另外：**内置插件库也要跟着装**。内核读插件的位置是 `config.yaml` 的 `plugins.root`
（默认 `~/.keeper/plugins`），不在代码目录里 —— 新机器上那个目录是空的，bash 插件
（agent 唯一的执行入口）也就没有。所以 app 里带一份 `plugins/`，首次运行时复制到
`~/.keeper/plugins`（**只补缺的**，用户装过/改过的不动）。

实测（打包产物，`/tmp` 隔离 HOME）：

```
bash 插件 → python3 solve.py   → sum = 31 | mean = 3.88
bash 插件 → node hello.js       → v20.16.0
代码沙箱（子 agent 写代码）      → 沙箱内 python 可用 / numpy 2.5.4
```

## 五、已知限制

1. **`python -m keeper.evals.run`**（评测跑批）在冻结形态下不可用：走子进程
   `sys.executable -m`，冻结后没有 `-m`。
2. **`npm` / `npx` 没有**：Electron 自带 node 但不带 npm。要跑 npm 生态的插件
   （`npx xxx` 形式的 MCP server）仍需用户自己装 node。
3. **沙箱里没有独立 site-packages**：冻结形态不建 venv，pandas 之类要 pip 装的库
   用不了（numpy 这类 bundle 里有的没问题）。
4. **单实例锁与开发态共享**：打包 app 与 `desktop/start_desktop.sh` 的应用名相同 →
   同一个 userData 目录 → 同一个锁。开发 app 在跑时，打包 app 会提示「keeper 已在运行」
   并退出（这是**故意的**：两个内核会抢同一个 `~/.keeper` 数据库）。
5. **只构建了 arm64**（Apple Silicon）。要给 Intel Mac 用，得在 Intel 机器上再构建
   一次（PyInstaller 不做 universal binary）。
6. **图标还是 Electron 默认的**：`desktop/electron-builder.yml` 里没设 `mac.icon`，
   把图标放进 `desktop/build/icon.icns` 并在配置里指一下即可。

## 五、改代码后怎么重新出包

```bash
# 只改了前端
cd web && npm run build && cd .. && ./packaging/build_macos.sh --skip-web

# 只改了 Electron 壳
./packaging/build_macos.sh --skip-web --skip-kernel

# 改了 Python 内核（必须重新冻结）
./packaging/build_macos.sh --skip-web
```

`--skip-kernel` 只在**没改 Python 代码**时用。

## 六、实跑记录（2026-10-26，本机 arm64）

```
==> [1/3] 前端产物 web/dist
==> [2/3] 冻结 Python 内核（PyInstaller）     # 6.22.3 / Python 3.13.5
==> [3/3] electron-builder 出 .app / .dmg
```

* dmg：156MB；app：382MB（Electron ~230MB + 内核 149MB）
* 冻结内核单跑：`/health` 返回 `{"ok":true}`，`/` 正常吐出前端 HTML
* 打包 app 起来后：Electron 主进程 → 拉起 `Resources/kernel/keeper-kernel` →
  页面资源与 API（/agents、/manage/settings、/health）全部 200 → 窗口标题「Keeper」
