/**
 * keeper 桌面壳的主进程。
 *
 * 职责边界很清楚：**只做窗口和进程托管，不碰任何业务逻辑**。
 * 页面是 keeper 内核自带的（内核把 web/dist 作为站点根同源托管），所以这里
 * 只要等内核就绪、把窗口指过去就行——前端代码一行都不用改。
 *
 * 这也意味着"桌面版"和"命令行版"跑的是同一套东西：数据目录（~/.keeper）、
 * 数据库、配置完全一致，切换形态不产生迁移成本。
 *
 * 三个必须守住的约束（都是踩过坑才写进来的）
 * -------------------------------------
 * 1. **单实例**：不抢锁的话，每跑一次 npm start 就多一个 Electron、多一个内核，
 *    它们会一起抢 ~/.keeper 的数据库。第二个实例直接退出并聚焦已有窗口。
 * 2. **内核只能起一个**：内核启动要几十秒（建表 + 装载 agent），这段时间里
 *    macOS 的 activate 事件会反复触发。若每次都调 startSidecar，就会在 state
 *    还没赋值时并发 spawn 出 N 个 Python。所以启动必须**去重成一个 Promise**。
 * 3. **建窗口要幂等**：activate 与 whenReady 可能在同一次启动里都走到建窗逻辑，
 *    已有窗口时只聚焦，不再 new 一个。
 */
const { app, BrowserWindow, dialog, shell } = require('electron');
const { startSidecar, stopSidecar } = require('./sidecar');

let win = null;
/** 内核启动的 Promise（单例）：并发调用共享同一次启动，而不是各起一个 */
let booting = null;

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
    },
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
  win.on('closed', () => {
    win = null;
  });

  win.loadURL(url);
}

/** 拉起内核并建窗；失败只弹一次错误框 */
async function boot() {
  if (!booting) {
    booting = startSidecar({ onLog: (line) => console.log('[keeper]', line) })
      .then((sidecar) => {
        console.log(`[desktop] 内核就绪：${sidecar.url}`);
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
    stopSidecar();
  });
}
