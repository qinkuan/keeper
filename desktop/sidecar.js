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
const { installBuiltinPlugins } = require('./seedPlugins');

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
 * 内置插件库：把 app 里带的插件装到 ``~/.keeper/plugins``。
 *
 * 为什么必须做：内核读插件的位置是 config.yaml 的 ``plugins.root``（默认
 * ``~/.keeper/plugins``），而**不在**代码目录里。新装的机器上那个目录是空的
 * —— bash 插件（agent 唯一的执行入口）也就没有，agent 连一条命令都跑不了。
 *
 * 安装 / 升级策略（逐文件比对哈希、用户改过的保留不动）见
 * ``desktop/seedPlugins.js`` 的文件头注释。实现放在那一个文件里，是为了能
 * 被 node 直接跑测试——这里只负责算路径再调进去。
 */
function seedPlugins() {
  const dest = path.join(os.homedir(), '.keeper', 'plugins');
  if (app.isPackaged) {
    // 打包形态：插件在 app 的 resources 里
    const src = path.join(process.resourcesPath, 'plugins');
    if (!fs.existsSync(src)) return;
    installBuiltinPlugins(src, dest);
    return;
  }
  // 开发形态：插件源码在仓库的 plugins/ 下。**同样要同步**——不然改了插件源码
  // 桌面版还在用 ~/.keeper/plugins 里的旧副本，改动看不到效果（曾经就是这样：
  // 仓库里修好了，用户目录里还是首装那版，只能手动删目录重装）。
  // 插件源码在 keeper/plugins/ 下（KEEPER_DIR = <repo>/keeper；
  // PROJECT_ROOT 是它的上一级，里面没有 plugins/）。
  const src = path.join(KEEPER_DIR, 'plugins');
  if (!fs.existsSync(src)) return;
  installBuiltinPlugins(src, dest);
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
  const isWin = process.platform === 'win32';
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

  /* --- python3 -------------------------------------------------------
   *
   * 打包形态：把冻结内核**复制**一份成同目录下的 python3（Windows 是
   * python3.exe）。复制件和内核挨着 → PyInstaller 的 _internal 照样找得到；
   * 冻结入口靠**自身文件名**切成解释器模式（packaging/launcher.py）。
   *
   * 为什么用「exe 副本」而不是 shell 脚本 shim：Windows 上 PATH 查找认的是
   * .exe/.cmd，而 .cmd **没法被 busybox 的 sh 执行** —— 复制成 exe 才是任何
   * shell（cmd / PowerShell / sh）都能调的。
   *
   * 开发形态：直接用仓库 .venv 的解释器（.venv/bin 里本来就有 python3）。
   */
  let python3;
  const pathEntries = [];
  if (kernel.pythonRunner) {
    // 复制件必须和内核**同目录**：PyInstaller 的 bootloader 按 exe 所在目录找
    // _internal（依赖都在那儿）。试过符号链接——不行，bootloader 会把它解析回
    // 真实路径，于是「文件名切换解释器模式」就失效了。
    const copy = path.join(kernel.dir, isWin ? 'python3.exe' : 'python3');
    try {
      fs.copyFileSync(kernel.pythonRunner, copy);
      // chmod 单独兜一层：Windows 上它只管只读位，个别环境（只读卷 / 受限
      // 目录）会抛错——但文件已经拷好了，不该因此整体退化成 .cmd shim。
      try {
        fs.chmodSync(copy, 0o755);
      } catch (_) {}
      python3 = copy;
      // 内核目录（含副本）进 PATH：python3 在任何 shell 下都能解析到
      pathEntries.push(kernel.dir);
    } catch (e) {
      // app 包只读（MDM 装的那种 /Applications/Keeper.app）→ 退回 shim 脚本。
      // 注意：Windows 上 .cmd 只有 cmd 能执行，busybox 的 sh 不行 → 所以这
      // 种情况下别用 busybox 当 shell（见下面 bundledSh 的判断）。
      console.error('[desktop] app 包只读，内置 python3 改用 shim：' + (e && e.message ? e.message : e));
      if (isWin) {
        python3 = write(
          'python3.cmd',
          `@echo off\r\n"${kernel.pythonRunner}" --py %*\r\n`
        );
      } else {
        python3 = write(
          'python3',
          `#!/bin/sh\n# keeper 内置的 Python（冻结内核兼当解释器）\nexec ${JSON.stringify(kernel.pythonRunner)} --py "$@"\n`
        );
      }
    }
  } else {
    const venvBin = path.dirname(kernel.command);
    python3 = path.join(venvBin, isWin ? 'python.exe' : 'python3');
    pathEntries.push(venvBin);
  }
  if (!python3) python3 = kernel.command;

  /* --- node ----------------------------------------------------------
   *
   * 优先用随包带的**真 node**（Windows 上带：node.exe 能被任何 shell 执行，
   * 还顺带把 npm/npx 补上）；没有就用 Electron 自带的那个
   * （ELECTRON_RUN_AS_NODE=1 跑 Electron 可执行文件就是一个完整 node）。
   */
  const bundledNode = isWin
    ? path.join(process.resourcesPath, 'runtime', 'node', 'node.exe')
    : path.join(process.resourcesPath, 'runtime', 'node', 'bin', 'node');
  let node;
  if (fs.existsSync(bundledNode)) {
    node = bundledNode;
    pathEntries.push(path.dirname(bundledNode));
  } else if (isWin) {
    // Windows 没有 /bin/sh，写 .cmd（cmd 能执行；busybox sh 不行，所以
    // Windows 上更推荐随包带真 node —— 见上）
    node = write(
      'node.cmd',
      `@echo off\r\nset ELECTRON_RUN_AS_NODE=1\r\n"${process.execPath}" %*\r\n`
    );
  } else {
    node = write(
      'node',
      `#!/bin/sh\n# keeper 内置的 Node（Electron 自带的运行时）\nexport ELECTRON_RUN_AS_NODE=1\nexec ${JSON.stringify(process.execPath)} "$@"\n`
    );
  }
  if (!fs.existsSync(bundledNode)) {
    // 说清楚退化到什么程度：Electron 自带的 node 能跑 .js/.mjs（内核按后缀或
    // shebang 选解释器时 KEEPER_NODE 指向的就是它），但**没有 npm/npx**——
    // 跑 npx 形态的 MCP server 会失败。别让它静悄悄退化。
    console.warn(
      '[desktop] 没找到随包带的 node（' + bundledNode + '）：改用 Electron 自带的 node。' +
        '能跑 .js/.mjs 脚本，但没有 npm/npx。'
    );
  }

  /* --- 内置 POSIX shell（Windows 用 busybox 的 sh.exe）---------------- */
  let bundledSh = null;
  const shCandidates = isWin
    ? [path.join(process.resourcesPath, 'runtime', 'sh', 'sh.exe')]
    : [];
  // 只有当 python3 是**真 exe**时才用 busybox 当 shell：busybox 的 sh 只能
  // 直接执行二进制，执行不了 .cmd shim（app 包只读时的退路）
  const pythonIsExe = !/\.cmd$/i.test(python3);
  if (pythonIsExe) {
    for (const c of shCandidates) {
      if (fs.existsSync(c)) {
        bundledSh = c;
        pathEntries.push(path.dirname(c));
        break;
      }
    }
  }

  return { dir, python3, node, bundledSh, pathEntries };
}

