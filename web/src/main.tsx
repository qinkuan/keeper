import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "./theme.css";
// 注意：这里**不要** import "./monaco"。
// 它静态 import 了 monaco-editor（3.9MB），放在入口就等于首屏必下。
// 改由按需加载的 CodeView 引入——只有真的打开编码工作台才会拉它。
import App from "./App";

/**
 * 应用入口。
 *
 * 这里只有 QueryClient 与 StrictMode——UI 层已经完全自建（Tailwind +
 * `components/ui/*`），不再有第三方组件库需要 Provider 或主题注入：
 * 换肤靠 theme.css 里的 CSS 变量（默认浅色，`<html class="dark">` 切深色），
 * 不需要在 JS 里再维护一套主题 token。
 */
const qc = new QueryClient();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={qc}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>
);
