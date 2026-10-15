import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";

import { apiMessageTimeline } from "../api/client";
import type {
  DuplicateCalls,
  MessageTimeline,
  TimelineStep,
} from "../api/types";
import { fmtCost, fmtDuration, fmtTokens } from "./UsageTag";
import { Modal } from "./ui/Modal";
import { cn } from "../lib/utils";

const C_LLM = "#1677ff"; // LLM 思考耗时
const C_TOOL = "#fa8c16"; // 工具执行耗时
const C_IN = "#13c2c2"; // 输入 token
const C_OUT = "#52c41a"; // 输出 token

/** 比例条：外层 width 表示该步相对最长步的长度，内层按 parts 切分配色 */
function Bar({
  parts,
  width,
}: {
  parts: { v: number; c: string; t: string }[];
  width: number;
}) {
  const total = parts.reduce((s, p) => s + p.v, 0);
  return (
    <div
      style={{ width: `${Math.max(width, 0.5)}%` }}
      className="flex h-2 overflow-hidden rounded bg-surface-soft"
    >
      {total > 0 &&
        parts
          .filter((p) => p.v > 0)
          .map((p, i) => (
            <div
              key={i}
              title={p.t}
              style={{ width: `${(p.v / total) * 100}%`, background: p.c }}
            />
          ))}
    </div>
  );
}

function Stat({
  label,
  value,
  color,
}: {
  label: string;
  value: string;
  color?: string;
}) {
  return (
    <div className="min-w-[88px] flex-1 rounded-lg bg-surface px-2.5 py-2">
      <div className="text-[11px] text-ink-muted">{label}</div>
      <div
        className={cn(
          "text-[15px] font-semibold",
          !color && "text-ink-strong"
        )}
        style={color ? { color } : undefined}
      >
        {value}
      </div>
    </div>
  );
}

function Legend({ color, text }: { color: string; text: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span
        style={{ background: color }}
        className="inline-block h-2.5 w-2.5 rounded-sm"
      />
      {text}
    </span>
  );
}

/** 高亮用的状态色：空转（orange）/ 重取（purple）/ 压缩（green） */
const ROW_TONE = {
  compact:
    "border-l-[3px] border-emerald-500 bg-emerald-500/[0.07] pl-1.5 py-0.5 rounded",
  dup: "border-l-[3px] border-orange-500 bg-orange-500/[0.07] pl-1.5 py-0.5 rounded",
  read: "border-l-[3px] border-purple-500 bg-purple-500/[0.07] pl-1.5 py-0.5 rounded",
} as const;

function StepRow({
  s,
  maxDur,
  maxTok,
}: {
  s: TimelineStep;
  maxDur: number;
  maxTok: number;
}) {
  const dur = s.duration_ms || 0;
  const llm = s.llm_duration_ms || 0;
  const tool = s.tool_duration_ms || 0;
  const u = s.usage;
  const inTok = u?.prompt_tokens || 0;
  const outTok = u?.completion_tokens || 0;
  // 具体内容（思考 / 工具入参 与 工具返回）默认收起，点开才展开——内容可能很长
  const [open, setOpen] = useState<"in" | "out" | null>(null);
  // 上下文压缩：不是步骤，但确实花了一次 LLM 调用的钱与时间——必须在时间线上
  // 看得见，否则只能靠 dump 文件名猜「哪一步压了」
  const isCompact = s.virtual === "compact";
  // 空转 / 追回抖动的步：整行高亮 + 左侧色条，扫一眼就知道卡在哪一步
  const tone = isCompact
    ? ROW_TONE.compact
    : s.dup
    ? ROW_TONE.dup
    : s.read_repeat
    ? ROW_TONE.read
    : "";

  return (
    <div className={cn("mb-3", tone)}>
      <div className="flex flex-wrap items-center gap-2 text-[12px]">
        <span className="w-[22px] text-ink-faint">
          {s.seq != null ? `#${s.seq}` : ""}
        </span>

        {isCompact ? (
          <span className="rounded-md bg-emerald-500/15 px-1.5 py-0.5 text-[11px] leading-4 text-emerald-600 dark:text-emerald-300">
            上下文压缩
          </span>
        ) : (
          <span
            className={cn(
              "rounded-md px-1.5 py-0.5 text-[11px] leading-4",
              s.is_tool
                ? "bg-orange-500/15 text-orange-600 dark:text-orange-300"
                : "bg-blue-500/15 text-blue-600 dark:text-blue-300"
            )}
          >
            {s.is_tool ? s.kind : "思考"}
          </span>
        )}

        {s.dup && (
          <span className="rounded-md bg-orange-500/15 px-1.5 py-0.5 text-[11px] leading-4 text-orange-600 dark:text-orange-300">
            空转 ×{s.dup.nth}
            {s.dup.first_seq != null ? `（同 #${s.dup.first_seq} 步）` : ""}
          </span>
        )}
        {s.read_repeat && (
          <span className="rounded-md bg-purple-500/15 px-1.5 py-0.5 text-[11px] leading-4 text-purple-600 dark:text-purple-300">
            重取同一块
          </span>
        )}

        <span className="text-ink">
          {fmtDuration(dur)}
          {tool > 0 && `（LLM ${fmtDuration(llm)} · 工具 ${fmtDuration(tool)}）`}
        </span>
        <span className="text-ink-muted">
          模型输入 {fmtTokens(inTok)} · 模型输出 {fmtTokens(outTok)}
        </span>
        {s.output_size > 0 && (
          <span
            className={cn(
              s.truncated
                ? "text-orange-600 dark:text-orange-300"
                : "text-ink-muted"
            )}
          >
            工具返回 {fmtTokens(s.output_size)}
            {s.truncated && "（已缩减）"}
          </span>
        )}

        {s.input_text && (
          <button
            type="button"
            onClick={() => setOpen(open === "in" ? null : "in")}
            className="text-[12px] text-brand transition-opacity hover:opacity-75"
          >
            {open === "in" ? "收起" : "思考"}
          </button>
        )}
        {s.output_text && (
          <button
            type="button"
            onClick={() => setOpen(open === "out" ? null : "out")}
            className="text-[12px] text-brand transition-opacity hover:opacity-75"
          >
            {open === "out" ? "收起" : "结果"}
          </button>
        )}
      </div>

      <div className="mt-1 flex gap-2.5 pl-[30px]">
        <div className="flex-1">
          <Bar
            width={(dur / maxDur) * 100}
            parts={[
              { v: llm, c: C_LLM, t: `LLM ${fmtDuration(llm)}` },
              { v: tool, c: C_TOOL, t: `工具 ${fmtDuration(tool)}` },
            ]}
          />
        </div>
        <div className="flex-1">
          <Bar
            width={((inTok + outTok) / maxTok) * 100}
            parts={[
              { v: inTok, c: C_IN, t: `输入 ${fmtTokens(inTok)}` },
              { v: outTok, c: C_OUT, t: `输出 ${fmtTokens(outTok)}` },
            ]}
          />
        </div>
      </div>

      {open && (
        <pre className="ml-[30px] mt-1.5 max-h-[320px] overflow-auto whitespace-pre-wrap break-words rounded-md border border-line-soft bg-surface p-2.5 font-mono text-[12px] leading-[1.6] text-ink">
          {open === "in" ? s.input_text : s.output_text}
        </pre>
      )}
    </div>
  );
}

