/**
 * 内置浏览器：一个原生子视图（WebContentsView）+ 独立持久化 profile。
 *
 * 为什么是原生子视图而不是网页里的 iframe
 * --------------------------------------
 * iframe 受服务端 `X-Frame-Options` / CSP `frame-ancestors` 管辖——百度、Google、
 * GitHub 都明确禁止被嵌入，这是浏览器强制的，前端绕不过去。而子视图是**独立顶层
 * 页面**，没有任何嵌入限制：用户想上哪个网站就上哪个。
 *
 * 为什么 profile 必须独立于 keeper 主界面
 * -------------------------------------
 * `session.fromPath()` 把这个浏览器的全部状态（cookie / localStorage / 密码 /
 * IndexedDB / Service Worker）落在 `~/.keeper/chrome/userdata`，和 keeper 自己的
 * 存储彻底分开。这一条是**安全边界**而不是配置项：内置浏览器要加载任意第三方
 * 站点，如果和主界面共用 session，那么任意外部页面拿到的就是 keeper 的身份，
 * 能读到会话、设置、甚至文件系统边界内的东西。
 *
 * 另外注意：不能用 `app.setPath('userData')` 来做这件事——那会把 keeper 主界面的
 * userData 一起搬走，波及整个应用。
 *
 * 抓取（Playwright）为什么连接这里而不是自己再开一个 Chromium，见 keeper/browser/cdp.py。
 */
const { session, shell, WebContentsView } = require('electron');
const path = require('path');
const os = require('os');

/** 内置浏览器的 profile 目录（登录态等全部数据落这里，跨启动复用） */
const PROFILE_DIR = path.join(os.homedir(), '.keeper', 'chrome', 'userdata');

/** 允许的顶层导航协议：挡住 file:// / javascript: 之类 */
const NAV_OK = /^(https?|about):/i;

/**
 * 网页可以申请到的少数几个权限。
 *
 * 用**允许清单**而不是拒绝清单：这里加载的是任意第三方网页，需要挡的是摄像头、
 * 麦克风、定位、通知、USB/串口这些"能碰到用户本人"的能力；而 openExternal 必须
 * 放行——它正是 shell.openExternal 自己要申请的权限，一并拒掉的话地址栏那个 ↗
 * 和 target=_blank 的新窗口都会静默失败。
 */
const ALLOWED_PERMISSIONS = new Set([
  'openExternal',           // ↗ 新标签页 / 系统浏览器打开
  'clipboard-sanitized-write', // 页面内复制
  'fullscreen',
  'pointerLock',
]);

let win = null;
/** @type {import('electron').WebContentsView | null} */
let view = null;
/** 窗口内容区相对窗口左上角的偏移：React 传来的坐标是「相对内容区」的原生 view 坐标系不含它 */
let lastBounds = null;

function attach(window) {
  win = window;
}

/** 把 React 传来的 DOM 矩形换算到原生 view 的坐标系并应用 */
function applyBounds(rect) {
  if (!win || !view || !rect) return;
  lastBounds = rect;
  const content = win.getContentBounds();
  const outer = win.getBounds();
  view.setBounds({
    x: Math.round(rect.x + (content.x - outer.x)),
    y: Math.round(rect.y + (content.y - outer.y)),
    width: Math.max(0, Math.round(rect.width)),
    height: Math.max(0, Math.round(rect.height)),
  });
}

/** 窗口尺寸变了：重放上一次的矩形，否则浏览器视图会停在旧尺寸上被裁掉 */
function refreshBounds() {
  if (view && lastBounds) applyBounds(lastBounds);
}

/** 状态回传页面：地址栏、标题、加载中、前进后退可用性都以主进程为准 */
function pushState() {
  if (!view || !win || win.isDestroyed()) return;
  try {
    const wc = view.webContents;
    win.webContents.send('browser:state', {
      url: wc.getURL(),
      title: wc.getTitle(),
      loading: wc.isLoading(),
      // 注意：canGoBack / canGoForward 在 **webContents** 上，不在
      // webContents.navigationHistory 上（后者只有 getActiveIndex /
      // getEntryAtIndex / length）。写错的后果很隐蔽：pushState 每次都抛
      // TypeError，状态事件一条都发不出去，前端于是"页面明明开着、抓取按钮却永远
      // 是灰的"，而主进程日志只有一条 uncaughtException —— 极难往界面上联想。
      canGoBack: wc.canGoBack(),
      canGoForward: wc.canGoForward(),
    });
  } catch (err) {
    // 上报状态失败只该记一条日志：它挂在 did-navigate 这类事件回调上，抛出去就是
    // 进程级 uncaughtException，而 main.js 那里会顺手把内核也带走 —— 一个纯 UI 状态
    // 问题最终表现为"整个 keeper 挂了"。
    console.error('[browser] 上报状态失败：', err && err.message);
  }
}

function isOpen() {
  return !!view && !view.webContents.isDestroyed();
}

