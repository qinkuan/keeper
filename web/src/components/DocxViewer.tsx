/**
 * Word(.docx) 只读渲染器。
 *
 * 输入是内核解析出来的 IR（见 api/types.ts 的 DocxDocument），不是 docx 本身——
 * 解析在 Python 侧（keeper/docx/），前端只负责"画"。
 *
 * 为什么第一版用 HTML/CSS 而不是 canvas 自绘：HTML/CSS 一天能出可读结果，
 * 而 canvas 自绘要自己做字体度量、行断行、表格分页。IR 是稳定契约，渲染层
 * 以后想换成 canvas 时，这个组件整体换掉即可，接口不变。
 *
 * **如实说清做不到什么**：浮动定位、文本框的精确位置、SmartArt、公式、修订、
 * 批注都不还原。内核会把遇到的这类特性列在 unsupported 里，这里把它显示出来——
 * 与其渲染出一个"看着挺像但内容对不上"的版本，不如告诉用户"用系统应用看原样"。
 */
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type MutableRefObject,
} from "react";
import { AlertTriangle } from "lucide-react";
import type {
  DocxBlock,
  DocxBorder,
  DocxCell,
  DocxDocument,
  DocxParagraph,
  DocxRun,
  DocxTab,
  DocxTable,
} from "../api/types";

/** Word 的对齐值 → CSS text-align */
function alignOf(a?: string): React.CSSProperties["textAlign"] | undefined {
  switch (a) {
    case "both":
    case "distribute":
      return "justify";
    case "center":
      return "center";
    case "right":
    case "end":
      return "right";
    case "left":
    case "start":
      return "left";
    default:
      return undefined;
  }
}

/** run 的字体族：中文优先用 eastAsia，西文用 ascii 兜底 */
function fontOf(r?: DocxRun): string | undefined {
  if (!r) return undefined;
  const cn = r.font_east_asia;
  const en = r.font;
  if (cn && en) return `"${cn}", "${en}", serif`;
  if (cn) return `"${cn}", serif`;
  if (en) return `"${en}", serif`;
  return undefined;
}

function runStyle(r: DocxRun): React.CSSProperties {
  const st: React.CSSProperties = {};
  const f = fontOf(r);
  if (f) st.fontFamily = f;
  if (r.size) st.fontSize = `${r.size}pt`;
  if (r.bold) st.fontWeight = 700;
  if (r.italic) st.fontStyle = "italic";
  const deco: string[] = [];
  if (r.underline) deco.push("underline");
  if (r.strike) deco.push("line-through");
  if (deco.length) st.textDecoration = deco.join(" ");
  if (r.color) st.color = r.color;
  if (r.highlight) st.backgroundColor = r.highlight;
  if (r.vert_align === 1) {
    st.verticalAlign = "super";
    st.fontSize = `${(r.size ?? 10) * 0.75}pt`;
  } else if (r.vert_align === 2) {
    st.verticalAlign = "sub";
    st.fontSize = `${(r.size ?? 10) * 0.75}pt`;
  }
  return st;
}

/** 段落样式（不含文字级） */
function paraStyle(p: DocxParagraph): React.CSSProperties {
  const st: React.CSSProperties = {};
  const align = alignOf(p.align);
  if (align) st.textAlign = align;
  if (p.indent_left) st.marginLeft = `${p.indent_left}pt`;
  if (p.indent_right) st.marginRight = `${p.indent_right}pt`;
  if (p.space_before) st.marginTop = `${p.space_before}pt`;
  if (p.space_after) st.marginBottom = `${p.space_after}pt`;
  if (p.line_height) st.lineHeight = p.line_height;
  // 首行缩进为负 = 悬挂缩进（列表项的首行顶格）
  if (p.indent_first_line) st.textIndent = `${p.indent_first_line}pt`;
  return st;
}

/** 编号标记的缩进宽度（磅）：层级越深越靠右 */
function markerIndent(p: DocxParagraph): number {
  const lv = p.numbering?.level ?? 0;
  return 18 + lv * 18;
}

