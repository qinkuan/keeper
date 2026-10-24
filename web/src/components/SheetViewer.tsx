import { useEffect, useRef, useState } from "react";
import * as XLSX from "xlsx";
import { ExternalLink } from "lucide-react";
import { cn, isComposingKey } from "../lib/utils";
import { toast } from "./ui/Toast";
import SheetGrid, { type GridRange } from "./SheetGrid";
import FileTypeIcon from "./FileTypeIcon";

/**
 * 「我的空间」表格：xlsx / xls / csv / tsv。
 *
 * 用 SheetJS(xlsx) 解析——它一个库通吃四种格式，省得 CSV 另写解析。网格渲染与交互
 * （行列号 / 选中 / 拖选 / 编辑 / 虚拟滚动）在 ./SheetGrid.tsx，本组件只负责数据侧。
 *
 * 数据侧职责：
 * 1. 取数 + 解析（CSV/TSV 按 UTF-8 优先 / GBK 兜底解码，否则中文乱码）；
 * 2. 多 sheet 切换 + **重命名**（改原 workbook 的 SheetNames/Sheets，随编辑一起落盘）；
 * 3. **编辑落盘**：编辑时改的是解析出来的**原 workbook 的那个 cell**（而不是从显示
 *    数据重建文件——那样会丢公式 / 格式 / 列宽），再整体序列化写回原文件。
 *    xlsx/xls 走二进制写（space.writeBinary），csv/tsv 走文本写（space.write）。
 *    连续编辑防抖 600ms 合并为一次写回；切文件 / 卸载前会把未落盘的修改落盘；
 * 4. **选区引用到对话**：把用户在网格里拖选的区域（如 A1:C5）经 onQuoteSelection
 *    上报，由页面层挂进聊天输入框（见 lib/quote.ts 的 addQuoteToChat）。
 */

type SheetData = { name: string; rows: string[][] };

/**
 * 把 CSV/TSV 的字节解成字符串：UTF-8 优先，解出替换符（U+FFFD）再按 GBK 兜底。
 * 为什么不直接让 SheetJS 解：它读纯文本默认走 CP1252，UTF-8 中文会被解成
 * "æœˆ" 这类乱码；而 Windows 上另存的 csv 又常是 GBK，两头都要照顾。
 */
function decodeText(bytes: Uint8Array): string {
  const utf8 = new TextDecoder("utf-8").decode(bytes);
  if (!utf8.includes("\uFFFD")) return utf8;
  try {
    return new TextDecoder("gbk").decode(bytes);
  } catch {
    return utf8;
  }
}

