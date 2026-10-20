import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import {
  apiListDelegatees,
  apiListModels,
  apiListPlugins,
  apiUpdateDelegatee,
} from "../api/client";
import type {
  DelegateeConfig,
  DelegateeUpdate,
  LibraryPlugin,
  ModelProfile,
} from "../api/types";
import { Hint, Section, Stack } from "./ui/Page";
import { SelectField, ToggleField } from "./ui/Form";
import { toast } from "./ui/Toast";
import { cn } from "../lib/utils";

/** 单个 delegatee 的本地编辑态（按 name 索引） */
interface EditState {
  enabled: boolean;
  llm_profile_id: string | null;
  plugins: string[];
  /** 透传保存，UI 不编辑这一层（工具级白名单走高级接口） */
  tools: string[];
}

/**
 * 子 agent（delegatee）设置：每个 delegatee 独立的模型 + 插件白名单。
 *
 * 与「核心 agent」等统一设置段不同，delegatee 配置落在独立的 delegatee_configs
 * 表（不是 settings.yaml），所以这里自带数据获取与逐项的保存按钮，不复用弹窗
 * 底部的统一「保存」。改完立即生效——下一轮对话该子 agent 就用上新配置。
 */
export function DelegateeSettings() {
  const qc = useQueryClient();
  const listQ = useQuery({ queryKey: ["delegatees"], queryFn: apiListDelegatees });
  const modelsQ = useQuery({ queryKey: ["models"], queryFn: apiListModels });
  const pluginsQ = useQuery({ queryKey: ["plugins"], queryFn: apiListPlugins });

  const [edits, setEdits] = useState<Record<string, EditState>>({});

  // 数据回来后灌进本地编辑态（保持「改哪改哪、保存时整体提交」的一致风格）
  useEffect(() => {
    if (!listQ.data) return;
    const init: Record<string, EditState> = {};
    for (const d of listQ.data.delegatees) {
      init[d.name] = {
        enabled: d.enabled,
        llm_profile_id: d.llm_profile_id,
        plugins: d.plugins ?? [],
        tools: d.tools ?? [],
      };
    }
    setEdits(init);
  }, [listQ.data]);

  const models = modelsQ.data ?? [];
  const plugins = (pluginsQ.data?.plugins ?? []) as LibraryPlugin[];
  const delegatees = listQ.data?.delegatees ?? [];

  const saveM = useMutation({
    mutationFn: ({ name, body }: { name: string; body: DelegateeUpdate }) =>
      apiUpdateDelegatee(name, body),
    onSuccess: () => {
      toast.success("已保存（立即生效，下一轮对话即使用新配置）");
      qc.invalidateQueries({ queryKey: ["delegatees"] });
    },
    onError: (e: Error) => toast.error("保存失败：" + e.message),
  });

  if (listQ.isLoading) {
    return (
      <div className="flex items-center justify-center py-16 text-ink-muted">
        <Loader2 className="h-5 w-5 animate-spin" />
      </div>
    );
  }

  const patch = (name: string, k: keyof EditState, v: unknown) =>
    setEdits((p) => ({ ...p, [name]: { ...(p[name] as EditState), [k]: v } }));

  const togglePlugin = (name: string, p: string, on: boolean) =>
    setEdits((prev) => {
      const cur = prev[name]?.plugins ?? [];
      const next = on ? [...cur, p] : cur.filter((x) => x !== p);
      return { ...prev, [name]: { ...(prev[name] as EditState), plugins: next } };
    });

  return (
    <Stack>
      <p className="text-[12px] leading-6 text-ink-muted">
        每个子 agent（delegatee）可单独指定模型与可用插件，独立于主 agent。改完立即生效，
        无需重启——下一轮对话里该子 agent 就会用上新的模型 / 插件白名单。
      </p>

      {delegatees.length === 0 && (
        <Hint title="暂无子 agent">后端还没注册任何 delegatee 类型。</Hint>
      )}

      {delegatees.map((d: DelegateeConfig) => {
        const e = edits[d.name];
        if (!e) return null;
        const dirty =
          e.enabled !== d.enabled ||
          e.llm_profile_id !== d.llm_profile_id ||
          JSON.stringify(e.plugins) !== JSON.stringify(d.plugins ?? []) ||
          JSON.stringify(e.tools) !== JSON.stringify(d.tools ?? []);
        return (
          <Section key={d.name} title={d.name} extra={d.description}>
            <Stack>
              <ToggleField
                label="启用该子 agent"
                extra="关闭后主循环不再把任务委托给它"
                checked={e.enabled}
                onChange={(v) => patch(d.name, "enabled", v)}
              />

              <SelectField
                label="模型（LLM 预设）"
                extra={
                  d.profile_name
                    ? `当前绑定：${d.profile_name}`
                    : "默认（与主 agent 同款模型）"
                }
                value={e.llm_profile_id ?? ""}
                onChange={(v) => patch(d.name, "llm_profile_id", v || null)}
                options={[
                  { value: "", label: "默认（与主 agent 同款）" },
                  ...models.map((m: ModelProfile) => ({
                    value: m.id,
                    label: `${m.name}（${m.provider} / ${m.model_name}）`,
                  })),
                ]}
              />

              <div>
                <div className="mb-1.5 text-[12.5px] font-medium text-ink">
                  可用插件（白名单）
                </div>
                <p className="mb-2 text-[11.5px] leading-[1.5] text-ink-faint">
                  只勾选的插件会出现在该子 agent 的工具表里；留空 = 不限制（用宿主全部工具）。
                </p>
                {plugins.length === 0 ? (
                  <p className="text-[12px] text-ink-faint">（暂无插件）</p>
                ) : (
                  <div className="grid grid-cols-2 gap-1.5">
                    {plugins.map((p: LibraryPlugin) => {
                      const on = e.plugins.includes(p.dirname);
                      return (
                        <label
                          key={p.dirname}
                          className={cn(
                            "flex cursor-pointer items-center gap-2 rounded-[7px] border px-2.5 py-1.5 text-[12px] transition-colors",
                            on
                              ? "border-brand/40 bg-brand/10 text-ink-strong"
                              : "border-line bg-panel text-ink-muted hover:bg-surface"
                          )}
                        >
                          <input
                            type="checkbox"
                            checked={on}
                            onChange={(ev) =>
                              togglePlugin(d.name, p.dirname, ev.target.checked)
                            }
                            className="accent-brand"
                          />
                          <span className="min-w-0 flex-1 truncate">
                            {p.name}
                            {p.name !== p.dirname && (
                              <span className="ml-1 text-ink-faint">
                                ({p.dirname})
                              </span>
                            )}
                          </span>
                        </label>
                      );
                    })}
                  </div>
                )}
              </div>

              <div className="flex justify-end">
                <button
                  type="button"
                  disabled={!dirty || saveM.isPending}
                  onClick={() =>
                    saveM.mutate({
                      name: d.name,
                      body: {
                        enabled: e.enabled,
                        llm_profile_id: e.llm_profile_id,
                        plugins: e.plugins,
                        tools: e.tools,
                      },
                    })
                  }
                  className="flex items-center gap-1.5 rounded-lg bg-brand-gradient px-4 py-[7px] text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {saveM.isPending && (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  )}
                  保存
                </button>
              </div>
            </Stack>
          </Section>
        );
      })}
    </Stack>
  );
}
