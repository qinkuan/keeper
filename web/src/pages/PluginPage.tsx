import { useQuery } from "@tanstack/react-query";
import { Alert, Card, Descriptions, Space, Table, Tag, Typography } from "antd";
import { FolderOpenOutlined, ReloadOutlined } from "@ant-design/icons";
import { apiListPlugins } from "../api/client";
import type { LibraryPlugin } from "../api/types";

const { Text } = Typography;

/** 能力类型 → 颜色/标签。分不清的话宁可都显示成同一个中性色。 */
function kindTag(k: string) {
  if (k === "bin") return <Tag color="blue">bin</Tag>;
  if (k === "mcp") return <Tag color="purple">mcp</Tag>;
  if (k === "skill") return <Tag>skill</Tag>;
  return <Tag key={k}>{k}</Tag>;
}

function fmtSize(n: number): string {
  if (!n) return "—";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)} ${u[i]}`;
}

/** 环境要求：把「要求」和「本机实况」并排写出来，缺什么一眼能看到。 */
function EnvCell({ items }: { items: LibraryPlugin["envDependencies"] }) {
  if (!items.length) return <Text type="secondary">—</Text>;
  return (
    <Space direction="vertical" size={0}>
      {items.map((d, i) => (
        <Text key={i} style={{ fontSize: 12 }}>
          <Text code>{d.kind}</Text>{" "}
          {d.minVersion ? `≥ ${d.minVersion}` : "不限"}
          {d.maxVersion ? ` ≤ ${d.maxVersion}` : ""}
        </Text>
      ))}
    </Space>
  );
}

/**
 * 插件库（只读）。
 *
 * **这里没有任何"安装 / 卸载"按钮，这是设计如此。** 插件库是一个目录：
 * 手写一个插件包丢进去、或用 plugin-authoring 写完放进去，都只是"往这个
 * 目录里放东西"。给安装按钮等于凭空加一套包格式、版本和冲突处理，而它带来
 * 的能力（放文件）你本来就有，而且更可控。
 *
 * 所以这一页的职责只有两个：让人看见库里有什么（名字、版本、能力类型、
 * 环境要求），以及告诉人**东西该往哪放**（库根路径）。
 */
export default function PluginPage() {
  const q = useQuery({ queryKey: ["plugins"], queryFn: apiListPlugins });

  const cols = [
    { title: "名称", dataIndex: "name", width: 200 },
    {
      title: "版本",
      dataIndex: "version",
      width: 90,
      render: (v?: string) => (v ? <Text code>{v}</Text> : "—"),
    },
    {
      title: "能力",
      dataIndex: "kinds",
      width: 150,
      render: (ks: string[]) => (
        <Space size={4} wrap>
          {ks?.length ? ks.map(kindTag) : <Text type="secondary">—</Text>}
        </Space>
      ),
    },
    {
      title: "环境要求",
      dataIndex: "envDependencies",
      render: (v: LibraryPlugin["envDependencies"]) => <EnvCell items={v} />,
    },
    {
      title: "体积",
      dataIndex: "sizeBytes",
      width: 90,
      align: "right" as const,
      render: (v: number) => <Text type="secondary">{fmtSize(v)}</Text>,
    },
    { title: "说明", dataIndex: "description", ellipsis: true },
    {
      title: "目录名",
      dataIndex: "dirname",
      width: 170,
      render: (v: string, r: LibraryPlugin) =>
        v === r.name ? (
          <Text code>{v}</Text>
        ) : (
          // 目录名和清单名不一致时要说清楚：创建 agent 时用的是目录名
          <Text code>
            {v} <Text type="secondary">（清单名 {r.name}）</Text>
          </Text>
        ),
    },
  ];

  const data = q.data;

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Card
        size="small"
        title="插件库"
        extra={
          <Space>
            <Text type="secondary">
              共 {data?.plugins.length ?? 0} 个
            </Text>
            <a onClick={() => q.refetch()}>
              <ReloadOutlined /> 刷新
            </a>
          </Space>
        }
      >
        <Descriptions size="small" column={1} colon={false}>
          <Descriptions.Item label="库根路径">
            <Text code copyable={{ text: data?.root }}>
              {data?.root ?? "读取中…"}
            </Text>
          </Descriptions.Item>
        </Descriptions>

        <Alert
          type="info"
          showIcon
          style={{ marginTop: 10 }}
          message="这一页是只读的：加插件就是往上面的目录里放文件"
          description={
            <Space direction="vertical" size={2}>
              <span>
                手写插件包（根目录放 <Text code>keeper-plugin.json</Text>）
                或用 <Text code>plugin-authoring</Text> 写完导出，放进该目录即生效——
                不需要重启，刷新本页就能看到。
              </span>
              <Text type="secondary">
                这个路径来自 config.yaml 的 plugins.root，是元配置；它不是项目结构的一部分，
                请自己决定要不要纳入版本管理。
              </Text>
            </Space>
          }
        />

        {!data?.exists ? (
          <Alert
            style={{ marginTop: 10 }}
            type="warning"
            showIcon
            message="插件库目录不存在"
            description={
              <span>
                <Text code>{data?.root}</Text> 还没建。创建后会重建，或直接把插件包放进去。
              </span>
            }
          />
        ) : null}
      </Card>

      <Card size="small">
        <Table
          rowKey="dirname"
          size="small"
          loading={q.isLoading}
          dataSource={data?.plugins ?? []}
          columns={cols}
          pagination={false}
          locale={{ emptyText: "插件库是空的——把插件包放进上面的目录" }}
          expandable={{
            expandedRowRender: (r: LibraryPlugin) =>
              r.error ? (
                <Text type="danger" style={{ fontSize: 12 }}>
                  {r.error}
                </Text>
              ) : (
                <Descriptions size="small" column={1} colon={false}>
                  <Descriptions.Item label="实际路径">
                    <Text code copyable={{ text: r.path }} style={{ fontSize: 12 }}>
                      {r.path}
                    </Text>
                  </Descriptions.Item>
                </Descriptions>
              ),
          }}
        />
      </Card>
    </Space>
  );
}
