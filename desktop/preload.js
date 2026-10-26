/**
 * 主界面（keeper 前端）的 preload 桥。
 *
 * 为什么要 preload 而不是直接在前端用 Node：主界面是同源托管的 Web 应用，
 * main.js 里已经关掉 nodeIntegration / 开了 contextIsolation。这里延续同样的口径——
 * 只暴露需要的那几样，不把 ipcRenderer 本身交出去。
 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('keeperHost', {
  /**
   * 读系统剪贴板里被复制的文件 / 文件夹路径列表（桌面壳专有）。
   * 返回 string[]；若当前不是桌面壳（纯 Web 版）则不存在，前端据此降级。
   * 主进程实现见 main.js 的 readClipboardFiles。
   */
  readClipboardFiles: () => ipcRenderer.invoke('keeper:clipboard-files'),

  /**
   * 宿主平台（'darwin' / 'win32' / 'linux'）。
   *
   * 前端要靠它决定「要不要给 macOS 的红绿灯按钮让位」：macOS 上窗口是
   * 无标题栏的（main.js 设了 titleBarStyle: 'hidden'），红绿灯浮在页面左上角，
   * 前端得在侧栏顶部留出一条空位，否则按钮会压住导航项。
   * 纯 Web 版没有这个对象 → 前端按「不是桌面壳」处理，不留空位。
   */
  platform: process.platform,

  /**
   * 双击标题栏 → 最大化 / 还原。
   *
   * 为什么需要单独做：标题栏那条带是 ``-webkit-app-region: drag``，
   * 而 **drag 区域里的指针事件不会派发给渲染进程**（它们被主进程拿去
   * 拖窗口了）—— 所以前端收不到 dblclick，只能由主进程代劳。
   * macOS 上双击标题栏最大化是肌肉记忆，少了这个会很突兀。
   */
  toggleMaximize: () => ipcRenderer.send('keeper:toggle-maximize'),
});

/**
 * 「我的空间」文件操作（桌面原生）。只给前端用，不暴露 ipcRenderer 本身。
 * 每个方法第一个参数都是 root（「我的空间」根绝对路径，由前端从内核可信取来再传入），
 * 主进程据此把路径锁在空间内（见 desktop/spaceFs.js 的 resolveSpacePath）。
 * 纯 Web 版没有这个对象，前端应回退到 HTTP 端点。
 */
contextBridge.exposeInMainWorld('keeperSpace', {
  list: (root, dir) => ipcRenderer.invoke('space:list', root, dir),
  read: (root, file) => ipcRenderer.invoke('space:read', root, file),
  /** 读文件二进制字节，回 { name, size, base64 }（预览 xlsx/pdf 用）。 */
  readBinary: (root, file, maxBytes) =>
    ipcRenderer.invoke('space:read-binary', root, file, maxBytes),
  write: (root, file, content) =>
    ipcRenderer.invoke('space:write', root, file, content),
  /** 写二进制文件（xlsx 表格预览「保存到原文件」用）：base64 进，落盘为字节。 */
  writeBinary: (root, file, base64) =>
    ipcRenderer.invoke('space:write-binary', root, file, base64),
  mkdir: (root, parent, name) =>
    ipcRenderer.invoke('space:mkdir', root, parent, name),
  rename: (root, file, newName) =>
    ipcRenderer.invoke('space:rename', root, file, newName),
  remove: (root, file) => ipcRenderer.invoke('space:delete', root, file),
  copy: (root, src, dstDir) =>
    ipcRenderer.invoke('space:copy', root, src, dstDir),
  import: (root, src, dstDir) =>
    ipcRenderer.invoke('space:import', root, src, dstDir),
  reveal: (file) => ipcRenderer.invoke('space:reveal', file),
  /** 用系统默认应用打开文件（docx/ppt 的编辑走这里） */
  openWithApp: (file) => ipcRenderer.invoke('space:open-with-app', file),
  pickFolder: () => ipcRenderer.invoke('space:pick-folder'),
  pickFile: (multi) => ipcRenderer.invoke('space:pick-file', multi),
  /** 右键菜单：让主进程弹原生 Menu，返回用户选中的动作 */
  contextMenu: (key, isDir, canPaste) =>
    ipcRenderer.invoke('space:context-menu', { key, isDir, canPaste }),
});
