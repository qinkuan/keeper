import { useEffect, useState } from "react";
import * as XLSX from "xlsx";
import { cn } from "../lib/utils";

/**
 * 「我的空间」表格预览：xlsx / xls / csv / tsv。
 *
 * 用 SheetJS(xlsx) 解析成行列渲染成 HTML 表格——它一个库通吃四种格式，省得 CSV 另写
 * 解析。只读预览：大表按行/列封顶，超出部分提示下载或用 AI 编辑（编辑留到后续）。
 */

const MAX_ROWS = 500;
const MAX_COLS = 50;

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

type SheetData = { name: string; rows: string[][] };

export default function SheetViewer({
  name,
  loadBytes,
}: {
  name: string;
  loadBytes: () => Promise<ArrayBuffer>;
}) {
  const [sheets, setSheets] = useState<SheetData[]>([]);
  const [active, setActive] = useState(0);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");

  useEffect(() => {
    let dead = false;
    (async () => {
      setLoading(true);
      setErr("");
      try {
        const buf = await loadBytes();
        const bytes = new Uint8Array(buf);
        // CSV/TSV 是纯文本：自己按编码解成字符串再喂 SheetJS（否则中文乱码）；
        // xlsx/xls 是二进制 zip，直接按字节读，编码它内部按 UTF-8 处理。
        const wb = /\.(csv|tsv)$/i.test(name)
          ? XLSX.read(decodeText(bytes), { type: "string" })
          : XLSX.read(bytes, { type: "array" });
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
        if (dead) return;
        setSheets(out);
        setActive(0);
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
  const rows = cur.rows.slice(0, MAX_ROWS);
  const truncatedRows = cur.rows.length > MAX_ROWS;
  const colCount = rows.reduce((m, r) => Math.max(m, r.length), 0);
  const cols = Math.min(MAX_COLS, colCount);
  const truncatedCols = colCount > MAX_COLS;

  return (
    <div className="flex h-full min-h-0 flex-col">
      {sheets.length > 1 && (
        <div className="flex shrink-0 gap-1 overflow-x-auto border-b border-line-soft px-2 py-1.5">
          {sheets.map((s, i) => (
            <button
              key={s.name}
              type="button"
              onClick={() => setActive(i)}
              className={cn(
                "shrink-0 rounded px-2 py-1 text-[12px] transition-colors",
                i === active
                  ? "bg-brand/10 font-medium text-brand"
                  : "text-ink-muted hover:text-ink"
              )}
            >
              {s.name}
            </button>
          ))}
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-auto p-2">
        <table className="border-collapse text-[12px] leading-5">
          <tbody>
            {rows.map((row, ri) => (
              <tr key={ri}>
                {Array.from({ length: cols }).map((_, ci) => (
                  <td
                    key={ci}
                    className={cn(
                      "max-w-[320px] whitespace-pre-wrap break-words border border-line-soft px-2 py-1 align-top text-ink",
                      ri === 0 && "bg-surface font-medium text-ink-strong"
                    )}
                  >
                    {String(row[ci] ?? "")}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {(truncatedRows || truncatedCols) && (
        <div className="shrink-0 border-t border-line-soft px-2 py-1 text-[11px] text-ink-faint">
          仅预览前 {MAX_ROWS} 行 / {MAX_COLS} 列，完整内容请下载或用 AI 编辑。
        </div>
      )}
    </div>
  );
}
