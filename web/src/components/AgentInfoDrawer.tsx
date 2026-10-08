import { useState } from "react";
import {
  Drawer,
  Descriptions,
  Tag,
  Empty,
  Spin,
  Typography,
  Input,
  Button,
  Space,
  Popconfirm,
  message,
  Alert,
  Select,
  Segmented,
  Checkbox,
  Tabs,
  Switch,
  List,
} from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import AgentUsagePanel from "./AgentUsagePanel";
import {
  apiConfig,
  apiSkills,
  apiPeers,
  apiAddPeer,
  apiDeletePeer,
  apiPatchPeer,
  apiEffectiveTools,
  apiListAgents,
  apiCapabilities,
  apiToggleCapability,
  apiListModels,
  apiGetAgentModel,
  apiSetAgentModel,
} from "../api/client";
import type { CapabilityItem } from "../api/types";

/** 传给后端的能力类型：内置工具 / 插件（插件统一承载 MCP、工具、技能） */
type CapKind = "builtin" | "plugin";

/** 能力模块的 tab key → 后端 kind */
const CAP_TABS: { key: string; label: string; kind: CapKind }[] = [
  { key: "builtin", label: "内置工具", kind: "builtin" },
  { key: "plugin", label: "插件", kind: "plugin" },
];

const STATE_COLOR: Record<string, string> = {
  已装配: "green",
  已关闭: "default",
  只读工作区跳过: "orange",
  未绑定: "default",
  依赖缺失: "red",
  已停用: "default",
};

/**
 * Agent 配置面板。
 *
 * 分区：概览 / 内置工具 / 插件 / A2A 对端 / 生效工具。
 * 能力开关写本地 override（覆盖平台 binding），切换后服务端**重建实例**生效。
 * 内置工具归 keeper 本地（平台不管理），默认启用；插件由平台管理，统一承载
 * MCP server、可执行工具（bin）与技能（skill），可按需开关。
 */
