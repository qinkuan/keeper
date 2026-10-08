import type { CSSProperties } from "react";
import {
  Alert,
  Collapse,
  Empty,
  Modal,
  Spin,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { useQuery } from "@tanstack/react-query";

import { apiMessageDump } from "../api/client";
import type { DumpCall } from "../api/types";
import { fmtDuration, fmtTokens } from "./UsageTag";

const preStyle: CSSProperties = {
  margin: 0,
  padding: 10,
  background: "#fafafa",
  border: "1px solid #f0f0f0",
  borderRadius: 6,
  fontSize: 12,
  lineHeight: 1.6,
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
  maxHeight: 300,
  overflow: "auto",
};

/** 人类可读标签：第几步 / 最终总结 / 记忆摘要 */
function labelOf(f: DumpCall, i: number): string {
  if (f.kind === "react_step" && f.step_seq != null) return `第 ${f.step_seq} 步`;
  if (f.kind === "final_summary") return "最终总结";
  if (f.kind === "turn_summary") return "记忆摘要";
  return f.kind || `调用 ${i + 1}`;
}

/** 记忆摘要用紫色突出——它不在 ReAct 步数里，别当成某一步 */
function tagColor(f: DumpCall): string {
  if (f.kind === "turn_summary") return "purple";
  if (f.kind === "final_summary") return "blue";
  if (f.kind === "react_step") return "geekblue";
  return "default";
}

function callItems(f: DumpCall, idx: number) {
  return [
    {
      key: "system",
      label: `system 提示词（${fmtTokens(f.system?.length ?? 0)} 字符）`,
      children: <pre style={preStyle}>{f.system || "（无）"}</pre>,
    },
    {
      key: "messages",
      label: `完整 messages（${f.messages?.length ?? 0} 条）`,
      children: (f.messages ?? []).map((m, j) => (
        <div key={j} style={{ marginBottom: 10 }}>
          <Tag>{m.role}</Tag>
          <pre style={{ ...preStyle, marginTop: 4 }}>{m.content}</pre>
        </div>
      )),
    },
    {
      key: "response",
      label: `模型响应（${fmtTokens(f.response?.length ?? 0)} 字符）`,
      children: <pre style={preStyle}>{f.response || "（无）"}</pre>,
    },
    ...(f.tool_args
      ? [
          {
            key: "tool_args",
            label: `工具入参（${fmtTokens(f.tool_args.length)} 字符）`,
            children: <pre style={preStyle}>{f.tool_args}</pre>,
          },
        ]
      : []),
  ];
}

/**
 * 查看某一轮落盘的**完整 prompt**（调试用）。

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
  const dumpQ = useQuery({
    queryKey: ["message-dump", messageId],
    queryFn: () => apiMessageDump(messageId as string),
    enabled: open && !!messageId,
  });

  const files = dumpQ.data?.files ?? [];
  const tabItems = files.map((f, i) => ({
    key: String(i),
    label: labelOf(f, i),
    children: (
      <div>
        <div style={{ fontSize: 12, color: "#595959", marginBottom: 10 }}>
          <Tag color={tagColor(f)}>{labelOf(f, i)}</Tag>
          {f.file ? (
            <>
              <code>{f.file}</code> ·{" "}
            </>
          ) : null}
          模型 {f.model ?? "—"} · {f.is_stream ? "流式" : "非流式"} · 耗时{" "}
          {fmtDuration(f.duration_ms)} · 首字 {fmtDuration(f.ttft_ms)} · 模型输入{" "}
          {fmtTokens(f.usage?.prompt_tokens ?? 0)} · 模型输出{" "}
          {fmtTokens(f.usage?.completion_tokens ?? 0)}
          {f.usage?.cached_tokens
            ? ` · 缓存命中 ${fmtTokens(f.usage.cached_tokens)}`
            : ""}
        </div>
        <Collapse size="small" items={callItems(f, i)} />
      </div>
    ),
  }));

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={900}
      title="完整 prompt（dump）"
      destroyOnClose
    >
      {dumpQ.isLoading ? (
        <div style={{ textAlign: "center", padding: 48 }}>
          <Spin />
        </div>
      ) : dumpQ.data && !dumpQ.data.enabled ? (
        <Alert
          type="info"
          showIcon
          message="未开启落盘"
          description="到「设置」里打开「完整 prompt 落盘」，之后的消息才会记录。"
        />
      ) : files.length === 0 ? (
        <Empty description="这一轮没有 dump 文件（可能是开启落盘之前的对话）" />
      ) : (
        <>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            共 {files.length} 次调用
            {dumpQ.data?.dir ? ` · ${dumpQ.data.dir}` : ""}
          </Typography.Text>
          <Tabs style={{ marginTop: 8 }} items={tabItems} />
        </>
      )}
    </Modal>
  );
}
