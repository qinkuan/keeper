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
import {
  Alert,
  Card,
  Col,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tooltip,
  Typography,
} from "antd";
import { LineChartOutlined } from "@ant-design/icons";

import AgentUsagePanel from "../components/AgentUsagePanel";
import EvalBaselinePanel from "../components/EvalBaselinePanel";
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

  return (
    <div style={{ padding: 16, overflow: "auto", height: "100%" }}>
      <Space style={{ marginBottom: 12 }} wrap>
        <Select
          style={{ width: 220 }}
          value={effectiveAgent}
          onChange={setAgentId}
          loading={agentsQ.isLoading}
          options={agents.map((a) => ({ value: a.id, label: a.name }))}
          placeholder="选择智能体"
        />
        <Segmented
          value={range}
          onChange={(v) => setRange(String(v))}
          options={RANGES.map((r) => ({ value: r.value, label: r.label }))}
        />
      </Space>

      {!effectiveAgent ? (
        <Card size="small">
          <Typography.Text type="secondary">请先选择一个智能体</Typography.Text>
        </Card>
      ) : (
        <>
          {/* 1. 用量：概览 / 趋势 / 工具 / 重复调用 / 空转 / 错误 / 追问率 / 能力加载 */}
          <AgentUsagePanel agentId={effectiveAgent} range={range} />

          {/* 2. 解析结构 */}
          <Card
            size="small"
            title={
              <Space>
                <LineChartOutlined />
                解析结构（模型有多想并行工具、有没有自编回执）
              </Space>
            }
          >
            {parseQ.isLoading ? (
              <Spin size="small" />
            ) : parse && parse.total > 0 ? (
              <>
                <Row gutter={16} style={{ marginBottom: 8 }}>
                  <Col span={4}>
                    <Statistic title="解析步数" value={parse.total} />
                  </Col>
                  <Col span={4}>
                    <Statistic title="多动作步数" value={parse.multi} />
                  </Col>
                  <Col span={4}>
                    <Statistic
                      title="多动作占比"
                      value={pct(parse.multi_rate)}
                      valueStyle={
                        parse.multi_rate >= 0.1 ? { color: "#fa541c" } : undefined
                      }
                    />
                  </Col>
                  <Col span={4}>
                    <Statistic
                      title="被丢弃的动作"
                      value={parse.dropped_total}
                      valueStyle={
                        parse.dropped_total > 0 ? { color: "#fa8c16" } : undefined
                      }
                    />
                  </Col>
                  <Col span={4}>
                    <Statistic title="丢弃率" value={pct(parse.dropped_ratio)} />
                  </Col>
                  <Col span={4}>
                    <Statistic
                      title="自编回执"
                      value={parse.hallucination}
                      valueStyle={
                        parse.hallucination > 0 ? { color: "#cf1322" } : undefined
                      }
                    />
                  </Col>
                  <Col span={4}>
                    <Statistic
                      title="格式错误"
                      value={parse.format_error}
                      valueStyle={
                        parse.format_error > 0 ? { color: "#cf1322" } : undefined
                      }
                    />
                  </Col>
                </Row>
                {verdict ? (
                  <Alert
                    type={
                      parse.format_error > 0 || parse.hallucination > 0
                        ? "warning"
                        : "info"
                    }
                    showIcon
                    message={verdict}
                  />
                ) : (
                  <Typography.Text type="secondary">
                    样本还太少（需 ≥20 步才给结论），先继续用几天再看。
                  </Typography.Text>
                )}
                {/* 按模型拆：这张表是用来挑模型的，聚合数字看不出差别 */}
                {parse.by_model && Object.keys(parse.by_model).length > 1 ? (
                  <Table
                    style={{ marginTop: 12 }}
                    size="small"
                    pagination={false}
                    rowKey={(r) => r.model}
                    dataSource={Object.entries(parse.by_model).map(([model, s]) => ({
                      model,
                      ...s,
                    }))}
                    columns={[
                      { title: "模型", dataIndex: "model", key: "model" },
                      {
                        title: "解析步数",
                        dataIndex: "total",
                        key: "total",
                        align: "right",
                      },
                      {
                        title: "自编回执",
                        dataIndex: "hallucination",
                        key: "hallucination",
                        align: "right",
                        render: (v: number) =>
                          v > 0 ? (
                            <Typography.Text type="danger">{v}</Typography.Text>
                          ) : (
                            <Typography.Text type="secondary">0</Typography.Text>
                          ),
                      },
                      {
                        title: "幻觉率",
                        key: "rate",
                        align: "right",
                        // 「未知」= 升级前的存量，没有模型名，不参与计算
                        render: (_, r) =>
                          r.rate == null ? (
                            <Tooltip title="升级前的记录没有模型名，不参与幻觉率计算">
                              <Typography.Text type="secondary">—</Typography.Text>
                            </Tooltip>
                          ) : (
                            <Typography.Text
                              type={r.rate >= 0.05 ? "danger" : undefined}
                            >
                              {pct(r.rate)}
                            </Typography.Text>
                          ),
                      },
                      {
                        // 格式错误单列：它不是模型跑飞，是端点返回了本接口不接受的
                        // 格式，点破基本没用 —— 定位到哪一列就去看那台机器的配置
                        title: "格式错误",
                        key: "format_error",
                        align: "right",
                        render: (_, r) =>
                          (r.format_error ?? 0) > 0 ? (
                            <Typography.Text type="danger">
                              {r.format_error}
                            </Typography.Text>
                          ) : (
                            <Typography.Text type="secondary">0</Typography.Text>
                          ),
                      },
                    ]}
                  />
                ) : null}
              </>
            ) : (
              <Typography.Text type="secondary">
                暂无解析统计（这项统计从启用后开始累积）
              </Typography.Text>
            )}
          </Card>
        {/* 3. 效果评估基线：改动之后跑一轮，看有没有用例掉 */}
          <EvalBaselinePanel agentId={effectiveAgent} />
        </>
      )}
    </div>
  );
}
