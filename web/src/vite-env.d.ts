/// <reference types="vite/client" />

/**
 * 桌面壳 preload 暴露的内置浏览器控制面（desktop/preload.js）。
 *
 * 只在 Electron 里存在；Web 形态没有这个对象，前端据此降级到 iframe。
 */
export interface KeeperBrowserState {
  url: string;
  title: string;
  loading: boolean;
  canGoBack: boolean;
  canGoForward: boolean;
}

/** 占位盒子的屏幕矩形（CSS px，相对窗口内容区） */
export interface KeeperBrowserRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface KeeperBrowserBridge {
  available: true;
  open: (url: string, rect: KeeperBrowserRect) => void;
  close: () => void;
  command: (
    cmd: "back" | "forward" | "reload" | "stop" | "load" | "external",
    arg?: string
  ) => void;
  setBounds: (rect: KeeperBrowserRect) => void;
  setVisible: (visible: boolean) => void;
  isOpen: () => Promise<boolean>;
  /** 主动问当前地址：onState 事件可能早于页面订阅到达，state.url 会是空的 */
  currentUrl: () => Promise<string>;
  onState: (cb: (s: KeeperBrowserState) => void) => () => void;
  onError: (
    cb: (e: { code: number; description: string; url: string }) => void
  ) => () => void;
  onClosed: (cb: () => void) => () => void;
}

declare global {
  interface Window {
    /** Electron 桌面壳注入；Web 形态为 undefined */
    keeperBrowser?: KeeperBrowserBridge;
  }
}
