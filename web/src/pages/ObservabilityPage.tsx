/**
 * 可观测面板：把「用量 / 能力加载 / 解析结构」三类诊断数据放在一起。
 *
 * 排布顺序是刻意的——**从外到内**：
 *   1. 用量（复用 AgentUsagePanel：概览 / 趋势 / 工具 / 重复调用 / 空转 / 错误 / 追问率）；
 *   2. 能力加载：技能正文 / 工具定义被取进来几次、谁在取（判断摘要写得准不准）；
 *   3. 解析结构：模型有多想并行、丢了多少动作（工具并行化的收益依据）；
 *   4. 效果评估基线：改动之后跑一轮回归，看有没有用例掉（EvalBaselinePanel）。
 *
 * 顺序是刻意的——前三块是「诊断现在发生了什么」，第四块是「回头验证改得对不对」，
 * 所以放在最后：它一次要跑十几分钟，不该挡在别人前面。
 *
 * 每块都要能直接推出一个动作，所以文案里都写明了「看到什么 → 该做什么」。
 *
 * 时间范围在页面级统一控制（今天 / 7 天 / 30 天），三块共用同一个窗口——
 * 否则「用量看的是 7 天、能力加载看的是 30 天」这种错位很容易让人读出错的结论。
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { cn } from "../lib/utils";

import AgentUsagePanel from "../components/AgentUsagePanel";
import EvalBaselinePanel from "../components/EvalBaselinePanel";
import { Hint, Section, Stack } from "../components/ui/Page";
import { SelectInput } from "../components/ui/Form";
import { SegmentedControl } from "../components/ui/Segmented";
import { Table } from "../components/ui/Table";
import type { Column } from "../components/ui/Table";
import { Stat } from "../components/ui/Stat";
import { apiListAgents, apiReactParseStats } from "../api/client";

const RANGES = [
  { label: "今天", value: "today", days: 1 },
  { label: "近 7 天", value: "7d", days: 7 },
  { label: "近 30 天", value: "30d", days: 30 },
];

function pct(v: number) {
  return `${(v * 100).toFixed(1)}%`;
}

export default function ObservabilityPage() {
  const [agentId, setAgentId] = useState<string | undefined>(undefined);
  const [range, setRange] = useState("7d");

  const agentsQ = useQuery({ queryKey: ["obs-agents"], queryFn: apiListAgents });
  const agents = agentsQ.data?.agents ?? [];
  // 没选就跟随第一个 agent：面板是"看某个 agent 表现"的，默认第一个最省事
  const effectiveAgent = agentId ?? agents[0]?.id;
  const days = RANGES.find((r) => r.value === range)?.days ?? 7;

  const parseQ = useQuery({
    queryKey: ["obs-parse", effectiveAgent, days],
    queryFn: () => apiReactParseStats({ agent_id: effectiveAgent, days }),
    enabled: !!effectiveAgent,
  });
  const parse = parseQ.data;

  // 结论直接给出来，别让人自己算
  const verdict = (() => {
    if (!parse || parse.total < 20) return null; // 样本太小，先不下结论
    // 幻觉比并行化严重得多：并行化只是浪费需求，幻觉是白烧 token +
    // 模型拿着假事实继续推理。优先报它。
    const worst = Object.entries(parse.by_model ?? {}).find(
      ([m, s]) => s.rate != null && s.rate >= 0.05 && s.total >= 10
    );
    // 格式错误排在幻觉**前面**：它不是模型跑飞，而是端点/模型返回了本接口不接受的
    // 格式，点破基本没用（模型改不过来），该先去查 base_url 与模型名。
    if (parse.format_error > 0) {
      const fmtWorst = Object.entries(parse.by_model ?? {}).find(
        ([, s]) => (s.format_error ?? 0) > 0
      );
      const who = fmtWorst ? `（集中在 ${fmtWorst[0]}）` : "";
      if (parse.format_forced_rate >= 0.5)
        return `有 ${parse.format_error} 次模型吐了原生工具调用格式${who}，过半点破无效——这是端点/模型侧的问题，先核对 base_url 与模型名，调 prompt 没用`;
      return `有 ${parse.format_error} 次模型吐了原生工具调用格式${who}，均已点破后重试成功`;
    }
    if (parse.hallucination > 0) {
      const who = worst ? `（集中在 ${worst[0]}，${pct(worst[1].rate!)}）` : "";
      if (parse.forced_rate >= 0.5)
        return `有 ${parse.hallucination} 次模型自己伪造了工具回执${who}，其中过半点破无效——换模型比继续调 prompt 省事`;
      return `有 ${parse.hallucination} 次模型自己伪造了工具回执${who}，均已点破后重试成功`;
    }
    if (parse.multi_rate >= 0.1)
      return `模型有 ${pct(parse.multi_rate)} 的步骤会连写多个动作，值得开启工具并行`;
    if (parse.dropped_total > 0)
      return `多动作只占 ${pct(parse.multi_rate)}，但已丢弃 ${parse.dropped_total} 个动作——先支持多动作（不做并发）就能止损`;
    return "模型基本遵守单动作约定，暂无并行化收益";
  })();

  const byModel = parse?.by_model ?? {};
  const showByModel = Object.keys(byModel).length > 1;

  const modelCols: Column<any>[] = [
    { key: "model", title: "模型", render: (r) => r.model },
    {
      key: "total",
      title: "解析步数",
      width: 100,
      align: "right",
      render: (r) => r.total,
    },
    {
      key: "hallucination",
      title: "自编回执",
      width: 100,
      align: "right",
      render: (r) =>
        r.hallucination > 0 ? (
          <span className="text-red-600 dark:text-red-400">
            {r.hallucination}
          </span>
        ) : (
          <span className="text-ink-faint">0</span>
        ),
    },
    {
      key: "rate",
      title: "幻觉率",
      width: 100,
      align: "right",
      // 「未知」= 升级前的存量，没有模型名，不参与计算
      render: (r) =>
        r.rate == null ? (
          <span
            title="升级前的记录没有模型名，不参与幻觉率计算"
            className="text-ink-faint"
          >
            —
          </span>
        ) : (
          <span
            className={
              r.rate >= 0.05 ? "text-red-600 dark:text-red-400" : undefined
            }
          >
            {pct(r.rate)}
          </span>
        ),
    },
    {
      // 格式错误单列：它不是模型跑飞，是端点返回了本接口不接受的
      // 格式，点破基本没用 —— 定位到哪一列就去看那台机器的配置
      key: "format_error",
      title: "格式错误",
      width: 100,
      align: "right",
      render: (r) =>
        (r.format_error ?? 0) > 0 ? (
          <span className="text-red-600 dark:text-red-400">
            {r.format_error}
          </span>
        ) : (
          <span className="text-ink-faint">0</span>
        ),
    },
  ];

  return (
    <div className="h-full overflow-auto px-7 py-6">
      {/* 控制条：agent + 时间范围。三块面板共用同一个窗口 */}
      <div className="mb-6 flex flex-wrap items-center gap-3">
        <div className="w-[220px]">
          <SelectInput
            value={effectiveAgent ?? ""}
            onChange={(v) => setAgentId(v || undefined)}
            disabled={agentsQ.isLoading}
            options={[
              ...(agents.length
                ? agents.map((a) => ({ value: a.id, label: a.name }))
                : [{ value: "", label: "暂无已装载的 agent" }]),
            ]}
          />
        </div>
        {/* 时间范围：走共享的 SegmentedControl，不再手写一份分段控件 */}
        <SegmentedControl
          value={range}
          onChange={(v) => setRange(v as typeof range)}
          options={RANGES}
        />
      </div>

      {!effectiveAgent ? (
        <Section>
          <span className="text-[13px] text-ink-muted">请先选择一个智能体</span>
        </Section>
      ) : (
        <Stack>
          {/* 1. 用量：概览 / 趋势 / 工具 / 重复调用 / 空转 / 错误 / 追问率 / 能力加载 */}
          <AgentUsagePanel agentId={effectiveAgent} range={range} />

          {/* 2. 解析结构 */}
          <Section title="解析结构（模型有多想并行工具、有没有自编回执）">
            {parseQ.isLoading ? (
              <div className="flex items-center gap-2 text-[13px] text-ink-muted">
                <Loader2 className="h-4 w-4 animate-spin" />
                加载中…
              </div>
            ) : parse && parse.total > 0 ? (
              <>
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7">
                  <Stat label="解析步数" value={parse.total} />
                  <Stat label="多动作步数" value={parse.multi} />
                  <Stat
                    label="多动作占比"
                    value={pct(parse.multi_rate)}
                    tone={parse.multi_rate >= 0.1 ? "warn" : undefined}
                  />
                  <Stat
                    label="被丢弃的动作"
                    value={parse.dropped_total}
                    tone={parse.dropped_total > 0 ? "warn" : undefined}
                  />
                  <Stat label="丢弃率" value={pct(parse.dropped_ratio)} />
                  <Stat
                    label="自编回执"
                    value={parse.hallucination}
                    tone={parse.hallucination > 0 ? "danger" : undefined}
                  />
                  <Stat
                    label="格式错误"
                    value={parse.format_error}
                    tone={parse.format_error > 0 ? "danger" : undefined}
                  />
                </div>

                <div className="mt-3">
                  {verdict ? (
                    <Hint
                      tone={
                        parse.format_error > 0 || parse.hallucination > 0
                          ? "warn"
                          : "info"
                      }
                    >
                      {verdict}
                    </Hint>
                  ) : (
                    <p className="text-[13px] text-ink-muted">
                      样本还太少（需 ≥20 步才给结论），先继续用几天再看。
                    </p>
                  )}
                </div>

                {/* 按模型拆：这张表是用来挑模型的，聚合数字看不出差别 */}
                {showByModel && (
                  <div className="mt-4">
                    <Table
                      rows={Object.entries(byModel).map(([model, s]) => ({
                        model,
                        ...s,
                      }))}
                      rowKey={(r) => r.model}
                      minWidth={620}
                      columns={modelCols}
                    />
                  </div>
                )}
              </>
            ) : (
              <p className="text-[13px] text-ink-muted">
                暂无解析统计（这项统计从启用后开始累积）
              </p>
            )}
          </Section>

          {/* 3. 效果评估基线：改动之后跑一轮，看有没有用例掉 */}
          <EvalBaselinePanel agentId={effectiveAgent} />
        </Stack>
      )}
    </div>
  );
}
