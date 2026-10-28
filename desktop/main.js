/**
 * keeper 桌面壳的主进程。
 *
 * 职责边界很清楚：**窗口 / 进程托管**，不碰业务逻辑。
 * 页面是 keeper 内核自带的（内核把 web/dist 作为站点根同源托管），所以这里
 * 只要等内核就绪、把窗口指过去就行——前端业务代码一行都不用改。
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
const { app, BrowserWindow, clipboard, dialog, ipcMain, Menu, net, protocol, shell } = require('electron');
const path = require('path');
const { fileURLToPath } = require('url');
const { startSidecar, stopSidecar } = require('./sidecar');
const spaceFs = require('./spaceFs');
const { startSpaceWatch } = require('./spaceWatch');

let win = null;
/** 内核启动的 Promise（单例）：并发调用共享同一次启动，而不是各起一个 */
let booting = null;
/** 「我的空间」文件监听句柄（退出时要关） */
let spaceWatcher = null;
/**
 * 读系统剪贴板里「被复制的文件 / 文件夹」的绝对路径列表。
 *
 * 用途：用户在系统文件管理器里 Ctrl+C 复制文件，再在「我的空间」里 Ctrl+V 粘贴。
 * 网页本身读不到系统剪贴板的文件路径（浏览器安全限制），但 Electron 主进程可以——
 * 这里用各平台通用的剪贴板格式取出 file:// URL 再转回本地路径。
 * 任何一步失败都吞掉（返回 []），不让粘贴操作崩掉。
 */
function readClipboardFiles() {
  const paths = [];
  const pushUrl = (u) => {
    try {
      if (u && /^file:/i.test(u.trim())) paths.push(fileURLToPath(u.trim()));
    } catch (_) {}
  };
  // 1) public.file-url：macOS / Windows 复制文件时都会写；多文件用 NUL(\0) 分隔
  try {
    const buf = clipboard.readBuffer('public.file-url');
    if (buf && buf.length) buf.toString('utf8').split('\0').forEach(pushUrl);
  } catch (_) {}
  // 2) Windows 专用 FileNameW：UTF-16LE，NUL 分隔，结尾双 NUL
  if (paths.length === 0) {
    try {
      const buf = clipboard.readBuffer('FileNameW');
      if (buf && buf.length > 2) {
        const end =
          buf[buf.length - 1] === 0 && buf[buf.length - 2] === 0
            ? buf.length - 2
            : buf.length;
        buf
          .slice(0, end)
          .toString('utf16le')
          .split('\0')
          .filter(Boolean)
          .forEach((p) => paths.push(p));
      }
    } catch (_) {}
  }
  // 3) 单文件兜底：read 只返回第一个
  if (paths.length === 0) {
    try {
      const u = clipboard.read('public.file-url');
      if (u) pushUrl(u);
    } catch (_) {}
  }
  return [...new Set(paths)];
}

