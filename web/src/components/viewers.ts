// 文件 → 展示器的分派表。
//
// 为什么集中在这里：判定散落在 CodeView / ArtifactCard 各自写正则，两边必然
// 给出不同答案（同一个 .csv 在一处当代码、在另一处当文本）。而「文件怎么展示」
// 是纯前端的事——后端不该关心渲染形态，它只管给内容和元信息。
//
// 判定按扩展名，刻意**不用** mime：文件在工作空间里，浏览器拿不到 mime，
// 而扩展名对源码/配置/文档这类场景已经足够；真正拿不准的（没扩展名、或内容
// 其实是二进制）交给下面的内容探测兜底。

import { monacoLangOf } from "../monacoLang";

export type ViewerKind =
  | "markdown" // 渲染后的文档（可切源码）
  | "html" // 网页：iframe 沙箱预览（可切源码）
  | "code" // 源码：Monaco + 语言高亮
  | "data" // 结构化数据 json/yaml/xml/toml：Monaco + 高亮，可格式化
  | "text" // 纯文本：Monaco plaintext
  | "image" // 图片：<img>
  | "pdf" // PDF：浏览器原生预览
  | "video" // 视频：浏览器原生播放器
  | "binary"; // 二进制：不渲染，只给下载

const MARKDOWN_EXTS = new Set(["md", "markdown", "mdx"]);

const IMAGE_EXTS = new Set([
  "png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "avif",
  // SVG 既能当图片预览、也是 XML 源码，默认按图片，展示器里提供「源码」切换
  "svg",
]);

// 结构化数据：能用 Monaco 高亮，且 json 可以一键格式化
const DATA_EXTS = new Set([
  "json", "jsonc", "json5", "yaml", "yml", "xml", "toml", "ini", "csv", "tsv",
]);

// 纯文本：Monaco 但没有（或不需要）语言服务
const TEXT_EXTS = new Set([
  "txt", "log", "env", "conf", "cfg", "config", "properties", "gitignore",
  "dockerignore", "editorconfig", "lock", "diff", "patch", "rst", "tex",
]);

const PDF_EXTS = new Set(["pdf"]);

// 网页：用浏览器渲染而不是看源码——agent 产出 HTML 的目的就是让人看到效果。
// 安全上后端会给 text/html 加 CSP，前端用不带 allow-same-origin 的 sandbox。
const HTML_EXTS = new Set(["html", "htm"]);

// 视频：交给浏览器内建播放器（比任何我们自己写的播放器都可靠）
const VIDEO_EXTS = new Set(["mp4", "webm", "ogg", "ogv", "mov", "m4v", "avi", "mkv"]);

// 明确的二进制：连读都不读，直接给下载
const BINARY_EXTS = new Set([
  "zip", "gz", "tar", "bz2", "xz", "7z", "rar",
  "exe", "dll", "so", "dylib", "bin", "class", "pyc", "pyo", "wasm",
  "mp3", "mp4", "avi", "mov", "mkv", "wav",
  "woff", "woff2", "ttf", "otf", "eot",
  "xlsx", "xls", "docx", "doc", "pptx", "ppt",
  "db", "sqlite", "sqlite3", "parquet", "pickle", "pkl",
]);

function extOf(name: string): string {
  const base = (name ?? "").split("/").pop() ?? "";
  if (!base.includes(".")) return "";
  return base.split(".").pop()!.toLowerCase();
}

/** 按扩展名判定展示器。未知扩展名按纯文本处理（Monaco 兜底，至少能看）。 */
export function viewerKindOf(name: string): ViewerKind {
  const ext = extOf(name);
  if (!ext) return "text";
  if (BINARY_EXTS.has(ext)) return "binary";
  if (PDF_EXTS.has(ext)) return "pdf";
  if (VIDEO_EXTS.has(ext)) return "video";
  if (HTML_EXTS.has(ext)) return "html";
  if (IMAGE_EXTS.has(ext)) return "image";
  if (MARKDOWN_EXTS.has(ext)) return "markdown";
  if (DATA_EXTS.has(ext)) return "data";
  if (TEXT_EXTS.has(ext)) return "text";
  // 剩下的交给语言映射：能映射上的当源码，映射不上仍是纯文本
  return monacoLangOf(name) ? "code" : "text";
}

/** 这类展示器能编辑并保存（图片/pdf/二进制不能）。 */
export function isEditableKind(kind: ViewerKind): boolean {
  return (
    kind === "markdown" ||
    kind === "html" ||
    kind === "code" ||
    kind === "data" ||
    kind === "text"
  );
}

/** 这类展示器走 Monaco（语言高亮可不同；html 默认看渲染结果，切源码时才用）。 */
export function isMonacoKind(kind: ViewerKind): boolean {
  return (
    kind === "markdown" ||
    kind === "html" ||
    kind === "code" ||
    kind === "data" ||
    kind === "text"
  );
}

/** 这类展示器用 URL 直接渲染，不需要先把内容读成文本。 */
export function isUrlKind(kind: ViewerKind): boolean {
  return kind === "html" || kind === "image" || kind === "pdf" || kind === "video";
}

/**
 * 这类文件交给**浏览器新窗口**打开，而不是塞进对话里的内联预览。
 *
 * 为什么：html / 图片 / pdf / 视频在 360px 的小框里基本没法看——网页要完整的
 * 视口、PDF 要有工具栏、视频要全屏。浏览器自己的查看器比我们内嵌的好用得多，
 * 而且新窗口能缩放、能另存、能分享链接。
 */
export function opensInNewTab(kind: ViewerKind): boolean {
  return kind === "html" || kind === "image" || kind === "pdf" || kind === "video";
}

/**
 * 内容探测：扩展名说它是文本，实际可能是二进制。
 *
 * 不探测的后果是 Monaco 里塞进一堆 \x00 和替换字符，既看不懂又卡——不如直接
 * 降级成「二进制，请下载」。两个信号：NUL 字节，以及 UTF-8 替换字符占比过高
 * （后端是 errors="replace" 读的，二进制文件解码后会满屏 U+FFFD）。
 */
export function looksBinary(text: string): boolean {
  if (!text) return false;
  if (text.includes("\u0000")) return true;
  const replaced = (text.match(/\uFFFD/g) ?? []).length;
  return text.length > 0 && replaced / text.length > 0.1;
}

/** 超过这个体量就不进编辑器：Monaco 渲染几 MB 文本会直接卡死浏览器。 */
export const MAX_EDIT_BYTES = 512_000;
