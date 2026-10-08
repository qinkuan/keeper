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
import {
  Alert,
  Button,
  Col,
  Divider,
  Row,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";

import type { EvalCaseResult, EvalCaseStatus, EvalReport } from "../api/types";

const { Text } = Typography;

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
  { label: string; color: string }
> = {
  broken: { label: "回归", color: "red" },
  fixed: { label: "修复", color: "green" },
  new: { label: "新增", color: "blue" },
  kept: { label: "保持", color: "default" },
  skipped: { label: "跳过", color: "default" },
};

export type EvalRow = { c: EvalCaseResult; st: EvalCaseStatus };

/** 逐条用例表：状态 / 用例 / 判定依据 / 步 / token / 耗时。 */
export function buildColumns(): ColumnsType<EvalRow> {
  return [
    {
      title: "状态",
      dataIndex: "st",
      width: 74,
      render: (st: EvalCaseStatus, r) =>
        r.c.skipped ? (
          <Tag>跳过</Tag>
        ) : (
          <Tag color={STATUS_META[st].color}>{STATUS_META[st].label}</Tag>
        ),
    },
    {
      title: "用例",
      dataIndex: ["c", "case_id"],
      width: 200,
      render: (_, r) => (
        <Space direction="vertical" size={0}>
          <Text code>{r.c.case_id}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {r.c.title}
          </Text>
        </Space>
      ),
    },
    {
      title: "判定依据",
      dataIndex: ["c", "summary"],
      render: (_, r) => {
        if (r.c.error) return <Tag color="red">运行异常</Tag>;
        if (!r.c.passed && r.c.failures.length) {
          const f = r.c.failures[0];
          return (
            <Tooltip
              title={r.c.failures
                .map((x) => `${x.assert}：${x.detail}`)
                .join("\n")}
            >
              <Text type="danger" style={{ fontSize: 12 }}>
                {f.assert}：{f.detail}
              </Text>
            </Tooltip>
          );
        }
        return (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {r.c.summary || "—"}
          </Text>
        );
      },
    },
    { title: "步", dataIndex: ["c", "metrics", "steps"], width: 50 },
    {
      title: "token",
      dataIndex: ["c", "metrics", "total_tokens"],
      width: 86,
      render: (v: number) => fmtNum(v),
    },
    {
      title: "耗时",
      dataIndex: ["c", "metrics", "duration_ms"],
      width: 76,
      render: (v: number) => fmtSec(v),
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
    <Text
      type={better ? "success" : "danger"}
      style={{ fontSize: 12, marginLeft: 4 }}
    >
      {d.delta > 0 ? "+" : ""}
      {p.toFixed(0)}%
    </Text>
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
  const cmp = report.compare;
  const s = report.summary;
  const fp = report.fingerprint;
  const broken = rows.filter((x) => x.st === "broken");
  const failedCount = rows.filter((x) => !x.c.passed && !x.c.skipped).length;
  // 选集运行（调试用的 --case / --suite）的通过率只针对跑过的那几条。
  // 不标出来的话，很容易把「1 条里过了 1 条」读成「整体 100%」。
  const subset = report.selection?.mode === "subset";

  return (
    <>
      {subset && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={`选集运行：本次只跑了 ${rows.length} 条，下面的通过率只针对这几条`}
          description="完整基线请看历史里 mode=all 的那次（latest 不会被选集运行覆盖）。"
        />
      )}
      {broken.length > 0 && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
          message={`${broken.length} 条用例回归`}
          description={
            <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
              {broken.map((b) => (
                <li key={b.c.case_id}>
                  <Text code>{b.c.case_id}</Text>
                  {b.c.failures.length ? `　${b.c.failures[0].detail}` : ""}
                </li>
              ))}
            </ul>
          }
        />
      )}
      {cmp?.has_baseline && broken.length === 0 && (
        <Alert
          type="success"
          showIcon
          style={{ marginBottom: 12 }}
          message={cmp.verdict}
        />
      )}
      {!cmp?.has_baseline && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="这是首次基线，没有对比对象。再跑一次才会有「相对上次」的变化。"
        />
      )}
      {/* 对比方向必须写明：拿一份**更早**的报告当对比对象时，「回归 1 条」
          指的是「那份老报告比现在差」，很容易被读成「现在退化了」 */}
      {cmp?.has_baseline && (
        <Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 8 }}>
          对比对象：{cmp.baseline_run_id}
          {(() => {
            const d = cmp.summary?.passed;
            if (!d) return null;
            const diff = d.to - d.from;
            if (diff === 0) return "　—— 与它持平";
            return diff > 0
              ? `　—— 当前比它好 ${diff} 条`
              : `　—— 当前比它差 ${Math.abs(diff)} 条`;
          })()}
        </Text>
      )}
      {cmp?.fingerprint_changed?.length ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="被测配置变了，下面的指标 delta 不能归因给代码改动"
          description={cmp.fingerprint_changed.join("；")}
        />
      ) : null}

      <Row gutter={16}>
        <Col span={6}>
          <Statistic
            title="通过率"
            value={fmtPct(s.pass_rate)}
            valueStyle={{
              color:
                s.pass_rate >= 0.9
                  ? "#389e0d"
                  : s.pass_rate >= 0.7
                  ? "#fa8c16"
                  : "#cf1322",
            }}
            suffix={
              <Text type="secondary" style={{ fontSize: 14 }}>
                {s.passed}/{s.total}
              </Text>
            }
          />
        </Col>
        <Col span={5}>
          <Statistic
            title="平均步数"
            value={fmtNum(s.avg_steps, 1)}
            suffix={<DeltaText d={cmp?.summary?.avg_steps} invert />}
          />
        </Col>
        <Col span={5}>
          <Statistic
            title="平均 token"
            value={fmtNum(s.avg_tokens)}
            suffix={<DeltaText d={cmp?.summary?.avg_tokens} invert />}
          />
        </Col>
        <Col span={4}>
          <Statistic
            title="平均耗时"
            value={fmtSec(s.avg_duration_ms)}
            suffix={<DeltaText d={cmp?.summary?.avg_duration_ms} invert />}
          />
        </Col>
        <Col span={4}>
          <Statistic
            title="成本合计"
            value={s.total_cost ?? "—"}
            precision={s.total_cost != null ? 4 : undefined}
          />
        </Col>
      </Row>

      <Text type="secondary" style={{ fontSize: 12 }}>
        模型 {fp?.model || "—"}　工具 {fp?.tool_count ?? 0} 个 / 技能{" "}
        {fp?.skill_count ?? 0} 个　追问 {s.asked_cases} 条 / 预算收尾{" "}
        {s.budget_stopped_cases} 条 / 跑挂 {s.error_cases} 条 / 工具调用{" "}
        {s.tool_calls} 次
      </Text>

      <Divider style={{ margin: "12px 0" }} />

      <Space style={{ marginBottom: 8 }}>
        <Text strong>逐条用例</Text>
        {failedCount > 0 && (
          <Text type="danger" style={{ fontSize: 12 }}>
            {failedCount} 条未通过
          </Text>
        )}
        <Button size="small" type="link" onClick={onOpenMd}>
          查看 Markdown
        </Button>
      </Space>
      <Table
        size="small"
        rowKey={(r) => r.c.case_id}
        columns={buildColumns()}
        dataSource={rows}
        pagination={false}
        scroll={{ y: 420 }}
        expandable={{
          expandedRowRender: (r) => (
            <Space direction="vertical" size={4} style={{ fontSize: 12 }}>
              <Text type="secondary">输入：{r.c.input}</Text>
              {r.c.tool_calls.length > 0 && (
                <Text type="secondary">
                  调用工具：{r.c.tool_calls.join("、")}
                </Text>
              )}
              {r.c.answer_preview && (
                <Text type="secondary">答案节选：{r.c.answer_preview}</Text>
              )}
              {r.c.failures.map((f, i) => (
                <Text key={i} type="danger">
                  {f.assert}：{f.detail}
                </Text>
              ))}
            </Space>
          ),
        }}
      />
    </>
  );
}