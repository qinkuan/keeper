/**
 * 表格（替代 antd Table）。
 *
 * 刻意**不**做「column 配置式」的表格：那套写法（dataIndex / render /
 * align / ellipsis / expandable / locale）本质是把「渲染一个表」拆成一堆
 * 回调，读代码时要在表头定义和单元格实现之间来回跳。这里只提供**容器与
 * 骨架**（表头样式、斑马/hover、横向滚动、加载态、空态），列的具体渲染
 * 由调用方直接写进 `columns[].render`，一行一列，跳转成本为零。
 *
 * 也不做分页——现有用到的几处都是 `pagination={false}`。
 */
import type { ReactNode } from "react";
import { Loader2 } from "lucide-react";
import { cn } from "../../lib/utils";

export interface Column<T> {
  key: string;
  /** 表头文案 */
  title: ReactNode;
  render: (row: T, index: number) => ReactNode;
  /** 数字列右对齐（默认左对齐） */
  align?: "left" | "right";
  /** 固定列宽，如 90 / "30%" */
  width?: number | string;
  /** 单元格自定义类（如 font-mono） */
  cellClassName?: string;
}

export function Table<T>({
  rows,
  columns,
  rowKey,
  loading,
  empty,
  /** 展开行：返回 null 表示该行不展开 */
  renderExpanded,
  expandedKey,
  className,
  minWidth,
}: {
  rows: T[];
  columns: Column<T>[];
  rowKey: (row: T, index: number) => string;
  loading?: boolean;
  empty?: ReactNode;
  renderExpanded?: (row: T, index: number) => ReactNode | null;
  /** 当前展开行的 key；不给则没有展开能力 */
  expandedKey?: string | null;
  className?: string;
  /** 表格最小宽度（列多时保留横向滚动而不是挤成一团） */
  minWidth?: number;
}) {
  if (loading) {
    return (
      <div
        className={cn(
          "flex items-center justify-center gap-2 rounded-card border border-line-soft bg-panel py-12 text-[13px] text-ink-muted",
          className
        )}
      >
        <Loader2 className="h-4 w-4 animate-spin text-brand" />
        加载中…
      </div>
    );
  }

  if (!rows.length) {
    return (
      <div
        className={cn(
          "rounded-card border border-dashed border-line bg-panel/70 py-14 text-center text-[13px] text-ink-faint",
          className
        )}
      >
        {empty ?? "暂无数据"}
      </div>
    );
  }

  const total = columns.reduce((s, c) => s + (typeof c.width === "number" ? c.width : 0), 0);
  const width = minWidth ?? (total > 0 ? total + 240 : undefined);

  return (
    <div
      className={cn(
        "overflow-x-auto rounded-card border border-line-soft bg-panel shadow-card",
        className
      )}
    >
      <table
        className="w-full border-collapse"
        style={{ minWidth: width ? `${width}px` : undefined }}
      >
        {/* 表头：灰底 + 更小的字号。桌面表格的表头是"标签"，不该和单元格
            同权重，所以压到 11.5px 并用次级色；再加一点字距让标签更像"标签"。 */}
        <thead>
          <tr className="border-b border-line-soft bg-surface/70 text-[11px] uppercase tracking-[0.05em] text-ink-faint">
            {columns.map((c) => (
              <th
                key={c.key}
                style={{ width: c.width }}
                className={cn(
                  "whitespace-nowrap px-3 py-2 font-medium",
                  c.align === "right" && "text-right"
                )}
              >
                {c.title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => {
            const key = rowKey(row, i);
            const expanded =
              expandedKey !== undefined && expandedKey === key
                ? renderExpanded?.(row, i) ?? null
                : null;
            return (
              <>
                <tr
                  key={key}
                  /* hover 用极淡的品牌底而不是灰底：一行行扫过去时，视线跟着
                     品牌色走，比中性灰更容易判断"当前选中的是哪一行" */
                  className="border-b border-line-soft transition-colors duration-100 last:border-0 hover:bg-brand/[0.035]"
                >
                  {columns.map((c) => (
                    <td
                      key={c.key}
                      className={cn(
                        "px-3 py-2 text-[12.5px] text-ink",
                        c.align === "right" && "text-right tabular-nums",
                        c.cellClassName
                      )}
                    >
                      {c.render(row, i)}
                    </td>
                  ))}
                </tr>
                {expanded && (
                  <tr key={`${key}-expanded`} className="bg-surface/40">
                    <td colSpan={columns.length} className="px-3 py-2.5">
                      {expanded}
                    </td>
                  </tr>
                )}
              </>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
