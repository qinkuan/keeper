import { Table, Tag } from "antd";
import type { ToolStat } from "../api/types";
import { fmtDuration, fmtTokens } from "./UsageTag";

/**
 * 工具维度统计表：**每列都可点表头排序**，默认按总耗时降序（后端返回的也是这个序）。
 *
 * 关注点：
 * - `avg_output_size` / `max_output_size` 大 → 该工具的返回会原样进下一轮 prompt，
 *   是上下文膨胀的主要来源；
 * - `truncated` 高 → 返回经常超过 observation_limit 被砍，说明该精简工具输出了。
 */
export default function ToolStatsTable({ stats }: { stats: ToolStat[] }) {
  if (!stats.length) return null;
  return (
    <Table
      size="small"
      rowKey="tool"
      pagination={false}
      dataSource={stats}
      columns={[
        {
          title: "工具",
          dataIndex: "tool",
          sorter: (a: ToolStat, b: ToolStat) => a.tool.localeCompare(b.tool),
        },
        {
          title: "调用",
          dataIndex: "calls",
          sorter: (a: ToolStat, b: ToolStat) => a.calls - b.calls,
        },
        {
          title: "失败",
          dataIndex: "errors",
          sorter: (a: ToolStat, b: ToolStat) => a.errors - b.errors,
          render: (v: number) => (v ? <Tag color="red">{v}</Tag> : "0"),
        },
        {
          title: "总耗时",
          dataIndex: "total_duration_ms",
          sorter: (a: ToolStat, b: ToolStat) =>
            a.total_duration_ms - b.total_duration_ms,
          defaultSortOrder: "descend",
          render: (v: number) => fmtDuration(v),
        },
        {
          title: "平均耗时",
          dataIndex: "avg_duration_ms",
          sorter: (a: ToolStat, b: ToolStat) =>
            a.avg_duration_ms - b.avg_duration_ms,
          render: (v: number) => fmtDuration(v),
        },
        {
          title: "平均返回",
          dataIndex: "avg_output_size",
          sorter: (a: ToolStat, b: ToolStat) =>
            a.avg_output_size - b.avg_output_size,
          render: (v: number) => fmtTokens(v),
        },
        {
          title: "最大返回",
          dataIndex: "max_output_size",
          sorter: (a: ToolStat, b: ToolStat) =>
            a.max_output_size - b.max_output_size,
          render: (v: number) => fmtTokens(v),
        },
        {
          title: "被截断",
          dataIndex: "truncated",
          sorter: (a: ToolStat, b: ToolStat) => a.truncated - b.truncated,
          render: (v: number) => (v ? <Tag color="orange">{v}</Tag> : "0"),
        },
      ]}
    />
  );
}
