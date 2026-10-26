/**
 * Python sidecar 管理：起 keeper 内核、等它就绪、退出时收干净。
 *
 * 为什么是 sidecar 而不是把内核搬进 Node
 * --------------------------------
 * keeper 的不可替代资产都在 Python 侧：jieba 中文分词（FTS5 检索依赖它）、
 * ReAct 解析与幻觉检测、工具沙箱（子进程 bin 协议）、A2A 实现、任务系统、
 * 可观测埋点。把这些重写成 Node 要付出极高代价，而换来的只是"少一跳进程间
 * 通信"——这一跳在本地回环上是毫秒级，相对 LLM 调用的秒级延迟可以忽略。
 *
 * 于是形态定为：**Electron 只做壳，内核仍是一个 Python 进程**，两者用本机
 * HTTP 通信。这也是 LM Studio / Ollama / AnythingLLM 走的路。
 *
 * 关键设计
 * --------
 * 1. **动态端口**：先用 Node 占一个空闲端口再交给 Python，避免"8080 被别的
 *    keeper 占了"这类冲突（同一台机器上可能已经有人手动 ./start_keeper.sh 起过）。
 * 2. **等就绪再开窗**：Python 要建表、装载所有 agent，慢的时候几十秒。直接
 *    loadURL 会先白屏再刷新，所以轮询健康接口，通了才开窗。
 * 3. **退出收干净**：走 before-quit 主动终止，否则关窗后 Python 变孤儿进程，
 *    下次启动又会撞数据库。
 */
const { spawn } = require('child_process');
const net = require('net');
const os = require('os');
const path = require('path');
const fs = require('fs');
const http = require('http');
const { app } = require('electron');

const KEEPER_DIR = path.resolve(__dirname, '..');
const PROJECT_ROOT = path.resolve(KEEPER_DIR, '..');

/** 当前 sidecar 状态；模块级单例——一个桌面实例只需要一个内核 */
let state = null;
/**
 * 启动中的 Promise。
 *
 * 为什么需要它：内核就绪要几十秒（建表 + 装载 agent），而 `state` 只有等
 * /health 通了才赋值。这段时间里如果有人再调 startSidecar（macOS 的 activate
 * 事件会这么干），只靠 `if (state) return` 挡不住——state 还是 null，于是又
 * spawn 一个 Python。实测一次启动起了 3 个内核、开了 3 个窗口，就是这么来的。
 * 所以「启动中」也要能被后来者复用。
 */
let pending = null;

/**
 * 内置插件库：首次运行时把 app 里带的插件复制到 ``~/.keeper/plugins``。
 *
 * 为什么必须做：内核读插件的位置是 config.yaml 的 ``plugins.root``（默认
 * ``~/.keeper/plugins``），而**不在**代码目录里。新装的机器上那个目录是空的
 * —— bash 插件（agent 唯一的执行入口）也就没有，agent 连一条命令都跑不了。
 *
 * 只补「缺的那个」：用户装过/改过的插件一律不动（app 升级不覆盖用户数据）。
 */
function seedPlugins() {
  if (!app.isPackaged) return;
  const src = path.join(process.resourcesPath, 'plugins');
  if (!fs.existsSync(src)) return;
  const dest = path.join(os.homedir(), '.keeper', 'plugins');
  fs.mkdirSync(dest, { recursive: true });
  for (const name of fs.readdirSync(src)) {
    const from = path.join(src, name);
    if (!fs.statSync(from).isDirectory()) continue;
    const to = path.join(dest, name);
    if (fs.existsSync(to)) continue; // 用户已有 → 不覆盖
    fs.cpSync(from, to, { recursive: true, dereference: false });
    console.log('[desktop] 已安装内置插件 ' + name);
  }
}

/**
 * 给 agent 准备一套「能用的 python3 / node」。
 *
 * 打包形态下用户电脑里可能**什么都没装**，但 keeper 自己带了两套运行时：
 *   - Python：冻结内核兼当解释器（``keeper-kernel --py script.py``），
 *     解释器本体和 numpy 之类的依赖都在 bundle 里；
 *   - Node：Electron 自带的那个（``ELECTRON_RUN_AS_NODE=1`` 跑 Electron 可执行
 *     文件就是一个完整的 node）。
 *
 * 这里给它们各生成一个 shim 放进 ``~/.keeper/runtime/bin``，再把这个目录
 * 提到内核进程的 PATH 最前 —— 于是
 *   - 插件 bin 的 ``#!/usr/bin/env python3`` 命中带依赖的解释器；
 *   - 模型写的 ``python3 x.py`` / ``node x.js`` 直接能跑；
 *   - 代码沙箱不用再依赖系统 python3。
 *
 * 顺带修一个 macOS 的坑：从 Finder 启动的 GUI app，``process.env.PATH`` 典型只有
 * ``/usr/bin:/bin:/usr/sbin:/sbin`` —— 用户装了 Homebrew 的 python/node 也找不到。
 * 所以这里把常见的几个目录补回去。
 */
