/**
 * 「我的空间」桌面原生文件操作适配器。
 *
 * 把 Electron 主进程暴露的 window.keeperSpace（见 desktop/spaceFs.js + preload.js）
 * 包成带类型的函数，root（「我的空间」根绝对路径）在创建时绑定好，调用点不用再传。
 *
 * 这是「我的空间」人肉操作的唯一前端入口；会话 / 内核侧的「系统操作」仍走 Python 端点。
 */
type RawNode = {
  name: string;
  path: string;
  isDir: boolean;
  size: number;
  mtime: number;
};

export interface SpaceApi {
  /** 列目录，返回该目录下子项（目录在前、按名排序）。 */
  list(dir: string): Promise<{ path: string; items: RawNode[] }>;
  /** 读取文件内容（文本 / 探测结果），结构同后端 MySpaceFile。 */
  read(p: string): Promise<any>;
  /** 写回文件内容（「我的空间」内联编辑保存用）。 */
  write(p: string, content: string): Promise<any>;
  /** 在 parent 下新建名为 name 的子文件夹。 */
  mkdir(parent: string, name: string): Promise<any>;
  /** 重命名文件 / 文件夹为 newName。 */
  rename(p: string, newName: string): Promise<any>;
  /** 删除文件 / 文件夹（递归）。 */
  remove(p: string): Promise<any>;
  /** 空间内复制：把 src 复制到 dstDir（递归、同名自动改名）。 */
  copy(src: string, dstDir: string): Promise<any>;
  /** 从本机任意路径把 src 复制到空间内 dstDir（递归、同名自动改名）。 */
  import(src: string, dstDir: string): Promise<any>;
  /** 在系统文件管理器里定位并选中该路径。 */
  reveal(p: string): Promise<any>;
  /** 原生选目录对话框，返回绝对路径或 null。 */
  pickFolder(): Promise<string | null>;
  /** 原生选文件对话框，multi=true 可多选，返回绝对路径数组。 */
  pickFile(multi?: boolean): Promise<string[]>;
  /** 右键菜单：弹原生 Menu，返回用户选中动作（open/download/copy/rename/delete/mkdir/paste），取消返回 null。 */
  contextMenu(
    key: string,
    isDir: boolean,
    canPaste: boolean
  ): Promise<string | null>;
}

export function makeSpaceApi(root: string): SpaceApi {
  const s: any = (window as any).keeperSpace;
  return {
    list: (dir) => s.list(root, dir),
    read: (p) => s.read(root, p),
    write: (p, content) => s.write(root, p, content),
    mkdir: (parent, name) => s.mkdir(root, parent, name),
    rename: (p, name) => s.rename(root, p, name),
    remove: (p) => s.remove(root, p),
    copy: (src, dst) => s.copy(root, src, dst),
    import: (src, dst) => s.import(root, src, dst),
    reveal: (p) => s.reveal(p),
    pickFolder: () => s.pickFolder(),
    pickFile: (multi) => s.pickFile(multi),
    contextMenu: (key, isDir, canPaste) => s.contextMenu(key, isDir, canPaste),
  };
}

// 「我的空间」媒体预览走原生 keeperfile:// 协议（desktop/main.js 注册），
// 把文件直接喂给 <img>/<video>/<iframe>，避免 UI 侧残留任何 HTTP。
function b64urlEncode(s: string): string {
  return btoa(unescape(encodeURIComponent(s)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

/** 构造 space 内某文件的 keeperfile:// URL（路径锁在空间根内，越界在主进程 403）。 */
export function spaceFileUrl(root: string, file: string): string {
  const r = root.replace(/\/$/, "");
  const rel = (file.startsWith(r) ? file.slice(r.length) : file).replace(/^\//, "");
  return `keeperfile:///${b64urlEncode(r)}/${b64urlEncode(rel)}`;
}
