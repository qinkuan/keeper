import { useMemo, useState } from "react";
import {
  Card,
  Col,
  Empty,
  Row,
  Segmented,
  Spin,
  Statistic,
  Table,
  Tag,
  Typography,
} from "antd";
import dayjs from "dayjs";
import { useQuery } from "@tanstack/react-query";

import {
  apiAgentMetrics,
  apiAskRate,
  apiCapabilityStats,
  apiDuplicateCalls,
  apiErrorBreakdown,
  apiSlowestCalls,
  apiToolStats,
  apiUsageTimeseries,
} from "../api/client";
import type { ErrorBreakdown, ErrorGroup, SlowCall } from "../api/types";
import ToolStatsTable from "./ToolStatsTable";
import UsageTrendChart from "./UsageTrendChart";
import { fmtCost, fmtDuration, fmtTokens } from "./UsageTag";

const RANGES = [
  { label: "今天", value: "today" },
  { label: "近 7 天", value: "7d" },
  { label: "近 30 天", value: "30d" },
  { label: "全部", value: "all" },
];

/** 时间范围 → since/until（ISO 8601，后端 aggregate 直接吃） */
function rangeToParams(v: string): { since?: string; until?: string } {
  if (v === "all") return {};
  const now = new Date();
  const from = new Date(now);
  if (v === "today") from.setHours(0, 0, 0, 0);
  else if (v === "7d") from.setDate(now.getDate() - 7);
  else if (v === "30d") from.setDate(now.getDate() - 30);
  return { since: from.toISOString(), until: now.toISOString() };
}

/** 把 LLM / 工具两组错误合成一张表，并标注来源 */
function errGroups(data: ErrorBreakdown) {
  return [
    ...data.llm.groups.map((g: ErrorGroup) => ({
      ...g,
      src: "LLM",
      key: `llm-${g.key}`,
    })),
    ...data.tool.groups.map((g: ErrorGroup) => ({
      ...g,
      src: "工具",
      key: `tool-${g.key}`,
    })),
  ];
}

/**
 * 单个 agent 的用量汇总：跨会话看总消耗与成本，支持时间范围。
 *
 * 数据来自 llm_calls 事实表按 agent_id 聚合（/metrics/agents/{id}），
 * 单价命中 llm_profiles 才会算出成本，否则显示「—」。
 */