/** 制表位（磅，从左边距算起）。 */
interface TabStop {
  pos: number;
  val: string;
  leader: string;
}

/** Word 默认制表位：1.27cm = 36 磅。 */
const DEFAULT_TAB_PT = 36;

/* 大文档分批渲染：首屏先画这么多块，剩下的每帧补一批。 */
const INITIAL_RENDER_BLOCKS = 1200;
const RENDER_BATCH_BLOCKS = 800;

/**
 * 估算一段文字的宽度（磅）：中日韩全角、西文半宽。
 *
 * 够用即可——制表位定位只需要"大概到哪儿了"，精确字形测量得靠字体度量，
 * 浏览器里拿不到。差几个磅不影响观感。
 */
function textWidthPt(text: string, sizePt: number): number {
  let w = 0;
  for (const ch of text) {
    w += /[\u2e80-\u9fff\uac00-\ud7af\uff00-\uffef\u3000-\u303f\u1100-\u11ff]/.test(ch)
      ? sizePt
      : sizePt * 0.5;
  }
  return w;
}

/** 文字 run → 元素（保持链接/格式） */
function renderRunText(r: DocxRun, text: string, key: number): React.ReactNode {
  const st = runStyle(r);
  if (r.link) {
    return (
      <a
        key={key}
        href={r.link}
        target="_blank"
        rel="noreferrer noopener"
        style={{
          ...st,
          color: st.color ?? "#2563eb",
          textDecoration: "underline",
        }}
      >
        {text}
      </a>
    );
  }
  return (
    <span key={key} style={st}>
      {text}
    </span>
  );
}

/** 取当前位置之后的下一个制表位；没有就用默认间隔（1.27cm）接着排。 */
function pickStop(stops: TabStop[], cursor: number): TabStop {
  for (const s of stops) if (s.pos > cursor + 0.5) return s;
  const next = Math.ceil((cursor + 1) / DEFAULT_TAB_PT) * DEFAULT_TAB_PT;
  return { pos: next, val: "left", leader: "none" };
}

/**
 * 制表位到下一段文字之间的"空隙"，顺带画前导符（目录里的点线）。
 *
 * 没有前导符时就是一个定宽的空白块——后面的文字因此落在制表位上。
 */
function leaderSpan(
  stop: TabStop,
  gap: number,
  sizePt: number,
  key: number
): React.ReactNode {
  if (gap <= 0.2) return null;
  const leader = (stop.leader || "none").toLowerCase();
  if (leader === "underscore") {
    return (
      <span
        key={key}
        style={{
          display: "inline-block",
          width: `${gap}pt`,
          height: "0.55em",
          borderBottom: "1px solid currentColor",
          verticalAlign: "baseline",
        }}
      />
    );
  }
  const ch =
    leader === "dot"
      ? "."
      : leader === "hyphen"
        ? "-"
        : leader === "middledot"
          ? "·"
          : "";
  if (!ch) {
    return (
      <span key={key} style={{ display: "inline-block", width: `${gap}pt` }} />
    );
  }
  const n = Math.max(0, Math.floor(gap / (sizePt * 0.3)));
  return (
    <span
      key={key}
      style={{
        display: "inline-block",
        width: `${gap}pt`,
        whiteSpace: "pre",
        overflow: "hidden",
      }}
    >
      {ch.repeat(n)}
    </span>
  );
}

/**
 * 段落内容（带制表位还原）。
 *
 * 早期版本把制表符当成"两个空格"，于是目录、条款对照表这类**靠制表位对齐**
 * 的排版全歪了。现在按 IR 里的 w:tabs 计算每个制表位的位置：
 * - left   → 文字落在制表位上（空隙在左）
 * - right  → 这一段文字右对齐到制表位（空隙在左，文字占右边）
 * - center → 这一段文字以制表位为中心
 */
