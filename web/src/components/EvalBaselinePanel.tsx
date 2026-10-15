/**
 * 评估面板（挂在「可观测」页最下方）：跑一轮回归、看成败与 delta。
 *
 * 这里只管「什么时候拉数据、怎么触发」，长什么样全在 `EvalReportView` 里。
 * 拆开是因为：前者是流程，后者是排版，混在一起两边都不好读。
 */
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FlaskConical, Loader2, Play, TriangleAlert } from "lucide-react";

import {
  apiEvalReport,
  apiEvalReportMarkdown,
  apiEvalReports,
  apiEvalRun,
  apiEvalRunStatus,
  apiEvalRunning,
  apiEvalSuites,
} from "../api/client";
import type { EvalCaseStatus } from "../api/types";
import EvalReportView, { type EvalRow } from "./EvalReportView";
import { Section } from "./ui/Page";
import { Hint } from "./ui/Page";
import { Modal } from "./ui/Modal";
import { SelectInput } from "./ui/Form";
import { toast } from "./ui/Toast";
import { cn } from "../lib/utils";

const LOG_CLS =
  "mt-2 max-h-[220px] overflow-auto rounded-lg border border-line-soft bg-surface p-2.5 font-mono text-[12px] leading-[1.5] text-ink";

export default function EvalBaselinePanel({ agentId }: { agentId?: string }) {
  const qc = useQueryClient();
  const [activeRun, setActiveRun] = useState<string | null>(null);
  const [viewRun, setViewRun] = useState<string | null>(null);
  const [mdOpen, setMdOpen] = useState(false);
  // 对比对象。undefined = 对比 latest（上次完整运行）；否则对比指定的那份。
  //
  // 这**一个**选择要同时作用于两处：① 重算当前报告的对比（切换后立刻能看到
  // 差异，否则改了选项页面却纹丝不动）；② 作为下次运行的基线。之前拆成两个
  // state 就踩了这个坑——选项只喂给了下次运行，当前报告不会重查。
  const [cmpWith, setCmpWith] = useState<string | undefined>(undefined);

  const suitesQ = useQuery({ queryKey: ["eval-suites"], queryFn: apiEvalSuites });
  const historyQ = useQuery({
    queryKey: ["eval-reports"],
    queryFn: apiEvalReports,
    refetchInterval: activeRun ? 3000 : false,
  });
  const runningQ = useQuery({
    queryKey: ["eval-running"],
    queryFn: apiEvalRunning,
    refetchInterval: activeRun ? false : 5000,
  });

  // 进页面时若别处（另一个标签页）已经在跑，接管显示进度
  useEffect(() => {
    if (!activeRun && runningQ.data?.items?.length) {
      setActiveRun(runningQ.data.items[0].run_id);
    }
  }, [runningQ.data, activeRun]);

  const runId =
    activeRun ?? viewRun ?? historyQ.data?.items?.[0]?.run_id ?? null;

  const statusQ = useQuery({
    queryKey: ["eval-run", activeRun],
    queryFn: () => apiEvalRunStatus(activeRun!, 80),
    enabled: !!activeRun,
    refetchInterval: (q) => (q.state.data?.status === "running" ? 3000 : false),
  });

  const reportQ = useQuery({
    // cmpWith 进 key：切换对比对象必须重新拉取，否则拿到的还是旧对比
    queryKey: ["eval-report", runId, cmpWith ?? "latest"],
    queryFn: () => apiEvalReport(runId!, cmpWith),
    enabled: !!runId,
  });

  // 运行结束：刷新历史列表，并自动切到这份新报告
  //
  // ⚠️ 必须先判「状态数据还没到」再判「是否还在跑」。刚 setActiveRun 的
  // 那一刻，statusQ 因为换了 queryKey，data 还是 undefined——此时
  // `undefined === "running"` 为 false，会直接掉进下面的「运行结束」分支
  // 把刚设上的 activeRun 清空。症状：点了运行没反应，刷新才出得来。
  useEffect(() => {
    if (!activeRun) return;
    const st = statusQ.data?.status;
    if (!st || st === "running") return;
    const finished = activeRun;
    const ok = statusQ.data?.has_report;
    setActiveRun(null);
    qc.invalidateQueries({ queryKey: ["eval-reports"] });
    if (ok) setViewRun(finished);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusQ.data, activeRun]);

  const runMutation = useMutation({
    mutationFn: () =>
      apiEvalRun({
        agent_id: agentId,
        case_timeout: 300,
        baseline: cmpWith,
      }),
    onSuccess: (r) => setActiveRun(r.run_id),
    onError: (e) => toast.error("启动评测失败：" + String(e)),
  });

  const mdQ = useQuery({
    queryKey: ["eval-md", runId, mdOpen],
    queryFn: () => apiEvalReportMarkdown(runId!),
    enabled: mdOpen && !!runId,
  });

  const history = historyQ.data?.items ?? [];
  const report = reportQ.data;

  // 把对比结论里的状态贴到用例上：光看「通过/失败」看不出是回归还是一直挂
  const rows: EvalRow[] = useMemo(() => {
    if (!report) return [];
    const m = new Map(
      (report.compare?.cases ?? []).map((x) => [x.case_id, x.status])
    );
    return report.cases.map((c) => ({
      c,
      st: (m.get(c.case_id) ?? "kept") as EvalCaseStatus,
    }));
  }, [report]);

  const running = !!activeRun && statusQ.data?.status === "running";
  const totalCases = suitesQ.data?.reduce((n, s) => n + s.cases, 0);

  return (
    <Section
      title={
        <span className="flex items-center gap-1.5">
          <FlaskConical className="h-4 w-4 text-ink-muted" />
          效果评估基线（回归护栏）
        </span>
      }
      extra={
        <div className="flex flex-wrap items-center gap-2">
          {/* 对比对象 */}
          <div className="w-[190px]">
            <SelectInput
              value={cmpWith ?? "__auto"}
              onChange={(v) => setCmpWith(v === "__auto" ? undefined : v)}
              options={[
                { value: "__auto", label: "对比 上一次运行" },
                ...history
                  // 排除当前正在看的那份：自己跟自己比没有意义，
                  // 后端会判成「无可比基线」，不如直接不给这个选项
                  .filter((h) => h.run_id !== runId)
                  .map((h) => ({ value: h.run_id, label: `对比 ${h.run_id}` })),
              ]}
            />
          </div>

          {/* 选看哪一份报告 */}
          <div className="w-[220px]">
            <SelectInput
              value={viewRun ?? history[0]?.run_id ?? ""}
              disabled={historyQ.isLoading || history.length === 0}
              onChange={(v) => {
                setViewRun(v || null);
                setActiveRun(null);
              }}
              options={
                history.length
                  ? history.map((h) => ({
                      value: h.run_id,
                      label: `${h.run_id}　${h.passed}/${h.total}`,
                    }))
                  : [{ value: "", label: "还没跑过评测" }]
              }
            />
          </div>

          <button
            type="button"
            disabled={running || runMutation.isPending}
            title="跑一轮固定用例集，产出可 diff 的基线。会真实消耗 token。"
            onClick={() => runMutation.mutate()}
            className="flex items-center gap-1.5 rounded-lg bg-brand-gradient px-3 py-[5px] text-[12.5px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
          >
            {runMutation.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Play className="h-3.5 w-3.5" />
            )}
            运行评估
          </button>
        </div>
      }
    >
      <div className="mb-3">
        <Hint title="每次改 prompt / 换模型 / 加 skill 之后跑一次，看有没有用例掉">
          评测在<strong className="text-ink-strong">独立进程 + 独立数据库</strong>
          里跑（库里克隆一份配置），不污染真实会话与可观测面板；工作空间默认只读。
          判定只看断言，不引入另一个模型打分——评分口径会漂，漂了的基线就没法比了。
          <br />
          <strong className="text-ink-strong">对比上一次</strong>
          看「本次改动的净效果」；
          <strong className="text-ink-strong">指定一份固定报告</strong>
          看「离起点还差多远」——连续小退化只有后者拦得住（每次对比上次都 ±0，
          其实已比起点掉了两条）。
        </Hint>
      </div>

      {running ? (
        <div>
          {/* 无进度百分比可用（后端只给日志尾巴），所以用一条滚动的高亮条表示
              「在跑」，而不是假装知道百分比 */}
          <div className="h-1 overflow-hidden rounded-full bg-surface">
            <div className="h-full w-1/3 animate-pulse rounded-full bg-brand" />
          </div>
          <div className="mt-1.5 text-[12px] text-ink-muted">
            正在跑 {activeRun}…
            {statusQ.data?.log_tail
              ? "十几条用例通常要十几分钟，可以先去做别的。"
              : "（正在获取进度…）"}
          </div>
          <pre className={LOG_CLS}>
            {statusQ.data?.log_tail || "（等待日志…）"}
          </pre>
          {statusQ.isError && (
            <div className="mt-2">
              <Hint
                tone="warn"
                title="取不到进度（服务可能重启过，内存里的运行状态已丢失）"
              >
                评测本身在<strong className="text-ink-strong">独立子进程</strong>
                里跑，不受服务重启影响；刷新页面会重新从服务端查询。进程跑完后，
                历史列表里会出现它的新报告。
              </Hint>
            </div>
          )}
        </div>
      ) : statusQ.data?.status === "failed" ? (
        <Hint
          tone="danger"
          title={
            <span className="flex items-center gap-1.5">
              <TriangleAlert className="h-3.5 w-3.5" />
              这次评测没有产出报告
            </span>
          }
        >
          <pre className="mt-1.5 max-h-[200px] overflow-auto rounded-md bg-surface p-2 font-mono text-[12px]">
            {statusQ.data.log_tail || "（无日志）"}
          </pre>
        </Hint>
      ) : reportQ.isLoading ? (
        <div className="flex items-center gap-2 text-[13px] text-ink-muted">
          <Loader2 className="h-4 w-4 animate-spin" />
          加载中…
        </div>
      ) : !report ? (
        <div className="text-[13px] text-ink-muted">
          还没有基线。点右上「运行评估」跑第一轮
          {totalCases ? `（当前用例集共 ${totalCases} 条）` : ""}。
        </div>
      ) : (
        <EvalReportView
          report={report}
          rows={rows}
          onOpenMd={() => setMdOpen(true)}
        />
      )}

      <Modal
        open={mdOpen}
        onClose={() => setMdOpen(false)}
        width={820}
        title={`评测报告 ${runId ?? ""}（Markdown）`}
        footer={
          <>
            <button
              type="button"
              onClick={() => {
                navigator.clipboard?.writeText(mdQ.data?.markdown ?? "");
                toast.success("已复制");
              }}
              className="rounded-lg border border-line bg-panel px-4 py-[7px] text-[13px] font-medium text-ink-muted shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-all duration-150 hover:bg-surface hover:text-ink-strong active:scale-[0.98]"
            >
              复制
            </button>
            <button
              type="button"
              onClick={() => setMdOpen(false)}
              className="rounded-lg bg-brand-gradient px-4 py-[7px] text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98]"
            >
              关闭
            </button>
          </>
        }
      >
        {mdQ.isFetching ? (
          <div className="flex items-center gap-2 text-[13px] text-ink-muted">
            <Loader2 className="h-4 w-4 animate-spin" />
            加载中…
          </div>
        ) : (
          <pre
            className={cn(
              "max-h-[520px] overflow-auto whitespace-pre-wrap break-words rounded-lg border border-line-soft bg-surface p-3 font-mono text-[12px] leading-[1.6] text-ink"
            )}
          >
            {mdQ.data?.markdown ?? "（无）"}
          </pre>
        )}
      </Modal>
    </Section>
  );
}