export default function AgentUsagePanel({
  agentId,
  range: rangeProp,
}: {
  agentId: string;
  /** 受控时间范围。给了就以外部为准（可观测页面统一控制三块的时间窗） */
  range?: string;
}) {
  const [innerRange, setInnerRange] = useState<string>("7d");
  const range = rangeProp ?? innerRange;
  const setRange = rangeProp ? () => {} : setInnerRange;
  const params = useMemo(() => rangeToParams(range), [range]);

  const usageQ = useQuery({
    queryKey: ["agent-usage", agentId, range],
    queryFn: () => apiAgentMetrics(agentId, params),
  });
  // 工具维度没有时间范围参数，始终是全量——够用：它是找「最慢/返回最大」的工具
  const toolsQ = useQuery({
    queryKey: ["agent-tool-stats", agentId],
    queryFn: () => apiToolStats({ agent_id: agentId }),
  });

  // 趋势：今天按小时看波动，更长区间按天看
  const granularity = range === "today" ? "hour" : "day";
  const trendQ = useQuery({
    queryKey: ["agent-trend", agentId, range],
    queryFn: () =>
      apiUsageTimeseries({ agent_id: agentId, ...params, granularity }),
  });

  const slowQ = useQuery({
    queryKey: ["agent-slowest", agentId, range],
    queryFn: () => apiSlowestCalls({ agent_id: agentId, ...params, limit: 10 }),
  });
  const errQ = useQuery({
    queryKey: ["agent-errors", agentId, range],
    queryFn: () =>
      apiErrorBreakdown({ agent_id: agentId, ...params, limit: 20 }),
  });
  const askQ = useQuery({
    queryKey: ["agent-ask-rate", agentId, range],
    queryFn: () => apiAskRate({ agent_id: agentId, ...params }),
  });
  const dupQ = useQuery({
    queryKey: ["agent-duplicates", agentId, range],
    queryFn: () =>
      apiDuplicateCalls({ agent_id: agentId, ...params, limit: 10 }),
  });
  // 能力加载只有 days 粒度（后端没做时间范围端点），跟着时间档粗粒度走
  const capDays = range === "today" ? 1 : range === "30d" ? 30 : 7;
  const capQ = useQuery({
    queryKey: ["agent-capabilities", agentId, capDays],
    queryFn: () =>
      apiCapabilityStats({ agent_id: agentId, days: capDays, top: 10 }),
  });

  const u = usageQ.data;

  return (
    <div>
      {!rangeProp && (
        <Segmented
          value={range}
          onChange={(v) => setRange(String(v))}
          options={RANGES}
          style={{ marginBottom: 12 }}
        />
      )}

      {usageQ.isLoading ? (
        <div style={{ textAlign: "center", padding: 32 }}>
          <Spin />
        </div>
      ) : !u || !u.calls ? (
        <Empty description="该时间范围内没有用量记录" />
      ) : (
        <>
          <Row gutter={[8, 8]}>
            <Col span={8}>
              <Card size="small">
                <Statistic title="模型输入" value={fmtTokens(u.prompt_tokens)} />
              </Card>
            </Col>
            <Col span={8}>
              <Card size="small">
                <Statistic
                  title="模型输出"
                  value={fmtTokens(u.completion_tokens)}
                />
              </Card>
            </Col>
            <Col span={8}>
              <Card size="small">
                <Statistic
                  title="缓存命中"
                  value={
                    u.cached_reported
                      ? `${(u.cache_hit_rate * 100).toFixed(0)}%`
                      : "未上报"
                  }
                />
              </Card>
            </Col>
            <Col span={8}>
              <Card size="small">
                <Statistic title="LLM 调用" value={u.calls} suffix="次" />
              </Card>
            </Col>
            <Col span={8}>
              <Card size="small">
                <Statistic title="LLM 耗时" value={fmtDuration(u.duration_ms)} />
              </Card>
            </Col>
            <Col span={8}>
              <Card size="small">
                <Statistic
                  title="成本"
                  value={fmtCost(u.cost, u.cost_currency)}
                />
              </Card>
            </Col>
            <Col span={8}>
              <Card size="small">
                <Statistic
                  title="追问率"
                  value={
                    askQ.data
                      ? `${(askQ.data.ask_rate * 100).toFixed(0)}%`
                      : "—"
                  }
                  suffix={
                    askQ.data
                      ? `${askQ.data.ask_rounds}/${askQ.data.rounds} 轮`
                      : undefined
                  }
                />
              </Card>
            </Col>
          </Row>

          {u.errors ? (
            <Typography.Text type="danger" style={{ fontSize: 12 }}>
              其中失败 {u.errors} 次（错误率{" "}
              {((u.error_rate ?? 0) * 100).toFixed(0)}%）
            </Typography.Text>
          ) : null}
        </>
      )}

      <div style={{ marginTop: 16 }}>
        <Typography.Text strong>
          趋势（{granularity === "hour" ? "按小时" : "按天"}）
        </Typography.Text>
        <div style={{ marginTop: 6 }}>
          {trendQ.isLoading ? (
            <Spin size="small" />
          ) : trendQ.data ? (
            <UsageTrendChart data={trendQ.data} currency={u?.cost_currency} />
          ) : null}
        </div>
      </div>

      <div style={{ marginTop: 16 }}>
        <Typography.Text strong>工具统计（全量）</Typography.Text>
        <div style={{ marginTop: 6 }}>
          {toolsQ.isLoading ? (
            <Spin size="small" />
          ) : toolsQ.data && toolsQ.data.length > 0 ? (
            <ToolStatsTable stats={toolsQ.data} />
          ) : (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              暂无工具调用记录
            </Typography.Text>
          )}
        </div>
      </div>

      <div style={{ marginTop: 16 }}>
        <Typography.Text strong>重复调用（空转）</Typography.Text>
        <div style={{ marginTop: 6 }}>
          {dupQ.isLoading ? (
            <Spin size="small" />
          ) : dupQ.data ? (
            <>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {dupQ.data.duplicate_count}/{dupQ.data.total} 次（
                {(dupQ.data.duplicate_rate * 100).toFixed(1)}%）是「同工具 +
                同参数」的重复调用
              </Typography.Text>
              {dupQ.data.groups.length > 0 ? (
                <Table
                  style={{ marginTop: 6 }}
                  size="small"
                  rowKey={(r) => `${r.tool}-${r.args_hash}-${r.message_id}`}
                  pagination={false}
                  dataSource={dupQ.data.groups}
                  columns={[
                    {
                      title: "工具",
                      dataIndex: "tool",
                      render: (v: string) => <Tag>{v}</Tag>,
                    },
                    { title: "同参数次数", dataIndex: "count", width: 100 },
                    {
                      title: "参数大小",
                      dataIndex: "args_size",
                      width: 100,
                      render: (v: number) => fmtTokens(v),
                    },
                    {
                      title: "所在轮",
                      dataIndex: "message_id",
                      render: (v: string | null) =>
                        v ? (
                          <span style={{ fontSize: 12, color: "#8c8c8c" }}>
                            {v.slice(-8)}
                          </span>
                        ) : (
                          "—"
                        ),
                    },
                  ]}
                />
              ) : (
                <div style={{ fontSize: 12, color: "#52c41a", marginTop: 4 }}>
                  没有发现空转
                </div>
              )}
              {/* read 单独一档：它是设计内的追回动作，不算空转；但同一块反复
                  展开说明「取回 → 被压 → 又取回」在抖动，必须看得见 */}
              {dupQ.data.read && dupQ.data.read.calls > 0 ? (
                <div style={{ marginTop: 8 }}>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    追回（read）：共 {dupQ.data.read.calls} 次，其中{" "}
                    {dupQ.data.read.repeats} 次是重复展开同一块（
                    {(dupQ.data.read.repeat_rate * 100).toFixed(1)}%）
                    {dupQ.data.read.repeats > 0
                      ? " —— 偏高说明上下文在抖动，可调大「追回内容保活条数」"
                      : ""}
                  </Typography.Text>
                </div>
              ) : null}
            </>
          ) : null}
        </div>
      </div>

      <div style={{ marginTop: 16 }}>
        <Typography.Text strong>能力加载（近 {capDays} 天）</Typography.Text>
        <div style={{ marginTop: 6 }}>
          {capQ.isLoading ? (
            <Spin size="small" />
          ) : capQ.data && capQ.data.total > 0 ? (
            <>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                共 {capQ.data.total} 次把正文 / 工具定义取进上下文：系统预加载{" "}
                {capQ.data.by_source.preload ?? 0} · 模型主动加载{" "}
                {capQ.data.by_source.model_load ?? 0} · 调用时自动补定义{" "}
                {capQ.data.by_source.auto_disclose ?? 0}
              </Typography.Text>
              <div style={{ fontSize: 12, color: "#8c8c8c", marginTop: 2 }}>
                预加载占比高 → L1 摘要写得不准；自动补定义占比高 → 工具折叠太狠或组摘要没写
              </div>
              {capQ.data.redundant_loads > 0 ? (
                <div style={{ fontSize: 12, color: "#faad14", marginTop: 4 }}>
                  抖动 {capQ.data.redundant_loads} 次：同一轮把同一个能力取了两遍以上，
                  说明模型不确定自己读过没有
                </div>
              ) : null}
              {capQ.data.by_key.length > 0 ? (
                <Table
                  style={{ marginTop: 6 }}
                  size="small"
                  rowKey="key"
                  pagination={false}
                  dataSource={capQ.data.by_key}
                  columns={[
                    {
                      title: "能力",
                      dataIndex: "key",
                      render: (v: string) => <Tag>{v}</Tag>,
                    },
                    {
                      title: "类型",
                      dataIndex: "kind",
                      width: 90,
                      render: (v: string) =>
                        v === "skill" ? "技能" : v === "tool" ? "工具" : "工具组",
                    },
                    { title: "次数", dataIndex: "loads", width: 70 },
                    {
                      title: "预加载",
                      dataIndex: "preload",
                      width: 70,
                      render: (v: number) => (v > 0 ? v : "—"),
                    },
                    {
                      title: "模型主动",
                      dataIndex: "model_load",
                      width: 80,
                      render: (v: number) => (v > 0 ? v : "—"),
                    },
                    {
                      title: "自动补载",
                      dataIndex: "auto_disclose",
                      width: 80,
                      render: (v: number) => (v > 0 ? v : "—"),
                    },
                    {
                      title: "平均体积",
                      dataIndex: "avg_chars",
                      width: 90,
                      render: (v: number) => fmtTokens(v),
                    },
                  ]}
                />
              ) : null}
            </>
          ) : (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              暂无能力加载记录
            </Typography.Text>
          )}
        </div>
      </div>

      <div style={{ marginTop: 16 }}>
        <Typography.Text strong>最慢的调用（Top 10）</Typography.Text>
        <div style={{ marginTop: 6 }}>
          {slowQ.isLoading ? (
            <Spin size="small" />
          ) : slowQ.data && slowQ.data.items.length > 0 ? (
            <Table
              size="small"
              rowKey="id"
              pagination={false}
              dataSource={slowQ.data.items}
              columns={[
                {
                  title: "时间",
                  dataIndex: "created_at",
                  render: (v: string | null) =>
                    v ? dayjs(v).format("MM-DD HH:mm") : "—",
                },
                {
                  title: "耗时",
                  dataIndex: "duration_ms",
                  render: (v: number | null) => fmtDuration(v),
                },
                {
                  title: "首字",
                  dataIndex: "ttft_ms",
                  render: (v: number | null) => fmtDuration(v),
                },
                {
                  title: "模型输入/输出",
                  key: "tok",
                  render: (_: unknown, r: SlowCall) =>
                    `${fmtTokens(r.prompt_tokens)} / ${fmtTokens(
                      r.completion_tokens
                    )}`,
                },
                {
                  title: "类型",
                  dataIndex: "kind",
                  render: (v: string) => <Tag>{v}</Tag>,
                },
                {
                  title: "流式",
                  dataIndex: "is_stream",
                  render: (v: boolean) => (v ? "是" : "否"),
                },
              ]}
            />
          ) : (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              暂无数据
            </Typography.Text>
          )}
        </div>
      </div>

      <div style={{ marginTop: 16 }}>
        <Typography.Text strong>错误</Typography.Text>
        <div style={{ marginTop: 6 }}>
          {errQ.isLoading ? (
            <Spin size="small" />
          ) : errQ.data ? (
            <>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                LLM 失败 {errQ.data.llm.failed}/{errQ.data.llm.total}（
                {(errQ.data.llm.error_rate * 100).toFixed(1)}%） · 工具失败{" "}
                {errQ.data.tool.failed}/{errQ.data.tool.total}（
                {(errQ.data.tool.error_rate * 100).toFixed(1)}%）
              </Typography.Text>
              {errGroups(errQ.data).length > 0 ? (
                <Table
                  style={{ marginTop: 6 }}
                  size="small"
                  rowKey="key"
                  pagination={false}
                  dataSource={errGroups(errQ.data)}
                  columns={[
                    { title: "来源", dataIndex: "src", width: 60 },
                    { title: "次数", dataIndex: "count", width: 60 },
                    {
                      title: "最近",
                      dataIndex: "last_at",
                      width: 110,
                      render: (v: string | null) =>
                        v ? dayjs(v).format("MM-DD HH:mm") : "—",
                    },
                    {
                      title: "错误",
                      dataIndex: "key",
                      render: (v: string) => (
                        <span style={{ fontSize: 12, wordBreak: "break-word" }}>
                          {v}
                        </span>
                      ),
                    },
                  ]}
                />
              ) : (
                <div style={{ fontSize: 12, color: "#52c41a", marginTop: 4 }}>
                  没有失败记录
                </div>
              )}
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
