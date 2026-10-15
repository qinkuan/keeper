/**
 * keeper 桌面壳的主进程。
 *
 * 职责边界很清楚：**窗口 / 进程托管 + 内置浏览器**，不碰业务逻辑。
 * 页面是 keeper 内核自带的（内核把 web/dist 作为站点根同源托管），所以这里
 * 只要等内核就绪、把窗口指过去就行——前端业务代码一行都不用改。
 *
 * 内置浏览器（desktop/browser.js）是唯一的例外：它是原生能力，前端拿不到，
 * 必须经 preload 桥转发命令（见 IPC 注册那段）。
 *
 * 这也意味着"桌面版"和"命令行版"跑的是同一套东西：数据目录（~/.keeper）、
 * 数据库、配置完全一致，切换形态不产生迁移成本。
 *
 * 五个必须守住的约束（都是踩过坑才写进来的）
 * -------------------------------------
 * 1. **单实例**：不抢锁的话，每跑一次 npm start 就多一个 Electron、多一个内核，
 *    它们会一起抢 ~/.keeper 的数据库。第二个实例直接退出并聚焦已有窗口。
 * 2. **内核只能起一个**：内核启动要几十秒（建表 + 装载 agent），这段时间里
 *    macOS 的 activate 事件会反复触发。若每次都调 startSidecar，就会在 state
 *    还没赋值时并发 spawn 出 N 个 Python。所以启动必须**去重成一个 Promise**。
 * 3. **建窗口要幂等**：activate 与 whenReady 可能在同一次启动里都走到建窗逻辑，
 *    已有窗口时只聚焦，不再 new 一个。
 * 4. **CDP 端口必须在 app ready 之前定下**：它是浏览器进程启动参数，ready 之后
 *    再 appendSwitch 就不生效了。而端口号还要交给 Python 内核（它用 Playwright
 *    连上来做抓取），所以取值只能是同步的——固定端口 + 环境变量覆盖。
 * 5. **原生视图永远在最上层**：WebContentsView 不在 DOM 里，会盖住页面上所有
 *    浮层（会话抽屉等）。窗口 resize 必须把视图尺寸重放一次，否则会停在旧尺寸
 *    被裁掉一半。
 */
const { app, BrowserWindow, dialog, ipcMain, shell } = require('electron');
const path = require('path');
const { startSidecar, stopSidecar } = require('./sidecar');
const browser = require('./browser');

let win = null;
/** 内核启动的 Promise（单例）：并发调用共享同一次启动，而不是各起一个 */
let booting = null;
/**
 * 本次启动的入站 token（sidecar.js 每次现生成，见 keeper/api/auth.py）。
 * preload 在 sandbox 环境里读不到环境变量，所以走 sendSync 来这里取。
 * 必须在 createWindow **之前**赋值 —— preload 是建窗时同步执行的。
 */
let sidecarToken = '';
ipcMain.on('keeper:token', (e) => {
  e.returnValue = sidecarToken;
});

/**
 * 内置浏览器的 CDP 端口：Python 内核用 Playwright 连它做抓取。
 * 必须与 desktop/browser.js 里的浏览器共用同一个 Electron 实例——所以端口是
 * Electron 的启动参数，而不是 Playwright 自己开一个浏览器。
 * 可用 KEEPER_CDP_PORT 覆盖（默认值被占用时换一个）。
 */
const CDP_PORT = Number(process.env.KEEPER_CDP_PORT || 9333);
app.commandLine.appendSwitch('remote-debugging-port', String(CDP_PORT));
// 只监听回环：这个端口等于「该浏览器的完全控制权」（能读全部 cookie），
// 绝不能绑到 0.0.0.0 让同网段的人够得着
app.commandLine.appendSwitch('remote-debugging-address', '127.0.0.1');

/** 前端 → 主进程的内置浏览器命令：桥的转发层，业务在 browser.js 里 */
function registerBrowserIpc() {
  ipcMain.on('browser:open', (_e, { url, rect } = {}) => browser.open(url, rect));
  ipcMain.on('browser:close', () => browser.close());
  ipcMain.on('browser:command', (_e, cmd, arg) => browser.command(cmd, arg));
  ipcMain.on('browser:set-bounds', (_e, rect) => browser.setBounds(rect));
  ipcMain.on('browser:set-visible', (_e, visible) => browser.setVisible(visible));
  ipcMain.handle('browser:is-open', () => browser.isOpen());
  ipcMain.handle('browser:current-url', () => browser.currentUrl());
}