function prepareRuntimeBin(kernel) {
  const dir = path.join(os.homedir(), '.keeper', 'runtime', 'bin');
  fs.mkdirSync(dir, { recursive: true });

  const write = (name, body) => {
    const p = path.join(dir, name);
    fs.writeFileSync(p, body, { mode: 0o755 });
    try {
      fs.chmodSync(p, 0o755);
    } catch (_) {}
    return p;
  };

  // python3 → 冻结内核的 --py 模式（打包形态）；开发形态就是 .venv 的 python3
  let python3;
  if (kernel.pythonRunner) {
    const runner = kernel.pythonRunner;
    python3 = write(
      'python3',
      `#!/bin/sh\n# keeper 内置的 Python（冻结内核兼当解释器：解释器与依赖都在 app 里）\nexec ${JSON.stringify(runner)} --py "$@"\n`
    );
  } else {
    // 开发形态：.venv 的 python3（也做成 shim，runtime 目录内容保持一致）
    python3 = write(
      'python3',
      `#!/bin/sh\nexec ${JSON.stringify(kernel.command)} "$@"\n`
    );
  }
  write(
    'python',
    `#!/bin/sh\nexec ${JSON.stringify(python3)} "$@"\n`
  );

  // node → Electron 自带的 node（ELECTRON_RUN_AS_NODE=1）
  const node = write(
    'node',
    `#!/bin/sh\n# keeper 内置的 Node（Electron 自带的运行时）\nexport ELECTRON_RUN_AS_NODE=1\nexec ${JSON.stringify(process.execPath)} "$@"\n`
  );

  return { dir, python3, node };
}

/**
 * 内核进程的 PATH：内置运行时目录 + 用户的常见路径 + 原有 PATH。
 */
function runtimePath(runtimeDir) {
  const extra = [
    runtimeDir,
    '/usr/local/bin',
    '/opt/homebrew/bin',
    '/usr/bin',
    '/bin',
    '/usr/sbin',
    '/sbin',
  ];
  const seen = new Set();
  const parts = [];
  for (const p of extra.concat((process.env.PATH || '').split(path.delimiter))) {
    if (!p || seen.has(p)) continue;
    seen.add(p);
    parts.push(p);
  }
  return parts.join(path.delimiter);
}

/**
 * 内核可执行文件：两种形态，两种找法。
 *
 * - **打包形态**（app.isPackaged）：内核是 PyInstaller 冻出来的自包含可执行文件
 *   （Resources/kernel/keeper-kernel），自带 Python 运行时——目标机不需要装
 *   Python，也不需要有源码。
 * - **开发形态**：仓库里的 .venv，跑 `python -m keeper.main`。
 */
function resolveKernel() {
  if (app.isPackaged) {
    const dir = path.join(process.resourcesPath, 'kernel');
    const exe = path.join(dir, 'keeper-kernel');
    if (!fs.existsSync(exe)) {
      throw new Error(`打包产物里没有内核可执行文件：${exe}`);
    }
    // 前端产物冻在 bundle 内（<kernel>/_internal/keeper/web/dist）；
    // 也允许桌面壳用外挂资源指路（KEEPER_WEB_DIST）。
    const webDist = path.join(dir, '_internal', 'keeper', 'web', 'dist');
    return {
      command: exe,
      args: [],
      cwd: dir,
      webDist: fs.existsSync(webDist) ? webDist : null,
      // 同一个冻结产物兼当 Python 解释器（--py 模式）
      pythonRunner: exe,
    };
  }
  const python = findPython();
  return {
    command: python,
    args: ['-m', 'keeper.main'],
    cwd: PROJECT_ROOT,
    webDist: null,
    pythonRunner: null,
  };
}

/**
 * 框架配置（config.yaml）：打包形态下从模板生成到 ~/.keeper/。
 *
 * 为什么不是把仓库里的 config.yaml 直接塞进包里：那是**开发态**的文件，
 * 路径都指向开发机；而用户数据（数据库、settings.yaml、插件、核心 agent）
 * 一律在 ~/.keeper —— 配置也该跟着走那儿，卸载 app 不该带走数据、换台机器
 * 也不该重新配。
 */
function resolveConfig() {
  if (process.env.KEEPER_CONFIG) return process.env.KEEPER_CONFIG;
  if (!app.isPackaged) return path.join(KEEPER_DIR, 'config.yaml');

  const home = path.join(os.homedir(), '.keeper');
  const cfg = path.join(home, 'config.yaml');
  if (!fs.existsSync(cfg)) {
    const tpl = path.join(process.resourcesPath, 'config.default.yaml');
    if (!fs.existsSync(tpl)) {
      throw new Error(`缺少配置模板：${tpl}`);
    }
    fs.mkdirSync(home, { recursive: true });
    fs.copyFileSync(tpl, cfg);
    console.log('[desktop] 首次运行：已生成配置 ' + cfg);
  }
  return cfg;
}

