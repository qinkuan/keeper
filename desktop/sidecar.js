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
 * 4. **每次启动现生成入站 token**：内核里有能让 agent 执行本机命令的接口，而这个
 *    进程绑在回环端口上——同一 WiFi 下的设备够不着，但**用户打开的任意网页**够得着
 *    （CORS 只拦读响应、不拦发请求，GET 连预检都不触发）。token 让那些网页猜不到
 *    入场券。生成在这里而不是让用户配，是因为：随机的才安全，写进 config.yaml 的
 *    迟早会被提交进仓库或者在截图里泄露。
 */
const { spawn } = require('child_process');
const crypto = require('crypto');
const net = require('net');
const os = require('os');
const path = require('path');
const fs = require('fs');
const http = require('http');

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

async function startSidecar({ onLog, cdpPort } = {}) {
  if (state) return state;
  if (pending) return pending;

  pending = (async () => {
    const port = await findFreePort();
    const python = findPython();
    const config = path.join(KEEPER_DIR, 'config.yaml');

    if (!fs.existsSync(config)) {
      throw new Error(`找不到配置文件：${config}`);
    }

    // 入站 token：32 字节 CSPRNG，base64url（无 +/= ，塞进 header 不用转义）。
    // 每次启动都换，纵使有一次泄露也只影响那一次会话。
    const token = crypto.randomBytes(32).toString('base64url');

    const child = spawn(
      python,
      ['-m', 'keeper.main', '--config', config, String(port)],
      {
        cwd: PROJECT_ROOT,
        env: {
          ...process.env,
          PORT: String(port),
          // 内核据此启用入站认证（见 keeper/api/auth.py）
          KEEPER_TOKEN: token,
          /*
           * 内置浏览器的 CDP 端口。内核靠它用 Playwright **连上桌面壳已经在跑的
           * 那个浏览器**（desktop/main.js 里 appendSwitch 的同一个端口），而不是自己
           * 再 launch 一个——同一个 user-data-dir 不能被两个 Chromium 进程同时打开，
           * 那样会把 cookie 库写坏。
           * 命令行形态（./start_keeper.sh）没有这个变量，抓取接口会明确回"不可用"。
           */
          KEEPER_CDP_PORT: cdpPort ? String(cdpPort) : '',
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

    state = { child, port, token, url: `http://127.0.0.1:${port}` };
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
