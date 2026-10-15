/**
 * 评估报告的**纯展示**部分：数字 + 回归提示 + 逐条用例表。
 *
 * 刻意做成无状态组件：它只吃一份 `EvalReport`，不查任何接口。这样「长什么样」
 * 和「什么时候拉数据」是两件能各自读懂的事。
 *
 * 三条排版上的取舍：
 *
 * 1. **回归排在最显眼处**。评测的价值不在「通过率 90%」，而在能变红——所以
 *    顶部第一块永远是「相对上次，掉了哪几条」，而不是一堆让人安心的数字。
 * 2. **指纹跟着数字一起显示**。换了模型、加了插件之后 token 会变，但那跟代码
 *    没关系。不显示指纹，就等于让人拿着不可比的数字下结论。
 * 3. **失败信息必须可执行**。「答案里找不到 8080；实际答案：…」比「不通过」有用
 *    得多——前者能直接拿去修。
 */
import { useState } from "react";
import { ChevronDown } from "lucide-react";

import type { EvalCaseResult, EvalCaseStatus, EvalReport } from "../api/types";
import { Hint, Section } from "./ui/Page";
import { Pill } from "./ui/Pill";
import type { PillTone } from "./ui/Pill";
import { Stat } from "./ui/Stat";
import { Table } from "./ui/Table";
import type { Column } from "./ui/Table";
import { cn } from "../lib/utils";

export function fmtPct(v: number | null | undefined) {
  return v == null ? "—" : `${(v * 100).toFixed(1)}%`;
}

