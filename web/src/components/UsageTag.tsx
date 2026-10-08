import { Typography } from "antd";
import type { StepUsage, UsageSummary } from "../api/types";

/** token 数紧凑写法：1.2k / 3.4M */
export function fmtTokens(n?: number): string {
  const v = n || 0;
  if (!v) return "0";
  if (v < 1000) return String(v);
  if (v < 1_000_000) return `${(v / 1000).toFixed(v < 10_000 ? 1 : 0)}k`;
  return `${(v / 1_000_000).toFixed(1)}M`;
}

/** 毫秒：<1s 用 ms，否则用 s */
export function fmtDuration(ms?: number | null): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`;
}

/** 成本：未配单价（null）显示「—」，不要显示成 0 */
export function fmtCost(cost?: number | null, currency?: string | null): string {
  if (cost === null || cost === undefined) return "—";
  const symbol = currency === "USD" ? "$" : "¥";
  return `${symbol}${cost < 0.01 ? cost.toFixed(6) : cost.toFixed(4)}`;
}

/**
 * 一轮 / 一个任务的用量汇总。
 *
 * 两个口径别混用：这里的 duration_ms 是 **LLM 耗时之和**（不含工具执行、等待、
 * 挂起恢复）；用户真正等待的端到端墙钟由调用方另行传入 wallMs。
 */
export function UsageTag({
  usage,
  wallMs,
  showCost = true,
  onDetail,
  onDump,
}: {
  usage?: UsageSummary | null;
  wallMs?: number | null;
  showCost?: boolean;
  /** 给了就渲染「查看详情」入口：点开看本轮逐步的耗时 / token 时间线 */
  onDetail?: () => void;
  /** 给了就渲染「查看 dump」入口：看完整 prompt（仅落盘开启时前端才传） */
  onDump?: () => void;
}) {
  if (!usage || !usage.calls) return null;
  // 把输入 / 输出 / 缓存命中拆开显示，而不是只给一个总数
  const inTok = usage.prompt_tokens ?? usage.total_tokens;
  const outTok = usage.completion_tokens ?? 0;
  const reasonTok = usage.reasoning_tokens ?? 0;
  const cache = usage.cached_reported
    ? `缓存 ${(usage.cache_hit_rate * 100).toFixed(0)}%`
    : "缓存未上报";
  const parts = [
    `模型输入 ${fmtTokens(inTok)}`,
    `模型输出 ${fmtTokens(outTok)}${reasonTok ? `（思考 ${fmtTokens(reasonTok)}）` : ""}`,
    cache,
    `${fmtDuration(usage.duration_ms)}`,
  ];
  if (wallMs) parts.push(`共耗时 ${fmtDuration(wallMs)}`);
  if (showCost) parts.push(fmtCost(usage.cost, usage.cost_currency));
  // 上下文水位告警：ReAct 多轮最容易出问题的地方，单独标出来
  if (usage.context_warning) {
    parts.push(
      `⚠ 上下文 ${((usage.context_usage_rate ?? 0) * 100).toFixed(0)}%`
    );
  }
  if (usage.errors) parts.push(`失败 ${usage.errors} 次`);
  return (
    <Typography.Text
      type={usage.context_warning ? "warning" : "secondary"}
      style={{ fontSize: 12 }}
    >
      {parts.join(" · ")}
      {onDetail && (
        <>
          {" · "}
          <Typography.Link
            style={{ fontSize: 12 }}
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              onDetail();
            }}
          >
            查看详情
          </Typography.Link>
        </>
      )}
      {onDump && (
        <>
          {" · "}
          <Typography.Link
            style={{ fontSize: 12 }}
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              onDump();
            }}
          >
            查看 dump
          </Typography.Link>
        </>
      )}
    </Typography.Text>
  );
}

/** 单个 ReAct 步骤的用量：额外区分「整步耗时」与「LLM 耗时」 */
export function StepUsageTag({ usage }: { usage?: StepUsage | null }) {
  if (!usage) return null;
  const cache = usage.cached_reported
    ? `缓存 ${(usage.cache_hit_rate * 100).toFixed(0)}%`
    : "缓存未上报";
  return (
    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
      模型输入 {fmtTokens(usage.prompt_tokens)} · 模型输出{" "}
      {fmtTokens(usage.completion_tokens)}
      {usage.reasoning_tokens ? `（思考 ${fmtTokens(usage.reasoning_tokens)}）` : ""}
      · {cache} ·{" "}
      {fmtDuration(usage.duration_ms ?? usage.llm_duration_ms)}
      {usage.duration_ms ? `（LLM ${fmtDuration(usage.llm_duration_ms)}）` : ""}
    </Typography.Text>
  );
}
