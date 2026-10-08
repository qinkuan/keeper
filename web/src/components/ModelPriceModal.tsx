import { useState } from "react";
import {
  Button,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  TimePicker,
  Typography,
  message,
} from "antd";
import dayjs, { type Dayjs } from "dayjs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiCreatePrice, apiDeletePrice, apiListPrices } from "../api/client";
import type { ModelPrice, ModelProfile } from "../api/types";

/** 分钟数 → HH:mm */
function fmtMinute(m?: number | null): string {
  if (m == null) return "—";
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
}

/** Dayjs → 一天内分钟数（0–1439） */
const toMinute = (d?: Dayjs | null): number | null =>
  d ? d.hour() * 60 + d.minute() : null;

/** ISO 星期号 → 中文（1=周一 … 7=周日） */
const WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
const WEEKDAY_OPTIONS = WEEKDAY_LABELS.map((label, i) => ({
  value: i + 1,
  label,
}));

/** "1,2,3" → "周一、周二、周三"；空 = 每天 */
function fmtWeekdays(spec?: string | null): string {
  if (!spec) return "每天";
  const nums = spec
    .split(",")
    .map((s) => parseInt(s.trim(), 10))
    .filter((n) => n >= 1 && n <= 7)
    .sort((a, b) => a - b);
  if (nums.length === 0 || nums.length === 7) return "每天";
  return nums.map((n) => WEEKDAY_LABELS[n - 1]).join("、");
}

type PriceFormValues = {
  /** 选中的星期号（1–7）；空 = 每天 */
  weekdays?: number[];
  time_from?: Dayjs | null;
  time_to?: Dayjs | null;
  input_price_per_1m: number;
  cached_price_per_1m: number;
  output_price_per_1m: number;
  currency?: string;
  note?: string;
};

/**
 * 模型价格段管理：一个模型可以配**多段**价（错峰计费）。
 *
 * 典型用法：一段「00:00–08:00 低峰价」+ 一段「08:00–24:00 高峰价」，
 * 或一段「全天通用价」兜底 + 若干时段价。匹配时优先命中时段，否则退回全天价。
 * 改价是「新增一段」而非改旧段，历史成本不会被重算。
 */
