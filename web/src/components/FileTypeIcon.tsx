// 文件类型图标：彩色底 + 字母标记。
//
// 为什么是「色块 + 字母」而不是 lucide 的 FileXxx 线性图标
// --------------------------------------------------------
// ① 线性图标在 14px（目录树那一行）几乎认不出是 Excel 还是 Word——轮廓细节
//    已经被采样吃掉了；一个色块 + 一个字母在那个尺寸下还能一眼分开。
// ② 线性图标只有**形状**一条信息通道，颜色得靠另外一套图标重复一遍；色块
//    把「形状 + 颜色」合成一个视觉整体，扫一眼目录树就能分类。
// ③ 字母标记可以直接对应格式名（PDF / DOC / XLS），比猜轮廓快。
//
// 颜色从哪来
// ----------
// 每种类型一个 `--ft-*` CSS 变量（theme.css），浅色 / 深色各一套：深色底上
// 颜色要往亮里走一档，否则色块会糊进背景。**色值不放 JS**——项目换肤靠
// theme.css 的变量，不在组件里维护第二套主题 token（见 web/src/main.tsx）。
//
// 字母标记
// --------
// 小尺寸（xs/sm）只放**首字母**：14px 方块里塞 3 个字母会糊成一团。
// 大尺寸（md/lg）放完整标记（PDF / XLS / IMG…），此时空间够。
// 未知类型用文件名的首字母 + 灰底，至少能区分开不同文件。

export type FileTypeKind =
  | "pdf"
  | "sheet"
  | "slides"
  | "doc"
  | "markdown"
  | "text"
  | "image"
  | "video"
  | "audio"
  | "archive"
  | "code"
  | "data"
  | "html"
  | "db"
  | "exec"
  | "font"
  | "unknown";

interface KindMeta {
  /** 字母标记（大尺寸显示全文，小尺寸取首字母）。 */
  label: string;
  /** 中文类型名，hover 提示用。 */
  title: string;
}

const KIND_META: Record<FileTypeKind, KindMeta> = {
  pdf: { label: "PDF", title: "PDF 文档" },
  sheet: { label: "XLS", title: "表格" },
  slides: { label: "PPT", title: "演示文稿" },
  doc: { label: "DOC", title: "文档" },
  markdown: { label: "MD", title: "Markdown" },
  text: { label: "TXT", title: "文本" },
  image: { label: "IMG", title: "图片" },
  video: { label: "VID", title: "视频" },
  audio: { label: "AUD", title: "音频" },
  archive: { label: "ZIP", title: "压缩包" },
  code: { label: "SRC", title: "源代码" },
  data: { label: "DATA", title: "数据 / 配置" },
  html: { label: "HTML", title: "网页" },
  db: { label: "DB", title: "数据库" },
  exec: { label: "EXE", title: "可执行文件" },
  font: { label: "FONT", title: "字体" },
  unknown: { label: "FILE", title: "文件" },
};

/** 扩展名 → 类型。键是小写扩展名（不含点）。 */
const EXT_KIND: Record<string, FileTypeKind> = {
  // PDF
  pdf: "pdf",
  // 表格
  xlsx: "sheet", xls: "sheet", xlsm: "sheet", csv: "sheet", tsv: "sheet",
  numbers: "sheet", et: "sheet",
  // 演示
  pptx: "slides", ppt: "slides", pptm: "slides", key: "slides",
  // 文档
  docx: "doc", doc: "doc", docm: "doc", rtf: "doc", odt: "doc", pages: "doc",
  // Markdown
  md: "markdown", markdown: "markdown", mdx: "markdown",
  // 纯文本
  txt: "text", log: "text", env: "text", conf: "text", cfg: "text",
  properties: "text", lock: "text", diff: "text", patch: "text",
  // 图片
  png: "image", jpg: "image", jpeg: "image", gif: "image", webp: "image",
  bmp: "image", ico: "image", avif: "image", svg: "image", heic: "image",
  tif: "image", tiff: "image",
  // 视频
  mp4: "video", webm: "video", mov: "video", mkv: "video", avi: "video",
  m4v: "video", ogv: "video", wmv: "video", flv: "video",
  // 音频
  mp3: "audio", wav: "audio", flac: "audio", m4a: "audio", aac: "audio",
  ogg: "audio", wma: "audio",
  // 压缩包
  zip: "archive", tar: "archive", gz: "archive", bz2: "archive", xz: "archive",
  "7z": "archive", rar: "archive", dmg: "archive",
  // 源代码
  js: "code", jsx: "code", ts: "code", tsx: "code", mjs: "code", cjs: "code",
  py: "code", rb: "code", go: "code", rs: "code", java: "code", kt: "code",
  swift: "code", php: "code", c: "code", h: "code", cc: "code", cpp: "code",
  hpp: "code", cs: "code", vue: "code", svelte: "code", sh: "code",
  bash: "code", zsh: "code", fish: "code", ps1: "code", lua: "code", r: "code",
  sql: "code", scala: "code", dart: "code", m: "code",
  // 数据 / 配置
  json: "data", json5: "data", jsonc: "data", yaml: "data", yml: "data",
  xml: "data", toml: "data", ini: "data",
  // 网页
  html: "html", htm: "html",
  // 数据库
  db: "db", sqlite: "db", sqlite3: "db", parquet: "db", mdb: "db", dbf: "db",
  // 可执行 / 编译产物
  exe: "exec", dll: "exec", so: "exec", dylib: "exec", bin: "exec",
  class: "exec", pyc: "exec", pyo: "exec", wasm: "exec", app: "exec",
  // 字体
  woff: "font", woff2: "font", ttf: "font", otf: "font", eot: "font",
};

