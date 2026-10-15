import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";

import { apiCreatePrice, apiDeletePrice, apiListPrices } from "../api/client";
import type { ModelPrice, ModelProfile } from "../api/types";
import { Modal } from "./ui/Modal";
import { Field, NumberInput, SelectInput, TextInput } from "./ui/Form";
import { toast } from "./ui/Toast";

/** 分钟数 → HH:mm */
function fmtMinute(m?: number | null): string {
  if (m == null) return "—";
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
}

/** HH:mm → 一天内分钟数（0–1439）；空串给 null */
function toMinute(hhmm: string): number | null {
  if (!hhmm) return null;
  const [h, m] = hhmm.split(":").map((s) => parseInt(s, 10));
  if (Number.isNaN(h) || Number.isNaN(m)) return null;
  return h * 60 + m;
}

/** ISO 星期号 → 中文（1=周一 … 7=周日） */
const WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

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

/** "1,2,3" → [1,2,3]，供复选框回显 */
function parseWeekdays(spec?: string | null): number[] {
  if (!spec) return [];
  return spec
    .split(",")
    .map((s) => parseInt(s.trim(), 10))
    .filter((n) => n >= 1 && n <= 7);
}

/** 价格段表格里的一行（每行一个删除按钮） */
function PriceRow({
  p,
  onDelete,
  deleting,
}: {
  p: ModelPrice;
  onDelete: () => void;
  deleting: boolean;
}) {
  const [confirm, setConfirm] = useState(false);
  return (
    <tr className="border-b border-line-soft last:border-0">
      <td className="py-2 pr-3 text-[12px] text-ink-muted">
        {new Date(p.effective_from).toLocaleString("zh-CN", {
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
        })}
      </td>
      <td className="py-2 pr-3 text-[12px] text-ink">
        {fmtWeekdays(p.weekdays)}
      </td>
      <td className="py-2 pr-3 text-[12px] text-ink">
        {p.time_from_minute == null ? (
          // 全天价是「兜底段」，跟时段价视觉上要能分开
          <span className="inline-flex rounded-md bg-black/[0.06] px-1.5 py-0.5 text-[11px] leading-4 text-ink-muted dark:bg-white/[0.08]">
            全天（兜底）
          </span>
        ) : (
          `${fmtMinute(p.time_from_minute)} – ${fmtMinute(p.time_to_minute)}`
        )}
      </td>
      <td className="py-2 pr-3 text-right font-mono text-[12px] text-ink">
        {p.input_price_per_1m}
      </td>
      <td className="py-2 pr-3 text-right font-mono text-[12px] text-ink">
        {p.cached_price_per_1m}
      </td>
      <td className="py-2 pr-3 text-right font-mono text-[12px] text-ink">
        {p.output_price_per_1m}
      </td>
      <td className="py-2 pr-3 text-[12px] text-ink-muted">{p.currency}</td>
      <td className="py-2 text-right">
        {/* 二次确认放在按钮内：删价格段后果可控，不值得再弹一层对话框 */}
        {confirm ? (
          <span className="flex items-center justify-end gap-1">
            <button
              type="button"
              disabled={deleting}
              onClick={onDelete}
              className="rounded-md bg-red-500 px-2 py-1 text-[12px] text-white transition hover:bg-red-400 disabled:opacity-50"
            >
              确认
            </button>
            <button
              type="button"
              onClick={() => setConfirm(false)}
              className="rounded-md px-2 py-1 text-[12px] text-ink-muted transition hover:text-ink"
            >
              取消
            </button>
          </span>
        ) : (
          <button
            type="button"
            onClick={() => setConfirm(true)}
            className="rounded-md px-2 py-1 text-[12px] text-ink-muted transition hover:bg-red-500/10 hover:text-red-600 dark:hover:text-red-300"
          >
            删除
          </button>
        )}
      </td>
    </tr>
  );
}

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
  const [submitting, setSubmitting] = useState(false);

  // 新增表单的本地状态（原来挂在 antd Form 上，现在直接拿 state 管）
  const [weekdays, setWeekdays] = useState<number[]>([]);
  const [timeFrom, setTimeFrom] = useState("");
  const [timeTo, setTimeTo] = useState("");
  const [inPrice, setInPrice] = useState(0);
  const [cachedPrice, setCachedPrice] = useState(0);
  const [outPrice, setOutPrice] = useState(0);
  const [currency, setCurrency] = useState("CNY");
  const [note, setNote] = useState("");

  const modelId = model?.id ?? "";
  const pricesQ = useQuery({
    queryKey: ["prices", modelId],
    queryFn: () => apiListPrices(modelId),
    enabled: open && !!modelId,
  });

  const resetForm = () => {
    setWeekdays([]);
    setTimeFrom("");
    setTimeTo("");
    setInPrice(0);
    setCachedPrice(0);
    setOutPrice(0);
    setCurrency("CNY");
    setNote("");
  };

  const createM = useMutation({
    mutationFn: () =>
      apiCreatePrice(modelId, {
        effective_from: new Date().toISOString(),
        weekdays: weekdays.length
          ? [...weekdays].sort((a, b) => a - b).join(",")
          : null,
        time_from_minute: toMinute(timeFrom),
        time_to_minute: toMinute(timeTo),
        input_price_per_1m: Number(inPrice ?? 0),
        cached_price_per_1m: Number(cachedPrice ?? 0),
        output_price_per_1m: Number(outPrice ?? 0),
        currency: currency || "CNY",
        note: note || null,
      }),
    onSuccess: () => {
      toast.success("已新增价格段");
      resetForm();
      qc.invalidateQueries({ queryKey: ["prices", modelId] });
      qc.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (e: Error) => toast.error("新增失败：" + e.message),
  });

  const delM = useMutation({
    mutationFn: (priceId: string) => apiDeletePrice(modelId, priceId),
    onSuccess: () => {
      toast.success("已删除");
      qc.invalidateQueries({ queryKey: ["prices", modelId] });
    },
    onError: (e: Error) => toast.error("删除失败：" + e.message),
  });

  const submit = async () => {
    setSubmitting(true);
    try {
      await createM.mutateAsync();
    } finally {
      setSubmitting(false);
    }
  };

  const prices = pricesQ.data ?? [];

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={880}
      title={model ? `单价配置 — ${model.name}` : "单价配置"}
    >
      <p className="text-[12px] leading-5 text-ink-muted">
        可配多段：例如「00:00–08:00 低峰价」+「08:00–24:00 高峰价」。匹配时
        优先取时段命中的那段，没命中就退回「全天」那段。留空时段 = 全天通用价。
      </p>

      <div className="mt-3 overflow-x-auto rounded-lg border border-line-soft">
        {pricesQ.isLoading ? (
          <div className="flex items-center justify-center py-8 text-ink-muted">
            <Loader2 className="h-4 w-4 animate-spin" />
          </div>
        ) : prices.length === 0 ? (
          <div className="py-8 text-center text-[13px] text-ink-faint">
            还没配价格，成本会显示「—」
          </div>
        ) : (
          <table className="w-full min-w-[720px] border-collapse">
            <thead>
              <tr className="border-b border-line-soft bg-surface text-left text-[12px] text-ink-muted">
                <th className="px-3 py-2 font-medium">生效自</th>
                <th className="px-3 py-2 font-medium">适用星期</th>
                <th className="px-3 py-2 font-medium">适用时段</th>
                <th className="px-3 py-2 text-right font-medium">输入</th>
                <th className="px-3 py-2 text-right font-medium">缓存命中</th>
                <th className="px-3 py-2 text-right font-medium">输出</th>
                <th className="px-3 py-2 font-medium">币种</th>
                <th className="px-3 py-2 text-right font-medium">操作</th>
              </tr>
            </thead>
            <tbody className="[&>tr>td:first-child]:pl-3 [&>tr>td:last-child]:pr-3">
              {prices.map((p) => (
                <PriceRow
                  key={p.id}
                  p={p}
                  deleting={delM.isPending}
                  onDelete={() => delM.mutate(p.id)}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="mt-5 border-t border-line-soft pt-4">
        <h3 className="mb-3 text-[13px] font-semibold text-ink-strong">
          新增价格段
        </h3>

        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Field label="时段开始">
            <input
              type="time"
              value={timeFrom}
              onChange={(e) => setTimeFrom(e.target.value)}
              className="w-full rounded-[7px] border border-line bg-panel px-2.5 py-2 text-[12.5px] text-ink shadow-[0_1px_1px_rgba(0,0,0,0.03)] outline-none transition-all duration-150 placeholder:text-ink-faint focus:border-brand focus:shadow-[0_0_0_3px_rgba(91,155,245,0.48)]"
            />
          </Field>
          <Field label="时段结束">
            <input
              type="time"
              value={timeTo}
              onChange={(e) => setTimeTo(e.target.value)}
              className="w-full rounded-[7px] border border-line bg-panel px-2.5 py-2 text-[12.5px] text-ink shadow-[0_1px_1px_rgba(0,0,0,0.03)] outline-none transition-all duration-150 placeholder:text-ink-faint focus:border-brand focus:shadow-[0_0_0_3px_rgba(91,155,245,0.48)]"
            />
          </Field>
          <Field label="输入 / 百万 token">
            <NumberInput value={inPrice} onChange={setInPrice} min={0} step={0.1} />
          </Field>
          <Field label="缓存命中 / 百万">
            <NumberInput
              value={cachedPrice}
              onChange={setCachedPrice}
              min={0}
              step={0.1}
            />
          </Field>
          <Field label="输出 / 百万">
            <NumberInput
              value={outPrice}
              onChange={setOutPrice}
              min={0}
              step={0.1}
            />
          </Field>
          <Field label="币种">
            <SelectInput
              value={currency}
              onChange={setCurrency}
              options={[
                { value: "CNY", label: "CNY（人民币）" },
                { value: "USD", label: "USD（美元）" },
              ]}
            />
          </Field>
          <Field label="备注" className="col-span-2">
            <TextInput
              value={note}
              onChange={setNote}
              placeholder="如：低峰优惠价"
            />
          </Field>
        </div>

        {/* 适用星期：7 个复选框比 antd 的多选下拉更直观——一屏看全，不用展开 */}
        <div className="mt-3">
          <div className="mb-1.5 text-[13px] font-medium text-ink">适用星期</div>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
            {WEEKDAY_LABELS.map((label, i) => {
              const n = i + 1;
              const on = weekdays.includes(n);
              return (
                <label
                  key={n}
                  className="flex cursor-pointer items-center gap-1.5 text-[13px] text-ink"
                >
                  <input
                    type="checkbox"
                    checked={on}
                    onChange={(e) =>
                      setWeekdays((s) =>
                        e.target.checked
                          ? [...s, n]
                          : s.filter((x) => x !== n)
                      )
                    }
                    className="h-3.5 w-3.5 accent-[var(--kp-primary)]"
                  />
                  {label}
                </label>
              );
            })}
            <span className="text-[12px] text-ink-faint">全不勾 = 每天</span>
          </div>
        </div>

        <button
          type="button"
          disabled={submitting || createM.isPending}
          onClick={submit}
          className="mt-4 flex items-center gap-1.5 rounded-lg bg-brand-gradient px-4 py-[7px] text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
        >
          {(submitting || createM.isPending) && (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          )}
          新增价格段
        </button>
      </div>
    </Modal>
  );
}
