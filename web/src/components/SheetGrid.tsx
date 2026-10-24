import { useCallback, useEffect, useRef, useState } from "react";
import { cn, isComposingKey } from "../lib/utils";

/**
 * 表格网格**渲染 + 交互**（纯展示组件，不碰数据获取 / 解析）。
 *
 * 输入 `string[][]`：由 SheetViewer 用 SheetJS 解析好传进来。数据侧的事（loadBytes /
 * 编码 / 多 sheet / 保存）都不在这里。
 *
 * 已实现：
 * - **行号列 + 列号头**（Excel 式 A/B/C…、1/2/3…），均 sticky，横滚竖滚都盯得住；
 * - **绿色主题**：表头/选中/悬停统一 green 系（含 dark 变体）；未选中的格子
 *   边线用全站极淡发丝线（--kp-hairline），选中态绿底 + 描边，对比分明；
 * - **选中**：点单元格高亮（内描边），所在行号/列号联动高亮；
 * - **拖选区域**：按住拖动框选一片（淡绿高亮），Esc 取消；松手后选区**右下角**
 *   浮动「添加到对话」按钮（onQuoteSelection），把「文件 + 区域」加进对话；
 * - **空白延伸**：数据区外固定多渲染 EXTRA 行 / 列空白格（Excel 式网格感），
 *   空白区可直接双击编辑填新数据（落盘时 SheetViewer 负责扩 range）；
 * - **编辑**：双击单元格进入输入，Enter 提交 / Esc 取消 / 失焦提交；
 * - **行虚拟滚动**：固定行高，只渲染视口 ±BUFFER 行——可编辑网格下几千行全量渲染
 *   会卡，这是性能前提。想改行高/列宽/主题色，改顶部四个常量即可。
 *
 * 编辑只改**内存**（经 onChangeCell 上报父组件），不写盘；是否落盘由数据侧决定。
 *
 * 根节点带 `kp-sheet`（theme.css）：本项目关了 Tailwind preflight，全局没有
 * border-width/style/box-sizing 基线，这三条只在本类名下补，不波及全站其他组件。
 */

const ROW_H = 28; // 固定行高（虚拟滚动前提；单元格单行截断，hover/编辑看全值）
const HEAD_H = 28; // 列号头 / 行号列高度
const COL_W = 120; // 列宽
const ROWNUM_W = 44; // 行号列宽
const BUFFER = 6; // 视口上下多渲染几行，减少快滚空白
const EXTRA = 5; // 数据区外默认延伸的空白行 / 列数（Excel 式无限网格 + 可编辑填新数据）
const ADD_BTN_H = 24; // 选区右下角浮动「添加到对话」按钮的占位高度

/** 0→A、25→Z、26→AA…（Excel 列名） */
export function colName(i: number): string {
  let n = i + 1;
  let s = "";
  while (n > 0) {
    const m = (n - 1) % 26;
    s = String.fromCharCode(65 + m) + s;
    n = Math.floor((n - 1) / 26);
  }
  return s;
}

/** 归一化选区：左上/右下角坐标，0-based */
export interface GridRange {
  r1: number;
  c1: number;
  r2: number;
  c2: number;
}

