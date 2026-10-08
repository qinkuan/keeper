import { useMemo, useState } from "react";
import {
  AutoComplete,
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Typography,
  message,
} from "antd";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  apiCreateModel,
  apiDeleteModel,
  apiListModels,
  apiTestModel,
  apiUpdateModel,
} from "../api/client";
import ModelPriceModal from "../components/ModelPriceModal";
import type { ModelProfile, ModelProfileInput } from "../api/types";

/** 单价改由「单价」弹窗按段管理（支持错峰多段），这里只管模型预设本身 */
type ModelFormValues = ModelProfileInput;

/** 常见 provider，新建模型时直接下拉选，避免手敲拼错。 */
const PROVIDERS = [
  "openai",
  "deepseek",
  "qwen",
  "dashscope",
  "moonshot",
  "zhipu",
  "anthropic",
  "google",
  "ollama",
  "baichuan",
  "yi",
  "minimax",
  "stepfun",
  "localai",
  "custom",
];

/** 各 provider 常见模型名，作为 model_name 的建议下拉（仍可自由输入）。 */
const PROVIDER_MODELS: Record<string, string[]> = {
  openai: ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4", "gpt-3.5-turbo", "o1", "o1-mini", "o3-mini"],
  deepseek: ["deepseek-chat", "deepseek-reasoner"],
  qwen: ["qwen-plus", "qwen-max", "qwen-turbo", "qwen-long", "qwen2.5-72b-instruct", "qwen2.5-32b-instruct", "qwen2.5-14b-instruct", "qwen2.5-7b-instruct"],
  dashscope: ["qwen-plus", "qwen-max", "qwen-turbo", "qwen-long"],
  moonshot: ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
  zhipu: ["glm-4-plus", "glm-4-air", "glm-4-airx", "glm-4-flash", "glm-4-flashx"],
  anthropic: ["claude-3-5-sonnet-latest", "claude-3-5-haiku-latest", "claude-3-opus-latest", "claude-3-sonnet-20240229", "claude-3-haiku-20240307"],
  google: ["gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash", "gemini-2.0-pro-exp"],
  ollama: ["llama3", "llama3.1", "qwen2", "qwen2.5", "mistral", "mixtral", "gemma2"],
  baichuan: ["Baichuan4", "Baichuan3-Turbo", "Baichuan2-Turbo"],
  yi: ["yi-large", "yi-medium", "yi-spark"],
  minimax: ["abab6.5-chat", "abab6.5s-chat", "abab5.5-chat"],
  stepfun: ["step-1v-8k", "step-2-16k"],
  localai: ["gpt-3.5-turbo", "gpt-4"],
};

/**
 * 模型管理：本地维护一组可复用的模型预设。
 *
 * 模型归属客户端（config.yaml 注释明确「用哪个模型平台不维护」），因此这里
 * 不连平台，纯本地 CRUD。配好后到「智能体」页的 Agent 配置里绑定即可。
 */
