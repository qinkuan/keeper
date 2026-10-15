/**
 * Monaco 编辑器主题。
 *
 * Monaco 自带的 `vs` 是**纯白**背景，而我们的画布是 `--kp-surface-soft`
 * (#fafafa)。差这一档色，编辑器看起来就像嵌进来的一块第三方控件——边框、
 * 行号槽、选中态也都还是它自己那套。`inherit: true` 保留官方配色规则，
 * 只把表面与 chrome 换掉。
 *
 * **为什么单独成文件**：主题本身只需要一个 monaco 实例。这里不 import
 * "monaco-editor"（那会把 3.9MB 拖进静态依赖链），而是导出一个函数，
 * 由两个调用方各自在拿到实例的时机执行：
 *   - `monaco.ts`：本地 monaco 的初始化路径（编码工作台）；
 *   - `ArtifactCard`：走 CDN 版 @monaco-editor/react，用它的 beforeMount 回调。
 * 后者因此不需要为了换个配色而把整个 monaco 打进包里。
 */

type MonacoLike = {
  editor: { defineTheme: (name: string, theme: unknown) => void };
};

const KEEPER_LIGHT = {
  base: "vs",
  inherit: true,
  rules: [],
  /* 取值对齐 Chrome 主题：表面用中性灰阶（#F8F8F8 / #D8D8D8），
     光标与选中态用 Chrome 蓝。下面的 hex 必须与 theme.css 的
     --kp-surface-soft / --kp-border 保持同步。 */
  colors: {
    "editor.background": "#F8F8F8",
    "editorGutter.background": "#F8F8F8",
    "editorLineNumber.foreground": "#B4B4B4",
    /* 行号激活态对齐应用正文色 #474747 */
    "editorLineNumber.activeForeground": "#474747",
    /* 选中态：主色的低饱和底（不是高亮黄），与全站选中态同色系 */
    "editor.selectionBackground": "#CFE0FC",
    "editor.inactiveSelectionBackground": "#E7EFFD",
    "editor.lineHighlightBackground": "#F0F0F0",
    "editorIndentGuide.background1": "#ECECEC",
    "editorIndentGuide.activeBackground1": "#D8D8D8",
    "editorWidget.background": "#FFFFFF",
    "editorWidget.border": "#D8D8D8",
    "editorCursor.foreground": "#5B9BF5",
    "scrollbarSlider.background": "#C4C4C455",
    "scrollbarSlider.hoverBackground": "#A8A8A899",
    "scrollbarSlider.activeBackground": "#8E8E8E66",
    "editorOverviewRuler.border": "#00000000",
  },
};

export const KEEPER_THEME = "keeper-light";

/** 注册主题。重复调用无副作用（defineTheme 只是覆盖同名定义）。 */
export function applyKeeperTheme(monaco: MonacoLike) {
  monaco.editor.defineTheme(KEEPER_THEME, KEEPER_LIGHT);
}