function createWindow(url) {
  // 幂等：已有窗口就聚焦，绝不 new 第二个
  if (win) {
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
    return;
  }

  // macOS：隐藏原生标题栏。桌面上那条系统标题栏（写着页面标题）既占高度，
  // 又和应用自己的侧栏重复 —— 侧栏顶部本来是空的，正好让红绿灯浮在那里
  // （trafficLightPosition 把它们移到左上角、压在侧栏底色上）。
  // 「hidden」保留红绿灯（与 'hiddenInset' 不同，后者把红绿灯内缩成小圆点）。
  //
  // Windows / Linux：**不动**原生标题栏。Electron 30 的 titleBarOverlay 能把
  // 系统按钮浮在右上角，但那是**盖在内容上**的 —— 知识库、设置、我的空间这些
  // 页面右上角都有控件，会被压住。为这点观感去改一堆页面的布局不划算，
  // 那边只把标题文字缩短（见 title 选项）。
  const isMac = process.platform === 'darwin';

  win = new BrowserWindow({
    width: 1340,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    title: 'Keeper',
    ...(isMac
      ? {
          titleBarStyle: 'hidden',
          // 红绿灯浮在自绘标题栏上（前端那条 38px 的毛玻璃带），
          // y=12 让三个按钮在 38px 里大致居中（按钮高 14px）。
          trafficLightPosition: { x: 12, y: 12 },
        }
      : {}),
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

  win.on('closed', () => {
    win = null;
  });

  // 壳里永远不新开一个 Electron 窗口：外链交系统浏览器，站内/未知链接直接拦截
  // （之前对「同域链接」返回 'allow' 会再拉起一个满屏 App 窗口，盖住当前应用、原窗口还在，
  //  正是「打开一个 URL 就把整个应用抢过去」的成因）
  win.webContents.setWindowOpenHandler(({ url: target }) => {
    if (target && /^https?:\/\//i.test(target)) {
      shell.openExternal(target);
    }
    return { action: 'deny' };
  });

  // 纵深防御：主窗口只允许在应用自身源内做顶层导航（SPA 的 pushState 不触发
  // will-navigate，不受影响；F5 刷新同源也放行）。没有这道闸，任何一条误导航
  // （比如自动化误把主界面当成网页 page 去 goto）都会让 React 应用整个被
  // 外部网页替换——"应用窗口变成浏览器"。
  try {
    const appOrigin = new URL(url).origin;
    win.webContents.on('will-navigate', (e, target) => {
      if (!target.startsWith(appOrigin)) e.preventDefault();
    });
  } catch (err) {
    // url 解析失败就不设闸（不应发生：url 来自内核 sidecar）
  }

  win.once('ready-to-show', () => win.show());
  win.loadURL(url);
}

/** 拉起内核并建窗；失败只弹一次错误框 */
async function boot() {
  if (!booting) {
    booting = startSidecar({
      onLog: (line) => console.log('[keeper]', line),
    })
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

// 「我的空间」媒体预览：注册 keeperfile:// 协议，直接把空间内文件喂给 <img>/<video>/<iframe>，
// 不再走 Python 的 /mine/file/raw（避免 UI 侧残留任何 HTTP）。路径锁在空间根内，越界返回 403。
function b64urlDecode(s) {
  const b = String(s).replace(/-/g, '+').replace(/_/g, '/');
  return Buffer.from(b, 'base64').toString('utf8');
}
function registerSpaceProtocol() {
  if (protocol.isProtocolRegistered('keeperfile')) return;
  protocol.handle('keeperfile', (request) => {
    try {
      const u = new URL(request.url);
      const parts = u.pathname.split('/').filter(Boolean);
      const root = b64urlDecode(parts[0] || '');
      const rel = b64urlDecode(parts[1] || '');
      const abs = spaceFs.resolveSpacePath(root, rel);
      return net.fetch('file://' + abs, { headers: request.headers });
    } catch (e) {
      return new Response('forbidden', { status: 403 });
    }
  });
}

/**
 * 「我的空间」文件监听：文件一变就通知内核重新索引（秒级）。
 *
 * 路径不在这儿硬编码——向内核问（GET /search/status 的 config.roots）：
 * 内核读的是 settings.yaml 的 user_space.path，与 Python 侧索引用的是同一个真源，
 * 用户改了设置两边一起生效。
 */
async function startIndexWatch(sidecarUrl) {
  try {
    const res = await fetch(`${sidecarUrl}/search/status`);
    const data = await res.json();
    const roots = (data && data.config && data.config.roots) || [];
    if (!roots.length) {
      console.log('[desktop] 未配置「我的空间」，不监听文件变化');
      return;
    }
    spaceWatcher = startSpaceWatch({ root: roots[0], url: sidecarUrl });
  } catch (e) {
    console.error('[desktop] 启动文件监听失败：', e && e.message);
  }
}

async function bootstrap() {
  try {
    const sidecar = await boot();
    registerSpaceProtocol();
    createWindow(sidecar.url);
    // 文件监听：内核起来后就开始盯「我的空间」，用户改完文件立刻能搜到
    startIndexWatch(sidecar.url);
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
  // 「我的空间」粘贴：读系统剪贴板里复制的文件路径
  ipcMain.handle('keeper:clipboard-files', () => readClipboardFiles());

  // 双击标题栏 → 最大化 / 还原。标题栏那条带是 -webkit-app-region: drag，
  // drag 区域的指针事件不会派发给渲染进程（被主进程拿去拖窗口了），
  // 所以前端收不到 dblclick，只能由主进程代劳（macOS 的肌肉记忆）。
  ipcMain.on('keeper:toggle-maximize', () => {
    if (!win) return;
    if (win.isMaximized()) win.unmaximize();
    else win.maximize();
  });

  // 「我的空间」文件操作（桌面原生）：人肉操作走这里，内核端点保留给会话/系统用
  ipcMain.handle('space:list', (_e, root, dir) => spaceFs.list(root, dir));
  ipcMain.handle('space:read', (_e, root, file) => spaceFs.readFile(root, file));
  ipcMain.handle('space:read-binary', (_e, root, file, maxBytes) =>
    spaceFs.readBinary(root, file, maxBytes)
  );
  ipcMain.handle('space:write', (_e, root, file, content) =>
    spaceFs.writeFile(root, file, content)
  );
  ipcMain.handle('space:write-binary', (_e, root, file, base64) =>
    spaceFs.writeBinary(root, file, base64)
  );
  ipcMain.handle('space:mkdir', (_e, root, parent, name) =>
    spaceFs.mkdir(root, parent, name)
  );
  ipcMain.handle('space:rename', (_e, root, file, newName) =>
    spaceFs.rename(root, file, newName)
  );
  ipcMain.handle('space:delete', (_e, root, file) => spaceFs.remove(root, file));
  ipcMain.handle('space:copy', (_e, root, src, dstDir) =>
    spaceFs.copy(root, src, dstDir)
  );
  ipcMain.handle('space:import', (_e, root, src, dstDir) =>
    spaceFs.importFromLocal(root, src, dstDir)
  );
  ipcMain.handle('space:reveal', (_e, file) => spaceFs.reveal(file));
  ipcMain.handle('space:open-with-app', (_e, file) => spaceFs.openWithSystemApp(file));
  ipcMain.handle('space:pick-folder', () => spaceFs.pickFolder());
  ipcMain.handle('space:pick-file', (_e, multi) => spaceFs.pickFile(multi));
  // 「我的空间」右键菜单：原生 Menu 弹在光标处，返回用户选中的动作；取消返回 null
  ipcMain.handle('space:context-menu', (_e, { key, isDir, canPaste }) => {
    return new Promise((resolve) => {
      let done = false;
      const finish = (action) => {
        if (done) return;
        done = true;
        resolve(action || null);
      };
      const items = [
        { label: '添加到对话', click: () => finish('add-to-chat') },
        { label: '在文件管理器中打开', click: () => finish('open') },
        { type: 'separator' },
        { label: '复制', click: () => finish('copy') },
        { label: '重命名', click: () => finish('rename') },
        { label: '删除', click: () => finish('delete') },
      ];
      if (isDir) {
        items.push({ type: 'separator' });
        items.push({ label: '在此新建文件夹', click: () => finish('mkdir') });
        if (canPaste) items.push({ label: '粘贴', click: () => finish('paste') });
      }
      const menu = Menu.buildFromTemplate(items);
      menu.popup({ window: win, callback: () => finish(null) });
    });
  });

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
    if (spaceWatcher) {
      spaceWatcher.close();
      spaceWatcher = null;
    }
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
