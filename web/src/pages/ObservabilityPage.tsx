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
                解析结构（模型有多想并行工具）
              </Space>
            }
          >
            {parseQ.isLoading ? (
              <Spin size="small" />
            ) : parse && parse.total > 0 ? (
              <>
                <Row gutter={16} style={{ marginBottom: 8 }}>
                  <Col span={5}>
                    <Statistic title="解析步数" value={parse.total} />
                  </Col>
                  <Col span={5}>
                    <Statistic title="多动作步数" value={parse.multi} />
                  </Col>
                  <Col span={5}>
                    <Statistic
                      title="多动作占比"
                      value={pct(parse.multi_rate)}
                      valueStyle={
                        parse.multi_rate >= 0.1 ? { color: "#fa541c" } : undefined
                      }
                    />
                  </Col>
                  <Col span={5}>
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
                </Row>
                {verdict ? (
                  <Alert type="info" showIcon message={verdict} />
                ) : (
                  <Typography.Text type="secondary">
                    样本还太少（需 ≥20 步才给结论），先继续用几天再看。
                  </Typography.Text>
                )}
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
