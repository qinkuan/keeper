// Monaco 本地化加载：不再从 CDN 拉取，离线也可用。
// 必须在任何 <Editor>/<DiffEditor> 挂载前执行一次。
import * as monaco from "monaco-editor";
import { loader } from "@monaco-editor/react";

import editorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import jsonWorker from "monaco-editor/esm/vs/language/json/json.worker?worker";
import cssWorker from "monaco-editor/esm/vs/language/css/css.worker?worker";
import htmlWorker from "monaco-editor/esm/vs/language/html/html.worker?worker";
import tsWorker from "monaco-editor/esm/vs/language/typescript/ts.worker?worker";

// 配置 Web Worker。纯只读展示/ diff 只需 editorWorker，
// 但 TS/JS/JSON/CSS/HTML 的语言服务（校验、提示）也一并接好更稳。
declare global {
  interface Window {
    MonacoEnvironment?: monaco.Environment;
  }
}

self.MonacoEnvironment = {
  getWorker(_workerId, label) {
    switch (label) {
      case "json":
        return new jsonWorker();
      case "css":
      case "scss":
      case "less":
        return new cssWorker();
      case "html":
      case "handlebars":
      case "razor":
        return new htmlWorker();
      case "typescript":
      case "javascript":
        return new tsWorker();
      default:
        return new editorWorker();
    }
  },
};

// 让 @monaco-editor/react 使用本地 monaco 实例，而非从 CDN 加载。
loader.config({ monaco });