function findPython() {
  const candidates = [
    path.join(KEEPER_DIR, '.venv', 'bin', 'python'),
    path.join(PROJECT_ROOT, '.venv', 'bin', 'python'),
  ];
  for (const p of candidates) {
    if (fs.existsSync(p)) return p;
  }
  return 'python3';
}

/** 占一个空闲端口拿到端口号，再立刻释放给它用（极小的时间窗，可接受） */
function findFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

/** 轮询健康接口，直到内核真的能应答 */
function waitHealthy(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get(
        { host: '127.0.0.1', port, path: '/health', timeout: 1500 },
        (res) => {
          res.resume();
          // 4xx 也算"活着"：说明 HTTP 栈已经起来了
          if (res.statusCode && res.statusCode < 500) return resolve();
          retry();
        }
      );
      req.on('error', retry);
      req.on('timeout', () => {
        req.destroy();
        retry();
      });
      function retry() {
        if (Date.now() > deadline) {
          return reject(new Error(`内核 ${timeoutMs}ms 内未就绪`));
        }
        setTimeout(tick, 400);
      }
    };
    tick();
  });
}

async function startSidecar({ onLog } = {}) {
  if (state) return state;
  if (pending) return pending;

  pending = (async () => {
    const port = await findFreePort();
    const kernel = resolveKernel();
    const config = resolveConfig();
    // 内置插件库（bash 插件是 agent 唯一的执行入口）
    try {
      seedPlugins();
    } catch (e) {
      console.error('[desktop] 安装内置插件失败：' + (e && e.message ? e.message : e));
    }
    // 给 agent 备一套能用的 python3 / node（用户机器上可能什么都没装）
    const runtime = prepareRuntimeBin(kernel);

    const child = spawn(
      kernel.command,
      [...kernel.args, '--config', config, String(port)],
      {
        cwd: kernel.cwd,
        env: {
          ...process.env,
          // 内置运行时目录提到最前：插件 bin 的 #!/usr/bin/env python3、
          // 模型写的 python3 x.py / node x.js 都靠它解析
          PATH: runtimePath(runtime.dir),
          KEEPER_PYTHON: runtime.python3,
          KEEPER_NODE: runtime.node,
          PORT: String(port),
          // 配置指路：内核里那些不带参数的 load_config()（核心 agent 装配等）
          // 也要拿到同一份配置，否则会用错、甚至去写只读的 app 包。
          KEEPER_CONFIG: config,
          // 前端产物指路：打包形态下 web/dist 冻在 bundle 里（main.py 自己能
          // 找到），这里额外指一次是为了让「外挂资源」这种放法也能用。
          ...(kernel.webDist ? { KEEPER_WEB_DIST: kernel.webDist } : {}),
          /*
           * 内核日志落盘。stdio 是 pipe，全靠 onLog 转给窗口上的日志面板——而面板
           * 只在窗口活着时才有。浏览器僵死恰恰是最需要日志的时候，那次排查就是因为
           * 日志只在面板里、什么都留不下，最后只能靠"进程还在不在"反推。
           *
           * main.py 里有这个开关（RotatingFileHandler，10MB × 5 份），命令行形态
           * 一直可用；桌面壳之前没设，等于白放着。
           */
          LOG_FILE: process.env.KEEPER_LOG_FILE
            || path.join(os.homedir(), '.keeper', 'logs', 'kernel.log'),
        },
        stdio: ['ignore', 'pipe', 'pipe'],
      }
    );

    const log = (chunk) => {
      if (!onLog) return;
      String(chunk)
        .trimEnd()
        .split('\n')
        .forEach((line) => onLog(line));
    };
    child.stdout.on('data', log);
    child.stderr.on('data', log);
    child.on('exit', (code, sig) => {
      log(`内核退出（code=${code} signal=${sig}）`);
      state = null;
    });
    child.on('error', (err) => log(`内核启动失败：${err.message}`));

    await waitHealthy(port, 120000);

    state = { child, port, url: `http://127.0.0.1:${port}` };
    return state;
  })();

  try {
    return await pending;
  } finally {
    pending = null;
  }
}

function stopSidecar() {
  if (!state) return;
  pending = null;
  const { child } = state;
  state = null;
  if (!child || child.exitCode !== null) return;
  // 先给优雅退出的机会（uvicorn 会关 MCP 会话），超时再强杀
  child.kill('SIGTERM');
  const timer = setTimeout(() => {
    if (child.exitCode === null) child.kill('SIGKILL');
  }, 5000);
  timer.unref?.();
}

module.exports = { startSidecar, stopSidecar };