/** 取文件名的扩展名（小写，不含点）；没有扩展名回空串。 */
export function extOf(name: string): string {
  const base = (name ?? "").split(/[\\/]/).pop() ?? "";
  const dot = base.lastIndexOf(".");
  if (dot <= 0 || dot === base.length - 1) return "";
  return base.slice(dot + 1).toLowerCase();
}

/** 文件名 → 类型。认不出时回 "unknown"。 */
export function fileTypeOf(name: string): FileTypeKind {
  return EXT_KIND[extOf(name)] ?? "unknown";
}

/** 未知类型时用的首字母：文件名第一个字符（中文也保留）；取不到回 "?"。 */
function fallbackLabel(name: string): string {
  const base = (name ?? "").split(/[\\/]/).pop() ?? "";
  const ch = base.trim().charAt(0);
  return ch ? ch.toUpperCase() : "?";
}

/**
 * 同类里再按扩展名细分标记。
 *
 * 「表格」统称 XLS 没问题，但 csv 显示成 XLS 会让人以为能用 Excel 的功能打开——
 * 这类「名字会误导后续动作」的地方必须精确到扩展名。
 */
const LABEL_BY_EXT: Record<string, string> = {
  csv: "CSV",
  tsv: "TSV",
  xlsx: "XLS",
  xls: "XLS",
  xlsm: "XLS",
  pptx: "PPT",
  pptm: "PPT",
  docx: "DOC",
  docm: "DOC",
};

type IconSize = "xs" | "sm" | "md" | "lg";

const SIZE_STYLE: Record<IconSize, { box: number; font: number; radius: number }> = {
  // 目录树那一行：16px 是「字母还能认出来」的下限（14px 时字母只剩一个色点）
  xs: { box: 16, font: 10, radius: 3 },
  // 会话产物卡片：18px
  sm: { box: 18, font: 11, radius: 4 },
  // 预览区的空态卡：28px
  md: { box: 28, font: 12, radius: 6 },
  // 详情页大图：40px
  lg: { box: 40, font: 16, radius: 9 },
};

/** 标记越长字越小：4 个字母塞进 28px 会顶到边。 */
function fontFor(base: number, len: number): number {
  if (len >= 5) return Math.round(base * 0.68);
  if (len >= 4) return Math.round(base * 0.78);
  return base;
}

export interface FileTypeIconProps {
  /** 文件名（只取扩展名判类型；未知类型还会取首字母）。 */
  name: string;
  size?: IconSize;
  /** 额外 className。 */
  className?: string;
  /** 覆盖 hover 提示（默认用类型中文名）。 */
  title?: string;
}

/**
 * 文件类型图标：彩色底 + 字母标记。
 *
 * 底色走 `--ft-<kind>` 变量（theme.css 里浅色 / 深色各一套），文字一律白色
 * ——深底白字在两套主题下对比都过 3:1（图形元素的门槛），比「浅底深字」稳。
 */
export default function FileTypeIcon({
  name,
  size = "sm",
  className,
  title,
}: FileTypeIconProps) {
  const kind = fileTypeOf(name);
  const meta = KIND_META[kind];
  const dim = SIZE_STYLE[size];
  const ext = extOf(name);
  const full = kind === "unknown" ? fallbackLabel(name) : LABEL_BY_EXT[ext] ?? meta.label;
  // 小尺寸只放首字母：大尺寸才有空间放完整标记
  const text = size === "xs" || size === "sm" ? full.charAt(0) : full;

  return (
    <span
      className={className}
      title={title ?? (kind === "unknown" ? meta.title : `${meta.title}（${full}）`)}
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        flexShrink: 0,
        width: dim.box,
        height: dim.box,
        borderRadius: dim.radius,
        background: `var(--ft-${kind})`,
        color: "#fff",
        fontSize: fontFor(dim.font, text.length),
        fontWeight: 700,
        lineHeight: 1,
        letterSpacing: "-0.02em",
        userSelect: "none",
        boxShadow: "inset 0 0 0 1px rgba(0,0,0,0.06)",
      }}
    >
      {text}
    </span>
  );
}
