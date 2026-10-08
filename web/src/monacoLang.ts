// 扩展名 → Monaco 语言 id（用于高亮与 diff 着色），供代码卡片与 coding 编辑器共用。

export const MONACO_LANG: Record<string, string> = {
  // 通用编程语言
  py: "python",
  pyw: "python",
  pyi: "python",
  java: "java",
  kt: "kotlin",
  kts: "kotlin",
  scala: "scala",
  js: "javascript",
  jsx: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  ts: "typescript",
  tsx: "typescript",
  vue: "html", // Monaco 没有 vue 语言，用 html 高亮最接近
  svelte: "html",
  go: "go",
  rs: "rust",
  c: "c",
  h: "c",
  cpp: "cpp",
  cc: "cpp",
  cxx: "cpp",
  hpp: "cpp",
  cs: "csharp",
  rb: "ruby",
  php: "php",
  swift: "swift",
  dart: "dart",
  lua: "lua",
  pl: "perl",
  pm: "perl",
  r: "r",
  jl: "julia",
  // shell / 构建
  sh: "shell",
  bash: "shell",
  zsh: "shell",
  fish: "shell",
  ps1: "powershell",
  dockerfile: "dockerfile",
  makefile: "makefile",
  gradle: "groovy",
  // 数据 / 标记 / 样式
  sql: "sql",
  json: "json",
  jsonc: "json",
  json5: "jsonc",
  yaml: "yaml",
  yml: "yaml",
  xml: "xml",
  html: "html",
  htm: "html",
  css: "css",
  scss: "scss",
  less: "less",
  toml: "ini", // Monaco 无 toml，ini 高亮最接近
  ini: "ini",
  csv: "plaintext",
  tsv: "plaintext",
  proto: "protobuf",
  graphql: "graphql",
};

export function monacoLangOf(name: string): string | null {
  const ext = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  return MONACO_LANG[ext] ?? null;
}