export function fmtNum(v: number | null | undefined, digits = 0) {
  if (v == null) return "—";
  return Number(v).toLocaleString(undefined, {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
}

export function fmtSec(ms: number | null | undefined) {
  return ms == null ? "—" : `${(ms / 1000).toFixed(1)}s`;
}

export const STATUS_META: Record<
  EvalCaseStatus,
  { label: string; tone: PillTone }
> = {
  broken: { label: "回归", tone: "red" },
  fixed: { label: "修复", tone: "green" },
  new: { label: "新增", tone: "blue" },
  kept: { label: "保持", tone: "muted" },
  skipped: { label: "跳过", tone: "muted" },
};

export type EvalRow = { c: EvalCaseResult; st: EvalCaseStatus };

/** 判定依据单元格：失败时优先给第一条失败原因（可直接拿去修） */
function VerdictCell({ r }: { r: EvalRow }) {
  if (r.c.error) return <Pill tone="red">运行异常</Pill>;
  if (!r.c.passed && r.c.failures.length) {
    const f = r.c.failures[0];
    return (
      <span
        title={r.c.failures.map((x) => `${x.assert}：${x.detail}`).join("\n")}
        className="block max-w-[320px] truncate text-[12px] text-red-600 dark:text-red-300"
      >
        {f.assert}：{f.detail}
      </span>
    );
  }
  return (
    <span className="block max-w-[320px] truncate text-[12px] text-ink-muted">
      {r.c.summary || "—"}
    </span>
  );
}

/** 逐条用例表：状态 / 用例 / 判定依据 / 步 / token / 耗时。 */
export function buildColumns(): Column<EvalRow>[] {
  return [
    {
      key: "status",
      title: "状态",
      width: 74,
      render: (r) =>
        r.c.skipped ? (
          <Pill tone="muted">跳过</Pill>
        ) : (
          <Pill tone={STATUS_META[r.st].tone}>{STATUS_META[r.st].label}</Pill>
        ),
    },
    {
      key: "case",
      title: "用例",
      width: 200,
      render: (r) => (
        <span className="block">
          <span className="block font-mono text-[12px] text-ink">
            {r.c.case_id}
          </span>
          <span className="block truncate text-[12px] text-ink-muted">
            {r.c.title}
          </span>
        </span>
      ),
    },
    { key: "verdict", title: "判定依据", render: (r) => <VerdictCell r={r} /> },
    {
      key: "steps",
      title: "步",
      width: 60,
      align: "right",
      render: (r) => r.c.metrics?.steps ?? "—",
    },
    {
      key: "tokens",
      title: "token",
      width: 86,
      align: "right",
      render: (r) => fmtNum(r.c.metrics?.total_tokens),
    },
    {
      key: "duration",
      title: "耗时",
      width: 76,
      align: "right",
      render: (r) => fmtSec(r.c.metrics?.duration_ms),
    },
  ];
}

/** delta 展示：变绿=变好。步数/token/耗时都是「越小越好」，所以都 invert。 */
export function DeltaText({
  d,
  invert,
}: {
  d?: { from: number; to: number; delta: number } | null;
  invert?: boolean;
}) {
  if (!d) return null;
  const p = d.from ? (d.delta / Math.abs(d.from)) * 100 : 0;
  if (Math.abs(p) < 1) return null; // 抖动小于 1% 不展示，否则全是噪音
  const better = invert ? d.delta < 0 : d.delta > 0;
  return (
    <span
      className={cn(
        "ml-1 text-[12px]",
        better
          ? "text-emerald-600 dark:text-emerald-400"
          : "text-red-600 dark:text-red-400"
      )}
    >
      {d.delta > 0 ? "+" : ""}
      {p.toFixed(0)}%
    </span>
  );
}

/** 折行的用例详情（展开行内容） */
function CaseDetail({ c }: { c: EvalCaseResult }) {
  return (
    <div className="space-y-1 text-[12px]">
      <div className="text-ink-muted">输入：{c.input}</div>
      {c.tool_calls.length > 0 && (
        <div className="text-ink-muted">
          调用工具：{c.tool_calls.join("、")}
        </div>
      )}
      {c.answer_preview && (
        <div className="text-ink-muted">答案节选：{c.answer_preview}</div>
      )}
      {c.failures.map((f, i) => (
        <div key={i} className="text-red-600 dark:text-red-300">
          {f.assert}：{f.detail}
        </div>
      ))}
    </div>
  );
}

export default function EvalReportView({
  report,
  rows,
  onOpenMd,
}: {
  report: EvalReport;
  rows: EvalRow[];
  onOpenMd: () => void;
}) {
  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  const [showByCase, setShowByCase] = useState(true);

  const cmp = report.compare;
  const s = report.summary;
  const fp = report.fingerprint;
  const broken = rows.filter((x) => x.st === "broken");
  const failedCount = rows.filter((x) => !x.c.passed && !x.c.skipped).length;
  // 选集运行（调试用的 --case / --suite）的通过率只针对跑过的那几条。
  // 不标出来的话，很容易把「1 条里过了 1 条」读成「整体 100%」。
  const subset = report.selection?.mode === "subset";

  return (
    <div className="space-y-3">
      {subset && (
        <Hint
          tone="warn"
          title={`选集运行：本次只跑了 ${rows.length} 条，下面的通过率只针对这几条`}
        >
          完整基线请看历史里 mode=all 的那次（latest 不会被选集运行覆盖）。
        </Hint>
      )}

      {broken.length > 0 && (
        <Hint tone="danger" title={`${broken.length} 条用例回归`}>
          <ul className="mt-1 list-disc space-y-0.5 pl-5">
            {broken.map((b) => (
              <li key={b.c.case_id}>
                <code className="rounded bg-surface px-1 py-0.5 font-mono text-[11px]">
                  {b.c.case_id}
                </code>
                {b.c.failures.length ? `　${b.c.failures[0].detail}` : ""}
              </li>
            ))}
          </ul>
        </Hint>
      )}
      {cmp?.has_baseline && broken.length === 0 && (
        <Hint tone="info">{cmp.verdict}</Hint>
      )}
      {!cmp?.has_baseline && (
        <Hint tone="warn">
          这是首次基线，没有对比对象。再跑一次才会有「相对上次」的变化。
        </Hint>
      )}

      {/* 对比方向必须写明：拿一份**更早**的报告当对比对象时，「回归 1 条」
          指的是「那份老报告比现在差」，很容易被读成「现在退化了」 */}
      {cmp?.has_baseline && (
        <div className="text-[12px] text-ink-muted">
          对比对象：<code className="font-mono">{cmp.baseline_run_id}</code>
          {(() => {
            const d = cmp.summary?.passed;
            if (!d) return null;
            const diff = d.to - d.from;
            if (diff === 0) return "　—— 与它持平";
            return diff > 0
              ? `　—— 当前比它好 ${diff} 条`
              : `　—— 当前比它差 ${Math.abs(diff)} 条`;
          })()}
        </div>
      )}

      {cmp?.fingerprint_changed?.length ? (
        <Hint
          tone="warn"
          title="被测配置变了，下面的指标 delta 不能归因给代码改动"
        >
          {cmp.fingerprint_changed.join("；")}
        </Hint>
      ) : null}

      {/* 指标：通过率是主角，所以单列一块并按阈值染色；其余吃同一套 Stat */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
        <Stat
          label="通过率"
          value={fmtPct(s.pass_rate)}
          sub={`${s.passed}/${s.total}`}
          tone={s.pass_rate >= 0.9 ? "success" : s.pass_rate >= 0.7 ? "warn" : "danger"}
        />
        <Stat
          label="平均步数"
          value={fmtNum(s.avg_steps, 1)}
          sub={<DeltaText d={cmp?.summary?.avg_steps} invert />}
        />
        <Stat
          label="平均 token"
          value={fmtNum(s.avg_tokens)}
          sub={<DeltaText d={cmp?.summary?.avg_tokens} invert />}
        />
        <Stat
          label="平均耗时"
          value={fmtSec(s.avg_duration_ms)}
          sub={<DeltaText d={cmp?.summary?.avg_duration_ms} invert />}
        />
        <Stat
          label="成本合计"
          value={s.total_cost ?? "—"}
        />
      </div>

      <div className="text-[12px] text-ink-muted">
        模型 {fp?.model || "—"}　工具 {fp?.tool_count ?? 0} 个 / 技能{" "}
        {fp?.skill_count ?? 0} 个　追问 {s.asked_cases} 条 / 预算收尾{" "}
        {s.budget_stopped_cases} 条 / 跑挂 {s.error_cases} 条 / 工具调用{" "}
        {s.tool_calls} 次
      </div>

      <div className="border-t border-line-soft pt-3">
        <div className="mb-2 flex items-center gap-3">
          <span className="text-[13px] font-semibold text-ink-strong">
            逐条用例
          </span>
          {failedCount > 0 && (
            <span className="text-[12px] text-red-600 dark:text-red-400">
              {failedCount} 条未通过
            </span>
          )}
          <button
            type="button"
            onClick={onOpenMd}
            className="text-[12px] text-brand transition-opacity hover:opacity-75"
          >
            查看 Markdown
          </button>
          {/* 40 多条用例时表格会很长，允许收起，把观感让给上面的指标 */}
          <button
            type="button"
            onClick={() => setShowByCase((v) => !v)}
            className="ml-auto flex items-center gap-1 text-[12px] text-ink-muted transition-colors hover:text-ink"
          >
            {showByCase ? "收起" : "展开"}
            <ChevronDown
              className={cn(
                "h-3.5 w-3.5 transition-transform",
                showByCase && "rotate-180"
              )}
            />
          </button>
        </div>

        {showByCase && (
          <Table
            rows={rows}
            columns={buildColumns()}
            rowKey={(r) => r.c.case_id}
            minWidth={860}
            expandedKey={expandedKey}
            renderExpanded={(r) => <CaseDetail c={r.c} />}
          />
        )}
      </div>
    </div>
  );
}
