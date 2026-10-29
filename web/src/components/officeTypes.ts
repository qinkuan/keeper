// Office 文档（Word / PowerPoint）的类型判定。
//
// 两种格式的待遇**不一样**（2026-10 起）：
//   docx —— 自研只读渲染（keeper/docx/ 解析成 IR + components/DocxViewer 画）：
//     段落/表格/图片/编号/样式继承都还原；浮动定位、SmartArt、公式、修订不还原，
//     渲染器会显式列出并给「用系统应用打开」兜底。
//   pptx —— 幻灯片靠版式还原，自研性价比太低，仍只提供「用系统应用打开」。
//
// 详见 doc/roadmap.md。

/** 属于 Office 文档。含 .docm/.pptm——它们同样只能用系统应用打开。 */
const OFFICE_EXTS = new Set(["docx", "docm", "pptx", "pptm"]);

/** 取最后一段文件名的扩展名（小写）；没有扩展名回空串。 */
function extOf(name: string): string {
  const base = (name ?? "").split("/").pop() ?? "";
  const dot = base.lastIndexOf(".");
  if (dot <= 0 || dot === base.length - 1) return "";
  return base.slice(dot + 1).toLowerCase();
}

export function isOfficeName(name: string): boolean {
  return OFFICE_EXTS.has(extOf(name));
}

/** docx 与 pptx 的提示文案不同（一个是文档、一个是演示文稿），所以分开给。 */
export function officeKindOf(name: string): "docx" | "pptx" | null {
  const ext = extOf(name);
  if (ext === "pptx" || ext === "pptm") return "pptx";
  if (ext === "docx" || ext === "docm") return "docx";
  return null;
}
