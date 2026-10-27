/**
 * 新增 / 编辑模型的表单弹窗。
 *
 * 为什么要抽成组件：同一张表单有两个入口——「模型管理」页的按钮，和对话页
 * 模型下拉里的「新建模型」。两处必须长得一模一样（字段、校验、候选、提示），
 * 所以只写一份，两个地方都用它。
 *
 * 它只管「一个模型预设」这件事；批量创建（选服务商 → 勾一堆模型）走
 * ``ModelCreateWizard``，向导里的「自定义」再回到本组件。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import {
  apiCreateModel,
  apiListProviders,
  apiUpdateModel,
} from "../api/client";
import {
  Field,
  NumberInput,
  PasswordInput,
  TextInput,
} from "./ui/Form";
import { ComboInput } from "./ui/ComboInput";
import type { ComboOption } from "./ui/ComboInput";
import { Modal } from "./ui/Modal";
import { toast } from "./ui/Toast";
import type { ModelProfile, ModelProfileInput } from "../api/types";

/** 全部字段可选：新建时是空表单，只有填了的键才进请求体。 */
type ModelFormValues = Partial<ModelProfileInput>;

export function ModelEditModal({
  open,
  editing,
  onClose,
}: {
  open: boolean;
  /** 传 null / 不传 = 新建 */
  editing?: ModelProfile | null;
  onClose: () => void;
}) {
  const qc = useQueryClient();

  // 表单状态：整份模型预设就是一个对象，改哪格改哪格，保存时整体提交
  const [form, setForm] = useState<ModelFormValues>({});
  const patch = (k: keyof ModelFormValues, v: unknown) =>
    setForm((p) => ({ ...p, [k]: v }));

  /* 服务商与模型名的候选项来自**出厂预设包**（keeper/presets/llm）：
     只列产品配好的那些，少打错字；但输入框仍可自由填写（预设没收录的服务商
     也能用），并能一键清空重来。 */
  const providersQ = useQuery({
    queryKey: ["model-providers"],
    queryFn: apiListProviders,
    enabled: open,
  });
  const providers = providersQ.data ?? [];
  const currentProvider = form.provider ?? "";
  /** 服务商候选：主行是标识，副行是中文名，右侧挂 Base URL 与模型数 */
  const providerOptions: ComboOption[] = providers.map((p) => ({
    value: p.provider,
    label: p.label,
    desc: `${(p.models ?? []).length} 个模型`,
  }));
  /** 模型名候选：**只列当前选中的服务商支持的模型**。
   *  没选服务商就没有候选——模型是"某家服务商的某个模型"，脱离服务商单列一
   *  堆模型名只会让人填出一个对不上的组合。 */
  const modelOptions: ComboOption[] = useMemo(() => {
    const hit = providers.find((p) => p.provider === currentProvider);
    if (!hit) return [];
    return (hit.models ?? []).map((m) => ({
      value: m.model_name,
      label: m.label,
      desc: m.context_limit
        ? `上下文 ${m.context_limit.toLocaleString()}`
        : undefined,
    }));
  }, [providers, currentProvider]);

  /* 换了服务商，之前从**它候选里挑的**模型名就失效了——留着它等于配出一个
     必然调不通的组合，所以清掉并说明一句。
     只清"当初是从候选里挑的"：用户自己手填的名字（如自建网关的别名）视为
     通用名，跨服务商沿用是有意的，不动它。 */
  const prevProviderRef = useRef("");
  useEffect(() => {
    const prev = prevProviderRef.current;
    if (prev === currentProvider) return;
    prevProviderRef.current = currentProvider;
    if (!prev || !currentProvider) return;
    const oldHit = providers.find((p) => p.provider === prev);
    const newHit = providers.find((p) => p.provider === currentProvider);
    const name = String(form.model_name ?? "").trim();
    if (!name || !oldHit || !newHit) return;
    const wasFromOld = (oldHit.models ?? []).some((m) => m.model_name === name);
    const stillOk = (newHit.models ?? []).some((m) => m.model_name === name);
    if (wasFromOld && !stillOk) {
      patch("model_name", "");
      toast.info(`已清空模型名「${name}」：${currentProvider} 不提供这个模型`);
    }
  }, [currentProvider, providers, form.model_name]);

  // 打开时把要编辑的模型灌进表单（api_key 服务端不回传，也不能回填）
  useEffect(() => {
    if (!open) return;
    if (!editing) {
      prevProviderRef.current = "";
      setForm({});
      return;
    }
    const { api_key: _omit, ...rest } = editing;
    void _omit;
    setForm(rest as unknown as ModelFormValues);
  }, [open, editing]);

  const saveM = useMutation({
    mutationFn: (v: ModelFormValues) =>
      editing
        ? apiUpdateModel(editing.id, v as ModelProfileInput)
        : apiCreateModel(v as ModelProfileInput),
    onSuccess: (saved) => {
      // 改预设必须重载绑着它的 agent（LLM 实例是 build 期固化的），所以如实告诉
      // 用户哪些生效了、哪个没起来——否则"保存成功"会让人以为全都生效了。
      const reloaded = saved?.reloaded_agents ?? [];
      const okNames = reloaded.filter((a) => a.ok).map((a) => a.name);
      const bad = reloaded.filter((a) => !a.ok);
      if (!editing) {
        toast.success("已创建");
      } else if (!reloaded.length) {
        toast.success("已更新（暂未被任何 agent 绑定）");
      } else if (bad.length) {
        toast.error(
          `已更新；已重载 ${okNames.length} 个 agent，但 ${bad
            .map((a) => a.name)
            .join("、")} 重载失败：${bad[0]?.reason ?? ""}`
        );
      } else {
        toast.success(`已更新，已重载 ${okNames.length} 个 agent：${okNames.join("、")}`);
      }
      onClose();
      qc.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (e: Error) => toast.error("保存失败：" + e.message),
  });

  const submit = () => {
    // 校验错误表单：必填三项缺失时先提示，别让请求白跑一趟
    if (!String(form.name ?? "").trim()) return toast.error("请填写名称");
    if (!String(form.provider ?? "").trim())
      return toast.error("请选择或填写模型服务商");
    if (!String(form.model_name ?? "").trim())
      return toast.error("请选择或填写模型名");
    saveM.mutate(form);
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={560}
      title={editing ? "编辑模型" : "新增模型"}
      footer={
        <>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-line bg-panel px-4 py-[7px] text-[13px] font-medium text-ink-muted shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-all duration-150 hover:bg-surface hover:text-ink-strong active:scale-[0.98]"
          >
            取消
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={saveM.isPending}
            className="flex items-center gap-1.5 rounded-lg bg-brand-gradient px-4 py-[7px] text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98] disabled:opacity-50"
          >
            {saveM.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            保存
          </button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="名称" extra="自己起个好认的名字，之后在 agent 配置里按它挑">
          <TextInput
            value={form.name}
            onChange={(v) => patch("name", v)}
            placeholder="如：DeepSeek 主力"
          />
        </Field>

        <Field
          label="模型服务商"
          extra="候选是出厂预设收录的服务商；也可以自己填别的（自建网关、内网代理）"
        >
          <ComboInput
            value={form.provider ?? ""}
            onChange={(v) => patch("provider", v)}
            options={providerOptions}
            onClear={() => patch("provider", "")}
            placeholder="选择或填写模型服务商"
            emptyText="没有匹配的候选，可直接手填服务商标识"
          />
        </Field>

        <Field
          label="模型名"
          extra="先选模型服务商，这里才列出它支持的模型；也可以自己填别的"
        >
          <ComboInput
            value={form.model_name ?? ""}
            onChange={(v) => patch("model_name", v)}
            options={modelOptions}
            /* 没选服务商就先别让填：模型是"某家服务商的某个模型"，先把服务商
               定下来，候选才说得清 */
            disabled={!currentProvider}
            onClear={() => patch("model_name", "")}
            placeholder={
              currentProvider
                ? "从候选里挑，或自己输入模型名"
                : "先选模型服务商，再挑它的模型"
            }
            emptyText={
              currentProvider
                ? "没有匹配的候选，可直接手填模型名"
                : "先选模型服务商，再从它的模型里挑"
            }
          />
        </Field>

        <Field
          label={
            <span className="flex items-center gap-1.5">
              <span>API Key</span>
              {editing?.has_api_key ? (
                <span className="rounded-md bg-emerald-500/15 px-1.5 py-0.5 text-[11px] leading-4 text-emerald-600 dark:text-emerald-300">
                  已配置
                </span>
              ) : (
                <span className="rounded-md bg-black/[0.06] px-1.5 py-0.5 text-[11px] leading-4 text-ink-muted dark:bg-white/[0.08]">
                  未配置
                </span>
              )}
            </span>
          }
          extra={
            editing?.has_api_key
              ? "出于安全考虑不显示已保存的 Key。留空表示不修改；要换 Key 请直接输入新值。"
              : "可留空（使用默认 / 环境变量）"
          }
        >
          <PasswordInput
            value={form.api_key}
            onChange={(v) => patch("api_key", v)}
            placeholder={
              editing?.has_api_key ? "留空 = 不修改" : "输入 API Key（可留空）"
            }
          />
        </Field>

        <Field label="Base URL">
          <TextInput
            value={form.base_url}
            onChange={(v) => patch("base_url", v)}
            placeholder="可选，如自建网关地址"
          />
        </Field>

        <div className="grid grid-cols-3 gap-3">
          <Field label="温度">
            <NumberInput
              value={form.temperature ?? 0.2}
              onChange={(v) => patch("temperature", v)}
              min={0}
              max={2}
              step={0.1}
            />
          </Field>
          <Field label="超时（秒）">
            <NumberInput
              value={form.timeout ?? 120}
              onChange={(v) => patch("timeout", v)}
              min={1}
              max={600}
            />
          </Field>
          <Field label="最大重试">
            <NumberInput
              value={form.max_retries ?? 3}
              onChange={(v) => patch("max_retries", v)}
              min={0}
              max={10}
            />
          </Field>
        </div>

        <Field
          label="上下文上限（token）"
          extra="配了才会出现「⚠ 上下文 X%」水位告警"
        >
          <NumberInput
            value={form.context_limit ?? 0}
            onChange={(v) => patch("context_limit", v)}
            min={0}
            step={1024}
          />
        </Field>

        <span className="block text-[12px] text-ink-muted">
          价格请在列表的「价格」按钮里配置：一个模型可配多段（错峰），
          如 00:00–08:00 低峰价 + 08:00–24:00 高峰价。
        </span>
      </div>
    </Modal>
  );
}
