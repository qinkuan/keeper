import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import {
  apiCreateModel,
  apiDeleteModel,
  apiListModels,
  apiTestModel,
  apiUpdateModel,
} from "../api/client";
import ModelPriceModal from "../components/ModelPriceModal";
import { PageHeader, Section } from "../components/ui/Page";
import { Field, NumberInput, SelectInput, TextInput, Toggle } from "../components/ui/Form";
import { Modal } from "../components/ui/Modal";
import { Table } from "../components/ui/Table";
import type { Column } from "../components/ui/Table";
import { toast } from "../components/ui/Toast";
import { cn } from "../lib/utils";
import type { ModelProfile, ModelProfileInput } from "../api/types";

/** 单价改由「单价」弹窗按段管理（支持错峰多段），这里只管模型预设本身。
 *  全部字段可选：新建时是空表单，只有填了的键才进请求体。 */
type ModelFormValues = Partial<ModelProfileInput>;

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

/** 密码输入：带「显示 / 隐藏」切换，避免长 Key 粘贴后无法核对 */
function PasswordInput({
  value,
  onChange,
  placeholder,
}: {
  value?: string | null;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  const [show, setShow] = useState(false);
  return (
    <div className="relative">
      <input
        type={show ? "text" : "password"}
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        autoComplete="new-password"
        className="w-full rounded-[7px] border border-line bg-panel px-2.5 py-2 pr-16 font-mono text-[12.5px] text-ink shadow-[0_1px_1px_rgba(0,0,0,0.03)] outline-none transition-all duration-150 focus:border-brand focus:shadow-[0_0_0_3px_rgba(66,133,244,0.48)] placeholder:font-sans placeholder:text-ink-faint"
      />
      <button
        type="button"
        onClick={() => setShow((s) => !s)}
        className="absolute right-2 top-1/2 -translate-y-1/2 rounded px-1.5 py-0.5 text-[12px] text-ink-faint transition-colors hover:text-brand"
      >
        {show ? "隐藏" : "显示"}
      </button>
    </div>
  );
}

/** 操作按钮：删除用红色文字，与其它动作区分开 */
function ActBtn({
  onClick,
  disabled,
  tone,
  children,
}: {
  onClick: () => void;
  disabled?: boolean;
  tone?: "danger";
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "inline-flex h-6 items-center gap-1 rounded-md border px-2 text-[12px] transition-colors disabled:opacity-50",
        tone === "danger"
          ? "border-red-500/30 text-red-600 hover:border-red-500 hover:bg-red-500/10 dark:text-red-300"
          : "border-line text-ink-muted hover:bg-surface hover:text-ink-strong"
      )}
    >
      {children}
    </button>
  );
}

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
  const [confirmDel, setConfirmDel] = useState<string | null>(null);

  // 表单状态：整份模型预设就是一个对象，改哪格改哪格，保存时整体提交
  const [form, setForm] = useState<ModelFormValues>({});
  const patch = (k: keyof ModelFormValues, v: unknown) =>
    setForm((p) => ({ ...p, [k]: v }));

  const currentProvider = form.provider ?? "";
  const modelOptions = useMemo(
    () => PROVIDER_MODELS[currentProvider] ?? [],
    [currentProvider]
  );

  // 编辑时把预设灌进表单（api_key 已被 openEdit 剥掉）
  useEffect(() => {
    if (!open) return;
    setForm(editing ? { ...(editing as unknown as ModelFormValues) } : {});
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
      setOpen(false);
      qc.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (e: Error) => toast.error("保存失败：" + e.message),
  });

  const delM = useMutation({
    mutationFn: (id: string) => apiDeleteModel(id),
    onSuccess: () => {
      toast.success("已删除");
      setConfirmDel(null);
      qc.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (e: Error) => toast.error("删除失败：" + e.message),
  });

  const testM = useMutation({
    mutationFn: (id: string) => apiTestModel(id),
    onSuccess: (d) => toast.success("连通正常：" + (d.reply ?? "")),
    onError: (e: Error) => toast.error("连通失败：" + e.message),
  });

  const openCreate = () => {
    setEditing(null);
    setOpen(true);
  };
  const openEdit = (m: ModelProfile) => {
    setEditing(m);
    // 显式剥掉 api_key：服务端已不再回传，这里也不该回填。
    // API Key 是只写字段——看得见就能抄走，而这个页面通常谁都能点开。
    // 空输入框 + 「已配置」提示就是完整的状态表达。
    const { api_key: _omit, ...rest } = m;
    void _omit;
    setEditing({ ...(rest as ModelProfile) });
    setOpen(true);
  };

  const submit = () => {
    // 校验错误表单：必填三项缺失时先提示，别让请求白跑一趟
    if (!String(form.name ?? "").trim()) return toast.error("请填写名称");
    if (!String(form.provider ?? "").trim()) return toast.error("请选择或填写 provider");
    if (!String(form.model_name ?? "").trim()) return toast.error("请选择或填写模型名");
    saveM.mutate(form);
  };

  const columns: Column<ModelProfile>[] = [
    { key: "name", title: "名称", render: (r) => r.name },
    {
      key: "provider",
      title: "Provider",
      width: 110,
      render: (r) => (
        <span className="rounded-md bg-black/[0.06] px-1.5 py-0.5 font-mono text-[11px] leading-4 text-ink-muted dark:bg-white/[0.08]">
          {r.provider}
        </span>
      ),
    },
    {
      key: "model_name",
      title: "模型",
      width: 200,
      render: (r) => <span className="font-mono text-[12px]">{r.model_name}</span>,
    },
    {
      key: "base_url",
      title: "Base URL",
      render: (r) => (
        <span className="block max-w-[220px] truncate font-mono text-[12px] text-ink-muted">
          {r.base_url || "—"}
        </span>
      ),
    },
    { key: "temperature", title: "温度", width: 70, align: "right", render: (r) => r.temperature },
    {
      key: "action",
      title: "操作",
      width: 250,
      render: (r) => (
        <div className="flex flex-wrap items-center gap-1">
          <ActBtn
            onClick={() => testM.mutate(r.id)}
            disabled={testM.isPending}
          >
            {testM.isPending && <Loader2 className="h-3 w-3 animate-spin" />}
            测试
          </ActBtn>
          <ActBtn onClick={() => setPriceModel(r)}>单价</ActBtn>
          <ActBtn onClick={() => openEdit(r)}>编辑</ActBtn>
          {/* 二次确认放在按钮内：删一个预设后果可控，不值得再弹一层对话框 */}
          {confirmDel === r.id ? (
            <>
              <ActBtn tone="danger" onClick={() => delM.mutate(r.id)}>
                确认删除
              </ActBtn>
              <ActBtn onClick={() => setConfirmDel(null)}>取消</ActBtn>
            </>
          ) : (
            <ActBtn tone="danger" onClick={() => setConfirmDel(r.id)}>
              删除
            </ActBtn>
          )}
        </div>
      ),
    },
  ];

  const suggestions = modelOptions.filter((m) =>
    m.toLowerCase().includes(String(form.model_name ?? "").toLowerCase())
  );

  return (
    <>
      <PageHeader
        title="模型管理"
        desc="模型归属客户端本地（平台不维护用哪个模型）。配置好后，到「智能体」页的 Agent 配置里绑定，即可让不同 agent 用不同模型。"
        actions={
          <button
            type="button"
            onClick={openCreate}
            className="rounded-lg bg-brand-gradient px-4 py-[7px] text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98]"
          >
            新增模型
          </button>
        }
      />
      <Section bodyClassName="p-0">
        <Table
          rows={modelsQ.data ?? []}
          columns={columns}
          rowKey={(r) => r.id}
          loading={modelsQ.isLoading}
          minWidth={900}
          empty="还没有模型预设，点右上角「新增模型」"
        />
      </Section>

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        width={560}
        title={editing ? "编辑模型" : "新增模型"}
        footer={
          <>
            <button
              type="button"
              onClick={() => setOpen(false)}
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
          <Field label="名称（下拉里选「用哪个模型」就靠它）">
            <TextInput
              value={form.name}
              onChange={(v) => patch("name", v)}
              placeholder="如：DeepSeek 主力"
            />
          </Field>

          <Field label="Provider">
            <SelectInput
              value={form.provider ?? ""}
              onChange={(v) => patch("provider", v)}
              options={[
                { value: "", label: "选择 provider" },
                ...PROVIDERS.map((p) => ({ value: p, label: p })),
              ]}
            />
          </Field>

          <Field label="模型名">
            <TextInput
              value={form.model_name}
              onChange={(v) => patch("model_name", v)}
              placeholder="根据 provider 选择，或自由输入模型名"
            />
            {/* 建议列表用 datalist 而不是 AutoComplete：原生就是「输入 + 联想」，
                自由输入不受限，也不用自己管下拉的展开与键盘交互 */}
            <datalist id="model-name-suggestions">
              {suggestions.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
            <input
              list="model-name-suggestions"
              value={form.model_name ?? ""}
              onChange={(e) => patch("model_name", e.target.value)}
              placeholder="也可从建议里选"
              className="w-full rounded-[7px] border border-line bg-panel px-2.5 py-2 text-[12.5px] text-ink shadow-[0_1px_1px_rgba(0,0,0,0.03)] outline-none transition-all duration-150 placeholder:text-ink-faint focus:border-brand focus:shadow-[0_0_0_3px_rgba(66,133,244,0.48)]"
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
            extra="配了才会出现「⚠ 上下文 X%」水位告警，如 DeepSeek 填 65536 或 131072"
          >
            <NumberInput
              value={form.context_limit ?? 0}
              onChange={(v) => patch("context_limit", v)}
              min={0}
              step={1024}
            />
          </Field>

          <span className="block text-[12px] text-ink-muted">
            单价请在列表的「单价」按钮里配置：一个模型可配多段（错峰），
            如 00:00–08:00 低峰价 + 08:00–24:00 高峰价。
          </span>
        </div>
      </Modal>

      <ModelPriceModal
        model={priceModel}
        open={!!priceModel}
        onClose={() => setPriceModel(null)}
      />
    </>
  );
}
