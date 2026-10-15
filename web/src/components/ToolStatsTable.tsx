import { useState } from "react";
import type { ToolStat } from "../api/types";
import { fmtDuration, fmtTokens } from "./UsageTag";
import { Table } from "./ui/Table";
import type { Column } from "./ui/Table";
import { cn } from "../lib/utils";

/** 可排序列的定义：取值 + 比较，表头点击即排序（再点反向） */
type SortKey = keyof ToolStat;

/**
 * 工具维度统计表：**每列都可点表头排序**，默认按总耗时降序（后端返回的也是这个序）。
 *
 * 关注点：
 * - `avg_output_size` / `max_output_size` 大 → 该工具的返回会原样进下一轮 prompt，
 *   是上下文膨胀的主要来源；
 * - `truncated` 高 → 返回经常超过内联上限，给模型的文本被换成了引用或截断，说明该精简工具输出了。
 */
export default function ToolStatsTable({ stats }: { stats: ToolStat[] }) {
  const [sortKey, setSortKey] = useState<SortKey>("total_duration_ms");
  const [desc, setDesc] = useState(true);

  if (!stats.length) return null;

  const sorted = [...stats].sort((a, b) => {
    const x = a[sortKey];
    const y = b[sortKey];
    const r =
      typeof x === "number" && typeof y === "number"
        ? x - y
        : String(x).localeCompare(String(y));
    return desc ? -r : r;
  });

  /** 表头：点击排序，当前列显示方向 */
  const Th = ({ k, title, align }: { k: SortKey; title: string; align?: "right" }) => (
    <button
      type="button"
      onClick={() => {
        if (sortKey === k) setDesc((d) => !d);
        else {
          setSortKey(k);
          setDesc(true);
        }
      }}
      className={cn(
        "inline-flex items-center gap-1 transition-colors hover:text-ink",
        sortKey === k && "text-brand"
      )}
    >
      {title}
      <span className="text-[10px] leading-none">
        {sortKey === k ? (desc ? "▼" : "▲") : "▾"}
      </span>
      {align === "right" && <span className="sr-only">右对齐</span>}
    </button>
  );

  const columns: Column<ToolStat>[] = [
    {
      key: "tool",
      title: <Th k="tool" title="工具" />,
      render: (r) => <span className="font-mono text-[12px]">{r.tool}</span>,
    },
    {
      key: "calls",
      title: <Th k="calls" title="调用" />,
      align: "right",
      width: 80,
      render: (r) => r.calls,
    },
    {
      key: "errors",
      title: <Th k="errors" title="失败" />,
      align: "right",
      width: 80,
      render: (r) =>
        r.errors ? (
          <span className="rounded-md bg-red-500/15 px-1.5 py-0.5 text-[11px] leading-4 text-red-600 dark:text-red-300">
            {r.errors}
          </span>
        ) : (
          <span className="text-ink-faint">0</span>
        ),
    },
    {
      key: "total_duration_ms",
      title: <Th k="total_duration_ms" title="总耗时" />,
      align: "right",
      width: 100,
      render: (r) => fmtDuration(r.total_duration_ms),
    },
    {
      key: "avg_duration_ms",
      title: <Th k="avg_duration_ms" title="平均耗时" />,
      align: "right",
      width: 100,
      render: (r) => fmtDuration(r.avg_duration_ms),
    },
    {
      key: "avg_output_size",
      title: <Th k="avg_output_size" title="平均返回" />,
      align: "right",
      width: 100,
      render: (r) => fmtTokens(r.avg_output_size),
    },
    {
      key: "max_output_size",
      title: <Th k="max_output_size" title="最大返回" />,
      align: "right",
      width: 100,
      render: (r) => fmtTokens(r.max_output_size),
    },
    {
      key: "truncated",
      title: <Th k="truncated" title="被截断" />,
      align: "right",
      width: 90,
      render: (r) =>
        r.truncated ? (
          <span className="rounded-md bg-orange-500/15 px-1.5 py-0.5 text-[11px] leading-4 text-orange-600 dark:text-orange-300">
            {r.truncated}
          </span>
        ) : (
          <span className="text-ink-faint">0</span>
        ),
    },
  ];

  return (
    <Table
      rows={sorted}
      columns={columns}
      rowKey={(r) => r.tool}
      minWidth={860}
    />
  );
}
