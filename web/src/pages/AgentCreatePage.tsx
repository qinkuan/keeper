import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  Select,
  Space,
  Typography,
  message,
} from "antd";
import { ArrowLeftOutlined } from "@ant-design/icons";
import {
  apiCreateLocalAgent,
  apiListPlugins,
  apiListUserSpaces,
} from "../api/client";
import type { LibraryPlugin } from "../api/types";

const { Text, Paragraph } = Typography;

/** 一条环境要求压成一行短文字，给插件多选项当后缀用。 */
function envHint(p: LibraryPlugin): string {
  if (!p.envDependencies.length) return "";
  return (
    "需要 " +
    p.envDependencies
      .map((d) => `${d.kind} ${d.minVersion ? `≥${d.minVersion}` : ""}`)
      .join("、")
  );
}

/**
 * 创建本地 agent。
 *
 * 只做"创建"，不做"编辑"——编辑要动的不只是表单，还有绑定与已装实例之间的
 * 一致性（改了插件得重装才生效），那是另一件事，等真需要时再做。
 *
 * **环境要求由后端把关**：选了本机满足不了的插件，后端直接 409 拒绝创建。
 * 前端不重复实现一遍版本比较——两套实现必然会给出不同答案，而"创建时通过、
 * 装载时失败"是最难查的一类问题。所以这里只在选项里**显示**要求，让用户在
 * 点提交之前就知道有哪几项环境依赖。
 */
export default function AgentCreatePage({ onDone }: { onDone?: () => void }) {
  const [form] = Form.useForm();
  const [plugins, setPlugins] = useState<string[]>([]);
  const qc = useQueryClient();

  const libQ = useQuery({ queryKey: ["plugins"], queryFn: apiListPlugins });
  const spacesQ = useQuery({
    queryKey: ["user-spaces"],
    queryFn: apiListUserSpaces,
  });

  // 默认选「default」空间：它是系统的默认工作区，几乎每个 agent 都要用它。
  // 放在 effect 里而不是 initialValues —— 空间列表是异步拿到的，
  // 初次渲染时还不知道 default 的 id 是什么。
  useEffect(() => {
    const list = spacesQ.data?.user_spaces ?? [];
    if (!list.length) return;
    const def = list.find((s) => s.name === "default") ?? list[0];
    if (def) form.setFieldsValue({ workspace_space_id: def.id });
  }, [spacesQ.data, form]);

  const create = useMutation({
    mutationFn: apiCreateLocalAgent,
    onSuccess: (r) => {
      message.success(`已创建「${r.name}」，已链接 ${r.plugins.length} 个插件`);
      qc.invalidateQueries({ queryKey: ["localAgents"] });
      onDone?.();
    },
    onError: (e: any) => {
      message.error(String(e?.message ?? e));
    },
  });

  const lib = libQ.data;
  const options = (lib?.plugins ?? [])
    // 清单坏掉的插件不让选：选了也装不起来，报错还更难指向
    .filter((p) => !p.error)
    .map((p) => {
      const hint = envHint(p);
      const kinds = p.kinds.join("/");
      return {
        value: p.dirname,
        label: (
          <Space size={6}>
            <Text strong>{p.dirname}</Text>
            {p.version ? <Text type="secondary">{p.version}</Text> : null}
            {kinds ? <Text type="secondary">· {kinds}</Text> : null}
            {hint ? (
              <Text type="warning" style={{ fontSize: 12 }}>
                {hint}
              </Text>
            ) : null}
          </Space>
        ),
      };
    });

  const onFinish = (v: any) => {
    create.mutate({
      name: String(v.name ?? "").trim(),
      description: v.description ?? null,
      persona: v.persona ?? "",
      status: v.status ?? "active",
      // 只给「选了哪个用户空间」，路径与只读由后端从 user_space 那条记录取。
      // 前端不再手填路径 / 摆只读开关：只读是**工作空间**的属性，两处各填一份
      // 曾经把 agent 的工作区判成只读，写类工具（bash__run）在界面上凭空消失。
      workspace_space_id: v.workspace_space_id || null,
      plugins,
    });
  };

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Card
        size="small"
        title="创建 Agent"
        extra={
          <Button size="small" icon={<ArrowLeftOutlined />} onClick={onDone}>
            返回
          </Button>
        }
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={onFinish}
          initialValues={{
            status: "active",
          }}
          style={{ maxWidth: 720 }}
        >
          <Form.Item
            label="名称"
            name="name"
            rules={[{ required: true, message: "给 agent 起个名字" }]}
            extra="本机唯一"
          >
            <Input placeholder="比如 code-reviewer" maxLength={64} />
          </Form.Item>

          <Form.Item label="说明" name="description">
            <Input placeholder="一句话说明它做什么（可空）" maxLength={200} />
          </Form.Item>

          <Form.Item label="状态" name="status">
            <Select
              style={{ width: 160 }}
              options={[
                { value: "active", label: "active" },
                { value: "disabled", label: "disabled" },
                { value: "archived", label: "archived" },
              ]}
            />
          </Form.Item>

          <Form.Item
            label="画像"
            name="persona"
            extra="每次对话都会带上。可以留空。"
          >
            <Input.TextArea rows={4} placeholder="角色、语气、该关注什么…" />
          </Form.Item>

          <Form.Item
            label="工作区"
            name="workspace_space_id"
            extra="选项来自「用户空间」。只读是工作空间自己的属性，在「用户空间」里改；这里选哪个，agent 就用哪个目录。"
          >
            <Select
              allowClear
              placeholder="不选 = 无工作区"
              options={[
                { value: "", label: "无工作区", disabled: true },
                ...(spacesQ.data?.user_spaces ?? []).map((s) => ({
                  value: s.id,
                  label: (
                    <Space size={6}>
                      <Text strong>{s.name}</Text>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {s.path}
                      </Text>
                      {s.read_only ? (
                        <Text type="warning" style={{ fontSize: 12 }}>
                          只读
                        </Text>
                      ) : null}
                    </Space>
                  ),
                })),
              ]}
            />
          </Form.Item>

          <Form.Item
            label="能力绑定（插件）"
            extra={
              <>
                从插件库里挑。插件本体不在 agent 目录里，而是在
                <Text code> ~/.keeper/agents/&lt;id&gt;/plugins/</Text>
                下建一个**符号链接**指回库里的真实目录——所以库里改了，所有
                agent 立刻生效，不用重新装。
              </>
            }
          >
            <Select
              mode="multiple"
              value={plugins}
              onChange={setPlugins}
              loading={libQ.isLoading}
              placeholder={
                lib?.plugins.length
                  ? "选插件（可多选）"
                  : "插件库是空的——先往插件库里放插件包"
              }
              options={options}
              optionFilterProp="label"
              style={{ width: "100%" }}
            />
          </Form.Item>

          {lib && !lib.exists ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message="插件库目录不存在"
              description={
                <span>
                  <Text code>{lib.root}</Text> 还没建，可以先不选插件创建，之后再补。
                </span>
              }
            />
          ) : null}

          <Paragraph type="secondary" style={{ fontSize: 12 }}>
            提示：选了环境要求满足不了的插件，**创建会直接被拒绝**并告诉你缺什么
            ——这是故意的。环境不满足的症状是子进程起不来、命令 not found，跟
            "插件坏了"长得很像，等到真去调的时候根本想不起来是创建时就没验过。
          </Paragraph>

          <Space>
            <Button type="primary" htmlType="submit" loading={create.isPending}>
              创建
            </Button>
            <Button onClick={onDone}>取消</Button>
          </Space>
        </Form>
      </Card>
    </Space>
  );
}