function Runs({
  runs,
  tabs,
  origin = 0,
}: {
  runs?: DocxRun[];
  tabs?: DocxTab[];
  /** 文字起点（磅，从左边距算）：通常是段落缩进 + 编号占位宽度。 */
  origin?: number;
}) {
  if (!runs?.length) return null;

  const stops: TabStop[] = (tabs ?? [])
    .filter((t) => typeof t.pos === "number" && (t.val ?? "left") !== "clear")
    .map((t) => ({
      pos: t.pos as number,
      val: t.val ?? "left",
      leader: t.leader ?? "none",
    }))
    .sort((a, b) => a.pos - b.pos);

  const baseSize = 10.5;
  const out: React.ReactNode[] = [];
  let key = 0;

  // 先按 <w:br/> 分行，再按制表符分片段
  type Item = { kind: "text" | "tab"; run: DocxRun; text?: string };
  const lines: Item[][] = [[]];
  runs.forEach((r) => {
    const line = lines[lines.length - 1];
    if (r.line_break) {
      lines.push([]);
      return;
    }
    if (r.tab) line.push({ kind: "tab", run: r });
    else line.push({ kind: "text", run: r, text: r.text ?? "" });
  });

  lines.forEach((line, li) => {
    if (li > 0) out.push(<br key={key++} />);

    // 片段：两个制表符之间的一段文字
    const segs: { texts: { run: DocxRun; text: string }[]; width: number }[] = [
      { texts: [], width: 0 },
    ];
    line.forEach((it) => {
      if (it.kind === "tab") {
        segs.push({ texts: [], width: 0 });
        return;
      }
      const seg = segs[segs.length - 1];
      const text = it.text ?? "";
      seg.texts.push({ run: it.run, text });
      seg.width += textWidthPt(text, it.run.size ?? baseSize);
    });

    let cursor = origin;
    segs[0].texts.forEach((t) => out.push(renderRunText(t.run, t.text, key++)));

    for (let i = 1; i < segs.length; i++) {
      const stop = pickStop(stops, cursor);
      const segWidth = segs[i].width;
      let gap: number;
      if (stop.val === "right") gap = stop.pos - (cursor + segWidth);
      else if (stop.val === "center") gap = (stop.pos - (cursor + segWidth)) / 2;
      else gap = stop.pos - cursor;
      gap = Math.max(0, gap);
      out.push(leaderSpan(stop, gap, baseSize, key++));
      segs[i].texts.forEach((t) => out.push(renderRunText(t.run, t.text, key++)));
      cursor = stop.val === "center" ? stop.pos + segWidth / 2 : stop.pos;
    }
  });

  return <>{out}</>;
}

function Paragraph({ p, blockIdx }: { p: DocxParagraph; blockIdx?: number }) {
  const marker = p.numbering?.marker;
  const indent = marker ? markerIndent(p) : 0;
  const st = paraStyle(p);
  // 有编号时：标记占固定宽度（模拟 Word 的悬挂缩进），段落整体右移
  if (marker) {
    st.marginLeft = `${(p.indent_left ?? 0) + indent}pt`;
    st.textIndent = `${-indent}pt`;
  }
  /* 文字起点（制表位换算的基准）：段落缩进 + 编号占位宽度。
     制表位本身是从**左边距**算起的，缩进会让文字离左边距更远。 */
  const origin = (p.indent_left ?? 0) + indent;
  return (
    <p
      /* 块序号：与内核切片时用的序号**必须一致**（IR blocks 的 1-based 下标）——
         检索命中的行号就是它，搜索结果点进来要靠它跳到准确的那一段。 */
      data-block-idx={blockIdx}
      style={{
        marginTop: st.marginTop ?? 0,
        marginBottom: st.marginBottom ?? 0,
        marginLeft: st.marginLeft,
        marginRight: st.marginRight,
        textAlign: st.textAlign,
        lineHeight: st.lineHeight,
        textIndent: st.textIndent,
      }}
    >
      {marker && (
        <span
          style={{
            display: "inline-block",
            width: `${indent}pt`,
            marginLeft: `${-indent}pt`,
          }}
        >
          {marker}
        </span>
      )}
      <Runs runs={p.runs} tabs={p.tabs} origin={origin} />
      {p.images?.map((img, i) => (
        <img
          key={i}
          src={img.data_uri}
          alt=""
          style={{
            display: "block",
            margin: "6px 0",
            width: img.width ? `${img.width}pt` : undefined,
            height: img.height ? `${img.height}pt` : undefined,
            maxWidth: "100%",
          }}
        />
      ))}
    </p>
  );
}

