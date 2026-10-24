// Office 文档（Word / PowerPoint）的类型判定。
//
// 这两个格式**只走「用系统应用打开」**，不做页面内预览：OOXML 的版面信息
// （字体字号、分栏、浮动定位、动画、图表）远多于文字，只抽文字渲染出来与本地
// 应用差异大到「根本没法看」；高保真方案（LibreOffice 转 PDF / OnlyOffice）与
// 这个项目的轻量桌面定位冲突。详见 doc/roadmap.md。

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
