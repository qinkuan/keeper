/**
 * 主界面（keeper 前端）的 preload 桥：把本次启动的**入站 token** 交给前端
 * （keeperHost）。
 *
 * 为什么要 preload 而不是直接在前端用 Node：主界面是同源托管的 Web 应用，
 * main.js 里已经关掉 nodeIntegration / 开了 contextIsolation。这里延续同样的口径——
 * 只暴露需要的那几样，不把 ipcRenderer 本身交出去。
 */
const { contextBridge, ipcRenderer } = require('electron');

/**
 * 入站 token。内核要求每个 API 请求带 X-Keeper-Token（见 keeper/api/auth.py）：
 * 内核里有能让 agent 执行本机命令的接口，而同源策略只拦"读响应"、不拦"发请求"，
 * 所以用户打开的任意网页都能打到这个端口。token 是唯一挡住它们的东西。
 *
 * 为什么向主进程要、而不是直接读 `process.env.KEEPER_TOKEN`：
 * Electron 20 起渲染进程默认 sandbox，此时 preload 里的 `process.env` 拿不到父进程
 * 环境变量（实测读出来是空，前端于是全部 401）。`ipcRenderer` 在 sandbox 下一定
 * 可用——它本来就是 preload 唯一稳定的出口。
 *
 * 用 sendSync 而不是 invoke：contextBridge 暴露的对象必须在 preload 阶段就取到值，
 * 页面初始化时同步读，拿不到 Promise。
 *
 * 页面 JS 能读到 token 是刻意的——这一页就是它的用户；别的网页读不到（不同源、
 * 不同进程）。开发形态（vite dev server，没有 preload）读不到就是 undefined，
 * 此时前端不带该头，配上 VITE_KEEPER_TOKEN 即可。
 */
contextBridge.exposeInMainWorld('keeperHost', {
  token: (() => {
    try {
      return ipcRenderer.sendSync('keeper:token') || '';
    } catch (e) {
      return '';
    }
  })(),

  /**
   * 读系统剪贴板里被复制的文件 / 文件夹路径列表（桌面壳专有）。
   * 返回 string[]；若当前不是桌面壳（纯 Web 版）则不存在，前端据此降级。
   * 主进程实现见 main.js 的 readClipboardFiles。
   */
  readClipboardFiles: () => ipcRenderer.invoke('keeper:clipboard-files'),
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
  pickFolder: () => ipcRenderer.invoke('space:pick-folder'),
  pickFile: (multi) => ipcRenderer.invoke('space:pick-file', multi),
  /** 右键菜单：让主进程弹原生 Menu，返回用户选中的动作 */
  contextMenu: (key, isDir, canPaste) =>
    ipcRenderer.invoke('space:context-menu', { key, isDir, canPaste }),
});
