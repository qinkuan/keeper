import {
  Alert,
  Button,
  Table,
  TableColumnsType,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  apiListPlatformAgents,
  loadAgent,
  platformAddAgent,
  platformListMarket,
} from "../api/client";
import type { MarketAgent } from "../api/types";

const { Paragraph, Text } = Typography;

/**
 * 智能体市场 —— **平台上有什么、我还没拿过来的**。
 *
 * 和「智能体」页的分工：那边是库存（本地创建的 + 已装载的），这边是目录。
 * 按 origin 天然分界，所以同一个 agent 不会在两页各出现一次。
 *
 * 这里**不显示启用/停用**——那是库存页的事。这边只有两个状态：已装载 /
 * 可装载。装载 = 从远程把配置拿到本地，之后去库存页管它。
 */
export default function AgentMarketPage() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["platform-market"], queryFn: platformListMarket });
  // 本地装载状态。用 platform（含已卸载的）而不是 all——后者是「库存」，
  // 卸载过的会被过滤掉，那就分不清「没装过」和「装过又卸了」。
  const loadedQ = useQuery({
    queryKey: ["platformAgents"],
    queryFn: apiListPlatformAgents,
  });
  const loadedIds = new Set(
    (loadedQ.data?.agents ?? []).filter((a) => a.loaded).map((a) => a.id),
  );

  // 注意：``isPending`` 是这一整个 mutation 的**共享**布尔值，表格里每一行都在读
  // 它——只写 ``loading={loadM.isPending}`` 会导致点一行、所有行的按钮一起转圈。
  // 必须再比对 ``variables``（本次 mutate 的入参）才能只让当前行动起来。
  const loadM = useMutation({
    mutationFn: async (a: MarketAgent) => {
      // 未添加就先加进「我已添加」，否则平台查不到它，装载会 404
      if (!a.added) await platformAddAgent(a.id);
      return loadAgent(a.id);
    },
    onSuccess: (d: any) => {
      message.success(
        d?.running === false
          ? `已装载，但${d?.reason ?? "未运行"}`
          : `已装载：${d?.name ?? ""}`,
      );
      qc.invalidateQueries({ queryKey: ["allAgents"] });
      qc.invalidateQueries({ queryKey: ["platformAgents"] });
      qc.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: (e: Error) => message.error("装载失败：" + e.message),
  });

  const cols: TableColumnsType<MarketAgent> = [
    { title: "名称", dataIndex: "name" },
    {
      title: "说明",
      dataIndex: "description",
      ellipsis: true,
      render: (d?: string) => d || "—",
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 90,
      render: (s: string) =>
        s === "active" ? <Tag color="green">启用</Tag> : <Tag>停用</Tag>,
    },
    {
      title: "本地",
      width: 110,
      render: (_: unknown, a: MarketAgent) =>
        loadedIds.has(a.id) ? <Tag color="blue">已装载</Tag> : <Tag>未装载</Tag>,
    },
    {
      title: "操作",
      width: 130,
      render: (_: unknown, a: MarketAgent) => (
        <Tooltip
          title={
            loadedIds.has(a.id)
              ? "重新从平台拉一遍配置，并重建插件链接"
              : "把配置从平台拿到本地，并按平台下发的插件名单建立链接"
          }
        >
          <Button
            size="small"
            type="primary"
            loading={loadM.isPending && loadM.variables?.id === a.id}
            onClick={() => loadM.mutate(a)}
          >
            {loadedIds.has(a.id) ? "重新装载" : "装载"}
          </Button>
        </Tooltip>
      ),
    },
  ];

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
          智能体市场
        </Typography.Title>
        <Button size="small" onClick={() => q.refetch()}>
          刷新
        </Button>
      </div>

      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="装载 = 把配置从平台拿到本地"
        description={
          <Paragraph style={{ marginBottom: 0 }}>
            装载完它会出现在<strong>智能体</strong>页——那边的启用开关、插件绑定都在那边管。
            本页不显示启用状态：装载与否是两件事。
          </Paragraph>
        }
      />

      {q.isError && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
          message="读不到平台配置"
          description={
            <>
              请先启动平台服务（默认 :9095），或用环境变量指定：
              <Text code>VITE_PLATFORM_URL=http://host:port</Text>
              <br />
              错误：{(q.error as Error)?.message}
            </>
          }
        />
      )}

      <Table
        rowKey="id"
        size="small"
        loading={q.isLoading}
        dataSource={q.data ?? []}
        columns={cols}
        locale={{ emptyText: "平台上没有你可见的 agent" }}
      />
    </>
  );
}
