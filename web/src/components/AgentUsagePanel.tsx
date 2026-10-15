import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";

/**
 * MM-DD HH:mm。原来用 dayjs，但它是 antd 的传递依赖——antd 卸掉后就没有了。
 * 这个格式用原生 Date 手写只要三行，不值得再引一个库。
 */
function fmtShortTime(v?: string | null): string {
  if (!v) return "—";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return "—";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(
    d.getMinutes()
  )}`;
}

import {
  apiAgentMetrics,
  apiAskRate,
  apiCapabilityStats,
  apiDuplicateCalls,
  apiErrorBreakdown,
  apiSlowestCalls,
  apiToolStats,
  apiUsageTimeseries,
} from "../api/client";
import type { ErrorBreakdown, ErrorGroup, SlowCall } from "../api/types";
import ToolStatsTable from "./ToolStatsTable";
import UsageTrendChart from "./UsageTrendChart";
import { fmtCost, fmtDuration, fmtTokens } from "./UsageTag";
import { Table } from "./ui/Table";
import type { Column } from "./ui/Table";
import { Stat } from "./ui/Stat";
import { cn } from "../lib/utils";

const RANGES = [
  { label: "今天", value: "today" },
  { label: "近 7 天", value: "7d" },
  { label: "近 30 天", value: "30d" },
  { label: "全部", value: "all" },
];

/** 时间范围 → since/until（ISO 8601，后端 aggregate 直接吃） */
function rangeToParams(v: string): { since?: string; until?: string } {
  if (v === "all") return {};
  const now = new Date();
  const from = new Date(now);
  if (v === "today") from.setHours(0, 0, 0, 0);
  else if (v === "7d") from.setDate(now.getDate() - 7);
  else if (v === "30d") from.setDate(now.getDate() - 30);
  return { since: from.toISOString(), until: now.toISOString() };
}

/** 把 LLM / 工具两组错误合成一张表，并标注来源 */
function errGroups(data: ErrorBreakdown) {
  return [
    ...data.llm.groups.map((g: ErrorGroup) => ({
      ...g,
      src: "LLM",
      key: `llm-${g.key}`,
    })),
    ...data.tool.groups.map((g: ErrorGroup) => ({
      ...g,
      src: "工具",
      key: `tool-${g.key}`,
    })),
  ];
}

/** 小节：统一的区块标题 + 间距（原来是每个 <div style={{marginTop:16}}> 手写） */
function Block({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="mt-5">
      <div className="text-[13px] font-semibold text-ink-strong">{title}</div>
      {hint && <div className="mt-1 text-[12px] text-ink-muted">{hint}</div>}
      <div className="mt-2">{children}</div>
    </div>
  );
}

function SmallLoading() {
  return <Loader2 className="h-3.5 w-3.5 animate-spin text-ink-muted" />;
}

function Muted({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <span className={cn("text-[12px] text-ink-muted", className)}>{children}</span>
  );
}

/** 「没有发现问题」这类正向结论用绿色，跟错误区分开 */
function Good({ children }: { children: ReactNode }) {
  return (
    <span className="text-[12px] text-emerald-600 dark:text-emerald-400">
      {children}
    </span>
  );
}

/** 小标签：原来的 antd Tag 用法都是「标识一个名字」 */
function Chip({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex rounded-md bg-black/[0.06] px-1.5 py-0.5 font-mono text-[11px] leading-4 text-ink-muted dark:bg-white/[0.08]">
      {children}
    </span>
  );
}

/**
 * 单个 agent 的用量汇总：跨会话看总消耗与成本，支持时间范围。
 *
 * 数据来自 llm_calls 事实表按 agent_id 聚合（/metrics/agents/{id}），
 * 单价命中 llm_profiles 才会算出成本，否则显示「—」。
 */
export default function AgentUsagePanel({
  agentId,
  range: rangeProp,
}: {
  agentId: string;
  /** 受控时间范围。给了就以外部为准（可观测页面统一控制三块的时间窗） */
  range?: string;
}) {
  const [innerRange, setInnerRange] = useState<string>("7d");
  const range = rangeProp ?? innerRange;
  const setRange = rangeProp ? () => {} : setInnerRange;
  const params = useMemo(() => rangeToParams(range), [range]);

  const usageQ = useQuery({
    queryKey: ["agent-usage", agentId, range],
    queryFn: () => apiAgentMetrics(agentId, params),
  });
  // 工具维度没有时间范围参数，始终是全量——够用：它是找「最慢/返回最大」的工具
  const toolsQ = useQuery({
    queryKey: ["agent-tool-stats", agentId],
    queryFn: () => apiToolStats({ agent_id: agentId }),
  });

  // 趋势：今天按小时看波动，更长区间按天看
  const granularity = range === "today" ? "hour" : "day";
  const trendQ = useQuery({
    queryKey: ["agent-trend", agentId, range],
    queryFn: () =>
      apiUsageTimeseries({ agent_id: agentId, ...params, granularity }),
  });

  const slowQ = useQuery({
    queryKey: ["agent-slowest", agentId, range],
    queryFn: () => apiSlowestCalls({ agent_id: agentId, ...params, limit: 10 }),
  });
  const errQ = useQuery({
    queryKey: ["agent-errors", agentId, range],
    queryFn: () =>
      apiErrorBreakdown({ agent_id: agentId, ...params, limit: 20 }),
  });
  const askQ = useQuery({
    queryKey: ["agent-ask-rate", agentId, range],
    queryFn: () => apiAskRate({ agent_id: agentId, ...params }),
  });
  const dupQ = useQuery({
    queryKey: ["agent-duplicates", agentId, range],
    queryFn: () =>
      apiDuplicateCalls({ agent_id: agentId, ...params, limit: 10 }),
  });
  // 能力加载只有 days 粒度（后端没做时间范围端点），跟着时间档粗粒度走
  const capDays = range === "today" ? 1 : range === "30d" ? 30 : 7;
  const capQ = useQuery({
    queryKey: ["agent-capabilities", agentId, capDays],
    queryFn: () =>
      apiCapabilityStats({ agent_id: agentId, days: capDays, top: 10 }),
  });

  const u = usageQ.data;

  const dupCols: Column<any>[] = [
    {
      key: "tool",
      title: "工具",
      render: (r) => <Chip>{r.tool}</Chip>,
    },
    { key: "count", title: "同参数次数", width: 110, align: "right", render: (r) => r.count },
    {
      key: "args_size",
      title: "参数大小",
      width: 100,
      align: "right",
      render: (r) => fmtTokens(r.args_size),
    },
    {
      key: "message_id",
      title: "所在轮",
      render: (r) =>
        r.message_id ? (
          <Muted>{String(r.message_id).slice(-8)}</Muted>
        ) : (
          <span className="text-ink-faint">—</span>
        ),
    },
  ];

  const capCols: Column<any>[] = [
    { key: "key", title: "能力", render: (r) => <Chip>{r.key}</Chip> },
    {
      key: "kind",
      title: "类型",
      width: 90,
      render: (r) =>
        r.kind === "skill" ? "技能" : r.kind === "tool" ? "工具" : "工具组",
    },
    { key: "loads", title: "次数", width: 70, align: "right", render: (r) => r.loads },
    {
      key: "preload",
      title: "预加载",
      width: 70,
      align: "right",
      render: (r) => (r.preload > 0 ? r.preload : <span className="text-ink-faint">—</span>),
    },
    {
      key: "model_load",
      title: "模型主动",
      width: 80,
      align: "right",
      render: (r) =>
        r.model_load > 0 ? r.model_load : <span className="text-ink-faint">—</span>,
    },
    {
      key: "auto_disclose",
      title: "自动补载",
      width: 80,
      align: "right",
      render: (r) =>
        r.auto_disclose > 0 ? (
          r.auto_disclose
        ) : (
          <span className="text-ink-faint">—</span>
        ),
    },
    {
      key: "avg_chars",
      title: "平均体积",
      width: 90,
      align: "right",
      render: (r) => fmtTokens(r.avg_chars),
    },
  ];

  const slowCols: Column<SlowCall>[] = [
    {
      key: "created_at",
      title: "时间",
      width: 110,
      render: (r) => fmtShortTime(r.created_at),
    },
    { key: "duration_ms", title: "耗时", width: 90, align: "right", render: (r) => fmtDuration(r.duration_ms) },
    { key: "ttft_ms", title: "首字", width: 80, align: "right", render: (r) => fmtDuration(r.ttft_ms) },
    {
      key: "tok",
      title: "模型输入/输出",
      width: 150,
      align: "right",
      render: (r) =>
        `${fmtTokens(r.prompt_tokens)} / ${fmtTokens(r.completion_tokens)}`,
    },
    { key: "kind", title: "类型", width: 110, render: (r) => <Chip>{r.kind}</Chip> },
    {
      key: "is_stream",
      title: "流式",
      width: 70,
      render: (r) => (r.is_stream ? "是" : "否"),
    },
  ];

  const errCols: Column<any>[] = [
    { key: "src", title: "来源", width: 60, render: (r) => r.src },
    { key: "count", title: "次数", width: 60, align: "right", render: (r) => r.count },
    {
      key: "last_at",
      title: "最近",
      width: 110,
      render: (r) => fmtShortTime(r.last_at),
    },
    {
      key: "key",
      title: "错误",
      render: (r) => <span className="break-words text-[12px]">{r.key}</span>,
    },
  ];

  return (
    <div>
      {!rangeProp && (
        <div className="mb-3 flex w-fit items-center gap-0.5 rounded-lg bg-surface p-0.5">
          {RANGES.map((r) => (
            <button
              key={r.value}
              type="button"
              onClick={() => setRange(r.value)}
              className={cn(
                "rounded-md px-2.5 py-1 text-[12px] transition-all",
                range === r.value
                  ? "bg-panel text-ink-strong shadow-[var(--kp-shadow-sm)]"
                  : "text-ink-muted hover:text-ink"
              )}
            >
              {r.label}
            </button>
          ))}
        </div>
      )}

      {usageQ.isLoading ? (
        <div className="flex justify-center py-8">
          <Loader2 className="h-4 w-4 animate-spin text-ink-muted" />
        </div>
      ) : !u || !u.calls ? (
        <div className="py-8 text-center text-[13px] text-ink-faint">
          该时间范围内没有用量记录
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-7">
            <Stat label="模型输入" value={fmtTokens(u.prompt_tokens)} />
            <Stat label="模型输出" value={fmtTokens(u.completion_tokens)} />
            <Stat
              label="缓存命中"
              value={
                u.cached_reported
                  ? `${(u.cache_hit_rate * 100).toFixed(0)}%`
                  : "未上报"
              }
            />
            <Stat label="LLM 调用" value={`${u.calls} 次`} />
            <Stat label="LLM 耗时" value={fmtDuration(u.duration_ms)} />
            <Stat label="成本" value={fmtCost(u.cost, u.cost_currency)} />
            <Stat
              label="追问率"
              value={askQ.data ? `${(askQ.data.ask_rate * 100).toFixed(0)}%` : "—"}
              sub={
                askQ.data
                  ? `${askQ.data.ask_rounds}/${askQ.data.rounds} 轮`
                  : undefined
              }
            />
          </div>

          {u.errors ? (
            <div className="mt-2">
              <span className="text-[12px] text-red-600 dark:text-red-400">
                其中失败 {u.errors} 次（错误率{" "}
                {((u.error_rate ?? 0) * 100).toFixed(0)}%）
              </span>
            </div>
          ) : null}
        </>
      )}

      <Block title={`趋势（${granularity === "hour" ? "按小时" : "按天"}）`}>
        {trendQ.isLoading ? (
          <SmallLoading />
        ) : trendQ.data ? (
          <UsageTrendChart data={trendQ.data} currency={u?.cost_currency} />
        ) : null}
      </Block>

      <Block title="工具统计（全量）">
        {toolsQ.isLoading ? (
          <SmallLoading />
        ) : toolsQ.data && toolsQ.data.length > 0 ? (
          <ToolStatsTable stats={toolsQ.data} />
        ) : (
          <Muted>暂无工具调用记录</Muted>
        )}
      </Block>

      <Block title="重复调用（空转）">
        {dupQ.isLoading ? (
          <SmallLoading />
        ) : dupQ.data ? (
          <>
            <Muted>
              {dupQ.data.duplicate_count}/{dupQ.data.total} 次（
              {(dupQ.data.duplicate_rate * 100).toFixed(1)}%）是「同工具 +
              同参数」的重复调用
            </Muted>
            {dupQ.data.groups.length > 0 ? (
              <div className="mt-2">
                <Table
                  rows={dupQ.data.groups}
                  columns={dupCols}
                  rowKey={(r) => `${r.tool}-${r.args_hash}-${r.message_id}`}
                  minWidth={620}
                />
              </div>
            ) : (
              <div className="mt-1">
                <Good>没有发现空转</Good>
              </div>
            )}
            {/* read 单独一档：它是设计内的追回动作，不算空转；但同一块反复
                展开说明「取回 → 被压 → 又取回」在抖动，必须看得见 */}
            {dupQ.data.read && dupQ.data.read.calls > 0 ? (
              <div className="mt-2">
                <Muted>
                  追回（read）：共 {dupQ.data.read.calls} 次，其中{" "}
                  {dupQ.data.read.repeats} 次是重复展开同一块（
                  {(dupQ.data.read.repeat_rate * 100).toFixed(1)}%）
                  {dupQ.data.read.repeats > 0
                    ? " —— 偏高说明上下文在抖动，可调大「追回内容保活条数」"
                    : ""}
                </Muted>
              </div>
            ) : null}
          </>
        ) : null}
      </Block>

      <Block title={`能力加载（近 ${capDays} 天）`}>
        {capQ.isLoading ? (
          <SmallLoading />
        ) : capQ.data && capQ.data.total > 0 ? (
          <>
            <Muted>
              共 {capQ.data.total} 次把正文 / 工具定义取进上下文：系统预加载{" "}
              {capQ.data.by_source.preload ?? 0} · 模型主动加载{" "}
              {capQ.data.by_source.model_load ?? 0} · 调用时自动补定义{" "}
              {capQ.data.by_source.auto_disclose ?? 0}
            </Muted>
            <div className="mt-1">
              <Muted className="text-ink-faint">
                预加载占比高 → L1 摘要写得不准；自动补定义占比高 → 工具折叠太狠或组摘要没写
              </Muted>
            </div>
            {capQ.data.redundant_loads > 0 ? (
              <div className="mt-1">
                <span className="text-[12px] text-amber-600 dark:text-amber-400">
                  抖动 {capQ.data.redundant_loads} 次：同一轮把同一个能力取了两遍以上，
                  说明模型不确定自己读过没有
                </span>
              </div>
            ) : null}
            {capQ.data.by_key.length > 0 ? (
              <div className="mt-2">
                <Table
                  rows={capQ.data.by_key}
                  columns={capCols}
                  rowKey={(r) => r.key}
                  minWidth={680}
                />
              </div>
            ) : null}
          </>
        ) : (
          <Muted>暂无能力加载记录</Muted>
        )}
      </Block>

      <Block title="最慢的调用（Top 10）">
        {slowQ.isLoading ? (
          <SmallLoading />
        ) : slowQ.data && slowQ.data.items.length > 0 ? (
          <Table
            rows={slowQ.data.items}
            columns={slowCols}
            rowKey={(r) => r.id}
            minWidth={700}
          />
        ) : (
          <Muted>暂无数据</Muted>
        )}
      </Block>

      <Block title="错误">
        {errQ.isLoading ? (
          <SmallLoading />
        ) : errQ.data ? (
          <>
            <Muted>
              LLM 失败 {errQ.data.llm.failed}/{errQ.data.llm.total}（
              {(errQ.data.llm.error_rate * 100).toFixed(1)}%） · 工具失败{" "}
              {errQ.data.tool.failed}/{errQ.data.tool.total}（
              {(errQ.data.tool.error_rate * 100).toFixed(1)}%）
            </Muted>
            {errGroups(errQ.data).length > 0 ? (
              <div className="mt-2">
                <Table
                  rows={errGroups(errQ.data)}
                  columns={errCols}
                  rowKey={(r) => r.key}
                  minWidth={520}
                />
              </div>
            ) : (
              <div className="mt-1">
                <Good>没有失败记录</Good>
              </div>
            )}
          </>
        ) : null}
      </Block>
    </div>
  );
}
