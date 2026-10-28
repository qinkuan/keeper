/**
 * 「我的空间」预览里把选中的文档片段"添加"进聊天时用到的轻量事件总线。
 *
 * 走 window 自定义事件而非 props/context：预览（UserSpacePage 的 FileViewer）和
 * 聊天输入框（ChatPage）并不一定在同一棵组件子树里直接相邻——尤其「我的空间」页
 * 右侧是 dock 出来的 ChatPage。事件总线让两者彻底解耦，且无论 ChatPage 是 dock 态
 * 还是独立整页，都能收到引用。
 */

export interface ChatQuote {
  /** 文件的绝对路径 */
  path: string;
  /** 选中的内容（全量） */
  text: string;
}

export const ADD_QUOTE_EVENT = "keeper:add-quote";

export function addQuoteToChat(q: ChatQuote) {
  window.dispatchEvent(new CustomEvent(ADD_QUOTE_EVENT, { detail: q }));
}

/**
 * 「我的空间」文件树右键「添加到对话」的文件引用：只带路径，不带内容。
 *
 * 与 ChatQuote 的区别：引用片段是「路径 + 选中内容」，由预览侧读好文本再给；
 * 文件引用只登记「这个文件参与本轮对话」，内容留给 agent 自己按路径去读——
 * 文件可能很大（甚至是个文件夹），前端不该替模型做全量读取。
 */
export interface ChatFileRef {
  /** 文件 / 文件夹的绝对路径 */
  path: string;
  /** 显示名（路径最后一段） */
  name: string;
  /** 是否文件夹 */
  isDir: boolean;
}

export const ADD_FILE_REF_EVENT = "keeper:add-file-ref";

export function addFileRefToChat(ref: ChatFileRef) {
  window.dispatchEvent(new CustomEvent(ADD_FILE_REF_EVENT, { detail: ref }));
}

/* ---------------------------------------------------------------------------
 * 待发送文件引用的**模块级暂存**
 * ---------------------------------------------------------------------------
 *
 * 为什么不用「派发事件 → 接收方存进自己的 state」就完事：
 * 事件只在**派发那一刻**有接收者。「我的空间」页右侧的对话面板是 dock 出来的
 * ChatPage，若那一刻它没挂载（还没装载核心智能体、面板未渲染、或者用户把对话
 * 面板收起来后再从目录树添加），引用就被**丢掉了**——用户看到「已添加到对话」
 * 的提示，输入框里却空空如也，于是「这功能不可用」。
 *
 * 暂存在模块里就绕开了这个时序：谁来取都取得到，谁挂载谁立刻看到。
 * ChatPage 用 useSyncExternalStore 订阅它（React 18 内置，不用引第三方状态库）。
 */

/** 当前待发送的文件引用（数组引用只在内容变化时替换 —— useSyncExternalStore 要求快照稳定） */
let pendingFileRefs: ChatFileRef[] = [];
const fileRefListeners = new Set<() => void>();

/** 读取当前待发送引用（useSyncExternalStore 的 getSnapshot） */
export function getPendingFileRefs(): ChatFileRef[] {
  return pendingFileRefs;
}

/** 订阅待发送引用的变化（useSyncExternalStore 的 subscribe） */
export function subscribePendingFileRefs(cb: () => void): () => void {
  fileRefListeners.add(cb);
  return () => {
    fileRefListeners.delete(cb);
  };
}

function emitFileRefs() {
  // 换一个新数组引用：快照变了，订阅者才被叫醒
  pendingFileRefs = [...pendingFileRefs];
  fileRefListeners.forEach((cb) => cb());
}

/** 加入一条待发送的文件引用（同一个路径只加一次） */
export function addPendingFileRef(ref: ChatFileRef) {
  if (!ref?.path) return;
  if (pendingFileRefs.some((p) => p.path === ref.path)) return;
  pendingFileRefs = [...pendingFileRefs, ref];
  emitFileRefs();
}

/** 移除一条（点 chip 上的 ✕） */
export function removePendingFileRef(path: string) {
  if (!pendingFileRefs.some((p) => p.path === path)) return;
  pendingFileRefs = pendingFileRefs.filter((p) => p.path !== path);
  emitFileRefs();
}

/** 清空（发出这一轮之后） */
export function clearPendingFileRefs() {
  if (!pendingFileRefs.length) return;
  pendingFileRefs = [];
  emitFileRefs();
}
