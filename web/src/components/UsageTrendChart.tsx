import { useState } from "react";
import { SegmentedControl } from "./ui/Segmented";
import { cn } from "../lib/utils";

import type { UsagePoint, UsageTimeseries } from "../api/types";
import { fmtCost, fmtDuration, fmtTokens } from "./UsageTag";

/* 图表三色：走 CSS 变量（theme.css 里浅/深两套）。原来写死的是 antd 老色板
   （青 / 绿 / 蓝），既不属于现在的品牌色，深色主题下也会变成刺眼的一组。 */
const C_IN = "var(--kp-chart-1)"; // 输入 token
const C_OUT = "var(--kp-chart-2)"; // 输出 token
const C_BAR = "var(--kp-chart-3)"; // 成本 / 调用数

type Metric = "tokens" | "cost" | "calls";

/** 桶标签简写：2026-10-01 → 10-01；2026-10-01 14:00 → 14:00 */
function shortLabel(bucket: string): string {
  const parts = bucket.split(" ");
  if (parts.length > 1) return parts[1];
  return bucket.length >= 10 ? bucket.slice(5) : bucket;
}

function valueOf(p: UsagePoint, m: Metric): number {
  if (m === "tokens") return p.prompt_tokens + p.completion_tokens;
  if (m === "cost") return p.cost ?? 0;
  return p.calls;
}

function titleOf(p: UsagePoint, currency?: string | null): string {
  return [
    p.bucket,
    `模型输入 ${fmtTokens(p.prompt_tokens)}`,
    `模型输出 ${fmtTokens(p.completion_tokens)}`,
    `缓存 ${fmtTokens(p.cached_tokens)}`,
    `调用 ${p.calls} 次`,
    `耗时 ${fmtDuration(p.duration_ms)}`,
    `成本 ${fmtCost(p.cost, currency)}`,
  ].join("\n");
}

/**
 * 用量趋势图：按时间桶（天 / 小时）展示 token、成本或调用次数。
 *
 * 纯 CSS 柱状图——不引图表库。Token 模式下柱子按「输入 / 输出」堆叠，
 * 一眼能看出哪天输入暴涨（通常是上下文膨胀）。
 */
export default function UsageTrendChart({
  data,
  currency,
}: {
  data: UsageTimeseries;
  currency?: string | null;
}) {
  const [metric, setMetric] = useState<Metric>("tokens");
  const points = data.points ?? [];

  if (points.length === 0) {
    return (
      <div className="py-6 text-center text-[13px] text-ink-faint">
        该时间范围内没有数据
      </div>
    );
  }

  const max = Math.max(1, ...points.map((p) => valueOf(p, metric)));
  const noCost = metric === "cost" && points.every((p) => p.cost == null);

  return (
    <div>
      <SegmentedControl
        value={metric}
        onChange={(v) => setMetric(v as Metric)}
        options={[
          { label: "Token", value: "tokens" },
          { label: "成本", value: "cost" },
          { label: "调用次数", value: "calls" },
        ]}
        className="mb-2.5"
      />

      {noCost ? (
        <span className="text-[12px] text-ink-muted">
          未配价格，成本不可见（去「模型管理 → 价格」配置）
        </span>
      ) : null}

      <div
        style={{
          display: "flex",
          alignItems: "flex-end",
          gap: 3,
          height: 130,
          marginTop: 10,
          padding: "0 2px",
          // 底线用主题变量：写死浅灰在深色下会变成一条刺眼的亮线
          borderBottom: "1px solid var(--kp-border-soft)",
        }}
      >
        {points.map((p) => {
          const v = valueOf(p, metric);
          const h = (v / max) * 100;
          const total = Math.max(p.prompt_tokens + p.completion_tokens, 1);
          return (
            <div
              key={p.bucket}
              title={titleOf(p, currency)}
              className="group/bar"
              style={{
                flex: 1,
                minWidth: 4,
                height: "100%",
                display: "flex",
                flexDirection: "column",
                justifyContent: "flex-end",
              }}
            >
              {metric === "tokens" ? (
                <div
                  style={{
                    height: `${h}%`,
                    display: "flex",
                    flexDirection: "column",
                    justifyContent: "flex-end",
                    /* 顶部圆角 2px：纯方柱在细柱宽下显得生硬 */
                    borderRadius: "2px 2px 0 0",
                    overflow: "hidden",
                    transition: "filter 0.15s",
                  }}
                >
                  {/* 上：输出；下：输入 */}
                  <div
                    style={{
                      height: `${(p.completion_tokens / total) * 100}%`,
                      background: C_OUT,
                    }}
                  />
                  <div
                    style={{
                      height: `${(p.prompt_tokens / total) * 100}%`,
                      background: C_IN,
                    }}
                  />
                </div>
              ) : (
                <div
                  style={{
                    height: `${h}%`,
                    background: C_BAR,
                    borderRadius: "2px 2px 0 0",
                    transition: "filter 0.15s",
                  }}
                />
              )}
            </div>
          );
        })}
      </div>

      {/* hover 提亮整根柱子：纯 CSS 实现，不为这点交互上状态 */}
      <style>{`
        .group\\/bar:hover > div { filter: brightness(1.12) saturate(1.05); }
      `}</style>

      <div style={{ display: "flex", gap: 3, marginTop: 4, padding: "0 2px" }}>
        {points.map((p, i) => (
          <div
            key={p.bucket}
            style={{
              flex: 1,
              minWidth: 4,
              fontSize: 10,
              color: "var(--kp-text-muted)",
              textAlign: "center",
              overflow: "hidden",
            }}
          >
            {/* 点太多时隔一个显示，避免标签挤成一团 */}
            {points.length > 12 && i % 2 === 1 ? "" : shortLabel(p.bucket)}
          </div>
        ))}
      </div>

      <div
        style={{
          fontSize: 11,
          color: "var(--kp-text-muted)",
          marginTop: 6,
          lineHeight: 1.7,
        }}
      >
        {metric === "tokens" ? (
          <>
            <span style={{ color: C_IN }}>■</span> 输入{" "}
            <span style={{ color: C_OUT }}>■</span> 输出 · 峰值{" "}
            {fmtTokens(max)} token
          </>
        ) : (
          <>峰值 {metric === "cost" ? fmtCost(max, currency) : `${max} 次`}</>
        )}
        <br />
        鼠标悬停柱子看该桶明细（桶按 UTC 切分）
      </div>
    </div>
  );
}
