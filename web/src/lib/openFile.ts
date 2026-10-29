/**
 * 「在『我的空间』里打开某个文件（可带落点）」的通道。
 *
 * 为什么要有它：
 * - 对话面板（ChatPage）与「我的空间」页（UserSpacePage）是两棵不相邻的子树，
 *   谁都不是谁的祖先，没法直接传 props；
 * - 而且对话页可能是**整页**（route = chat），此时「我的空间」页**根本没挂载**——
 *   事件派发过去没人接，文件就永远打不开。
 *
 * 所以这里用「模块级暂存 + 订阅」两段式（与 lib/quote.ts 的待发送引用同一思路）：
 * 1. requestOpenFile() 记下目标并广播——已挂载的空间页立刻响应；
 * 2. 没挂载时目标留在暂存里，App 切到「我的空间」路由后，空间页挂载时取走它。
 *
 * 落点（line / page）的口径与检索命中一致：
 * - docx：line = 段序号（IR 块序号，前端 data-block-idx 同一口径）
 * - pdf ：page = 页码
 * - md  ：line = 源文件行号
 */
export interface OpenFileTarget {
  /** 文件绝对路径 */
  path: string;
  /** 行号 / 段序号（markdown 行号、docx 段序号） */
  line?: number;
  /** 页码（pdf） */
  page?: number;
  /** 命中词（用于高亮） */
  terms?: string[];
}

let pending: OpenFileTarget | null = null;
const listeners = new Set<(t: OpenFileTarget) => void>();

/** 请求打开某个文件（可带落点）。已挂载的空间页会立刻收到。 */
export function requestOpenFile(t: OpenFileTarget) {
  if (!t?.path) return;
  pending = t;
  listeners.forEach((cb) => {
    try {
      cb(t);
    } catch {
      /* 单个订阅者出错不该影响其他人 */
    }
  });
}

/** 取走暂存的目标（只取一次）。空间页挂载时用它补上"事件到达时还没挂载"的那次请求。 */
export function takePendingOpenFile(): OpenFileTarget | null {
  const t = pending;
  pending = null;
  return t;
}

/** 只看一眼，不取走。 */
export function peekPendingOpenFile(): OpenFileTarget | null {
  return pending;
}

/** 订阅"打开文件"请求。 */
export function subscribeOpenFile(cb: (t: OpenFileTarget) => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}
