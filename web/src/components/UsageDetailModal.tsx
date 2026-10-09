import { useEffect, useState } from "react";
import { Empty, Modal, Spin, Tag, Typography } from "antd";

import { apiMessageTimeline } from "../api/client";
import type {
  DuplicateCalls,
  MessageTimeline,
  TimelineStep,
} from "../api/types";
import { fmtCost, fmtDuration, fmtTokens } from "./UsageTag";

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
      style={{
        width: `${Math.max(width, 0.5)}%`,
        display: "flex",
        height: 8,
        borderRadius: 4,
        overflow: "hidden",
        background: "#f5f5f5",
      }}
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

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div
      style={{
        flex: 1,
        minWidth: 88,
        background: "#fafafa",
        borderRadius: 8,
        padding: "8px 10px",
      }}
    >
      <div style={{ fontSize: 11, color: "#8c8c8c" }}>{label}</div>
      <div style={{ fontSize: 15, fontWeight: 600, color: color || "#262626" }}>
        {value}
      </div>
    </div>
  );
}

function Legend({ color, text }: { color: string; text: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
      <span
        style={{
          width: 10,
          height: 10,
          borderRadius: 2,
          background: color,
          display: "inline-block",
        }}
      />
      {text}
    </span>
  );
}

function StepRow({ s, maxDur, maxTok }: { s: TimelineStep; maxDur: number; maxTok: number }) {
  const dur = s.duration_ms || 0;
  const llm = s.llm_duration_ms || 0;
  const tool = s.tool_duration_ms || 0;
  const u = s.usage;
  const inTok = u?.prompt_tokens || 0;
  const outTok = u?.completion_tokens || 0;
  // 具体内容（思考 / 工具入参 与 工具返回）默认收起，点开才展开——内容可能很长
  const [open, setOpen] = useState<"in" | "out" | null>(null);
  // 空转 / 追回抖动的步：整行高亮 + 左侧色条，扫一眼就知道卡在哪一步
  const flagged = !!s.dup || !!s.read_repeat;
  // 上下文压缩：不是步骤，但确实花了一次 LLM 调用的钱与时间——必须在时间线上
  // 看得见，否则只能靠 dump 文件名猜「哪一步压了」
  const isCompact = s.virtual === "compact";
  return (
    <div
      style={{
        marginBottom: 12,
        ...(isCompact
          ? {
              background: "#f6ffed",
              borderLeft: "3px solid #52c41a",
              paddingLeft: 6,
              paddingTop: 2,
              paddingBottom: 2,
              borderRadius: 4,
            }
          : flagged
          ? {
              background: s.dup ? "#fff7e6" : "#f9f0ff",
              borderLeft: `3px solid ${s.dup ? "#fa8c16" : "#722ed1"}`,
              paddingLeft: 6,
              paddingTop: 2,
              paddingBottom: 2,
              borderRadius: 4,
            }
          : {}),
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          fontSize: 12,
          flexWrap: "wrap",
        }}
      >
        <span style={{ color: "#bfbfbf", width: 22 }}>
          {s.seq != null ? `#${s.seq}` : ""}
        </span>
        {isCompact ? (
          <Tag color="green" style={{ marginInlineEnd: 0 }}>
            上下文压缩
          </Tag>
        ) : (
          <Tag color={s.is_tool ? "orange" : "blue"} style={{ marginInlineEnd: 0 }}>
            {s.is_tool ? s.kind : "思考"}
          </Tag>
        )}
        {s.dup && (
          <Tag color="volcano" style={{ marginInlineEnd: 0 }}>
            空转 ×{s.dup.nth}
            {s.dup.first_seq != null ? `（同 #${s.dup.first_seq} 步）` : ""}
          </Tag>
        )}
        {s.read_repeat && (
          <Tag color="purple" style={{ marginInlineEnd: 0 }}>
            重取同一块
          </Tag>
        )}
        <span style={{ color: "#595959" }}>
          {fmtDuration(dur)}
          {tool > 0 &&
            `（LLM ${fmtDuration(llm)} · 工具 ${fmtDuration(tool)}）`}
        </span>
        <span style={{ color: "#8c8c8c" }}>
          模型输入 {fmtTokens(inTok)} · 模型输出 {fmtTokens(outTok)}
        </span>
        {s.output_size > 0 && (
          <span style={{ color: s.truncated ? "#fa8c16" : "#8c8c8c" }}>
            工具返回 {fmtTokens(s.output_size)}
            {s.truncated && "（已缩减）"}
          </span>
        )}
        {s.input_text && (
          <Typography.Link
            style={{ fontSize: 12 }}
            onClick={() => setOpen(open === "in" ? null : "in")}
          >
            {open === "in" ? "收起" : "思考"}
          </Typography.Link>
        )}
        {s.output_text && (
          <Typography.Link
            style={{ fontSize: 12 }}
            onClick={() => setOpen(open === "out" ? null : "out")}
          >
            {open === "out" ? "收起" : "结果"}
          </Typography.Link>
        )}
      </div>
      <div style={{ display: "flex", gap: 10, marginTop: 4, paddingLeft: 30 }}>
        <div style={{ flex: 1 }}>
          <Bar
            width={(dur / maxDur) * 100}
            parts={[
              { v: llm, c: C_LLM, t: `LLM ${fmtDuration(llm)}` },
              { v: tool, c: C_TOOL, t: `工具 ${fmtDuration(tool)}` },
            ]}
          />
        </div>
        <div style={{ flex: 1 }}>
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
        <pre
          style={{
            marginTop: 6,
            marginBottom: 0,
            marginLeft: 30,
            padding: 10,
            background: "#fafafa",
            border: "1px solid #f0f0f0",
            borderRadius: 6,
            fontSize: 12,
            lineHeight: 1.6,
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            maxHeight: 320,
            overflow: "auto",
          }}
        >
          {open === "in" ? s.input_text : s.output_text}
        </pre>
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
    <div style={{ marginTop: 18 }}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>
          重复调用 / 空转
          <span style={{ fontWeight: 400, color: "#8c8c8c", marginLeft: 6 }}>
            （上面时间线里被高亮的就是这几步）
          </span>
        </div>
      {d.total === 0 ? (
        <div style={{ fontSize: 12, color: "#8c8c8c" }}>这一轮没有工具调用记录。</div>
      ) : clean ? (
        <div style={{ fontSize: 12, color: "#52c41a" }}>
          共 {d.total} 次工具调用，没有发现空转。
        </div>
      ) : (
        <div style={{ fontSize: 12, lineHeight: 1.9 }}>
          <div>
            共 {d.total} 次工具调用，其中{" "}
            <b style={{ color: C_TOOL }}>{d.duplicate_count} 次是重复</b>
            （同工具 + 同参数）· 空转率{" "}
            {(d.duplicate_rate * 100).toFixed(1)}%
          </div>
          {d.groups.length > 0 && (
            <div style={{ marginTop: 4, color: "#595959" }}>
              {d.groups.map((g, i) => (
                <div key={i}>
                  <code>{g.tool}</code> ×{g.count}
                  {g.args_hash ? (
                    <span style={{ color: "#bfbfbf" }}> · 入参 #{g.args_hash}</span>
                  ) : null}
                </div>
              ))}
            </div>
          )}
          {hasRead ? (
            <div style={{ marginTop: 4 }}>
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
    <Modal open={open} onCancel={onClose} footer={null} width={880} title="本轮用量详情">
      {loading ? (
        <div style={{ textAlign: "center", padding: 48 }}>
          <Spin />
        </div>
      ) : !data || !sum ? (
        <Empty description="没有查到这一轮的用量明细" />
      ) : (
        <div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Stat label="模型输入" value={fmtTokens(sum.prompt_tokens)} color={C_IN} />
            <Stat label="模型输出" value={fmtTokens(sum.completion_tokens)} color={C_OUT} />
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
            <Stat label="LLM 耗时" value={fmtDuration(sum.duration_ms)} color={C_LLM} />
            <Stat label="成本" value={fmtCost(sum.cost, sum.cost_currency)} />
          </div>

          {steps.length > 0 && (
            <>
              <div
                style={{
                  display: "flex",
                  gap: 16,
                  fontSize: 11,
                  color: "#8c8c8c",
                  margin: "16px 0 8px",
                }}
              >
                <span style={{ color: "#595959" }}>耗时分解</span>
                <Legend color={C_LLM} text="LLM" />
                <Legend color={C_TOOL} text="工具" />
                <span style={{ marginLeft: 16, color: "#595959" }}>token 分解</span>
                <Legend color={C_IN} text="输入" />
                <Legend color={C_OUT} text="输出" />
              </div>
              <div style={{ fontSize: 12, color: "#8c8c8c", marginBottom: 8 }}>
                共 {realSteps.length} 步
                {steps.length > realSteps.length && (
                  <span style={{ color: "#52c41a" }}>
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
              <div
                style={{ fontSize: 11, color: "#bfbfbf", marginTop: 4, lineHeight: 1.7 }}
              >
                条形长度 = 该步相对最长一步的比例；左条为耗时（LLM/工具），右条为
                token（输入/输出）。橙色 = 空转步，紫色 = 重取同一块，
                <span style={{ color: "#52c41a" }}>绿色 = 上下文压缩</span>
                （不是步骤，但是一次真实的 LLM 调用）。
                <br />
                「模型输入 / 模型输出」是<b>大模型调用</b>的真实 token；
                「思考」是模型这步的决策，「结果」是工具产物（会算进下一步的
                输入）——完整 prompt 未落库，故不展示。
              </div>
            </>
          )}
          {steps.length === 0 && (
            <div style={{ fontSize: 12, color: "#8c8c8c", marginTop: 12 }}>
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