export default function ModelPriceModal({
  model,
  open,
  onClose,
}: {
  model: ModelProfile | null;
  open: boolean;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [form] = Form.useForm<PriceFormValues>();
  const [submitting, setSubmitting] = useState(false);

  const modelId = model?.id ?? "";
  const pricesQ = useQuery({
    queryKey: ["prices", modelId],
    queryFn: () => apiListPrices(modelId),
    enabled: open && !!modelId,
  });

  const createM = useMutation({
    mutationFn: (v: PriceFormValues) =>
      apiCreatePrice(modelId, {
        effective_from: dayjs().toISOString(),
        weekdays:
          v.weekdays && v.weekdays.length
            ? [...v.weekdays].sort((a, b) => a - b).join(",")
            : null,
        time_from_minute: toMinute(v.time_from),
        time_to_minute: toMinute(v.time_to),
        input_price_per_1m: Number(v.input_price_per_1m ?? 0),
        cached_price_per_1m: Number(v.cached_price_per_1m ?? 0),
        output_price_per_1m: Number(v.output_price_per_1m ?? 0),
        currency: v.currency || "CNY",
        note: v.note || null,
      }),
    onSuccess: () => {
      message.success("已新增价格段");
      form.resetFields();
      qc.invalidateQueries({ queryKey: ["prices", modelId] });
      qc.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (e: Error) => message.error("新增失败：" + e.message),
  });

  const delM = useMutation({
    mutationFn: (priceId: string) => apiDeletePrice(modelId, priceId),
    onSuccess: () => {
      message.success("已删除");
      qc.invalidateQueries({ queryKey: ["prices", modelId] });
    },
    onError: (e: Error) => message.error("删除失败：" + e.message),
  });

  const columns = [
    {
      title: "生效自",
      dataIndex: "effective_from",
      key: "effective_from",
      render: (v: string) => dayjs(v).format("YYYY-MM-DD HH:mm"),
    },
    {
      title: "适用星期",
      key: "weekdays",
      render: (_: unknown, r: ModelPrice) => fmtWeekdays(r.weekdays),
    },
    {
      title: "适用时段",
      key: "range",
      render: (_: unknown, r: ModelPrice) =>
        r.time_from_minute == null ? (
          <Tag>全天（兜底）</Tag>
        ) : (
          <span>
            {fmtMinute(r.time_from_minute)} – {fmtMinute(r.time_to_minute)}
          </span>
        ),
    },
    { title: "输入", dataIndex: "input_price_per_1m", key: "in" },
    { title: "缓存命中", dataIndex: "cached_price_per_1m", key: "cached" },
    { title: "输出", dataIndex: "output_price_per_1m", key: "out" },
    { title: "币种", dataIndex: "currency", key: "currency" },
    {
      title: "操作",
      key: "action",
      render: (_: unknown, r: ModelPrice) => (
        <Popconfirm title="删除这段价格？" onConfirm={() => delM.mutate(r.id)}>
          <Button size="small" danger loading={delM.isPending}>
            删除
          </Button>
        </Popconfirm>
      ),
    },
  ];

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={820}
      title={model ? `单价配置 — ${model.name}` : "单价配置"}
      destroyOnClose
    >
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        可配多段：例如「00:00–08:00 低峰价」+「08:00–24:00 高峰价」。匹配时
        优先取时段命中的那段，没命中就退回「全天」那段。留空时段 = 全天通用价。
      </Typography.Text>

      <Table
        style={{ marginTop: 12 }}
        rowKey="id"
        size="small"
        loading={pricesQ.isLoading}
        dataSource={pricesQ.data ?? []}
        columns={columns}
        pagination={false}
        locale={{ emptyText: <Empty description="还没配价格，成本会显示「—」" /> }}
      />

      <div style={{ marginTop: 16, borderTop: "1px solid #f0f0f0", paddingTop: 12 }}>
        <Typography.Text strong>新增价格段</Typography.Text>
        <Form
          form={form}
          layout="vertical"
          style={{ marginTop: 8 }}
          onFinish={async (v) => {
            setSubmitting(true);
            try {
              await createM.mutateAsync(v);
            } finally {
              setSubmitting(false);
            }
          }}
        >
          <Space wrap>
            <Form.Item
              label="适用星期"
              name="weekdays"
              style={{ marginBottom: 8 }}
              extra="留空 = 每天"
            >
              <Select
                mode="multiple"
                allowClear
                placeholder="默认每天"
                style={{ minWidth: 240 }}
                options={WEEKDAY_OPTIONS}
              />
            </Form.Item>
            <Form.Item label="时段" style={{ marginBottom: 8 }}>
              <Space>
                <Form.Item name="time_from" noStyle>
                  <TimePicker format="HH:mm" placeholder="开始" style={{ width: 110 }} />
                </Form.Item>
                <span style={{ color: "#8c8c8c" }}>—</span>
                <Form.Item name="time_to" noStyle>
                  <TimePicker format="HH:mm" placeholder="结束" style={{ width: 110 }} />
                </Form.Item>
              </Space>
            </Form.Item>
          </Space>
          <Space wrap>
            <Form.Item
              label="输入 / 百万 token"
              name="input_price_per_1m"
              rules={[{ required: true, message: "必填" }]}
              style={{ marginBottom: 8 }}
            >
              <InputNumber min={0} step={0.1} style={{ width: 150 }} />
            </Form.Item>
            <Form.Item
              label="缓存命中 / 百万"
              name="cached_price_per_1m"
              rules={[{ required: true, message: "必填" }]}
              style={{ marginBottom: 8 }}
            >
              <InputNumber min={0} step={0.1} style={{ width: 150 }} />
            </Form.Item>
            <Form.Item
              label="输出 / 百万"
              name="output_price_per_1m"
              rules={[{ required: true, message: "必填" }]}
              style={{ marginBottom: 8 }}
            >
              <InputNumber min={0} step={0.1} style={{ width: 150 }} />
            </Form.Item>
            <Form.Item label="币种" name="currency" initialValue="CNY" style={{ marginBottom: 8 }}>
              <Select
                style={{ width: 130 }}
                options={[
                  { value: "CNY", label: "CNY（人民币）" },
                  { value: "USD", label: "USD（美元）" },
                ]}
              />
            </Form.Item>
          </Space>
          <Form.Item label="备注" name="note" style={{ marginBottom: 8 }}>
            <Input placeholder="如：低峰优惠价" />
          </Form.Item>
          <Button type="primary" loading={submitting || createM.isPending} onClick={() => form.submit()}>
            新增价格段
          </Button>
        </Form>
      </div>
    </Modal>
  );
}