function create() {
  // fromPath 会自动建目录，且返回**同一个** session 实例（Electron 内部缓存），
  // 所以反复 open() 复用同一份登录态
  const sess = session.fromPath(PROFILE_DIR);

  /*
   * 权限按允许清单放行（见 ALLOWED_PERMISSIONS）。这是加载任意第三方网页的浏览器，
   * 不做这层拦截等于把摄像头 / 麦克风 / 定位 / 通知开放给了任何一个被打开的网页
   * ——而用户可能只是想查个资料。
   */
  sess.setPermissionRequestHandler((_wc, permission, callback) =>
    callback(ALLOWED_PERMISSIONS.has(permission))
  );
  sess.setPermissionCheckHandler((_wc, permission) =>
    ALLOWED_PERMISSIONS.has(permission)
  );

  const v = new WebContentsView({
    webPreferences: {
      session: sess,
      // 外部网页绝不能有 Node：nodeIntegration 一旦打开，任意站点都能读 ~/.keeper
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
    },
  });

  /*
   * 网页想开新窗口（target=_blank、window.open）时怎么办
   * --------------------------------------------------
   * 第一版这里是无条件 `shell.openExternal` + deny，结果是国内站点（百度首页尤其
   * 明显）的链接几乎都带 target="_blank"，于是"点一下链接"全被甩到系统浏览器里
   * 去了——对内置浏览器来说这是最坏的结果：用户明明在自己的浏览器里，却弹出了
   * 一个自己不认识的 Chrome 窗口，而且带不回浏览历史。
   *
   * 现在改成按 disposition 分流：
   *   new-window   → 在**当前视图**里打开（等价于"新标签打开"，但我们只有一格地方；
   *                  用户可以点后退回来，这比跳到系统浏览器更符合直觉）
   *   save-to-disk → 放行给 Electron 正常下载（拦下来会把下载也一起吞掉）
   *   其它协议      → mailto/tel 之类交给系统处理
   */
  v.webContents.setWindowOpenHandler((details) => {
    const { url, disposition } = details;
    if (disposition === 'save-to-disk') return { action: 'allow' };
    if (/^https?:/i.test(url)) {
      load(v, url);
      return { action: 'deny' };
    }
    if (/^(mailto:|tel:)/i.test(url)) {
      Promise.resolve(shell.openExternal(url)).catch(() => {});
      return { action: 'deny' };
    }
    return { action: 'deny' };
  });

  v.webContents.on('will-navigate', (e, url) => {
    if (!NAV_OK.test(url)) e.preventDefault();
  });
  v.webContents.on('page-title-updated', pushState);
  // 注意：Electron 30 没有 page-url-updated 事件。URL 变化由 did-navigate
  // （跨文档）与 did-navigate-in-page（同文档：锚点、pushState 路由）覆盖。
  v.webContents.on('did-navigate', pushState);
  v.webContents.on('did-navigate-in-page', pushState);
  v.webContents.on('did-start-loading', pushState);
  v.webContents.on('did-stop-loading', pushState);
  v.webContents.on('did-fail-load', (_e, code, desc, url) => {
    pushState();
    // -3 是 ERR_ABORTED：用户中途跳走 / 点停止，属正常，不该报错吓人
    if (code === -3) return;
    win?.webContents.send('browser:error', { code, description: desc, url });
  });
  v.webContents.on('destroyed', () => {
    /*
     * 必须比对 `view === v` 再清引用。销毁是**异步**的：close() 里先同步把 view
     * 置空，紧接着的 open() 已经建好了 v2，于是 v1 的 destroyed 事件姗姗来迟——
     * 无条件 `view = null` 会把**活着的 v2** 的引用抹掉。之后 isOpen() 恒为 false，
     * 地址栏输入什么都不再加载，而页面上还挂着上一个视图：现象是"页面能看、
     * 点什么都不动"。StrictMode 下 mount→cleanup→mount 必然踩到这条路径。
     */
    if (view === v) {
      view = null;
      lastBounds = null;
    }
    win?.webContents.send('browser:closed', {});
  });

  view = v;
  win.contentView.addChildView(v);
  return v;
}

/** 载入地址。loadURL 对非法地址是 rejected Promise，不接住就成了进程级未捕获错误 */
function load(view, url) {
  if (!url) return;
  try {
    const p = view.webContents.loadURL(url);
    if (p && typeof p.catch === 'function') {
      p.catch((err) => {
        win?.webContents.send('browser:error', {
          code: -2,
          description: (err && err.message) || '地址无效',
          url,
        });
      });
    }
  } catch (err) {
    win?.webContents.send('browser:error', {
      code: -2,
      description: (err && err.message) || '地址无效',
      url,
    });
  }
}

/** 打开（或聚焦）内置浏览器：url 为空则只显示当前页面 */
function open(url, rect) {
  if (!win || win.isDestroyed()) return false;
  if (!isOpen()) {
    const v = create();
    if (url) load(v, url);
  } else if (url) {
    load(view, url);
  }
  view.setVisible(true);
  applyBounds(rect);
  pushState();
  return true;
}

function close() {
  if (!isOpen()) return;
  win.contentView.removeChildView(view);
  // 销毁 webContents 而不是只隐藏：留着一个看不见的视图会继续跑它的定时器和视频，
  // 白耗 CPU / 电。profile 在磁盘上，重新打开会恢复
  view.webContents.close();
  view = null;
  lastBounds = null;
}

/** 前端地址栏 / 前进后退按钮的统一入口 */
function command(cmd, arg) {
  if (!isOpen()) return;
  const wc = view.webContents;
  if (cmd === 'back') wc.goBack();
  else if (cmd === 'forward') wc.goForward();
  else if (cmd === 'reload') wc.reload();
  else if (cmd === 'stop') wc.stop();
  else if (cmd === 'load') load(view, arg);
  else if (cmd === 'external' && arg) {
    // shell.openExternal 会失败返回 rejected Promise（如没有默认浏览器），
    // 不接住就变成进程级未捕获错误
    Promise.resolve(shell.openExternal(arg)).catch(() => {});
  } else return;
  pushState();
}

function setVisible(v) {
  if (isOpen()) view.setVisible(!!v);
}

/** 当前地址：给「抓取」这类接口用 */
function currentUrl() {
  return isOpen() ? view.webContents.getURL() : '';
}

module.exports = {
  PROFILE_DIR,
  attach,
  open,
  close,
  command,
  setVisible,
  setBounds: applyBounds,
  refreshBounds,
  currentUrl,
  isOpen,
};