export default function AgentInfoDrawer({
  open,
  onClose,
  agentId,
}: {
  open: boolean;
  onClose: () => void;
  agentId: string;
}) {
  const qc = useQueryClient();
  const [peerName, setPeerName] = useState("");
  const [peerUrl, setPeerUrl] = useState("");
  const [peerSource, setPeerSource] = useState<"local" | "external">("local");
  const [pickedAgentId, setPickedAgentId] = useState<string | null>(null);
  const [peerTrustEnv, setPeerTrustEnv] = useState(true);
  // MCP server 展开状态：记录哪些 server 被点击展开以查看其下工具
  const [expandedMcp, setExpandedMcp] = useState<Record<string, boolean>>({});

  const configQ = useQuery({
    queryKey: ["config", agentId],
    queryFn: () => apiConfig(agentId),
    enabled: open,
  });
  const skillsQ = useQuery({
    queryKey: ["skills", agentId],
    queryFn: () => apiSkills(agentId),
    enabled: open,
  });
  const peersQ = useQuery({
    queryKey: ["peers", agentId],
    queryFn: () => apiPeers(agentId),
    enabled: open,
  });
  const toolsQ = useQuery({
    queryKey: ["effective-tools", agentId],
    queryFn: () => apiEffectiveTools(agentId),
    enabled: open,
  });
  const capsQ = useQuery({
    queryKey: ["capabilities", agentId],
    queryFn: () => apiCapabilities(agentId),
    enabled: open,
  });
  const agentsQ = useQuery({
    queryKey: ["agents"],
    queryFn: apiListAgents,
    enabled: open,
  });
  const modelsQ = useQuery({
    queryKey: ["models"],
    queryFn: apiListModels,
    enabled: open,
  });
  const agentModelQ = useQuery({
    queryKey: ["agent-model", agentId],
    queryFn: () => apiGetAgentModel(agentId),
    enabled: open,
  });
  const setModelM = useMutation({
    mutationFn: (llmProfileId: string | null) =>
      apiSetAgentModel(agentId, llmProfileId),
    onSuccess: () => {
      message.success("模型绑定已更新");
      qc.invalidateQueries({ queryKey: ["agent-model", agentId] });
      qc.invalidateQueries({ queryKey: ["config", agentId] });
    },
    onError: (e: Error) => message.error("绑定失败：" + e.message),
  });

  const localAgents = (agentsQ.data?.agents ?? []).filter((a) => a.id !== agentId);

  const refreshAfterCapChange = () => {
    qc.invalidateQueries({ queryKey: ["capabilities", agentId] });
    qc.invalidateQueries({ queryKey: ["effective-tools", agentId] });
    qc.invalidateQueries({ queryKey: ["skills", agentId] });
  };

  const toggleCapM = useMutation({
    mutationFn: (v: { kind: CapKind; name: string; enabled: boolean }) =>
      apiToggleCapability(agentId, v),
    onSuccess: (_d, v) => {
      message.success(`${v.name} 已${v.enabled ? "启用" : "关闭"}（已重建实例）`);
      refreshAfterCapChange();
    },
    onError: (e: Error) => message.error("切换失败：" + e.message),
  });

  const refreshPeers = () => {
    qc.invalidateQueries({ queryKey: ["peers", agentId] });
    qc.invalidateQueries({ queryKey: ["effective-tools", agentId] });
  };

  const handlePickAgent = (id: string) => {
    setPickedAgentId(id);
    const a = localAgents.find((x) => x.id === id);
    if (!a) return;
    setPeerName(a.name);
    setPeerUrl(a.a2a_url ?? "");
  };

  const handleSourceChange = (v: "local" | "external") => {
    setPeerSource(v);
    setPickedAgentId(null);
    setPeerName("");
    setPeerUrl("");
    setPeerTrustEnv(true);
  };

  const addM = useMutation({
    mutationFn: () =>
      apiAddPeer(agentId, {
        name: peerName.trim(),
        a2a_url: peerUrl.trim(),
        source: peerSource,
        trust_env: peerSource === "external" ? peerTrustEnv : false,
      }),
    onSuccess: () => {
      message.success(`已添加对端：${peerName}`);
      setPeerName("");
      setPeerUrl("");
      setPickedAgentId(null);
      refreshPeers();
    },
    onError: (e: Error) => message.error("添加失败：" + e.message),
  });

  const patchM = useMutation({
    mutationFn: (v: { name: string; trustEnv: boolean }) =>
      apiPatchPeer(agentId, v.name, v.trustEnv),
    onSuccess: (_d, v) => {
      message.success(v.trustEnv ? "已改为走代理" : "已改为直连");
      refreshPeers();
    },
    onError: (e: Error) => message.error("修改失败：" + e.message),
  });

  const delM = useMutation({
    mutationFn: (name: string) => apiDeletePeer(agentId, name),
    onSuccess: () => {
      message.success("已移除对端");
      refreshPeers();
    },
    onError: (e: Error) => message.error("移除失败：" + e.message),
  });

  const cfg = configQ.data;
  const llm = cfg?.llm;
  const peers = peersQ.data?.peers ?? [];
  const effectiveTools = toolsQ.data?.tools ?? [];
  const busy = addM.isPending || delM.isPending || patchM.isPending;
  const canAdd = peerName.trim() !== "" && peerUrl.trim() !== "";

  // 能力列表：内置工具 / 插件 共用。
  // 插件项若带 tools（其下已连上的 MCP 工具）可点击展开查看。
  const CapList = ({ items, kind }: { items: CapabilityItem[]; kind: CapKind }) => {
    if (capsQ.isLoading) return <Spin />;
    if (capsQ.isError)
      return (
        <Typography.Text type="danger">
          读取失败：{(capsQ.error as Error)?.message}
        </Typography.Text>
      );
    if (!items.length)
      return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无" />;
    return (
      <List
        size="small"
        dataSource={items}
        renderItem={(it) => {
          const tools = it.tools ?? [];
          const hasTools = tools.length > 0;
          const open = !!expandedMcp[it.name];
          return (
            <List.Item
              style={{
                cursor: hasTools ? "pointer" : "default",
                flexDirection: "column",
                alignItems: "stretch",
              }}
              onClick={() =>
                hasTools && setExpandedMcp((s) => ({ ...s, [it.name]: !open }))
              }
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                }}
              >
                <Space size={6}>
                  <span style={{ fontWeight: 500 }}>{it.name}</span>
                  <Tag color={STATE_COLOR[it.state] ?? "default"}>{it.state}</Tag>
                  {it.mutating && <Tag color="red">写操作</Tag>}
                  {hasTools && (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {open ? "收起" : "展开"} {tools.length} 个工具
                    </Typography.Text>
                  )}
                </Space>
                <Switch
                  size="small"
                  checked={it.enabled}
                  loading={toggleCapM.isPending}
                  disabled={toggleCapM.isPending}
                  onClick={(_checked, e) => e.stopPropagation()}
                  onChange={(checked) =>
                    toggleCapM.mutate({ kind, name: it.name, enabled: checked })
                  }
                />
              </div>
              {it.description && (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {it.description}
                </Typography.Text>
              )}
              {open && hasTools && (
                <List
                  size="small"
                  style={{ marginTop: 8, background: "var(--kp-surface)", borderRadius: 6 }}
                  dataSource={tools}
                  renderItem={(tl) => (
                    <List.Item key={tl.name} style={{ padding: "4px 8px" }}>
                      <List.Item.Meta
                        title={
                          <span style={{ fontFamily: "monospace", fontSize: 13 }}>
                            {tl.name}
                          </span>
                        }
                        description={tl.description || "—"}
                      />
                    </List.Item>
                  )}
                />
              )}
            </List.Item>
          );
        }}
      />
    );
  };

  const caps = capsQ.data;

  const tabItems = [
    {
      key: "overview",
      label: "概览",
      children: (
        <>
          {configQ.isLoading && <Spin />}
          {configQ.isError && (
            <Typography.Text type="danger">
              配置读取失败：{(configQ.error as Error)?.message}
            </Typography.Text>
          )}
          {!cfg && !configQ.isLoading && (
            <Typography.Text type="secondary">暂无配置</Typography.Text>
          )}
          {cfg && (
            <>
              <Descriptions column={1} size="small" bordered title="基本信息">
                <Descriptions.Item label="名称">{cfg.name}</Descriptions.Item>
                <Descriptions.Item label="工作区">
                  {cfg.workspace || "未绑定"}
                </Descriptions.Item>
                <Descriptions.Item label="MCP 连接">
                  {cfg.mcp_connected ? (
                    <Tag color="green">已连接</Tag>
                  ) : (
                    <Tag color="red">未连接</Tag>
                  )}
                </Descriptions.Item>
                <Descriptions.Item label="已注册 MCP server">
                  {cfg.mcp_servers?.length
                    ? cfg.mcp_servers.map((s: string) => (
                        <Tag key={s} color="blue">
                          {s}
                        </Tag>
                      ))
                    : "无"}
                </Descriptions.Item>
              </Descriptions>

              <div style={{ marginTop: 20 }}>
                <Typography.Text strong>大模型：</Typography.Text>{" "}
                {llm?.provider ? (
                  <Space size={6}>
                    <Tag>{llm.provider}</Tag>
                    <Tag>{llm.model_name || "-"}</Tag>
                    <Tag color={llm.has_api_key ? "green" : "default"}>
                      {llm.has_api_key ? "已配置 Key" : "未提供 Key"}
                    </Tag>
                  </Space>
                ) : (
                  <Typography.Text type="secondary">未配置（纯索引模式）</Typography.Text>
                )}
              </div>

              <div style={{ marginTop: 12 }}>
                <Typography.Text strong>绑定模型预设：</Typography.Text>{" "}
                <Select
                  style={{ minWidth: 260 }}
                  placeholder="默认（全局配置）"
                  allowClear
                  loading={agentModelQ.isLoading}
                  value={agentModelQ.data?.llm_profile_id ?? undefined}
                  onChange={(v) => setModelM.mutate(v ?? null)}
                  options={(modelsQ.data ?? []).map((m) => ({
                    label: `${m.name}（${m.provider}/${m.model_name}）`,
                    value: m.id,
                  }))}
                />
                <Typography.Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                  留空则用全局默认（config.yaml 的 llm 段）
                </Typography.Text>
              </div>

              <div style={{ marginTop: 12 }}>
                <Typography.Text strong>技能：</Typography.Text>{" "}
                {skillsQ.data?.skills?.length
                  ? skillsQ.data.skills.map((s: string) => (
                      <Tag key={s} color="purple">
                        {s}
                      </Tag>
                    ))
                  : "无"}
              </div>
            </>
          )}
        </>
      ),
    },
    ...CAP_TABS.map((t) => ({
      key: t.key,
      label: t.label,
      children: (
        <>
          {t.key === "builtin" && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message="内置工具归 keeper 本地"
              description="代码在 keeper 客户端里，平台不管理也不需要下载；默认启用，可按需关闭（写类工具在只读工作区下会自动跳过）。"
            />
          )}
          {t.key === "plugin" && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message="插件由平台管理"
              description="一个插件统一承载 MCP server、可执行工具（bin）与技能（skill）；可在本页按需开关，点开插件可查看其下已连上的 MCP 工具。"
            />
          )}
          <CapList items={(caps?.[t.key] ?? []) as CapabilityItem[]} kind={t.kind} />
        </>
      ),
    })),
    {
      key: "peers",
      label: "A2A 对端",
      children: (
        <>
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="运行时可增删，立即生效并持久化"
            description="本进程内 agent 走下拉选择（直连）；外部 agent 手填地址，默认走系统代理。"
          />
          <Space direction="vertical" style={{ width: "100%" }} size="small">
            {peers.length === 0 && (
              <Typography.Text type="secondary">暂无对端</Typography.Text>
            )}
            {peers.map((p) => (
              <div key={p.name} style={{ display: "flex", alignItems: "center" }}>
                <Tag color="geekblue" style={{ marginInlineEnd: 8 }}>
                  {p.name}
                </Tag>
                <Tag
                  color={p.source === "local" ? "cyan" : "orange"}
                  style={{ marginInlineEnd: 8 }}
                >
                  {p.source === "local" ? "本进程" : "外部"}
                </Tag>
                {p.source === "external" && (
                  <Tag
                    color={p.trust_env ? "blue" : "default"}
                    style={{ marginInlineEnd: 8, cursor: "pointer" }}
                    title="点击切换：走系统代理 / 直连"
                    onClick={() =>
                      patchM.mutate({ name: p.name, trustEnv: !p.trust_env })
                    }
                  >
                    {p.trust_env ? "走代理" : "直连"}
                  </Tag>
                )}
                <Typography.Text
                  type="secondary"
                  style={{ flex: 1, fontSize: 12 }}
                >
                  {p.a2a_url}
                </Typography.Text>
                <Popconfirm
                  title={`移除对端 ${p.name}？`}
                  onConfirm={() => delM.mutate(p.name)}
                  okText="移除"
                  cancelText="取消"
                >
                  <Button size="small" danger loading={busy}>
                    移除
                  </Button>
                </Popconfirm>
              </div>
            ))}
            <Segmented
              value={peerSource}
              onChange={handleSourceChange}
              style={{ marginTop: 8 }}
              options={[
                { label: "本进程", value: "local" },
                { label: "外部", value: "external" },
              ]}
            />
            {peerSource === "local" ? (
              <Space.Compact style={{ width: "100%", marginTop: 8 }}>
                <Input
                  style={{ width: "30%" }}
                  placeholder="对端名"
                  value={peerName}
                  onChange={(e) => setPeerName(e.target.value)}
                />
                <Select
                  style={{ flex: 1 }}
                  placeholder={
                    localAgents.length
                      ? "选择本进程已装载的 agent"
                      : "本进程暂无其他 agent"
                  }
                  value={pickedAgentId ?? undefined}
                  onChange={handlePickAgent}
                  loading={agentsQ.isLoading}
                  options={localAgents.map((a) => ({
                    value: a.id,
                    label: `${a.name}  (${a.id.slice(0, 8)}…)`,
                  }))}
                />
                <Button
                  type="primary"
                  disabled={!canAdd}
                  loading={busy}
                  onClick={() => addM.mutate()}
                >
                  添加
                </Button>
              </Space.Compact>
            ) : (
              <div style={{ marginTop: 8 }}>
                <Space.Compact style={{ width: "100%" }}>
                  <Input
                    style={{ width: "30%" }}
                    placeholder="对端名"
                    value={peerName}
                    onChange={(e) => setPeerName(e.target.value)}
                  />
                  <Input
                    placeholder="http://host:8080/agents/{id}/a2a"
                    value={peerUrl}
                    onChange={(e) => setPeerUrl(e.target.value)}
                  />
                  <Button
                    type="primary"
                    disabled={!canAdd}
                    loading={busy}
                    onClick={() => addM.mutate()}
                  >
                    添加
                  </Button>
                </Space.Compact>
                <div style={{ marginTop: 8 }}>
                  <Checkbox
                    checked={peerTrustEnv}
                    onChange={(e) => setPeerTrustEnv(e.target.checked)}
                  >
                    走系统代理
                  </Checkbox>
                  <Typography.Text
                    type="secondary"
                    style={{ fontSize: 12, marginLeft: 8 }}
                  >
                    外部对端通常需要；若对端在内网可直连，取消勾选
                  </Typography.Text>
                </div>
              </div>
            )}
          </Space>
        </>
      ),
    },
    {
      key: "effective",
      label: "生效工具",
      children: (
        <>
          {toolsQ.isLoading && <Spin size="small" />}
          {toolsQ.isError && (
            <Typography.Text type="danger">
              读取失败：{(toolsQ.error as Error)?.message}
            </Typography.Text>
          )}
          {!toolsQ.isLoading && effectiveTools.length === 0 && (
            <Typography.Text type="secondary">无</Typography.Text>
          )}
          <div>
            {effectiveTools.map((t, i) => (
              <Tag key={`${t.name}-${i}`} style={{ marginBottom: 4 }}>
                {t.name}
              </Tag>
            ))}
          </div>
        </>
      ),
    },
    {
      key: "usage",
      label: "用量",
      children: (
        <Alert
          type="info"
          showIcon
          message="用量数据已移到侧边栏「可观测」"
          description="那边同时提供用量趋势、工具统计、重复调用 / 空转、错误聚合、能力加载与解析结构，并且可以按时间范围对照查看。"
        />
      ),
    },
  ];

  return (
    <Drawer title="Agent 配置" width={720} open={open} onClose={onClose}>
      <Tabs items={tabItems} />
    </Drawer>
  );
}