/** CSV/TSV 序列化：引号 / 分隔符按 RFC4180 规则转义，\r\n 行尾（Excel 友好）。 */
function toDelimited(rows: string[][], sep: string): string {
  const esc = (s: string) =>
    /["\n\r]/.test(s) || s.includes(sep)
      ? '"' + s.replace(/"/g, '""') + '"'
      : s;
  return rows
    .map((r) => r.map((c) => esc(String(c ?? ""))).join(sep))
    .join("\r\n");
}

/**
 * 把 (r,c) 处的修改应用回显示用 rows：编辑落在网格延伸的空白格（越界）时，
 * 先把 rows 撑到 r 行 c 列再写——空串补齐，与 sheet_to_json 的 defval:"" 一致。
 */
function setCellInRows(
  rows: string[][],
  r: number,
  c: number,
  v: string
): string[][] {
  const next = rows.map((row) => row.slice());
  while (next.length <= r) next.push([]);
  for (let ri = 0; ri <= r; ri++) {
    while (next[ri].length <= c) next[ri].push("");
  }
  next[r][c] = v;
  return next;
}

export default function SheetViewer({
  name,
  loadBytes,
  persist,
  onQuoteSelection,
  onOpenWithApp,
}: {
  name: string;
  loadBytes: () => Promise<ArrayBuffer>;
  /** 把序列化后的内容写回原文件：csv/tsv 给 text，xlsx/xls 给 binary(ArrayBuffer)。 */
  persist?: (p: { text?: string; binary?: ArrayBuffer }) => Promise<void>;
  /** 拖选区域后「添加到对话」：上报 Excel 式区域引用（如 A1:C5）与规模。 */
  onQuoteSelection?: (
    range: string,
    rows: number,
    cols: number
  ) => void;
  /** 「用系统应用打开」：不传则不显示该按钮（会话侧的文件卡片没有 space 桥）。 */
  onOpenWithApp?: () => void;
}) {
  const [sheets, setSheets] = useState<SheetData[]>([]);
  const [active, setActive] = useState(0);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  // 解析出的**原 workbook**：编辑/重命名只改它，写回时整体序列化——保留公式/格式。
  const wbRef = useRef<XLSX.WorkBook | null>(null);
  // 保存状态机：idle → saving → saved / error（底部状态条展示）
  const [saveState, setSaveState] = useState<
    "idle" | "saving" | "saved" | "error"
  >("idle");
  const [saveMsg, setSaveMsg] = useState("");
  const saveTimer = useRef<number | null>(null);
  // 始终指向最新的 persist / 保存函数：防抖回调和卸载兜底要用，避免闭包陈旧。
  const persistRef = useRef(persist);
  persistRef.current = persist;
  const runSaveRef = useRef<() => Promise<void>>();
  // sheet 重命名编辑态
  const [renaming, setRenaming] = useState<number | null>(null);
  const [renameDraft, setRenameDraft] = useState("");

  const isCsvTsv = /\.(csv|tsv)$/i.test(name);
  const isTsv = /\.tsv$/i.test(name);

  useEffect(() => {
    let dead = false;
    (async () => {
      setLoading(true);
      setErr("");
      // 切文件前，把上一个文件还在防抖窗口里的修改先落盘（refs 此刻还是旧文件的）。
      if (saveTimer.current) {
        window.clearTimeout(saveTimer.current);
        saveTimer.current = null;
        try {
          await runSaveRef.current?.();
        } catch {
          /* 旧文件保存失败不阻塞新文件打开，状态条已报过 */
        }
      }
      try {
        const buf = await loadBytes();
        const bytes = new Uint8Array(buf);
        // CSV/TSV 是纯文本：自己按编码解成字符串再喂 SheetJS（否则中文乱码）；
        // xlsx/xls 是二进制 zip，直接按字节读，编码它内部按 UTF-8 处理。
        const wb = isCsvTsv
          ? XLSX.read(decodeText(bytes), { type: "string" })
          : XLSX.read(bytes, { type: "array" });
        if (dead) return;
        wbRef.current = wb;
        const out: SheetData[] = wb.SheetNames.map((sn) => {
          const ws = wb.Sheets[sn];
          // header:1 → 数组的数组；raw:false → 按显示值取（日期/数字格式化过）；
          // defval:"" → 空单元格补空串，避免 undefined 漏渲染。
          const rows = XLSX.utils.sheet_to_json(ws, {
            header: 1,
            raw: false,
            defval: "",
          }) as string[][];
          return { name: sn, rows };
        });
        setSheets(out);
        setActive(0);
        setRenaming(null);
        setSaveState("idle");
        setSaveMsg("");
      } catch (e: any) {
        if (!dead) setErr(e?.message || "解析失败");
      } finally {
        if (!dead) setLoading(false);
      }
    })();
    return () => {
      dead = true;
    };
  }, [name]);

  // 序列化当前 workbook 并写回原文件
  const runSave = async () => {
    if (!persistRef.current) return;
    const wb = wbRef.current;
    if (!wb) return;
    setSaveState("saving");
    try {
      if (isCsvTsv) {
        const ws = wb.Sheets[wb.SheetNames[0]];
        const rows = XLSX.utils.sheet_to_json(ws, {
          header: 1,
          raw: false,
          defval: "",
        }) as string[][];
        await persistRef.current({ text: toDelimited(rows, isTsv ? "\t" : ",") });
      } else {
        const out = XLSX.write(wb, {
          bookType: /\.xls$/i.test(name) ? "xls" : "xlsx",
          type: "array",
        });
        // SheetJS type:"array" 可能是 ArrayBuffer 或 Uint8Array，统一成 ArrayBuffer
        const ab: ArrayBuffer =
          out instanceof ArrayBuffer
            ? out
            : (out as Uint8Array).buffer.slice(
                (out as Uint8Array).byteOffset,
                (out as Uint8Array).byteOffset + (out as Uint8Array).byteLength
              );
        await persistRef.current({ binary: ab });
      }
      setSaveState("saved");
      setSaveMsg(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
    } catch (e: any) {
      setSaveState("error");
      setSaveMsg(e?.message || "写入失败");
    }
  };
  runSaveRef.current = runSave;

  // 连续编辑合并为一次写回（600ms 静默期）
  const scheduleSave = () => {
    if (!persist) return;
    setSaveState("saving");
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      saveTimer.current = null;
      void runSave();
    }, 600);
  };

  // 卸载前落盘未保存的修改
  useEffect(
    () => () => {
      if (saveTimer.current) {
        window.clearTimeout(saveTimer.current);
        saveTimer.current = null;
        void runSaveRef.current?.();
      }
    },
    []
  );

  const changeCell = (r: number, c: number, v: string) => {
    const wb = wbRef.current;
    const sn = sheets[active]?.name;
    const ws = wb && sn ? wb.Sheets[sn] : null;
    if (ws) {
      // 只改原 workbook 的这一个 cell——其余公式 / 格式 / 列宽原样保留
      ws[XLSX.utils.encode_cell({ r, c })] = { v, t: "s" };
      // 编辑落在数据区外（网格延伸的空白格）时同步扩 !ref，否则 XLSX.write /
      // sheet_to_json 仍按旧 range 裁掉这个新 cell，白改
      const ref = XLSX.utils.decode_range(ws["!ref"] || "A1");
      if (r > ref.e.r || c > ref.e.c) {
        ref.e.r = Math.max(ref.e.r, r);
        ref.e.c = Math.max(ref.e.c, c);
        ws["!ref"] = XLSX.utils.encode_range(ref);
      }
    }
    setSheets((prev) =>
      prev.map((s, i) =>
        i !== active
          ? s
          : {
              ...s,
              rows: setCellInRows(s.rows, r, c, v),
            }
      )
    );
    scheduleSave();
  };

  // ── sheet 重命名（双击 tab 进入；改 workbook 的 SheetNames + Sheets key）──
  const startRename = (i: number) => {
    setRenaming(i);
    setRenameDraft(sheets[i]?.name ?? "");
  };
  const commitRename = (i: number) => {
    const draft = renameDraft.trim();
    setRenaming(null);
    const oldName = sheets[i]?.name;
    if (!draft || !oldName || draft === oldName) return;
    if (
      sheets.some(
        (s, j) => j !== i && s.name.toLowerCase() === draft.toLowerCase()
      )
    ) {
      toast.error(`已有同名工作表「${draft}」`);
      return;
    }
    const wb = wbRef.current;
    if (wb && wb.SheetNames.includes(oldName)) {
      const idx = wb.SheetNames.indexOf(oldName);
      wb.SheetNames[idx] = draft;
      wb.Sheets[draft] = wb.Sheets[oldName];
      delete wb.Sheets[oldName];
    }
    setSheets((prev) =>
      prev.map((s, j) => (j === i ? { ...s, name: draft } : s))
    );
    scheduleSave();
  };

  if (loading)
    return (
      <div className="flex h-full items-center justify-center text-[13px] text-ink-muted">
        解析表格中…
      </div>
    );
  if (err)
    return (
      <div className="p-4 text-[13px] text-red-500">无法预览该表格：{err}</div>
    );
  if (!sheets.length)
    return <div className="p-4 text-[13px] text-ink-muted">空表格</div>;

  const cur = sheets[Math.min(active, sheets.length - 1)];

  // 网格选区 → 引用到对话：多 sheet 时区域带上表名（Excel 的 Sheet2!A1:C5 语法），
  // 避免 AI 读错表；「文件路径 + 表格区域」的描述由 UserSpacePage 拼（addQuoteToChat）
  const quoteSelection = (range: string, rows: number, cols: number) => {
    onQuoteSelection?.(sheets.length > 1 ? `${cur.name}!${range}` : range, rows, cols);
  };

  return (
    <div className="kp-sheet flex h-full min-h-0 flex-col">
      {/* 顶部条：文件名 + 「用系统应用打开」。
          放在 sheet tab 之上——文件名是文件级信息，tab 是表级。
          按钮在调用方给了 onOpenWithApp 时才出现（会话侧的文件卡片没有 space 桥）。 */}
      <div className="flex shrink-0 items-center gap-2 border-b border-line-soft px-2.5 py-1.5">
        <FileTypeIcon name={name} size="xs" />
        <span
          className="min-w-0 flex-1 truncate text-[12px] font-medium text-ink-muted"
          title={name}
        >
          {name}
        </span>
        {onOpenWithApp && (
          <button
            type="button"
            title="用系统里的 Excel 打开（原样，含图表与格式）"
            onClick={onOpenWithApp}
            className="flex h-6 shrink-0 items-center gap-1 rounded-md border border-line px-2 text-[11.5px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong"
          >
            <ExternalLink className="h-3 w-3" />
            用系统应用打开
          </button>
        )}
      </div>

      {sheets.length > 1 && (
        <div className="flex shrink-0 gap-1 overflow-x-auto border-b border-line-soft px-2 py-1.5">
          {sheets.map((s, i) =>
            renaming === i ? (
              <input
                key={s.name}
                autoFocus
                value={renameDraft}
                onChange={(e) => setRenameDraft(e.target.value)}
                onMouseDown={(e) => e.stopPropagation()}
                onClick={(e) => e.stopPropagation()}
                onBlur={() => commitRename(i)}
                onKeyDown={(e) => {
                  // 输入法拼字中：按键交给输入法（回车=确认候选词），
                  // 否则拼音打一半就把文件名改了
                  if (isComposingKey(e)) return;
                  e.stopPropagation();
                  if (e.key === "Enter") {
                    e.preventDefault();
                    commitRename(i);
                  } else if (e.key === "Escape") {
                    e.preventDefault();
                    setRenaming(null);
                  }
                }}
                className="w-28 shrink-0 rounded border border-green-400 bg-white px-2 py-0.5 text-[12px] text-ink outline-none dark:bg-green-950/80"
              />
            ) : (
              <button
                key={s.name}
                type="button"
                title="双击重命名"
                onClick={() => setActive(i)}
                onDoubleClick={() => startRename(i)}
                className={cn(
                  "shrink-0 rounded px-2 py-1 text-[12px] transition-colors",
                  i === active
                    ? "bg-green-100 font-medium text-green-700 dark:bg-green-900/50 dark:text-green-300"
                    : "text-ink-muted hover:text-ink"
                )}
              >
                {s.name}
              </button>
            )
          )}
        </div>
      )}
      <SheetGrid
        rows={cur.rows}
        onChangeCell={changeCell}
        onQuoteSelection={quoteSelection}
      />
      {/* 底部状态条：只显示保存状态（「添加到对话」已移到网格内选区右下角的浮动按钮） */}
      {saveState !== "idle" && (
        <div className="flex shrink-0 items-center justify-between gap-2 border-t border-line-soft px-2 py-1 text-[11px]">
          <span
            className={cn(
              "min-w-0 truncate",
              saveState === "error"
                ? "text-red-500"
                : saveState === "saving"
                  ? "text-ink-muted"
                  : "text-green-600 dark:text-green-400"
            )}
          >
            {saveState === "saving"
              ? "保存中…"
              : saveState === "saved"
                ? `已保存到原文件 ${saveMsg}`
                : `保存失败：${saveMsg}`}
          </span>
          {saveState === "error" && (
            <button
              type="button"
              onClick={() => void runSave()}
              className="shrink-0 rounded px-1.5 py-0.5 text-ink-muted transition-colors hover:bg-black/5 hover:text-ink dark:hover:bg-white/10"
            >
              重试
            </button>
          )}
        </div>
      )}
    </div>
  );
}
