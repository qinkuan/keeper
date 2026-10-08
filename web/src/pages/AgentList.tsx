import { useState } from "react";
import {
  Alert,
  Button,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  TableColumnsType,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  apiDeleteLocalAgent,
  apiListAgents,
  apiListAllAgents,
  apiListPlugins,
  apiSetAgentEnabled,
  apiSetLocalAgentPlugins,
  unloadAgent,
} from "../api/client";
import type { LocalAgent } from "../api/types";

const { Paragraph, Text } = Typography;

/**
 * 智能体 —— **我已有的**（本地创建的 + 从平台装载的）。
 *
 * 平台上「有什么、还没拿过来的」在**智能体市场**那一页；这里只管库存。
 * 两页按 origin 天然分界，所以同一个 agent 不会显示两遍。
 *
 * 每行一个**启用开关**（Switch），它是决定 agent 跑不跑、进不进概览的唯一开关，
 * 缺省是开的。停用不会删任何东西，也不会被下一次平台同步撤销——写的是
 * override 表，不是 status。
 *
 * 只有本地创建的（origin=local）能删：平台 agent 的配置归平台，删了还得重装。
 */
export default function AgentList({ onCreate }: { onCreate?: () => void }) {
  const qc = useQueryClient();
  const [pluginEdit, setPluginEdit] = useState<LocalAgent | null>(null);
  const [pluginSel, setPluginSel] = useState<string[]>([]);

  const listQ = useQuery({ queryKey: ["allAgents"], queryFn: apiListAllAgents });
  const libQ = useQuery({ queryKey: ["plugins"], queryFn: apiListPlugins });
  // 本进程实际跑着哪些（/agents 是运行时视角）
  const runningQ = useQuery({ queryKey: ["agents"], queryFn: apiListAgents });
  const runningIds = new Set((runningQ.data?.agents ?? []).map((a) => a.id));

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["allAgents"] });
    qc.invalidateQueries({ queryKey: ["agents"] });
    qc.invalidateQueries({ queryKey: ["health"] });
  };

  const enableM = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      apiSetAgentEnabled(id, enabled),
    onSuccess: (d, v) => {
      message.success(v.enabled ? (d.running ? "已启用，正在运行" : "已启用") : "已停用");
      refresh();
    },
    onError: (e: Error) => message.error("操作失败：" + e.message),
  });

  const unloadM = useMutation({
    mutationFn: (id: string) => unloadAgent(id),
    onSuccess: () => {
      message.success("已卸载（配置缓存保留，重新装载即可再跑）");
      refresh();
    },
    onError: (e: Error) => message.error("卸载失败：" + e.message),
  });

  const setPluginsM = useMutation({
    mutationFn: ({ id, plugins }: { id: string; plugins: string[] }) =>
      apiSetLocalAgentPlugins(id, plugins),
    onSuccess: (d: any) => {
      message.success(d?.restarted ? "已更新插件绑定，实例已重建" : "已更新插件绑定");
      refresh();
    },
    onError: (e: Error) => message.error("更新失败：" + e.message),
  });

  const deleteM = useMutation({
    mutationFn: (id: string) => apiDeleteLocalAgent(id),
    onSuccess: () => {
      message.success("已删除");
      refresh();
    },
    onError: (e: Error) => message.error("删除失败：" + e.message),
  });

  const cols: TableColumnsType<LocalAgent> = [
    {
      title: "名称",
      dataIndex: "name",
      render: (n: string, r) => (
        <Space size={6}>
          <Text strong>{n}</Text>
          <Tag color={r.origin === "local" ? "green" : "blue"}>
            {r.origin === "local" ? "本地创建" : "平台装载"}
          </Tag>
        </Space>
      ),
    },
    {
      title: "说明",
      dataIndex: "description",
      ellipsis: true,
      render: (d?: string | null) => d || "—",
    },
    {
      title: "插件",
      dataIndex: "plugins",
      width: 220,
      render: (ps: string[], r: LocalAgent) => {
        if (!ps?.length) return <Text type="secondary">未绑定</Text>;
        const missing = new Set(r.missing_plugins ?? []);
        return (
          <Space size={4} wrap>
            {ps.map((p) =>
              missing.has(p) ? (
                <Tooltip key={p} title="插件库里已经没有这个目录，装载时会跳过">
                  <Tag color="error">{p} · 缺失</Tag>
                </Tooltip>
              ) : (
                <Tag key={p}>{p}</Tag>
              ),
            )}
          </Space>
        );
      },
    },
    {
      title: "启用",
      dataIndex: "enabled",
      width: 130,
      render: (v: boolean, r: LocalAgent) => (
        <Space size={6}>
          <Switch
            size="small"
            checked={v}
            loading={enableM.isPending && enableM.variables?.id === r.id}
            onChange={(on) => enableM.mutate({ id: r.id, enabled: on })}
          />
          {runningIds.has(r.id) ? (
            <Tag color="green">运行中</Tag>
          ) : v ? (
            <Tag>已启用</Tag>
          ) : (
            <Tag>已停用</Tag>
          )}
        </Space>
      ),
    },
    {
      title: "操作",
      key: "ops",
      width: 200,
      render: (_: unknown, r: LocalAgent) => (
        <Space size={4} wrap>
          {r.origin === "platform" ? (
            // 只有「卸载」。卸载后它就从本页消失（本地配置一并清掉），装回去去
            // 「智能体市场」——那边按本地装载状态显示「装载」/「重新装载」。
            <Button
              size="small"
              loading={unloadM.isPending && unloadM.variables === r.id}
              onClick={() => unloadM.mutate(r.id)}
            >
              卸载
            </Button>
          ) : (
            <Button
              size="small"
              onClick={() => {
                setPluginEdit(r);
                setPluginSel(r.plugins ?? []);
              }}
            >
              管理插件
            </Button>
          )}
          {r.origin === "local" && (
            <Button
              size="small"
              danger
              loading={deleteM.isPending && deleteM.variables === r.id}
              onClick={() => {
                if (window.confirm(`删除「${r.name}」？插件链接会被清掉，工作区文件不动。`)) {
                  deleteM.mutate(r.id);
                }
              }}
            >
              删除
            </Button>
          )}
        </Space>
      ),
    },
  ];

  const agents = listQ.data?.agents ?? [];

  return (
    <>
      <div
        style={{
          marginBottom: 16,
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
        }}
      >
        <Typography.Title level={4} style={{ margin: 0 }}>
          智能体
        </Typography.Title>
        <Space>
          <Button size="small" onClick={() => listQ.refetch()}>
            刷新
          </Button>
          <Button size="small" type="primary" onClick={onCreate}>
            创建 Agent
          </Button>
        </Space>
      </div>

      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="这里是我已有的 agent：本地创建的 + 从平台装载的"
        description={
          <Paragraph style={{ marginBottom: 0 }}>
            启用开关决定它跑不跑、进不进概览，缺省是开的；停用不会删东西，也不会被下一次平台同步撤销。
            平台上还没拿过来的 agent 在<strong>智能体市场</strong>那一页。
          </Paragraph>
        }
      />

      <Table
        rowKey="id"
        size="small"
        loading={listQ.isLoading}
        dataSource={agents}
        columns={cols}
        locale={{ emptyText: "还没有 agent——点右上角「创建 Agent」，或去智能体市场装载" }}
      />

      <Modal
        open={!!pluginEdit}
        title={`插件绑定：${pluginEdit?.name ?? ""}`}
        okText="保存"
        cancelText="取消"
        confirmLoading={setPluginsM.isPending}
        onCancel={() => setPluginEdit(null)}
        onOk={() => {
          if (!pluginEdit) return;
          setPluginsM.mutate(
            { id: pluginEdit.id, plugins: pluginSel },
            { onSuccess: () => setPluginEdit(null) },
          );
        }}
      >
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            插件本体在插件库里，这里改的是「勾哪些」。保存后链接立刻建好 / 删掉，agent
            在跑的话会重建实例。
          </Text>
          <Select
            mode="multiple"
            value={pluginSel}
            onChange={setPluginSel}
            loading={libQ.isLoading}
            style={{ width: "100%" }}
            placeholder="选插件（可多选）"
            options={(libQ.data?.plugins ?? [])
              .filter((p) => !p.error)
              .map((p) => ({
                value: p.dirname,
                label: `${p.dirname}${p.version ? ` · ${p.version}` : ""}`,
              }))}
          />
          {(pluginEdit?.missing_plugins ?? []).length ? (
            <Alert
              type="warning"
              showIcon
              message={`库里已经找不到：${pluginEdit!.missing_plugins.join("、")}`}
              description="这些绑定还在，但插件目录没了，装载时会跳过。保存一次即可清掉。"
            />
          ) : null}
        </Space>
      </Modal>
    </>
  );
}
