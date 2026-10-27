/**
 * 新建模型向导：选服务商 → 填 Key → 勾选该服务商的模型 → 批量创建。
 *
 * 为什么做成"向导"而不是一张表单
 * ----------------------------
 * 模型预设里最容易填错、又最没必要让用户填的东西，是**服务商侧的固定信息**：
 * 地址、上下文长度——这些是产品该给的知识（出厂预设包里有），用户真正需要
 * 知道的只有两件事：「我用的是哪家」「我的 Key 是什么」。
 *
 * 所以流程按"用户脑子里有的信息"排：
 *   1. 选服务商（产品给的可选列表，一个卡片 = 一家）
 *   2. 填 API Key（唯一必填项）
 *   3. 底部列出这家服务商支持的模型及其配置（上下文、温度……），可全选或
 *      逐个勾选；展开某个模型可以改它的全部配置
 *
 * 价格不在这个流程里配——统一到列表的「价格」弹窗（那里能配多段错峰价）。
 *
 * 勾选几个就建几个模型预设条目（同一个 Key 共用）——这就是"我买了这个账号，
 * 把它支持的模型都配上"这个动作的本来样子。
 *
 * 系统没有预设的服务商走「自定义」：仍然是原来那张手填表单。
 */
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import { apiCreateModelsFromPreset, apiListProviders } from "../api/client";
import type { ModelProvider, ModelProviderModel, ModelSpec } from "../api/types";
import { Modal } from "./ui/Modal";
import { NumberInput, PasswordInput, TextInput } from "./ui/Form";
import { ComboInput } from "./ui/ComboInput";
import type { ComboOption } from "./ui/ComboInput";
import { toast } from "./ui/Toast";

/**
 * 单个模型的可编辑字段（= 模型配置的全量信息，**不含价格**）。
 *
 * 展开一个模型 = 把它当成"自定义"里那张表单来填：名称、模型名、地址、
 * Key、上下文、温度、超时、重试……预设只是把这些格子**预填好**，用户改哪个
 * 都算数。
 *
 * 价格不在这里配：统一到模型管理列表的「价格」弹窗（那里才能配多段错峰价，
 * 混在创建流程里既挤又容易填错）。
 *
 * 表单控件给的是字符串，提交时才转数字（转不动就留空，不写脏数据）。
 */
type ModelEdit = {
  name: string;
  model_name: string;
  base_url: string;
  api_key: string;
  context_limit: string;
  temperature: string;
  timeout: string;
  max_retries: string;
};

/** 空编辑态（用户还没展开过某个模型时的占位） */
const EMPTY_EDIT: ModelEdit = {
  name: "",
  model_name: "",
  base_url: "",
  api_key: "",
  context_limit: "",
  temperature: "0.2",
  timeout: "120",
  max_retries: "3",
};

/** 展开时的初值：以预设为准预填，用户改哪个就变哪个 */
function initEdit(m: ModelProviderModel, provider: ModelProvider): ModelEdit {
  return {
    name: m.name || m.model_name,
    model_name: m.model_name,
    base_url: provider.base_url ?? "",
    api_key: "",
    context_limit: m.context_limit != null ? String(m.context_limit) : "",
    temperature: String(m.temperature),
    timeout: String(m.timeout),
    max_retries: String(m.max_retries),
  };
}

