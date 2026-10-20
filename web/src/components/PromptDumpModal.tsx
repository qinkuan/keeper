import { useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";

import { apiMessageDump, apiMessageTimeline } from "../api/client";
import type { DumpCall, SubagentBreakdownItem } from "../api/types";
import { fmtCost, fmtDuration, fmtTokens } from "./UsageTag";
import { Modal } from "./ui/Modal";
import { Collapse } from "./ui/Collapse";
import { Hint } from "./ui/Page";
import { Tabs } from "./ui/Tabs";
import { useState } from "react";

/** 落盘内容的展示块：等宽、可滚动、保留换行 */
function Pre({ text }: { text: string }) {
  return (
    <pre className="max-h-[300px] overflow-auto whitespace-pre-wrap break-words rounded-md border border-line-soft bg-surface p-2.5 font-mono text-[12px] leading-[1.6] text-ink">
      {text}
    </pre>
  );
}

/** 人类可读标签：第几步 / 子迭代 / 最终总结 / 记忆摘要 */
function labelOf(f: DumpCall, i: number): string {
  if (f.kind === "react_step" && f.step_seq != null)
    return f.sub_step != null
      ? `第 ${f.step_seq}.${f.sub_step} 步`
      : `第 ${f.step_seq} 步`;
  if (f.kind === "final_summary") return "最终总结";
  if (f.kind === "turn_summary") return "记忆摘要";
  return f.kind || `调用 ${i + 1}`;
}

/**
 * 调用类型标签配色。
 *
 * 三类之间要能区分，但不能是三支互不相干的颜色——这里是**同一支品牌蓝的
 * 三档深浅**：主体步骤最实、轮摘要居中、终局摘要最浅。层级关系一眼看出，
 * 界面也不会因为一条时间线上出现三种色相而变吵。
 */
function tagClass(f: DumpCall): string {
  if (f.kind === "turn_summary")
    return "bg-brand/12 text-brand";
  if (f.kind === "final_summary")
    return "bg-brand/20 text-brand";
  if (f.kind === "react_step")
    return "bg-brand/[0.08] text-brand";
  return "bg-black/[0.06] text-ink-muted dark:bg-white/[0.08]";
}

function CallBody({ f }: { f: DumpCall }) {
  const items = [
    {
      key: "system",
      label: `system 提示词（${fmtTokens(f.system?.length ?? 0)} 字符）`,
      children: <Pre text={f.system || "（无）"} />,
    },
    {
      key: "messages",
      label: `完整 messages（${f.messages?.length ?? 0} 条）`,
      children: (
        <div className="space-y-2.5">
          {(f.messages ?? []).map((m, j) => (
            <div key={j}>
              <span
                className={`inline-flex rounded-md px-1.5 py-0.5 text-[11px] leading-4 ${
                  m.role === "assistant"
                    ? "bg-brand/15 text-brand"
                    : m.role === "user"
                    ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-300"
                    : "bg-black/[0.06] text-ink-muted dark:bg-white/[0.08]"
                }`}
              >
                {m.role}
              </span>
              <div className="mt-1">
                <Pre text={m.content} />
              </div>
            </div>
          ))}
        </div>
      ),
    },
    {
      key: "response",
      label: `模型响应（${fmtTokens(f.response?.length ?? 0)} 字符）`,
      children: <Pre text={f.response || "（无）"} />,
    },
    ...(f.tool_args
      ? [
          {
            key: "tool_args",
            label: `工具入参（${fmtTokens(f.tool_args.length)} 字符）`,
            children: <Pre text={f.tool_args} />,
          },
        ]
      : []),
  ];
  return <Collapse items={items} />;
}

/**
 * 查看某一轮落盘的**完整 prompt**（调试用）。
 *
 * 数据来自本地 dump 文件（设置里开启后才会有）。未开启 / 该轮没有文件时给出
 * 明确提示，而不是空白。
 */
export default function PromptDumpModal({
  messageId,
  open,
  onClose,
}: {
  messageId: string | null;
  open: boolean;
  onClose: () => void;
}) {
  const [tab, setTab] = useState("0");
  const dumpQ = useQuery({
    queryKey: ["message-dump", messageId],
    queryFn: () => apiMessageDump(messageId as string),
    enabled: open && !!messageId,
  });
  // 同一轮的时间线：用来拿子 agent 的成本汇总（dump 文件里只有 token，没有单价换算）
  const tlQ = useQuery({
    queryKey: ["message-timeline", messageId],
    queryFn: () => apiMessageTimeline(messageId as string),
    enabled: open && !!messageId,
  });
  const subagents: SubagentBreakdownItem[] = tlQ.data?.subagents ?? [];

  const files = dumpQ.data?.files ?? [];
  const idx = Math.min(Number(tab) || 0, Math.max(0, files.length - 1));
  const cur = files[idx];

  return (
    <Modal open={open} onClose={onClose} title="完整 prompt（dump）" width={900}>
      {dumpQ.isLoading ? (
        <div className="flex items-center justify-center py-12 text-ink-muted">
          <Loader2 className="h-5 w-5 animate-spin" />
        </div>
      ) : dumpQ.data && !dumpQ.data.enabled ? (
        <Hint tone="info" title="未开启落盘">
          到「设置」里打开「完整 prompt 落盘」，之后的消息才会记录。
        </Hint>
      ) : files.length === 0 ? (
        <div className="py-12 text-center text-[13px] text-ink-faint">
          这一轮没有 dump 文件（可能是开启落盘之前的对话）
        </div>
      ) : (
        <>
          <div className="mb-2 text-[12px] text-ink-muted">
            共 {files.length} 次调用
            {dumpQ.data?.dir ? ` · ${dumpQ.data.dir}` : ""}
          </div>

          {/* 子 agent 调用汇总：轮次 + token + 成本（成本来自时间线，dump 文件里只有 token） */}
          {subagents.length > 0 && (
            <div className="mb-3 flex flex-wrap gap-2">
              {subagents.map((s) => (
                <span
                  key={s.session_id}
                  className="inline-flex items-center gap-1 rounded-md bg-violet-500/12 px-2 py-1 text-[12px] leading-4 text-violet-600 dark:text-violet-300"
                >
                  <b>{s.label}</b>
                  <span>{s.calls} 轮</span>
                  <span>
                    {fmtTokens(s.prompt_tokens + s.completion_tokens)}
                  </span>
                  {s.cost != null ? (
                    <span>· {fmtCost(s.cost, s.cost_currency)}</span>
                  ) : null}
                </span>
              ))}
            </div>
          )}

          <Tabs
            className="mb-3"
            value={String(idx)}
            onChange={setTab}
            items={files.map((f, i) => ({
              key: String(i),
              label: labelOf(f, i),
            }))}
          />

          {cur && (
            <>
              <div className="mb-3 flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px] text-ink-muted">
                {cur.is_delegatee && cur.subagent_label ? (
                  <span className="inline-flex rounded-md bg-violet-500/15 px-1.5 py-0.5 text-[11px] leading-4 text-violet-600 dark:text-violet-300">
                    {cur.subagent_label}
                  </span>
                ) : null}
                <span
                  className={`inline-flex rounded-md px-1.5 py-0.5 text-[11px] leading-4 ${tagClass(
                    cur
                  )}`}
                >
                  {labelOf(cur, idx)}
                </span>
                {cur.file && (
                  <code className="rounded bg-surface px-1.5 py-0.5 font-mono text-[11px]">
                    {cur.file}
                  </code>
                )}
                <span>模型 {cur.model ?? "—"}</span>
                {cur.ts ? (
                  <span title={cur.ts}>
                    ⏱{" "}
                    {new Date(cur.ts).toLocaleTimeString("zh-CN", {
                      hour12: false,
                    })}
                  </span>
                ) : null}
                <span>{cur.is_stream ? "流式" : "非流式"}</span>
                <span>耗时 {fmtDuration(cur.duration_ms)}</span>
                <span>首字 {fmtDuration(cur.ttft_ms)}</span>
                <span>模型输入 {fmtTokens(cur.usage?.prompt_tokens ?? 0)}</span>
                <span>模型输出 {fmtTokens(cur.usage?.completion_tokens ?? 0)}</span>
                {cur.usage?.cached_tokens ? (
                  <span>缓存命中 {fmtTokens(cur.usage.cached_tokens)}</span>
                ) : null}
              </div>
              <CallBody f={cur} />
            </>
          )}
        </>
      )}
    </Modal>
  );
}