function createWindow(url) {
  // 幂等：已有窗口就聚焦，绝不 new 第二个
  if (win) {
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
    return;
  }

  win = new BrowserWindow({
    width: 1340,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    title: 'keeper',
    backgroundColor: '#0b0f17',
    show: false, // 等首屏渲染完再 show，避免白闪
    webPreferences: {
      // 页面是纯 Web 应用：所有数据都经 HTTP 走内核，不需要 node 集成。
      // 关掉它可以避免页面拿到本地文件系统权限——第三方插件的 Web 内容
      // 不该能读写 ~/.keeper。
      nodeIntegration: false,
      contextIsolation: true,
      // 内置浏览器的控制面走这里（见 preload.js）；不开 preload 前端就够不到
      // 原生视图，Web 版靠"没有这个对象"降级到 iframe
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  browser.attach(win);
  // 原生视图是浮在页面之上的一层，窗口尺寸变了必须跟着重算，否则会被裁掉
  win.on('resize', () => browser.refreshBounds());
  win.on('maximize', () => browser.refreshBounds());
  win.on('unmaximize', () => browser.refreshBounds());
  win.on('closed', () => {
    browser.close();
    win = null;
  });

  // 外链交给系统浏览器，别在壳里开（壳内的导航没有地址栏，用户出不来）
  win.webContents.setWindowOpenHandler(({ url: target }) => {
    if (target && !target.startsWith(url)) {
      shell.openExternal(target);
      return { action: 'deny' };
    }
    return { action: 'allow' };
  });

  win.once('ready-to-show', () => win.show());
  win.loadURL(url);
}

/** 拉起内核并建窗；失败只弹一次错误框 */
async function boot() {
  if (!booting) {
    booting = startSidecar({
      onLog: (line) => console.log('[keeper]', line),
      // 把 CDP 端口交给内核：抓取走「连上桌面壳那个浏览器」，而不是自己再开一个
      cdpPort: CDP_PORT,
    })
      .then((sidecar) => {
        console.log(`[desktop] 内核就绪：${sidecar.url}`);
        // preload 通过 sendSync 来要 token（sandbox 下读不到环境变量），
        // 所以必须在这一步赋值 —— 建窗紧接着就发生，preload 同步执行。
        sidecarToken = sidecar.token || '';
        return sidecar;
      })
      .catch((err) => {
        booting = null; // 允许下一次重试
        throw err;
      });
  }
  return booting;
}

async function bootstrap() {
  try {
    const sidecar = await boot();
    createWindow(sidecar.url);
  } catch (err) {
    const msg = (err && err.message) || String(err);
    console.error('[desktop] 启动失败：', msg);
    dialog.showErrorBox(
      'keeper 启动失败',
      `${msg}\n\n可以先用命令行确认内核本身能起来：\n  cd keeper && ./start_keeper.sh`
    );
    app.quit();
  }
}

// 单实例锁：第二个实例（再跑一次 npm start / ./start_desktop.sh）直接退出
if (!app.requestSingleInstanceLock()) {
  console.log('[desktop] keeper 已在运行，聚焦已有实例');
  app.quit();
} else {
  // IPC 注册放在 ready 之前：ipcMain.handle 只允许注册一次，早注册省得建窗时序出问题
  registerBrowserIpc();

  // 第二个实例启动时会走到这里：把已有窗口顶到前台
  app.on('second-instance', () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.show();
      win.focus();
    }
  });

  app.whenReady().then(bootstrap);

  // macOS：点 Dock 图标 / Spotlight 唤起时触发。注意它在**启动过程中也会触发**
  // （那时窗口还没建出来），所以这里必须走 boot() 去重，否则会重复拉内核。
  app.on('activate', () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.show();
      win.focus();
      return;
    }
    bootstrap();
  });

  // 关窗不等于退出：macOS 上点红叉后点 Dock 图标还要能再开出来。
  // 真正的收尾放在 before-quit——内核必须在这里停掉。
  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
  });

  app.on('before-quit', () => {
    stopSidecar();
  });

  // 兜底：进程被强杀（SIGINT / 未捕获异常）时也要带上内核，
  // 否则它会变成孤儿进程继续占着数据库。
  process.on('SIGINT', () => {
    stopSidecar();
    app.quit();
  });
  process.on('uncaughtException', (err) => {
    console.error('[desktop] 未捕获异常：', err);
    // 内核一起退，然后**真的退出**：只停内核的话，留下的是一个打不开任何页面的
    // 空壳窗口（页面全靠内核同源托管），用户会以为应用坏了却关不掉。
    stopSidecar();
    app.quit();
  });
}