export default function ModelCreateWizard({
  open,
  onClose,
  onCustom,
}: {
  open: boolean;
  onClose: () => void;
  /** 「自定义」：交给外层打开原来那张手填表单 */
  onCustom: () => void;
}) {
  const qc = useQueryClient();
  const providersQ = useQuery({
    queryKey: ["model-providers"],
    queryFn: apiListProviders,
    enabled: open,
  });

  const [provider, setProvider] = useState<ModelProvider | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  /** 展开了哪些模型（展开后才能改它的具体配置） */
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  /** 每个模型被改过的值（没改的字段不进 overrides，交给预设） */
  const [edits, setEdits] = useState<Record<string, ModelEdit>>({});

  const providers = providersQ.data ?? [];

  /** 选服务商：默认全选它的模型（多数人就是要全配上），并把 base_url 带出来 */
  const chooseProvider = (p: ModelProvider) => {
    setProvider(p);
    setBaseUrl(p.base_url ?? "");
    const all: Record<string, boolean> = {};
    for (const m of p.models ?? []) all[m.model_name] = true;
    setPicked(all);
  };

  const chosen = useMemo(
    () => (provider?.models ?? []).filter((m) => picked[m.model_name]),
    [provider, picked]
  );
  /** 模型名候选：这家服务商支持的模型（主行标识、副行展示名、右侧上下文） */
  const modelNameOptions: ComboOption[] = useMemo(
    () =>
      (provider?.models ?? []).map((x) => ({
        value: x.model_name,
        label: x.label,
        desc: x.context_limit
          ? `上下文 ${x.context_limit.toLocaleString()}`
          : undefined,
      })),
    [provider]
  );

  const createM = useMutation({
    mutationFn: () =>
      apiCreateModelsFromPreset({
        provider: provider!.provider,
        api_key: apiKey.trim() || null,
        base_url: baseUrl.trim() || null,
        models: buildModels(),
      }),
    onSuccess: (res) => {
      if (res.conflicts.length) {
        toast.error(
          `有 ${res.conflicts.length} 个模型没建成（名称被占用：${res.conflicts
            .map((c) => c.name)
            .join("、")}），改成别的名字再试一次。`
        );
      } else {
        toast.success(
          res.created.length === 1
            ? `已创建 ${res.created[0].name}`
            : `已创建 ${res.created.length} 个模型预设`
        );
      }
      qc.invalidateQueries({ queryKey: ["models"] });
      if (!res.conflicts.length) {
        setProvider(null);
        setApiKey("");
        setPicked({});
        setEdits({});
        onClose();
      }
    },
    onError: (e: Error) => toast.error("创建失败：" + e.message),
  });

  const toggle = (m: ModelProviderModel) =>
    setPicked((p) => ({ ...p, [m.model_name]: !p[m.model_name] }));

  /** 展开时用预设值初始化编辑区（用户改哪个就变哪个） */
  const toggleExpand = (m: ModelProviderModel) => {
    const next = !expanded[m.model_name];
    setExpanded((s) => ({ ...s, [m.model_name]: next }));
    if (next && !edits[m.model_name] && provider) {
      setEdits((s) => ({ ...s, [m.model_name]: initEdit(m, provider) }));
    }
  };

  const setEdit = (key: string, field: keyof ModelEdit, value: unknown) =>
    setEdits((s) => ({
      ...s,
      [key]: {
        ...EMPTY_EDIT,
        ...(s[key] ?? {}),
        [field]: value,
      },
    }));

  /** 数字输入框的值 → 数字；空/非法 → undefined（不写脏数据进库） */
  const num = (v: string): number | undefined => {
    const t = (v ?? "").trim();
    if (!t) return undefined;
    const n = Number(t);
    return Number.isFinite(n) ? n : undefined;
  };

  /**
   * 组装提交给接口的**完整配置**。
   *
   * 预设只负责"预填"：这里提交的一切都是用户眼前看到的那份表单值——包括
   * 用户改了价格后我们自己写的那一段价格。服务端照单全收，不做二次推断。
   */
  const buildModels = (): ModelSpec[] =>
    chosen.map((m) => {
      const e = edits[m.model_name];
      return {
        name: (e?.name ?? m.name ?? m.model_name).trim(),
        provider: provider?.provider ?? "",
        model_name: (e?.model_name ?? m.model_name).trim(),
        base_url: (e?.base_url ?? baseUrl).trim() || null,
        api_key: (e?.api_key ?? "").trim() || apiKey.trim() || null,
        // 没展开过的模型 = 用户没动过 → 用预设值兜底（不能因为"没编辑"就把
        // 预设给的默认值丢掉，落到库里的会是空的）
        context_limit: num(e?.context_limit ?? "") ?? m.context_limit ?? null,
        temperature: num(e?.temperature ?? "") ?? m.temperature,
        timeout: num(e?.timeout ?? "") ?? m.timeout,
        max_retries: num(e?.max_retries ?? "") ?? m.max_retries,
        // 价格不在这里配：统一到列表里的「价格」弹窗（可配多段错峰价）
        prices: [],
      };
    });

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={760}
      title={provider ? `新建模型 · ${provider.label}` : "新建模型"}
      footer={
        provider ? (
          <>
            <button
              type="button"
              onClick={() => setProvider(null)}
              className="rounded-lg border border-line bg-panel px-4 py-[7px] text-[13px] font-medium text-ink-muted shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-all duration-150 hover:bg-surface hover:text-ink-strong active:scale-[0.98]"
            >
              换一个服务商
            </button>
            <button
              type="button"
              disabled={!chosen.length || createM.isPending}
              onClick={() => createM.mutate()}
              className="rounded-lg bg-brand-gradient px-4 py-[7px] text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
            >
              {createM.isPending && (
                <Loader2 className="mr-1 inline h-3 w-3 animate-spin" />
              )}
              创建 {chosen.length} 个模型
            </button>
          </>
        ) : (
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
              onClick={onCustom}
              className="rounded-lg border border-line bg-panel px-4 py-[7px] text-[13px] font-medium text-ink-muted shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-all duration-150 hover:bg-surface hover:text-ink-strong active:scale-[0.98]"
            >
              自定义
            </button>
          </>
        )
      }
    >
      {providersQ.isLoading && (
        <div className="flex items-center gap-2 py-6 text-[13px] text-ink-muted">
          <Loader2 className="h-4 w-4 animate-spin" />
          加载中…
        </div>
      )}

      {/* ① 选服务商 */}
      {!provider && !providersQ.isLoading && (
        <div className="space-y-2">
          <p className="text-[12.5px] leading-5 text-ink-muted">
            选一家服务商：地址、上下文长度这些都是产品配好的，你只需要填 API
            Key（价格稍后在列表的「价格」里配）。
          </p>
          {providers.map((p) => (
            <button
              key={p.provider}
              type="button"
              onClick={() => chooseProvider(p)}
              className="flex w-full items-center justify-between gap-3 rounded-card border border-line-soft px-3.5 py-3 text-left transition-colors hover:border-brand/50 hover:bg-surface"
            >
              <div className="min-w-0">
                <div className="text-[13px] font-medium text-ink">{p.label}</div>
                <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[12px] text-ink-muted">
                  <span className="font-mono">{p.base_url || "—"}</span>
                  <span>{(p.models ?? []).length} 个模型</span>
                </div>
              </div>
              <span className="shrink-0 text-[12px] text-brand">选择</span>
            </button>
          ))}
          {!providers.length && (
            <div className="py-6 text-center text-[13px] text-ink-faint">
              这个安装包没有内置服务商预设，用「自定义」手填吧
            </div>
          )}
        </div>
      )}

      {/* ②③ 填 Key + 勾模型 */}
      {provider && (
        <div className="space-y-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <label className="block text-[12.5px] font-medium text-ink">
                API Key
              </label>
              <TextInput
                value={apiKey}
                onChange={setApiKey}
                placeholder={provider.label + " 的 API Key"}
              />
              <p className="text-[11.5px] leading-[1.5] text-ink-faint">
                选中的这些模型共用这一个 Key；先留空也行，之后在列表里补。
              </p>
            </div>
            <div className="space-y-1">
              <label className="block text-[12.5px] font-medium text-ink">
                Base URL
              </label>
              <TextInput value={baseUrl} onChange={setBaseUrl} />
              <p className="text-[11.5px] leading-[1.5] text-ink-faint">
                预设值，可改成自建网关的地址。
              </p>
            </div>
          </div>

          <div>
            <div className="mb-1.5 flex items-center justify-between gap-3">
              <span className="text-[12.5px] font-medium text-ink">
                配置哪些模型
              </span>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() =>
                    setPicked(
                      Object.fromEntries(
                        (provider.models ?? []).map((m) => [m.model_name, true])
                      )
                    )
                  }
                  className="text-[12px] text-brand hover:underline"
                >
                  全选
                </button>
                <span className="text-ink-faint">/</span>
                <button
                  type="button"
                  onClick={() => setPicked({})}
                  className="text-[12px] text-brand hover:underline"
                >
                  全不选
                </button>
              </div>
            </div>

            <div className="divide-y divide-line-soft rounded-card border border-line-soft">
              {(provider.models ?? []).map((m) => (
                <div key={m.model_name} className="px-3 py-2.5">
                  <div className="flex items-start gap-2.5">
                    <input
                      type="checkbox"
                      checked={!!picked[m.model_name]}
                      onChange={() => toggle(m)}
                      className="mt-[3px] h-3.5 w-3.5 shrink-0 accent-[var(--brand)]"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                        <span className="font-mono text-[12.5px] text-ink">
                          {m.model_name}
                        </span>
                        {m.label && m.label !== m.model_name && (
                          <span className="text-[12px] text-ink-muted">
                            {m.label}
                          </span>
                        )}
                        <span className="text-[12px] text-ink-muted">
                          上下文{" "}
                          {m.context_limit
                            ? m.context_limit.toLocaleString()
                            : "未标注"}
                        </span>
                      </div>
                    </div>
                    <button
                      type="button"
                      onClick={() => toggleExpand(m)}
                      title={expanded[m.model_name] ? "收起配置" : "展开配置"}
                      className="mt-[1px] shrink-0 rounded p-1 text-ink-muted transition-colors hover:bg-surface hover:text-ink"
                    >
                      {expanded[m.model_name] ? (
                        <ChevronDown className="h-3.5 w-3.5" />
                      ) : (
                        <ChevronRight className="h-3.5 w-3.5" />
                      )}
                    </button>
                  </div>
                  {expanded[m.model_name] && (
                    <div className="mt-2 ml-[22px] space-y-2.5 rounded-lg border border-line-soft bg-surface/50 px-3 py-2.5">
                      {/* 模型服务商是服务商级的（第一步选的），这里只作说明：
                          想换服务商请点底部「换一个服务商」——那样这一个服务商的
                          模型会整体跟着换。 */}
                      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11.5px] text-ink-muted">
                        <span>
                          模型服务商：
                          <span className="font-mono">{provider.provider}</span>
                        </span>
                        <span className="text-ink-faint">
                          服务商级的 Key / Base URL 在上方，改这里只影响这个模型
                        </span>
                      </div>
                      {/* 全量配置：和「自定义」那张表单是同一套字段 */}
                      <div className="grid grid-cols-1 gap-x-3 gap-y-2 sm:grid-cols-2">
                        <div>
                          <label className="mb-1 block text-[11.5px] text-ink-muted">
                            名称
                          </label>
                          <TextInput
                            value={edits[m.model_name]?.name ?? ""}
                            onChange={(v) => setEdit(m.model_name, "name", v)}
                            placeholder={m.model_name}
                          />
                        </div>
                        <div>
                          <label className="mb-1 block text-[11.5px] text-ink-muted">
                            模型名
                          </label>
                          {/* 候选 = 这家服务商支持的模型；仍可自由输入（预设没收录的
                              模型也能用） */}
                          <ComboInput
                            value={edits[m.model_name]?.model_name ?? ""}
                            onChange={(v) =>
                              setEdit(m.model_name, "model_name", v)
                            }
                            options={modelNameOptions}
                            onClear={() => setEdit(m.model_name, "model_name", "")}
                            placeholder={m.model_name}
                            emptyText="没有匹配的候选，可直接手填模型名"
                          />
                        </div>
                        <div>
                          <label className="mb-1 block text-[11.5px] text-ink-muted">
                            API Key（留空 = 用服务商级那个）
                          </label>
                          <PasswordInput
                            value={edits[m.model_name]?.api_key ?? ""}
                            onChange={(v) => setEdit(m.model_name, "api_key", v)}
                            placeholder="可留空"
                          />
                        </div>
                        <div>
                          <label className="mb-1 block text-[11.5px] text-ink-muted">
                            Base URL
                          </label>
                          <TextInput
                            value={edits[m.model_name]?.base_url ?? ""}
                            onChange={(v) => setEdit(m.model_name, "base_url", v)}
                            placeholder={provider.base_url || "https://…"}
                          />
                        </div>
                      </div>
                      <div className="grid grid-cols-2 gap-x-3 gap-y-2 sm:grid-cols-4">
                        <div>
                          <label className="mb-1 block text-[11.5px] text-ink-muted">
                            上下文上限
                          </label>
                          <TextInput
                            value={edits[m.model_name]?.context_limit ?? ""}
                            onChange={(v) =>
                              setEdit(m.model_name, "context_limit", v)
                            }
                            placeholder="未标注"
                          />
                        </div>
                        <div>
                          <label className="mb-1 block text-[11.5px] text-ink-muted">
                            温度
                          </label>
                          <TextInput
                            value={edits[m.model_name]?.temperature ?? ""}
                            onChange={(v) =>
                              setEdit(m.model_name, "temperature", v)
                            }
                          />
                        </div>
                        <div>
                          <label className="mb-1 block text-[11.5px] text-ink-muted">
                            超时（秒）
                          </label>
                          <TextInput
                            value={edits[m.model_name]?.timeout ?? ""}
                            onChange={(v) =>
                              setEdit(m.model_name, "timeout", v)
                            }
                          />
                        </div>
                        <div>
                          <label className="mb-1 block text-[11.5px] text-ink-muted">
                            重试次数
                          </label>
                          <TextInput
                            value={edits[m.model_name]?.max_retries ?? ""}
                            onChange={(v) =>
                              setEdit(m.model_name, "max_retries", v)
                            }
                          />
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>

          <p className="text-[11.5px] leading-[1.5] text-ink-faint">
            价格稍后在模型列表的「价格」里配置（那里能配多段错峰价：比如低峰价
            + 高峰价）。
          </p>

          {provider.docs_url && (
            <a
              href={provider.docs_url}
              target="_blank"
              rel="noreferrer"
              className="inline-block text-[12px] text-brand hover:underline"
            >
              {provider.label} 官方文档
            </a>
          )}
        </div>
      )}
    </Modal>
  );
}