/** 边框 → CSS border 简写。 */
function borderCss(b?: DocxBorder): string {
  if (!b) return "none";
  const style = (b.style ?? "single").toLowerCase();
  if (style === "none" || style === "nil" || style === "hidden") return "none";
  // OOXML 的 w:sz 已是磅（抽取时换算过），这里换像素
  const px = Math.max(1, Math.round((b.size ?? 0.5) * (96 / 72) * 10) / 10);
  const color = b.color ?? "#000";
  switch (style) {
    case "double":
      return `${Math.max(3, px * 3)}px double ${color}`;
    case "dotted":
      return `${px}px dotted ${color}`;
    case "dashed":
    case "dashdot":
    case "dotdash":
      return `${px}px dashed ${color}`;
    case "thick":
      return `${Math.max(2, px * 2)}px solid ${color}`;
    default:
      return `${px}px solid ${color}`;
  }
}

function Cell({ c }: { c: DocxCell }) {
  // rowspan=0 = 纵向合并的延续行：这一格不渲染（内容由上面的格承担）
  if (c.rowspan === 0) return null;
  /* 边框按 IR 里解析好的「有效边框」画：Word 里没有框线的表格不该被我们
     画上一圈灰框（早期版本一律加 1px 灰边，看着像有框，其实是无框表格）。 */
  const b = c.borders ?? {};
  const st: React.CSSProperties = {
    borderTop: borderCss(b.top),
    borderRight: borderCss(b.right),
    borderBottom: borderCss(b.bottom),
    borderLeft: borderCss(b.left),
    padding: "2px 4px",
    verticalAlign:
      c.valign === "center" ? "middle" : c.valign === "bottom" ? "bottom" : "top",
  };
  if (c.shading) st.backgroundColor = c.shading;
  if (c.width) st.width = `${c.width}pt`;
  return (
    <td colSpan={c.colspan && c.colspan > 1 ? c.colspan : undefined} style={st}>
      <Blocks blocks={c.blocks} inCell />
    </td>
  );
}

