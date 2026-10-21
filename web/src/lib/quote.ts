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