export default function ModelManage() {
  const qc = useQueryClient();
  const modelsQ = useQuery({ queryKey: ["models"], queryFn: apiListModels });

  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<ModelProfile | null>(null);
  /** 「单价」弹窗：一个模型可配多段价（错峰），独立管理 */
  const [priceModel, setPriceModel] = useState<ModelProfile | null>(null);
  const [form] = Form.useForm<ModelFormValues>();
  const currentProvider = Form.useWatch("provider", form);
  const modelOptions = useMemo(() => {
    const list = PROVIDER_MODELS[currentProvider ?? ""] ?? [];
    return list.map((m) => ({ value: m, label: m }));
  }, [currentProvider]);

  const saveM = useMutation({
    mutationFn: (v: ModelFormValues) =>
      editing ? apiUpdateModel(editing.id, v) : apiCreateModel(v),
    onSuccess: () => {
      message.success(editing ? "已更新" : "已创建");
      setOpen(false);
      qc.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (e: Error) => message.error("保存失败：" + e.message),
  });

  const delM = useMutation({
    mutationFn: (id: string) => apiDeleteModel(id),
    onSuccess: () => {
      message.success("已删除");
      qc.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (e: Error) => message.error("删除失败：" + e.message),
  });

  const testM = useMutation({
    mutationFn: (id: string) => apiTestModel(id),
    onSuccess: (d) => message.success("连通正常：" + (d.reply ?? "")),
    onError: (e: Error) => message.error("连通失败：" + e.message),
  });

  const openCreate = () => {
    setEditing(null);
    form.resetFields();
    setOpen(true);
  };
  const openEdit = (m: ModelProfile) => {
    setEditing(m);
    form.setFieldsValue(m);
    setOpen(true);
  };

  const columns = [
    { title: "名称", dataIndex: "name", key: "name" },
    { title: "Provider", dataIndex: "provider", key: "provider" },
    { title: "模型", dataIndex: "model_name", key: "model_name" },
    {
      title: "Base URL",
      dataIndex: "base_url",
      key: "base_url",
      render: (v: string | null) => v || "-",
    },
    { title: "温度", dataIndex: "temperature", key: "temperature" },
    {
      title: "操作",
      key: "action",
      render: (_: unknown, r: ModelProfile) => (
        <Space>
          <Button
            size="small"
            loading={testM.isPending}
            onClick={() => testM.mutate(r.id)}
          >
            测试
          </Button>
          <Button size="small" onClick={() => setPriceModel(r)}>
            单价
          </Button>
          <Button size="small" onClick={() => openEdit(r)}>
            编辑
          </Button>
          <Popconfirm
            title="确认删除该模型预设？"
            onConfirm={() => delM.mutate(r.id)}
          >
            <Button size="small" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Space
        style={{ justifyContent: "space-between", width: "100%", marginBottom: 16 }}
      >
        <Typography.Title level={3} style={{ margin: 0 }}>
          模型管理
        </Typography.Title>
        <Button type="primary" onClick={openCreate}>
          新增模型
        </Button>
      </Space>
      <Typography.Text type="secondary">
        模型归属客户端本地（平台不维护用哪个模型）。配置好后，到「智能体」页的
        Agent 配置里绑定，即可让不同 agent 用不同模型。
      </Typography.Text>
      <Table
        style={{ marginTop: 12 }}
        rowKey="id"
        loading={modelsQ.isLoading}
        dataSource={modelsQ.data ?? []}
        columns={columns}
        pagination={false}
      />

      <Modal
        open={open}
        title={editing ? "编辑模型" : "新增模型"}
        onCancel={() => setOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={saveM.isPending}
        destroyOnClose
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={(v) => saveM.mutate(v)}
        >
          <Form.Item
            label="名称（下拉里选「用哪个模型」就靠它）"
            name="name"
            rules={[{ required: true, message: "请填写名称" }]}
          >
            <Input placeholder="如：DeepSeek 主力" />
          </Form.Item>
          <Form.Item
            label="Provider"
            name="provider"
            rules={[{ required: true, message: "请选择或填写 provider" }]}
          >
            <Select
              showSearch
              placeholder="选择或输入 provider"
              options={PROVIDERS.map((p) => ({ value: p, label: p }))}
            />
          </Form.Item>
          <Form.Item
            label="模型名"
            name="model_name"
            rules={[{ required: true, message: "请选择或填写模型名" }]}
          >
            <AutoComplete
              showSearch
              placeholder="根据 provider 选择，或自由输入模型名"
              options={modelOptions}
              filterOption={(input, option) =>
                (option?.label ?? "")
                  .toLowerCase()
                  .includes(input.toLowerCase())
              }
            />
          </Form.Item>
          <Form.Item label="API Key" name="api_key">
            <Input.Password placeholder="可留空（使用默认 / 环境变量）" />
          </Form.Item>
          <Form.Item label="Base URL" name="base_url">
            <Input placeholder="可选，如自建网关地址" />
          </Form.Item>
          <Form.Item
            label="温度"
            name="temperature"
            initialValue={0.2}
          >
            <InputNumber min={0} max={2} step={0.1} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item label="超时（秒）" name="timeout" initialValue={120}>
            <InputNumber min={1} max={600} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item label="最大重试" name="max_retries" initialValue={3}>
            <InputNumber min={0} max={10} style={{ width: "100%" }} />
          </Form.Item>

          <Form.Item
            label="上下文上限（token）"
            name="context_limit"
            extra="配了才会出现「⚠ 上下文 X%」水位告警，如 DeepSeek 填 65536 或 131072"
          >
            <InputNumber
              min={0}
              step={1024}
              style={{ width: "100%" }}
              placeholder="可留空"
            />
          </Form.Item>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            单价请在列表的「单价」按钮里配置：一个模型可配多段（错峰），
            如 00:00–08:00 低峰价 + 08:00–24:00 高峰价。
          </Typography.Text>
        </Form>
      </Modal>

      <ModelPriceModal
        model={priceModel}
        open={!!priceModel}
        onClose={() => setPriceModel(null)}
      />
    </div>
  );
}