function Table({ t, blockIdx }: { t: DocxTable; blockIdx?: number }) {
  const widths = t.col_widths ?? [];
  return (
    <table
      data-block-idx={blockIdx}
      style={{
        borderCollapse: "collapse",
        width: "100%",
        margin: "6px 0",
        tableLayout: widths.length ? "fixed" : "auto",
      }}
    >
      {widths.length > 0 && (
        <colgroup>
          {widths.map((w, i) => (
            <col key={i} style={{ width: `${w}pt` }} />
          ))}
        </colgroup>
      )}
      <tbody>
        {(t.rows ?? []).map((row, ri) => (
          <tr key={ri}>
            {row.map((c, ci) => (
              <Cell key={ci} c={c} />
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Blocks({
  blocks,
  inCell,
  limit,
}: {
  blocks?: DocxBlock[];
  inCell?: boolean;
  /** 顶层分批渲染时只画前 N 块（块序号仍按原始下标，定位口径不变）。 */
  limit?: number;
}) {
  if (!blocks?.length) return null;
  const list = limit && limit < blocks.length ? blocks.slice(0, limit) : blocks;
  return (
    <>
      {list.map((b, i) => {
        if (b.type === "table")
          return <Table key={i} t={b as DocxTable} blockIdx={inCell ? undefined : i + 1} />;
        const p = b as DocxParagraph;
        // 单元格里的段落：Word 默认段前段后为 0，行距也不放大
        if (inCell) {
          const st = paraStyle(p);
          return (
            <p
              key={i}
              style={{
                marginTop: st.marginTop ?? 0,
                marginBottom: st.marginBottom ?? 0,
                marginLeft: st.marginLeft,
                textAlign: st.textAlign,
                lineHeight: st.lineHeight ?? 1.35,
                textIndent: st.textIndent,
              }}
            >
              <Runs runs={p.runs} tabs={p.tabs} origin={p.indent_left ?? 0} />
            </p>
          );
        }
        return <Paragraph key={i} p={p} blockIdx={i + 1} />;
      })}
    </>
  );
}

/**
 * docx 预览的缩放信息：报给外层标题栏（缩放控件搬上去之后用的）。
 *
 * `zoom` 是标签（「适宽」或「85%」）；`canIn/canOut` 说明按钮该不该可点；
 * `meta` 是文档元信息（「2654 段 · 12 个表格」）。
 */
export interface DocxInfo {
  zoom: string;
  canIn: boolean;
  canOut: boolean;
  meta: string;
}

/**
 * 缩放命令句柄。
 *
 * 缩放控件现在放在**外层标题栏**（和「添加到对话」同一行——预览区自己的那条
 * 工具栏已经撤掉，免得文件名出现两遍），但缩放状态仍归本组件（"适宽"要按
 * 预览区可视宽度实时算）。所以：标题栏通过句柄下命令，组件通过 onInfo 回报。
 */
export interface DocxCtl {
  zoomIn?: () => void;
  zoomOut?: () => void;
  zoomFit?: () => void;
}

export function DocxViewer({
  doc,
  ctl,
  onInfo,
  jump,
}: {
  doc: DocxDocument;
  /** 外层标题栏的缩放命令句柄（见 DocxCtl） */
  ctl?: MutableRefObject<DocxCtl>;
  /** 把缩放状态与文档元信息报给外层标题栏 */
  onInfo?: (info: DocxInfo) => void;
  /** 检索命中的落点：line = 块序号（与内核切片一致），terms = 命中词。 */
  jump?: { line?: number; terms?: string[]; seq?: number } | null;
}) {
  const page = doc.page ?? {};
  const unsupported = doc.unsupported ?? [];

  const widthPt = page.width ?? 595.28;
  const pad = {
    paddingTop: `${page.margin_top ?? 72}pt`,
    paddingRight: `${page.margin_right ?? 72}pt`,
    paddingBottom: `${page.margin_bottom ?? 72}pt`,
    paddingLeft: `${page.margin_left ?? 72}pt`,
  };

  /* 页面宽度自适应。
   *
   * A4 宽 595.28pt = 793px，而预览区常常比这窄（侧栏 + 对话面板占掉一部分）。
   * 早期版本把页面写死 793px，于是：文字按 793px 排版、每行末尾落在可视区之外
   * 被裁掉，还多出一条横向滚动条——用户看到的"宽度和视图对不上"。
   *
   * 正确做法跟 Word 一样：**默认"适应页宽"**，按可视宽度等比缩小整页。
   * 用 transform: scale 而不是改字号：字号是文档语义（该 16pt 就 16pt），
   * 缩放只是"看的方式"，两者混在一起会让后续的字号/行距计算全歪。
   */
  const PX_PER_PT = 96 / 72;
  const pageWidthPx = widthPt * PX_PER_PT;
  const pageHeightPx = (page.height ?? 841.89) * PX_PER_PT;

  const wrapRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const [fitScale, setFitScale] = useState(1);
  const [zoom, setZoom] = useState<number | null>(null); // null = 适应页宽
  const [contentH, setContentH] = useState(0);

  const scale = zoom ?? fitScale;

  // 可视宽度变化 → 重算"适应页宽"的比例
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const measure = () => {
      const avail = el.clientWidth - 24; // 两侧留白，别让纸贴边
      /* 下限 0.45：预览区被挤得极窄时（侧栏+文件树+对话面板都占着），
       * 再缩就成一团糊了——这时宁可让页面略微超出、能横向滚动，
       * 也不要给一个看不清字的"完整页面"。 */
      setFitScale(Math.max(0.45, Math.min(1, avail / pageWidthPx)));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [pageWidthPx]);

  // 内容高度（未缩放）→ 缩放后占位高度。transform 不改布局，得自己算，
  // 否则滚动条会按缩放前的高度给，页面下方留一大截空白。
  useEffect(() => {
    const el = contentRef.current;
    if (!el) return;
    const measure = () => setContentH(el.scrollHeight);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  /* 大文档：分批渲染。
   *
   * 实测（民法典 2654 块 / 6557 个 DOM 节点）：一次性渲染约 380ms，
   * 滚动 60fps——**并不慢**，所以没上虚拟滚动（那会把"适宽缩放 + 占位高度 +
   * 检索定位"三处都变成难点，收益却不匹配风险）。
   *
   * 这里做的是更省事也更安全的一层保护：块数多时先画首屏，其余分批补上，
   * 让主线程在每一批之间有机会响应用户操作（滚动、点击、跳转）。
   * 代价只是"文档尾部晚零点几秒出现"，页面上会明说还剩多少段没画。 */
  const totalBlocks = doc.blocks?.length ?? 0;
  const [renderCount, setRenderCount] = useState(() =>
    Math.min(totalBlocks, INITIAL_RENDER_BLOCKS)
  );
  useEffect(() => {
    if (renderCount >= totalBlocks) return;
    const t = setTimeout(
      () =>
        setRenderCount((c) => Math.min(totalBlocks, c + RENDER_BATCH_BLOCKS)),
      0
    );
    return () => clearTimeout(t);
  }, [renderCount, totalBlocks]);
  const pendingBlocks = Math.max(0, totalBlocks - renderCount);

  /* 检索命中 → 滚到那一段并闪一下。
   *
   * line 是**块序号**（IR blocks 的 1-based 下标），与内核切片时用的序号同一个
   * 口径——所以"搜到的第 245 条"和"文档里第 245 段"是同一段。
   * 依赖 seq（每次点击 +1）：同一个词在两处命中时，能再次跳到下一处。
   *
   * 依赖里带上 contentH：刚打开文档时占位高度还是"一页"的初值（内容高度要
   * 等测量完），此时滚过去会被容器高度截断——等高度到位后再滚一次才准。
   * 依赖里带上 renderCount：大文档分批渲染时，目标段可能还没画出来，
   * 下一批补上后这个 effect 会再跑一次，届时再跳。
   * 闪动只播一次（用 seq 记住），重复滚动不再闪。 */
  const lastJumpSeq = useRef<number | null>(null);
  useEffect(() => {
    if (!jump?.line) return;
    const el = wrapRef.current?.querySelector<HTMLElement>(
      `[data-block-idx="${jump.line}"]`
    );
    if (!el) return;
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    if (lastJumpSeq.current === jump.seq) return;
    lastJumpSeq.current = jump.seq ?? null;
    el.animate?.(
      [
        { backgroundColor: "rgba(250, 204, 21, 0.45)" },
        { backgroundColor: "rgba(250, 204, 21, 0.45)", offset: 0.6 },
        { backgroundColor: "rgba(250, 204, 21, 0)" },
      ],
      { duration: 1800, easing: "ease-out" }
    );
  }, [jump?.seq, jump?.line, contentH, renderCount]);

  const meta = useMemo(() => {
    const n = doc.paragraph_count ?? 0;
    const t = (doc.blocks ?? []).filter((b) => b.type === "table").length;
    return [n > 0 ? `${n} 段` : null, t > 0 ? `${t} 个表格` : null]
      .filter(Boolean)
      .join(" · ");
  }, [doc]);

  /* 缩放命令：控件搬到了外层标题栏（和「添加到对话」同一行），状态还在这儿。
   *
   * 命令挂在句柄上而不是 props 回调：外层只需要"点一下放大/缩小/适宽"这三个
   * 动作，拿到的是函数，不需要知道缩放是怎么算出来的。 */
  useEffect(() => {
    const c = ctl?.current;
    if (!c) return;
    c.zoomIn = () =>
      setZoom((z) => Math.min(3, Math.round(((z ?? fitScale) + 0.1) * 10) / 10));
    c.zoomOut = () =>
      setZoom((z) => Math.max(0.3, Math.round(((z ?? fitScale) - 0.1) * 10) / 10));
    c.zoomFit = () => setZoom(null);
    return () => {
      c.zoomIn = undefined;
      c.zoomOut = undefined;
      c.zoomFit = undefined;
    };
  }, [ctl, fitScale]);

  /* 把缩放状态与文档元信息报给外层标题栏（文件名只留标题栏那一处）。 */
  useEffect(() => {
    onInfo?.({
      zoom: zoom === null ? "适宽" : `${Math.round(scale * 100)}%`,
      canIn: scale < 3 - 1e-6,
      canOut: scale > 0.3 + 1e-6,
      meta,
    });
  }, [zoom, scale, meta, onInfo]);

  return (
    <div className="flex h-full min-h-0 flex-col bg-surface">
      {/* 这里原来有一条自己的工具条（文件名 + 缩放 + 「用 Word 打开」）。
          它和「我的空间」标题栏那行重复了文件名（同一个文件名前后各出现一次），
          现在整条撤掉：缩放控件搬去标题栏（与「添加到对话」同一行），
          文件名也只留标题栏那一处。 */}

      {/* 保真提示：说清这份渲染缺了什么，别让人以为这就是原样 */}
      {unsupported.length > 0 && (
        <div className="flex shrink-0 items-start gap-2 border-b border-amber-500/20 bg-amber-500/[0.07] px-4 py-2 text-[12px] leading-5 text-amber-700 dark:text-amber-300">
          <AlertTriangle className="mt-[1px] h-3.5 w-3.5 shrink-0" />
          <span>
            这个文档含暂不支持的特性（{unsupported.join("、")}），
            页面内的还原不完全——需要原样版面请用系统应用打开。
          </span>
        </div>
      )}

      {/* 页面：白纸 + 灰底，跟 Word 的页面观感一致。
          整页用 transform 等比缩放（scale 由"适应页宽"或手动缩放决定），
          外层占位盒按缩放后的尺寸算高度，避免滚动条留一大截空白。

          **为什么缩放层要绝对定位**：transform 不改布局，缩放层自己的布局宽度
          始终是未缩放的页面宽（A4 = 793px）。它若留在普通流里，这个 793px 的
          布局盒会把外层撑出横向滚动条——外层一旦被横向滚动（甚至只是浏览器
          自动滚过去），白纸就会偏向一边、甚至被裁掉一截。
          所以：占位盒（=缩放后的页面尺寸）负责滚动与居中，缩放层绝对定位贴在
          占位盒左上角，占位盒 overflow:hidden 把多出来的那部分裁掉。 */}
      <div ref={wrapRef} className="min-h-0 flex-1 overflow-auto">
        <div
          style={{
            width: pageWidthPx * scale,
            height: (contentH || pageHeightPx) * scale,
            margin: "20px auto",
            position: "relative",
            overflow: "hidden",
            boxSizing: "border-box",
            borderRadius: "2px",
            /* 阴影挂在占位盒上：内容盒被裁了，阴影放里面会被一起裁掉 */
            boxShadow: "0 1px 3px rgba(0,0,0,0.12), 0 8px 24px rgba(0,0,0,0.08)",
          }}
        >
          <div
            style={{
              position: "absolute",
              top: 0,
              left: 0,
              width: pageWidthPx,
              transform: `scale(${scale})`,
              transformOrigin: "top left",
            }}
          >
            <div
              ref={contentRef}
              style={{
                width: pageWidthPx,
                /* **必须是 border-box**：本项目关了 Tailwind preflight，全站是
                   content-box —— 那样 width 不含内边距，纸面会被撑宽（左右页边距
                   各加一次），文字区也跟着变宽，字排到纸外头去了。
                   border-box 下 width = 纸宽，内边距吃在纸宽之内，文字区正好是
                   「纸宽 − 左右页边距」。 */
                boxSizing: "border-box",
                background: "#fff",
                color: "#000",
                borderRadius: "2px",
                ...pad,
              }}
            >
              <Blocks blocks={doc.blocks} limit={renderCount} />
              {pendingBlocks > 0 && (
                <div className="py-3 text-center text-[11.5px] text-ink-faint">
                  正在渲染剩余 {pendingBlocks} 段…
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
