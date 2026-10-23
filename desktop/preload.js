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