/**
 * 内核进程的 PATH：内置运行时目录 + 用户的常见路径 + 原有 PATH。
 */
function runtimePath(entries) {
  const extra = entries.concat([
    '/usr/local/bin',
    '/opt/homebrew/bin',
    '/usr/bin',
    '/bin',
    '/usr/sbin',
    '/sbin',
  ]);
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
    /* 内核可执行文件的名字**随平台变**：Windows 上 PyInstaller 产出
     * `keeper-kernel.exe`，mac/Linux 上是 `keeper-kernel`。
     * 早先这里写死了无后缀的名字，于是 Windows 包一启动就报
     * 「打包产物里没有内核可执行文件：…\resources\kernel\keeper-kernel」——
     * 目录里明明有 keeper-kernel.exe，只是差一个后缀。
     * 两个名字都试一遍，换平台构建也不用改代码。 */
    const names =
      process.platform === 'win32'
        ? ['keeper-kernel.exe', 'keeper-kernel']
        : ['keeper-kernel', 'keeper-kernel.exe'];
    let exe = null;
    for (const n of names) {
      const p = path.join(dir, n);
      if (fs.existsSync(p)) {
        exe = p;
        break;
      }
    }
    if (exe && !/\.exe$/i.test(exe) && process.platform === 'win32') {
      // Windows 上没有 .exe 后缀的多半是**别平台冻出来的产物**（在 mac 上交叉
      // 打包 win 时就会这样）。先提醒一句：直接跑会报「不是有效的 Win32 应用程序」。
      console.warn(
        '[desktop] 内核可执行文件没有 .exe 后缀：' +
          exe +
          '（若是 mac/Linux 产物，Windows 包需在 Windows 上重新构建）'
      );
    }
    if (!exe) {
      // 报错时把目录内容列出来：最常见的原因是**拿错了平台的产物**
      // （在 mac 上交叉打包 win，resources/kernel 里塞的是 mac 的二进制）。
      let listing = [];
      try {
        listing = fs.readdirSync(dir);
      } catch (_) {}
      throw new Error(
        `打包产物里没有内核可执行文件：${path.join(dir, names[0])}\n` +
          (listing.length
            ? `该目录实际有：${listing.join('、')}`
            : `目录不存在或为空：${dir}`) +
          '\nWindows 安装包必须在 Windows 上构建（packaging/build_windows.ps1）：' +
          'PyInstaller 不能交叉编译，mac 上冻出来的内核在 Windows 上跑不了。'
      );
    }
    // 前端产物冻在 bundle 内（<kernel>/_internal/keeper/web/dist）；
    // 也允许桌面壳用外挂资源指路（KEEPER_WEB_DIST）。
    const webDist = path.join(dir, '_internal', 'keeper', 'web', 'dist');
    return {
      command: exe,
      args: [],
      cwd: dir,
      // 内核所在目录：内置 python3 的副本要放在这儿（PyInstaller 的 bootloader
      // 按 exe 所在目录找 _internal，副本必须挨着它）
      dir,
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
    dir: path.dirname(python),
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
          PATH: runtimePath([...runtime.pathEntries, runtime.dir]),
          KEEPER_PYTHON: runtime.python3,
          KEEPER_NODE: runtime.node,
          // Windows 上跑用户命令用的 POSIX shell（busybox 的 sh.exe）；
          // 内核据此选 shell（见 keeper/framework/shell.py）
          ...(runtime.bundledSh ? { KEEPER_BUNDLED_SH: runtime.bundledSh } : {}),
          PORT: String(port),
          // 配置指路：内核里那些不带参数的 load_config()（核心 agent 装配等）
          // 也要拿到同一份配置，否则会用错、甚至去写只读的 app 包。
          KEEPER_CONFIG: config,
          // 前端产物指路：打包形态下 web/dist 冻在 bundle 里（main.py 自己能
          // 找到），这里额外指一次是为了让「外挂资源」这种放法也能用。
          ...(kernel.webDist ? { KEEPER_WEB_DIST: kernel.webDist } : {}),
          /*
           * 内核日志落盘由内核自己做：默认写 ~/.keeper/logs/<YYYY-MM-DD>.log，
           * 每天一个文件、保留最近 14 天（见 keeper/log/）。
           *
           * 这里**不再**额外指定 LOG_FILE —— 那会多出一份按大小轮转的
           * kernel.log，与按天文件内容重复，两份日志得分头翻。
           *
           * 保留 KEEPER_LOG_FILE 这个口子：想把内核日志重定向到某个固定文件
           * （比如采集器盯着一个文件）时仍然可用。
           */
          ...(process.env.KEEPER_LOG_FILE
            ? { LOG_FILE: process.env.KEEPER_LOG_FILE }
            : {}),
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