export default function SheetGrid({
  rows,
  maxRows = 500,
  maxCols = 50,
  onChangeCell,
  onSelectionChange,
  onQuoteSelection,
}: {
  /** 二维单元格文本，第一行按表头加粗 */
  rows: string[][];
  maxRows?: number;
  maxCols?: number;
  /** 编辑提交回调（内存修改，不写盘）；不传则整体只读 */
  onChangeCell?: (r: number, c: number, value: string) => void;
  /** 拖选区域变化（null = 无选区）；父组件据此做联动 */
  onSelectionChange?: (sel: GridRange | null) => void;
  /** 点选区右下角浮动「添加到对话」按钮：上报 Excel 式区域引用（如 A1:C5）与规模 */
  onQuoteSelection?: (range: string, rows: number, cols: number) => void;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewH, setViewH] = useState(0);
  const [sel, setSel] = useState<{ r: number; c: number } | null>(null);
  const [editing, setEditing] = useState<{
    r: number;
    c: number;
    draft: string;
  } | null>(null);
  // 拖选：mousedown 起、mouseenter 扩、window mouseup 收（距离 0 视为单击）
  const [dragging, setDragging] = useState(false);
  const [dragStart, setDragStart] = useState<{ r: number; c: number } | null>(
    null
  );
  const [dragEnd, setDragEnd] = useState<{ r: number; c: number } | null>(null);
  const [selection, setSelectionState] = useState<GridRange | null>(null);

  const shown = rows.slice(0, maxRows);
  const truncatedRows = rows.length > maxRows;
  const rawCols = shown.reduce((m, r) => Math.max(m, r.length), 0);
  const colCount = Math.min(maxCols, rawCols);
  const truncatedCols = rawCols > maxCols;
  // 数据区外固定延伸 EXTRA 个空白行/列：拖选可以落到空白区，双击还能填新数据
  const renderRows = shown.length + EXTRA;
  const renderCols = colCount + EXTRA;
  const totalW = ROWNUM_W + renderCols * COL_W;
  const bodyH = renderRows * ROW_H;

  const setSelection = useCallback(
    (s: GridRange | null) => {
      setSelectionState(s);
      onSelectionChange?.(s);
    },
    [onSelectionChange]
  );

  // 滚动（rAF 节流）+ 视口高度：虚拟滚动的两个输入
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    setViewH(host.clientHeight);
    let raf = 0;
    const onScroll = () => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        setScrollTop(host.scrollTop);
      });
    };
    const ro = new ResizeObserver(() => setViewH(host.clientHeight));
    ro.observe(host);
    host.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      host.removeEventListener("scroll", onScroll);
      if (raf) cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, []);

  // 行列数变化（切 sheet / 重置）后清掉越界的选择与选区
  useEffect(() => {
    setSel((s) => (s && s.r < renderRows && s.c < renderCols ? s : null));
    setSelectionState((s) =>
      s && s.r2 < renderRows && s.c2 < renderCols ? s : null
    );
  }, [renderRows, renderCols]);

  // 松鼠标：定选区（起止同格 = 单击选中，否则框选一片）
  useEffect(() => {
    if (!dragging) return;
    const onUp = () => {
      if (dragStart && dragEnd) {
        if (dragStart.r === dragEnd.r && dragStart.c === dragEnd.c) {
          setSel({ r: dragStart.r, c: dragStart.c });
          setSelection(null);
        } else {
          const sel = {
            r1: Math.min(dragStart.r, dragEnd.r),
            c1: Math.min(dragStart.c, dragEnd.c),
            r2: Math.max(dragStart.r, dragEnd.r),
            c2: Math.max(dragStart.c, dragEnd.c),
          };
          // 活动格（浅底 + 描边、行/列号联动高亮）保持在**拖拽起点**，
          // 同 Excel：从下往上选时锚点是按下鼠标的那格，不是归一化后的左上角
          setSel({ r: dragStart.r, c: dragStart.c });
          setSelection(sel);
        }
      }
      setDragging(false);
      setDragStart(null);
      setDragEnd(null);
    };
    window.addEventListener("mouseup", onUp);
    return () => window.removeEventListener("mouseup", onUp);
  }, [dragging, dragStart, dragEnd, setSelection]);

  // Esc 清除选区
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setSelection(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setSelection]);

  // 提交：函数式 setState 保证「Enter 主动提交」与「失焦提交」只会生效一次
  const commit = useCallback(() => {
    setEditing((prev) => {
      if (!prev) return null;
      const { r, c, draft } = prev;
      if (String(rows[r]?.[c] ?? "") !== draft) onChangeCell?.(r, c, draft);
      return null;
    });
  }, [rows, onChangeCell]);

  const start = Math.max(0, Math.floor(scrollTop / ROW_H) - BUFFER);
  const end = Math.min(
    renderRows,
    Math.ceil((scrollTop + viewH) / ROW_H) + BUFFER
  );
  const visible: number[] = [];
  for (let i = start; i < end; i++) visible.push(i);

  // 拖选进行中：用起止点实时算预览选区——要优先于旧 selection，否则已有选区时
  // 再拖第二次，高亮还停在旧区域上不跟着走；松手后（selection）同样式
  const liveSel: GridRange | null =
    dragging && dragStart && dragEnd
      ? {
          r1: Math.min(dragStart.r, dragEnd.r),
          c1: Math.min(dragStart.c, dragEnd.c),
          r2: Math.max(dragStart.r, dragEnd.r),
          c2: Math.max(dragStart.c, dragEnd.c),
        }
      : selection;

  const inSelection = (r: number, c: number) =>
    liveSel
      ? r >= liveSel.r1 &&
        r <= liveSel.r2 &&
        c >= liveSel.c1 &&
        c <= liveSel.c2
      : false;

  // 边线策略（选中态一律不受影响）：未选中的数据格用**极淡发丝线**
  // （--kp-hairline，看得见格界但不抢戏），选中区靠绿底 + green-500 描边/外框
  // 自然跳出；表头/行号列深一档（line-soft）与数据区分开。
  // 边线能渲染出来靠根节点的 kp-sheet（theme.css）：preflight 关掉后全局没有
  // border-style/box-sizing，只在本组件类名下补，不波及全站。
  const lineB = "border-line-soft";
  const lineC = "border-[color:var(--kp-hairline)]";

  return (
    <div className="kp-sheet flex h-full min-h-0 flex-col">
      <div
        ref={hostRef}
        className="min-h-0 flex-1 select-none overflow-auto"
      >
        <div className="relative" style={{ width: totalW }}>
          {/* 列号头：sticky 顶部；横滚时随内容走，行号角格 sticky 左侧 */}
          <div className={cn("sticky top-0 z-20 flex border-b", lineB)} style={{ height: HEAD_H }}>
            <div
              className={cn(
                "sticky left-0 z-30 border-r bg-green-50 dark:bg-green-950/60",
                lineB
              )}
              style={{ width: ROWNUM_W, height: HEAD_H }}
            />
            {Array.from({ length: renderCols }).map((_, c) => (
              <div
                key={c}
                className={cn(
                  "flex items-center justify-center border-r text-[11px] font-medium transition-colors",
                  lineB,
                  sel?.c === c
                    ? "bg-green-200 text-green-900 dark:bg-green-900 dark:text-green-100"
                    : "bg-green-50 text-green-700 dark:bg-green-950/60 dark:text-green-300"
                )}
                style={{ width: COL_W, height: HEAD_H }}
              >
                {colName(c)}
              </div>
            ))}
          </div>
          {/* 身体：撑高 + 可见行绝对定位 */}
          <div className="relative" style={{ height: bodyH }}>
            {visible.map((i) => (
              <div
                key={i}
                className="absolute left-0 flex"
                style={{ top: i * ROW_H, height: ROW_H, width: totalW }}
              >
                {/* 行号：sticky 左侧，选中行高亮 */}
                <div
                  className={cn(
                    "sticky left-0 z-10 flex items-center justify-center border-b border-r text-[11px] tabular-nums transition-colors",
                    lineB,
                    sel?.r === i
                      ? "bg-green-200 text-green-900 dark:bg-green-900 dark:text-green-100"
                      : "bg-green-50/95 text-green-700 backdrop-blur dark:bg-green-950/50 dark:text-green-300"
                  )}
                  style={{ width: ROWNUM_W, height: ROW_H }}
                >
                  {i + 1}
                </div>
                {Array.from({ length: renderCols }).map((_, c) => {
                  const isSel = sel?.r === i && sel?.c === c;
                  const inSel = inSelection(i, c);
                  // 选区内：外边缘格用 inset 绿线拼出整片选区的外框（box-shadow 不改
                  // 布局，避免边框加宽把行顶歪）；内部格淡绿背景。
                  const edgeShadow = inSel
                    ? [
                        i === liveSel!.r1 ? "inset 0 2px 0 0 #22c55e" : "",
                        i === liveSel!.r2 ? "inset 0 -2px 0 0 #22c55e" : "",
                        c === liveSel!.c1 ? "inset 2px 0 0 0 #22c55e" : "",
                        c === liveSel!.c2 ? "inset -2px 0 0 0 #22c55e" : "",
                      ]
                        .filter(Boolean)
                        .join(", ")
                    : "";
                  const val = String(rows[i]?.[c] ?? "");
                  const isEditing = editing?.r === i && editing?.c === c;
                  return (
                    <div
                      key={c}
                      title={val}
                      onMouseDown={(e) => {
                        if (e.button !== 0 || isEditing) return;
                        // 先亮锚点：拖选时行/列号头实时联动（同 Excel）
                        setSel({ r: i, c });
                        setDragStart({ r: i, c });
                        setDragEnd({ r: i, c });
                        setDragging(true);
                      }}
                      onMouseEnter={() => {
                        if (dragging) setDragEnd({ r: i, c });
                      }}
                      onDoubleClick={() => {
                        if (!onChangeCell) return;
                        setSel({ r: i, c });
                        setEditing({ r: i, c, draft: val });
                      }}
                      className={cn(
                        "flex items-center overflow-hidden border-b border-r px-2 text-[12px] transition-colors",
                        lineC,
                        i === 0 && "font-medium",
                        isSel
                          ? "z-10 bg-green-50 ring-2 ring-inset ring-green-500 dark:bg-green-900/40"
                          : "text-ink hover:bg-green-50/60 dark:hover:bg-green-900/25",
                        inSel && !isSel && "bg-green-100/70 dark:bg-green-800/40"
                      )}
                      style={{
                        width: COL_W,
                        height: ROW_H,
                        boxShadow: edgeShadow || undefined,
                      }}
                    >
                      {isEditing ? (
                        <input
                          autoFocus
                          value={editing!.draft}
                          onChange={(e) =>
                            setEditing({ r: i, c, draft: e.target.value })
                          }
                          onBlur={commit}
                          onMouseDown={(e) => e.stopPropagation()}
                          onKeyDown={(e) => {
                            // 输入法拼字中：按键交给输入法（回车=确认候选词）
                            if (isComposingKey(e)) return;
                            if (e.key === "Enter") {
                              e.preventDefault();
                              commit();
                            } else if (e.key === "Escape") {
                              e.preventDefault();
                              setEditing(null);
                            }
                          }}
                          className="w-full border-0 bg-transparent text-[12px] text-ink outline-none"
                        />
                      ) : (
                        <span className="truncate">{val}</span>
                      )}
                    </div>
                  );
                })}
              </div>
            ))}
            {/* 选区右下角浮动按钮：松手定选区后贴在选区右下角（随内容一起滚动），
                点它把「文件 + 表格区域」引用加进对话；拖选中（dragging）先藏起来 */}
            {selection && !dragging && onQuoteSelection && (
              <div
                className="absolute z-40 flex items-center justify-end pr-1"
                style={{
                  top: selection.r2 * ROW_H + ROW_H - ADD_BTN_H,
                  left: ROWNUM_W + selection.c2 * COL_W,
                  width: COL_W,
                  height: ADD_BTN_H,
                }}
              >
                <button
                  type="button"
                  title={`把 ${colName(selection.c1)}${
                    selection.r1 + 1
                  }:${colName(selection.c2)}${
                    selection.r2 + 1
                  } 添加到对话`}
                  onClick={() =>
                    onQuoteSelection(
                      `${colName(selection.c1)}${selection.r1 + 1}:${colName(
                        selection.c2
                      )}${selection.r2 + 1}`,
                      selection.r2 - selection.r1 + 1,
                      selection.c2 - selection.c1 + 1
                    )
                  }
                  className="rounded-full bg-green-600 px-2 py-1 text-[11px] font-medium text-white shadow-md transition-colors hover:bg-green-700 dark:bg-green-500 dark:hover:bg-green-400"
                >
                  添加到对话
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
      {(truncatedRows || truncatedCols) && (
        <div className="shrink-0 border-t border-line-soft px-2 py-1 text-[11px] text-ink-faint">
          仅预览前 {maxRows} 行 / {maxCols} 列，完整内容请下载或用 AI 编辑。
        </div>
      )}
    </div>
  );
}
