/**
 * 模型选择器（对话输入框下方右侧）。
 *
 * **改的是 agent 的绑定，不是逐会话的。**
 *
 * 这一点必须说清楚，因为它决定了界面怎么写：选了新模型，对该 agent 的**所有**
 * 会话都生效——核心 agent 尤其如此，所有会话都在它身上。所以这里不是一个
 * "本次对话用什么模型"的临时开关，而是改配置。
 *
 * 服务端改完会重装配该 agent（模型是 build 期读进来的，不改就不会生效），并把
 * 结果回报回来。装配失败时这里显示原因，而不是静默不变——"点了一下没反应"是最
 * 难反馈的问题。
 *
 * 这里只做"选一个模型"。解绑（回到纯索引模式）不在这个下拉里——它是个
 * 少用得多的动作，放在「智能体」页的 agent 配置里做，别占用对话时最常点
 * 的那个下拉。
 */
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Check, ChevronDown, Loader2, Plus, Sparkles } from "lucide-react";
import { cn } from "../lib/utils";
import {
  apiGetAgentModel,
  apiListModels,
  apiSetAgentModel,
} from "../api/client";
import ModelCreateWizard from "./ModelCreateWizard";
import { ModelEditModal } from "./ModelEditModal";
import { toast } from "./ui/Toast";

const {
  Root: MenuRoot,
  Trigger: MenuTrigger,
  Content: MenuContent,
  Item: MenuItem,
} = DropdownMenu;

export function ModelPicker({ agentId }: { agentId: string }) {
  const qc = useQueryClient();
  const bindingQ = useQuery({
    queryKey: ["agent-model", agentId],
    queryFn: () => apiGetAgentModel(agentId),
  });
  const modelsQ = useQuery({ queryKey: ["models"], queryFn: apiListModels });
  const [open, setOpen] = useState(false);
  /** 一个模型都还没有时，下拉里直接给「新建模型」——把人送到向导，而不是
      只丢一句"去模型管理页配一个"。 */
  const [wizardOpen, setWizardOpen] = useState(false);
  /** 向导里的「自定义」→ 同一张手填表单（与模型管理页一致） */
  const [formOpen, setFormOpen] = useState(false);

  /** 先让下拉收完（Radix 的关闭有动画），再开弹窗，避免两层浮层打架 */
  const openWizard = () => {
    setOpen(false);
    setTimeout(() => setWizardOpen(true), 120);
  };
  const openForm = () => {
    setWizardOpen(false);
    setTimeout(() => setFormOpen(true), 120);
  };

  const setM = useMutation({
    mutationFn: (llmProfileId: string) =>
      apiSetAgentModel(agentId, llmProfileId),
    onSuccess: (d) => {
      if (d.running) {
        toast.success("已切换模型，已重新装配");
      } else {
        // 绑定写进去了，但 agent 没起来：必须说清楚，否则用户以为没生效
        toast.error(`配置已保存，但 agent 未重新装配：${d.reason ?? "未知原因"}`);
      }
      qc.invalidateQueries({ queryKey: ["agent-model", agentId] });
      qc.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: (e: Error) => toast.error("切换失败：" + e.message),
  });

  // 关掉菜单：选中后立即收起，别挡着输入框
  useEffect(() => {
    if (setM.isSuccess) setOpen(false);
  }, [setM.isSuccess]);

  const bound = bindingQ.data?.llm_profile_id ?? null;
  const boundName = bindingQ.data?.profile_name;
  const state = bindingQ.data?.state;

  const label = boundName
    ? boundName
    : state === "invalid"
    ? "模型已失效"
    : "未绑定模型";

  return (
    <>
      <MenuRoot open={open} onOpenChange={setOpen}>
        <MenuTrigger asChild>
          <button
            type="button"
            disabled={setM.isPending}
            title={
              boundName
                ? `当前模型：${boundName}（改这里会改这个 agent 的绑定）`
                : "未绑定模型，点击选择一个"
            }
            className={cn(
              "flex h-[26px] max-w-[150px] items-center gap-1 rounded-full border px-2 text-[11.5px] font-medium transition-colors duration-150",
              boundName
                ? "border-line-soft bg-surface/70 text-ink-muted hover:border-brand/40 hover:text-brand"
                : "border-amber-500/40 bg-amber-500/10 text-amber-700 hover:border-amber-500/70 dark:text-amber-300"
            )}
          >
            {setM.isPending ? (
              <Loader2 className="h-3 w-3 shrink-0 animate-spin" />
            ) : (
              <Sparkles className="h-3 w-3 shrink-0" />
            )}
            <span className="truncate">{label}</span>
            <ChevronDown className="h-3 w-3 shrink-0 opacity-60" />
          </button>
        </MenuTrigger>

          <MenuContent
            side="top"
            align="end"
            sideOffset={6}
            className="z-[1700] max-h-[320px] min-w-[200px] overflow-auto rounded-[10px] border border-line-soft bg-panel p-1 shadow-pop"
          >
          {(modelsQ.data ?? []).map((m) => (
            <MenuItem
              key={m.id}
              onSelect={() => setM.mutate(m.id)}
              data-selected={bound === m.id}
              className={cn(
                "flex cursor-pointer items-center gap-2 rounded-[7px] px-2 py-1.5 text-[12px] outline-none transition-colors duration-100",
                "data-[highlighted]:bg-brand/15",
                bound === m.id
                  ? "bg-brand/10 font-medium text-brand"
                  : "text-ink hover:text-ink-strong"
              )}
            >
              <Check
                className={cn(
                  "h-3 w-3 shrink-0",
                  bound === m.id ? "opacity-100" : "opacity-0"
                )}
              />
              <span className="min-w-0 flex-1 truncate">{m.name}</span>
            </MenuItem>
          ))}

          {!(modelsQ.data ?? []).length && (
            <>
              <div className="px-2 py-1.5 text-[11.5px] text-ink-faint">
                还没有模型预设，先建一个
              </div>
              <MenuItem
                onSelect={openWizard}
                className="flex cursor-pointer items-center gap-2 rounded-[7px] px-2 py-1.5 text-[12px] font-medium text-brand outline-none transition-colors duration-100 data-[highlighted]:bg-brand/15"
              >
                <Plus className="h-3 w-3 shrink-0" />
                <span>新建模型</span>
              </MenuItem>
            </>
          )}
        </MenuContent>
      </MenuRoot>

    {/* 新建模型：与「模型管理」页点「新建模型」是同一套向导与表单 */}
    <ModelCreateWizard
      open={wizardOpen}
      onClose={() => setWizardOpen(false)}
      onCustom={openForm}
    />
    <ModelEditModal open={formOpen} onClose={() => setFormOpen(false)} />
    </>
  );
}