/**
 * 本轮的空转（重复调用）与追回抖动。
 *
 * 只给 token 曲线看不出模型在绕圈子：同样的步数、相似的耗时，可能是正常推进，
 * 也可能是同一个工具反复调。把重复调用摆在同一屏，排查才有抓手。
 */
function DuplicateSection({ d }: { d?: DuplicateCalls | null }) {
  if (!d) return null;
  const rd = d.read;
  const hasRead = !!rd && rd.calls > 0;
  const clean = d.duplicate_count === 0 && (!hasRead || rd.repeats === 0);
  return (
    <div className="mt-5">
      <div className="mb-1.5 text-[13px] font-semibold text-ink-strong">
        重复调用 / 空转
        <span className="ml-1.5 font-normal text-ink-muted">
          （上面时间线里被高亮的就是这几步）
        </span>
      </div>

      {d.total === 0 ? (
        <div className="text-[12px] text-ink-muted">
          这一轮没有工具调用记录。
        </div>
      ) : clean ? (
        <div className="text-[12px] text-emerald-600 dark:text-emerald-300">
          共 {d.total} 次工具调用，没有发现空转。
        </div>
      ) : (
        <div className="text-[12px] leading-[1.9] text-ink">
          <div>
            共 {d.total} 次工具调用，其中{" "}
            <b style={{ color: C_TOOL }}>{d.duplicate_count} 次是重复</b>
            （同工具 + 同参数）· 空转率{" "}
            {(d.duplicate_rate * 100).toFixed(1)}%
          </div>

          {d.groups.length > 0 && (
            <div className="mt-1 text-ink-muted">
              {d.groups.map((g, i) => (
                <div key={i}>
                  <code className="rounded bg-surface px-1.5 py-0.5 font-mono text-[11px]">
                    {g.tool}
                  </code>{" "}
                  ×{g.count}
                  {g.args_hash ? (
                    <span className="text-ink-faint"> · 入参 #{g.args_hash}</span>
                  ) : null}
                </div>
              ))}
            </div>
          )}

          {hasRead ? (
            <div className="mt-1">
              追回（read）：{rd.calls} 次，重复展开同一块 {rd.repeats} 次（
              {(rd.repeat_rate * 100).toFixed(1)}%）
              {rd.repeats > 0 ? (
                <span style={{ color: C_TOOL }}>
                  {" "}
                  · 偏高说明「取回 → 被压 → 又取回」在抖动，可调大「追回内容保活条数」
                </span>
              ) : null}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

/**
 * 一轮用量的详情可视化：总览 + 每个 ReAct 步骤的「耗时分解 / token 分解」。
 *
 * 数据按需拉取（点开才请求），因此对历史消息同样有效——历史消息只带汇总，
 * 分步明细不存在消息里。
 */
export default function UsageDetailModal({
  messageId,
  open,
  onClose,
}: {
  messageId: string | null;
  open: boolean;
  onClose: () => void;
}) {
  const [data, setData] = useState<MessageTimeline | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open || !messageId) return;
    let alive = true;
    setLoading(true);
    setData(null);
    apiMessageTimeline(messageId)
      .then((d) => {
        if (alive) setData(d);
      })
      .catch(() => {
        if (alive) setData(null);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [open, messageId]);

  const steps = data?.steps ?? [];
  // 压缩节点混在 steps 里，但它不是步骤——步数、上限判断都只数真步骤
  const realSteps = steps.filter((s) => s.virtual !== "compact");
  const maxDur = Math.max(1, ...steps.map((s) => s.duration_ms || 0));
  const maxTok = Math.max(1, ...steps.map((s) => s.usage?.total_tokens || 0));
  const sum = data?.summary;

  return (
    <Modal open={open} onClose={onClose} width={880} title="本轮用量详情">
      {loading ? (
        <div className="flex items-center justify-center py-12 text-ink-muted">
          <Loader2 className="h-5 w-5 animate-spin" />
        </div>
      ) : !data || !sum ? (
        <div className="py-12 text-center text-[13px] text-ink-faint">
          没有查到这一轮的用量明细
        </div>
      ) : (
        <div>
          <div className="flex flex-wrap gap-2">
            <Stat label="模型输入" value={fmtTokens(sum.prompt_tokens)} color={C_IN} />
            <Stat
              label="模型输出"
              value={fmtTokens(sum.completion_tokens)}
              color={C_OUT}
            />
            <Stat
              label="缓存命中"
              value={
                sum.cached_reported
                  ? `${(sum.cache_hit_rate * 100).toFixed(0)}%`
                  : "未上报"
              }
            />
            {/* 缓存写入也是成本项（写比读贵），有值才展示 */}
            {sum.cache_write_tokens ? (
              <Stat
                label="缓存写入"
                value={fmtTokens(sum.cache_write_tokens)}
                color={C_TOOL}
              />
            ) : null}
            <Stat label="LLM 调用" value={`${sum.calls} 次`} />
            <Stat
              label="LLM 耗时"
              value={fmtDuration(sum.duration_ms)}
              color={C_LLM}
            />
            <Stat label="成本" value={fmtCost(sum.cost, sum.cost_currency)} />
          </div>

          {steps.length > 0 && (
            <>
              <div className="mb-2 mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-ink-muted">
                <span className="text-ink">耗时分解</span>
                <Legend color={C_LLM} text="LLM" />
                <Legend color={C_TOOL} text="工具" />
                <span className="ml-4 text-ink">token 分解</span>
                <Legend color={C_IN} text="输入" />
                <Legend color={C_OUT} text="输出" />
              </div>

              <div className="mb-2 text-[12px] text-ink-muted">
                共 {realSteps.length} 步
                {steps.length > realSteps.length && (
                  <span className="text-emerald-600 dark:text-emerald-300">
                    {" "}
                    · 另有 {steps.length - realSteps.length} 次上下文压缩
                  </span>
                )}
                {realSteps.length >= 30 && (
                  <span style={{ color: C_TOOL }}>
                    {" "}
                    · 已达步数上限（30），最后一步被强制收尾——通常意味着
                    模型在绕圈子
                  </span>
                )}
              </div>

              <div>
                {steps.map((s, i) => (
                  // 压缩节点 step_id 为 null，不能拿它当 key（多个压缩节点会撞 key）
                  <StepRow
                    key={s.step_id ?? `virtual-${s.virtual}-${i}`}
                    s={s}
                    maxDur={maxDur}
                    maxTok={maxTok}
                  />
                ))}
              </div>

              <div className="mt-1 text-[11px] leading-[1.7] text-ink-faint">
                条形长度 = 该步相对最长一步的比例；左条为耗时（LLM/工具），右条为
                token（输入/输出）。橙色 = 空转步，紫色 = 重取同一块，
                <span className="text-emerald-600 dark:text-emerald-300">
                  绿色 = 上下文压缩
                </span>
                （不是步骤，但是一次真实的 LLM 调用）。
                <br />
                「模型输入 / 模型输出」是<b>大模型调用</b>的真实 token；
                「思考」是模型这步的决策，「结果」是工具产物（会算进下一步的
                输入）——完整 prompt 未落库，故不展示。
              </div>
            </>
          )}

          {steps.length === 0 && (
            <div className="mt-3 text-[12px] text-ink-muted">
              这一轮没有分步记录（可能是纯对话，未走 ReAct 工具循环）。
            </div>
          )}

          {/* 空转 / 追回抖动：和 token 曲线放一起才能回答「这轮为什么慢、为什么贵」 */}
          <DuplicateSection d={data?.duplicates} />
        </div>
      )}
    </Modal>
  );
}
