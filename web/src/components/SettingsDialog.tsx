/**
 * 设置对话框：本机可调配置（写回 settings.yaml，与 config.yaml 元配置分开）。
 *
 * **为什么是弹窗而不是整页**：设置是低频操作，而"整页设置"在侧栏里要占一个
 * 永久位置、还要一个返回动作。现在它收在侧栏底部的齿轮里——四项设置各开一个
 * 弹窗，用完即走，不打断当前上下文。
 *
 * 一个弹窗只展示**一段**（进来时指定），而不是四段堆在一个长页面里：
 * 四段之间没有共同的操作上下文，堆在一起只是让人上下滚动找自己要看的那段。
 *
 * 状态刻意不用表单实例（antd 已移除）：整份配置本来就是一个对象，改哪段改哪段，
 * 保存时整体提交。用表单反而要把读写塞进 setFieldsValue / getFieldsValue 的
 * 生命周期，状态散在两处。
 */
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { Hint, Section, Stack } from "./ui/Page";
import { NumField, SelectField, TextField, ToggleField } from "./ui/Form";
import { Modal } from "./ui/Modal";
import { toast } from "./ui/Toast";
import { CoreAgentEditor } from "./CoreAgentEditor";
import { DelegateeSettings } from "./DelegateeSettings";
import { MySpaceSettings } from "./MySpaceSettings";

import {
  apiGetSettings,
  apiReloadSettings,
  apiResetBreaker,
  apiUpdateSettings,
} from "../api/client";
import {
  loadMention,
  saveMention,
  type MentionConfig,
  type MentionItem,
} from "../lib/mention";
import type {
  A2ASettings,
  ContextSettings,
  CoreAgentSettings,
  PromptDumpSettings,
  SettingsInput,
} from "../api/types";

/** 可单独打开的设置段 */
export type SettingsSection =
  | "core"
  | "context"
  | "a2a"
  | "debug"
  | "delegatee"
  | "mention"
  | "space";

/** 侧栏弹出菜单用它渲染条目（label/描述集中在这里，菜单与标题不会写错） */
export const SETTINGS_SECTIONS: {
  key: SettingsSection;
  label: string;
  desc: string;
}[] = [
  {
    key: "space",
    label: "我的空间",
    desc: "文件浏览与 @ 检索用的那个目录",
  },
  {
    key: "core",
    label: "核心 agent",
    desc: "新建会话默认落到谁，以及它的人设",
  },
  { key: "context", label: "上下文", desc: "长任务下上下文怎么治理" },
  { key: "a2a", label: "协作", desc: "调对端时的超时与熔断" },
  { key: "debug", label: "调试", desc: "排查用的 prompt 落盘" },
  {
    key: "delegatee",
    label: "子 agent",
    desc: "每个子 agent 独立的模型与插件",
  },
  {
    key: "mention",
    label: "输入 @ 快捷项",
    desc: "在聊天框 @ 唤出、可插入消息的短语",
  },
];

/** 表单即整份设置：各段与后端一一对应，保存时整体提交 */
interface SettingsForm {
  a2a: A2ASettings;
  prompt_dump: PromptDumpSettings;
  context: ContextSettings;
}

type SectionKey = keyof SettingsForm;


const DEFAULT_A2A: A2ASettings = {
  request_timeout: 15,
  task_timeout: 120,
  poll_interval: 3,
  cancel_on_timeout: true,
  breaker_enabled: true,
  breaker_threshold: 3,
  breaker_cooldown: 60,
};

const DEFAULT_CONTEXT: ContextSettings = {
  enabled: true,
  externalize_min_chars: 5000,
  never_externalize: "",
  retain_days: 7,
  preview_head: 400,
  preview_tail: 200,
  dir: "~/.keeper/context-store",
  compact_enabled: true,
  model_context_limit: 64000,
  max_output_tokens: 16384,
  compact_ratio: 0.7,
  compact_min_steps: 20,
  compact_min_ratio: 0.7,
  compact_min_interval: 5,
  keep_recent_steps: 6,
  compact_max_chars: 1500,
  observation_limit: 3500,
  max_steps: 30,
  read_budget: 6000,
  max_ctx_recall: 5,
  chars_per_token: 2.5,
  pin_recent_reads: 2,
  capability_preload: "auto",
  cap_small_limit: 6000,
  tool_group_min: 3,
  tool_parallel: 0,
  tool_parallel_max: 5,
};

const DEFAULT_DUMP: PromptDumpSettings = {
  enabled: false,
  dir: "~/.keeper/prompt-dump",
  min_prompt_tokens: 0,
  retain_days: 7,
};

/** 输入 @ 快捷项：纯前端 localStorage 配置，不依赖后端 settings */
function MentionSettings() {
  const [cfg, setCfg] = useState<MentionConfig>(() => loadMention());
  const patchItem = (i: number, k: keyof MentionItem, v: string) =>
    setCfg((p) => ({
      ...p,
      items: p.items.map((it, idx) => (idx === i ? { ...it, [k]: v } : it)),
    }));
  const addItem = () =>
    setCfg((p) => ({
      ...p,
      items: [
        ...p.items,
        { key: "m" + Date.now(), label: "新项", snippet: "" },
      ],
    }));
  const delItem = (i: number) =>
    setCfg((p) => ({ ...p, items: p.items.filter((_, idx) => idx !== i) }));
  const save = () => {
    saveMention(cfg);
    toast.success("已保存（立即生效）");
  };

  const btn =
    "flex items-center gap-1.5 rounded-lg border border-line bg-panel px-3 py-[7px] text-[13px] font-medium text-ink-muted shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-all duration-150 hover:bg-surface hover:text-ink-strong active:scale-[0.98]";

  return (
    <Stack>
      <p className="text-[12px] leading-6 text-ink-muted">
        在聊天输入框输入 <code>@</code> 会弹出下面的快捷项，选中后把对应文本插入消息。
        短语里支持两个占位符，插入时由后端给出的真实绝对路径替换（都不是 <code>~</code> 简写）：
      </p>
      <ul className="ml-4 list-disc text-[12px] leading-6 text-ink-muted">
        <li>
          <code>{"{user_space}"}</code>：我的空间的绝对路径（来自 settings.yaml 的 user_space.path，单空间）；
        </li>
        <li>
          <code>{"{session_path}"}</code>：当前会话私有工作目录的绝对路径（agent_space）。
        </li>
      </ul>
      <p className="text-[12px] leading-6 text-ink-muted">
        「工作空间根」仅在没有活跃会话（还没发第一条）时给 <code>{"{session_path}"}</code> 作兜底。
      </p>

      <Section title="工作空间根（{session_path} 用）">
        <Stack>
          <TextField
            label="根路径"
            placeholder="~/.keeper/workspace/session/"
            value={cfg.root}
            onChange={(v) => setCfg((p) => ({ ...p, root: v }))}
          />
        </Stack>
      </Section>

      <Section title="快捷项">
        <Stack>
          {cfg.items.map((it, i) => (
            <div key={it.key} className="rounded-lg border border-line-soft p-2.5">
              <div className="mb-1.5 flex items-center gap-2">
                <span className="shrink-0 text-[12px] text-ink-muted">@</span>
                <input
                  value={it.label}
                  onChange={(e) => patchItem(i, "label", e.target.value)}
                  placeholder="显示名"
                  className="min-w-0 flex-1 rounded-md border border-line bg-panel px-2 py-1 text-[12.5px] text-ink outline-none focus:border-brand"
                />
                <button
                  type="button"
                  onClick={() => delItem(i)}
                  className={btn + " px-2.5 text-red-600 dark:text-red-300"}
                >
                  删除
                </button>
              </div>
              <textarea
                value={it.snippet}
                onChange={(e) => patchItem(i, "snippet", e.target.value)}
                placeholder="插入的文本（可用 {session_path}）"
                rows={2}
                className="w-full resize-none rounded-md border border-line bg-panel px-2 py-1 text-[12.5px] text-ink outline-none focus:border-brand"
              />
            </div>
          ))}
          <button type="button" onClick={addItem} className={btn}>
            ＋ 新增
          </button>
        </Stack>
      </Section>

      <div className="flex justify-end">
        <button
          type="button"
          onClick={save}
          className={
            btn +
            " bg-brand-gradient text-white hover:brightness-110 active:scale-95"
          }
        >
          保存
        </button>
      </div>
    </Stack>
  );
}

export function SettingsDialog({
  section,
  onClose,
}: {
  /** 打开哪一段；为 null 时不渲染 */
  section: SettingsSection | null;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const settingsQ = useQuery({ queryKey: ["settings"], queryFn: apiGetSettings });

  const [cfg, setCfg] = useState<SettingsForm>({
    a2a: DEFAULT_A2A,
    prompt_dump: DEFAULT_DUMP,
    context: DEFAULT_CONTEXT,
  });

  useEffect(() => {
    if (settingsQ.data) {
      setCfg({
        a2a: settingsQ.data.a2a ?? DEFAULT_A2A,
        prompt_dump: settingsQ.data.prompt_dump ?? DEFAULT_DUMP,
        context: settingsQ.data.context ?? DEFAULT_CONTEXT,
      });
    }
  }, [settingsQ.data]);

  /** 改某一段里的某个键 */
  const patch = (sec: SectionKey, key: string, v: unknown) =>
    setCfg((p) => ({ ...p, [sec]: { ...(p[sec] as any), [key]: v } }));

  const saveM = useMutation({
    // 兜底：万一某段还是空的，也别把整段提交成空对象
    mutationFn: () =>
      apiUpdateSettings({
        a2a: { ...DEFAULT_A2A, ...cfg.a2a },
        prompt_dump: { ...DEFAULT_DUMP, ...cfg.prompt_dump },
        context: { ...DEFAULT_CONTEXT, ...cfg.context },
        // 核心 agent 不做默认值合并：它的"空"是有意义的值（= 走回落规则）

      } as SettingsInput),
    onSuccess: (d) => {
      toast.success("已保存（立即生效，无需重启）");
      setCfg({
        a2a: d.a2a,
        prompt_dump: d.prompt_dump,
        context: d.context,
      });
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (e: Error) => toast.error("保存失败：" + e.message),
  });

  const reloadM = useMutation({
    mutationFn: () => apiReloadSettings(),
    onSuccess: (d) => {
      toast.success("已重新加载");
      setCfg({
        a2a: d.a2a,
        prompt_dump: d.prompt_dump,
        context: d.context,
      });
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (e: Error) => toast.error("重新加载失败：" + e.message),
  });

  const resetM = useMutation({
    mutationFn: (peer?: string) => apiResetBreaker(peer),
    onSuccess: (d) => {
      toast.success(`已重置 ${d.reset ?? 0} 个对端的熔断状态`);
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (e: Error) => toast.error("重置失败：" + e.message),
  });

  const breakers = settingsQ.data?.a2a_breakers ?? [];
  /** 配置没读出来（后端返回 error）：此时表单里是默认值，必须禁掉保存 */
  const settingsError = settingsQ.data?.error;

  const c = cfg.context;
  const a = cfg.a2a;
  const d = cfg.prompt_dump;

  const title =
    SETTINGS_SECTIONS.find((s) => s.key === section)?.label ?? "设置";

  return (
    <Modal
      open={!!section}
      onClose={onClose}
      title={title}
      width={620}
      footer={
        // delegatee / mention / space 段自带保存逻辑（mention 是纯前端 localStorage，
        // space 走自己的「我的空间」端点），不需要弹窗底部的统一「保存 / 重新加载」
        section === "delegatee" || section === "mention" || section === "space" ? (
          <></>
        ) : (
          <>
          <button
            type="button"
            disabled={reloadM.isPending}
            onClick={() => reloadM.mutate()}
            title="绕过界面直接改了 settings.yaml 时用；这里保存会自动生效"
            className="flex items-center gap-1.5 rounded-lg border border-line bg-panel px-4 py-[7px] text-[13px] font-medium text-ink-muted shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-all duration-150 hover:bg-surface hover:text-ink-strong active:scale-[0.98] disabled:opacity-50"
          >
            {reloadM.isPending && (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            )}
            重新加载
          </button>
          <button
            type="button"
            disabled={!!settingsError || saveM.isPending}
            onClick={() => saveM.mutate()}
            className="flex items-center gap-1.5 rounded-lg bg-brand-gradient px-4 py-[7px] text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
          >
            {saveM.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            保存
          </button>
        </>
        )
      }
    >
      {settingsQ.isLoading ? (
        <div className="flex items-center justify-center py-16 text-ink-muted">
          <Loader2 className="h-5 w-5 animate-spin" />
        </div>
      ) : (
        <div className="-mt-1">
          {!!settingsError && (
            <div className="mb-4">
              <Hint tone="danger" title="配置读取失败">
                {settingsError}（已禁用保存，避免把默认值写回去覆盖真实配置）
              </Hint>
            </div>
          )}

          {section === "core" && <CoreAgentEditor />}

          {section === "context" && (
            <Stack>
              <p className="text-[12px] leading-6 text-ink-muted">
                ReAct 每步都把思考与工具结果累加进上下文，长任务下会线性膨胀。这里的策略是
                「先不把大东西放进上下文、实在膨胀了再压早期步骤」——两种手段都会把原文存成
                块，模型可用 <code>read(block_id=&quot;ctx:...&quot;)</code> 追回，信息不丢。
              </p>

              <ToggleField
                label="开启上下文治理"
                extra="关闭后行为与旧版一致：工具输出只做硬截断，不做外部化与压缩"
                checked={!!c.enabled}
                onChange={(v) => patch("context", "enabled", v)}
              />

              <Section title="外部化（工具输出）">
                <Stack>
                  <NumField
                    label="滚动外部化阈值（字符）"
                    extra={
                      "只管「滚动」这一条：这一步已退出「保留最近步数」窗口 且 超过这个长度，才换成引用。" +
                      "刚返回的结果永远完整可见——模型下一步要用它，换预览会漏文件、漏条目。" +
                      "单条超长另由下面的「内联上限」触发（与本项无关）；0 = 不做滚动外部化"
                    }
                    value={c.externalize_min_chars}
                    onChange={(v) => patch("context", "externalize_min_chars", v)}
                    min={0}
                    max={100000}
                    step={500}
                  />
                  <TextField
                    label="豁免名单（永不外部化的工具）"
                    extra={
                      "默认空 = 所有工具一视同仁。支持 task.* 前缀，逗号分隔。" +
                      "两条路径都不豁免：这些工具超内联上限后会直接硬截断（只留头尾）且没有 block_id，" +
                      "所以只填「输出小且必须看全」的工具（如 fs.list_dir）"
                    }
                    placeholder="留空 = 不豁免"
                    value={c.never_externalize}
                    onChange={(v) => patch("context", "never_externalize", v)}
                  />
                </Stack>
              </Section>

              <Section title="单条输出内联上限与步数（决定一轮能涨多大）">
                <Stack>
                  <NumField
                    label="单条输出内联上限（字符）"
                    extra={
                      "超过它：原文落盘，上下文里换成「首尾预览 + block_id」，模型 read(block_id) 可追回全文。" +
                      "仅当外部化不可用时（治理总开关关掉 / 工具在豁免名单 / 写盘失败）才退回硬截断（只留头尾）。" +
                      "调小 = 更多结果变成引用（省上下文，但模型要多 read 一次）；调大 = 更少打断、更容易看全，也更涨"
                    }
                    value={c.observation_limit}
                    onChange={(v) => patch("context", "observation_limit", v)}
                    min={200}
                    max={200000}
                    step={500}
                  />
                  <NumField
                    label="每轮最大步数"
                    extra="ReAct 一轮最多跑多少步，超过就强制总结"
                    value={c.max_steps}
                    onChange={(v) => patch("context", "max_steps", v)}
                    min={1}
                    max={200}
                    step={5}
                  />
                  <NumField
                    label="token 粗估系数（字符/token）"
                    extra="只用于算压缩水位，不参与记账。中文多调小（1.5~2），代码/英文多调大（3~4）"
                    value={c.chars_per_token}
                    onChange={(v) => patch("context", "chars_per_token", v)}
                    min={1}
                    max={10}
                    step={0.5}
                  />
                </Stack>
              </Section>

              <Section title="取回（read / recall）">
                <Stack>
                  <NumField
                    label="read 展开预算（字符）"
                    extra="一次 read 多块时的总字符上限，防止取回时又把上下文撑爆"
                    value={c.read_budget}
                    onChange={(v) => patch("context", "read_budget", v)}
                    min={500}
                    max={200000}
                    step={1000}
                  />
                  <NumField
                    label="recall 返回上下文块上限"
                    extra="recall 额外返回「本轮外部化块」的条数；0 = 不返回"
                    value={c.max_ctx_recall}
                    onChange={(v) => patch("context", "max_ctx_recall", v)}
                    min={0}
                    max={50}
                  />
                  <NumField
                    label="追回内容保活条数"
                    extra="read 回来的内容保持原文的条数。0 = 不保（走出窗口就会被再外部化，可能出现「读了又被压又读」的抖动）；调大更稳但上下文更容易涨"
                    value={c.pin_recent_reads}
                    onChange={(v) => patch("context", "pin_recent_reads", v)}
                    min={0}
                    max={50}
                  />
                </Stack>
              </Section>

              <Section title="工具 / 技能按需加载">
                <Stack>
                  <NumField
                    label="工具组折叠门槛"
                    extra="同一个插件 / MCP server 下的工具达到几个就折成一行：只列工具名、不列参数说明，真正调用时系统会自动把完整定义补进上下文（模型不会因为折叠而不会用）。1 = 从不折叠，等同以前的全量清单"
                    value={c.tool_group_min}
                    onChange={(v) => patch("context", "tool_group_min", v)}
                    min={1}
                    max={100}
                  />
                  <SelectField
                    label="工具并行执行"
                    extra="同一轮里模型连写了多个只读工具时并发执行以降低时延。0=关闭（默认，行为与从前完全一致）；1=只并行只读工具（读文件 / 查询类）；2=再加上插件清单里显式标了 mutating: false 的工具。写类工具与问用户永远串行"
                    value={c.tool_parallel}
                    onChange={(v) =>
                      patch("context", "tool_parallel", Number(v))
                    }
                    options={[
                      { value: 0, label: "0 · 关闭（默认）" },
                      { value: 1, label: "1 · 只并行只读工具" },
                      { value: 2, label: "2 · 只读 + 显式声明无副作用的工具" },
                    ]}
                  />
                  <NumField
                    label="单轮最大并发数"
                    extra="一次最多并发几个工具。防止模型一次写出几十个调用把 MCP server 或数据库连接池打爆"
                    value={c.tool_parallel_max}
                    onChange={(v) => patch("context", "tool_parallel_max", v)}
                    min={1}
                    max={20}
                  />
                  <SelectField
                    label="技能正文预加载策略"
                    extra="每轮开头系统要不要替模型把技能正文取来：off=不预加载，正文只等模型自己按名字读（最纯粹的「概要常驻 + 按需加载」）；keyword=拿技能 keywords 跟问题做包含匹配，命中才取，零成本；llm=让模型看一眼清单挑名字，最准但多一次调用；auto=技能少就全取，多了先 keyword、没命中再判读"
                    value={c.capability_preload}
                    onChange={(v) => patch("context", "capability_preload", v)}
                    options={[
                      {
                        value: "auto",
                        label: "auto（推荐：技能少全取，多了先关键词再判读）",
                      },
                      { value: "off", label: "off（不预加载，纯靠模型自己读）" },
                      { value: "keyword", label: "keyword（关键词匹配，零额外调用）" },
                      { value: "llm", label: "llm（每次判读，最准）" },
                    ]}
                  />
                  <NumField
                    label="「技能少」判定线（token）"
                    extra="auto 模式下：按需技能的正文 token 粗估合计低于这个数就全部取来、连判读都不做——这时省下的 token 抵不过多一次调用。0 = 永不走全量分支"
                    value={c.cap_small_limit}
                    onChange={(v) => patch("context", "cap_small_limit", v)}
                    min={0}
                    max={200000}
                    step={500}
                  />
                </Stack>
              </Section>

              <Section title="外部块与预览">
                <Stack>
                  <NumField
                    label="外部化块保留天数"
                    extra="过期块自动清理（最多每小时扫一次）。外部化会持续产块，且「先外部化、后又被压缩打包」会留下不再被引用的旧块，所以需要回收；0 = 不清理"
                    value={c.retain_days}
                    onChange={(v) => patch("context", "retain_days", v)}
                    min={0}
                    max={365}
                  />
                  <NumField
                    label="预览：开头字符数"
                    extra="留在上下文里的开头部分"
                    value={c.preview_head}
                    onChange={(v) => patch("context", "preview_head", v)}
                    min={0}
                    max={5000}
                    step={100}
                  />
                  <NumField
                    label="预览：结尾字符数"
                    extra="结论、报错常在末尾，值得留一小段"
                    value={c.preview_tail}
                    onChange={(v) => patch("context", "preview_tail", v)}
                    min={0}
                    max={5000}
                    step={100}
                  />
                  <TextField
                    label="外部块目录"
                    placeholder="~/.keeper/context-store"
                    value={c.dir}
                    onChange={(v) => patch("context", "dir", v)}
                  />
                </Stack>
              </Section>

              <Section title="自动压缩（早期步骤）">
                <Stack>
                  <ToggleField
                    label="开启自动压缩"
                    extra="到水位时把最早的一批步骤压成「骨架 + block_id」，头部与最近几步不动（保住前缀缓存）"
                    checked={!!c.compact_enabled}
                    onChange={(v) => patch("context", "compact_enabled", v)}
                  />
                  <NumField
                    label="模型窗口（token）"
                    extra="按所用模型的实际窗口填，用于计算水位"
                    value={c.model_context_limit}
                    onChange={(v) =>
                      patch("context", "model_context_limit", v)
                    }
                    min={1000}
                    max={2000000}
                    step={8000}
                  />
                  <NumField
                    label="单次输出上限（token）"
                    extra="一次回复最多能输出多少 token。0 = 不限制（交给模型默认）。设了之后系统提示词会把这个数字告诉模型，让它把大文件、长脚本分段输出——因为撞上上限时输出是被静默截断的，模型自己察觉不到，只会表现为参数 JSON 不完整、工具莫名失败"
                    value={c.max_output_tokens}
                    onChange={(v) => patch("context", "max_output_tokens", v)}
                    min={0}
                    max={200000}
                    step={1000}
                  />
                  <NumField
                    label="压缩水位（占窗口比例）"
                    extra="输入估算达到窗口的这个比例就压缩一次；留足空间给输出与后续增长"
                    value={c.compact_ratio}
                    onChange={(v) => patch("context", "compact_ratio", v)}
                    min={0.1}
                    max={0.95}
                    step={0.05}
                  />
                  <NumField
                    label="步数阈值"
                    extra={
                      "步数达到这个值**且**已过下方水位下限才触发。只看步数会压得太早：实测 20 步时输入才占窗口 22%，" +
                      "此时省下的多是缓存命中的廉价 token，却让后续一批变未命中价，每步成本反而涨"
                    }
                    value={c.compact_min_steps}
                    onChange={(v) => patch("context", "compact_min_steps", v)}
                    min={2}
                    max={500}
                    step={5}
                  />
                  <NumField
                    label="步数触发的水位下限"
                    extra="步数触发的**必要条件**：输入至少占到窗口的这个比例，否则步数再多也不压"
                    value={c.compact_min_ratio}
                    onChange={(v) => patch("context", "compact_min_ratio", v)}
                    min={0.05}
                    max={0.95}
                    step={0.05}
                  />
                  <NumField
                    label="最小压缩间隔（步）"
                    extra="两次压缩之间至少隔这么多步，避免刚压完又压（每次压缩都会让前缀缓存失效一次）"
                    value={c.compact_min_interval}
                    onChange={(v) => patch("context", "compact_min_interval", v)}
                    min={0}
                    max={100}
                    step={1}
                  />
                  <NumField
                    label="保留最近步数（原文）"
                    extra="最近这几步永远保留原文——模型最依赖最近上下文，压掉会明显掉质量"
                    value={c.keep_recent_steps}
                    onChange={(v) => patch("context", "keep_recent_steps", v)}
                    min={1}
                    max={100}
                    step={1}
                  />
                  <NumField
                    label="骨架摘要上限（字符）"
                    extra="压缩后那段摘要的长度上限"
                    value={c.compact_max_chars}
                    onChange={(v) => patch("context", "compact_max_chars", v)}
                    min={200}
                    max={20000}
                    step={100}
                  />
                </Stack>
              </Section>
            </Stack>
          )}

          {section === "a2a" && (
            <Stack>
              <p className="text-[12px] leading-6 text-ink-muted">
                对端是别人的进程，快慢与可用性都不由我们控制。这里决定「等它多久」和
                「它一直失败时怎么办」——改完立即生效，已在途的调用不受影响。
              </p>

              <Section>
                <Stack>
                  <NumField
                    label="单次请求超时（秒）"
                    extra="SendMessage / GetTask / 拉 AgentCard 都用它；10~30 秒为宜"
                    value={a.request_timeout}
                    onChange={(v) => patch("a2a", "request_timeout", v)}
                    min={1}
                    max={600}
                    step={5}
                  />
                  <NumField
                    label="等待对端任务完成（秒）"
                    extra="从发出问题算起的总时长；超时按下方策略处理。0 = 不限（不推荐，可能挂死整轮）"
                    value={a.task_timeout}
                    onChange={(v) => patch("a2a", "task_timeout", v)}
                    min={0}
                    max={3600}
                    step={30}
                  />
                  <NumField
                    label="轮询间隔（秒）"
                    extra="等终态时查询对端状态的频率；长任务可放宽，短任务可收紧"
                    value={a.poll_interval}
                    onChange={(v) => patch("a2a", "poll_interval", v)}
                    min={0.5}
                    max={60}
                    step={0.5}
                  />
                  <ToggleField
                    label="超时后取消对端任务"
                    extra="对端已经不回我们了，让它继续跑只是白烧对端的 token"
                    checked={!!a.cancel_on_timeout}
                    onChange={(v) => patch("a2a", "cancel_on_timeout", v)}
                  />
                </Stack>
              </Section>

              <Section title="熔断">
                <Stack>
                  <ToggleField
                    label="开启熔断"
                    extra="连续失败（连不上 / 超时）达到阈值后，短期内直接快速失败，不再每轮都等满超时"
                    checked={!!a.breaker_enabled}
                    onChange={(v) => patch("a2a", "breaker_enabled", v)}
                  />
                  <NumField
                    label="连续失败阈值（次）"
                    extra="对端「业务上失败」（返回 FAILED）不计入——那是它给的正常答案"
                    value={a.breaker_threshold}
                    onChange={(v) => patch("a2a", "breaker_threshold", v)}
                    min={1}
                    max={100}
                  />
                  <NumField
                    label="冷却时长（秒）"
                    extra="熔断后等多久再放行一次探测请求；成功即恢复，失败重新计时"
                    value={a.breaker_cooldown}
                    onChange={(v) => patch("a2a", "breaker_cooldown", v)}
                    min={5}
                    max={3600}
                    step={30}
                  />
                </Stack>
              </Section>

              <Section title="对端熔断状态（运行时）">
                {breakers.length === 0 ? (
                  <p className="py-3 text-center text-[13px] text-ink-faint">
                    暂无失败记录，所有对端都正常
                  </p>
                ) : (
                  <Stack>
                    {breakers.map((b) => (
                      <div
                        key={b.key}
                        className="flex items-center gap-2 text-[12px]"
                      >
                        <span
                          className={
                            b.open
                              ? "shrink-0 rounded-md bg-red-500/15 px-1.5 py-0.5 text-[11px] leading-4 text-red-600 dark:text-red-300"
                              : b.failures
                              ? "shrink-0 rounded-md bg-amber-500/15 px-1.5 py-0.5 text-[11px] leading-4 text-amber-600 dark:text-amber-300"
                              : "shrink-0 rounded-md bg-emerald-500/15 px-1.5 py-0.5 text-[11px] leading-4 text-emerald-600 dark:text-emerald-300"
                          }
                        >
                          {b.open
                            ? "已熔断"
                            : b.failures
                            ? `失败 ${b.failures} 次`
                            : "正常"}
                        </span>
                        <span className="min-w-0 flex-1 truncate text-ink">
                          {b.name}
                        </span>
                        {b.open && (
                          <span className="shrink-0 text-ink-faint">
                            剩余 {Math.ceil(b.remaining)}s
                          </span>
                        )}
                        {b.last_error && (
                          <span
                            title={b.last_error}
                            className="max-w-[220px] shrink-0 truncate text-ink-faint"
                          >
                            {b.last_error}
                          </span>
                        )}
                      </div>
                    ))}
                    <button
                      type="button"
                      disabled={resetM.isPending}
                      onClick={() => resetM.mutate(undefined)}
                      className="flex w-fit items-center gap-1.5 rounded-lg border border-line px-3 py-1.5 text-[13px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong disabled:opacity-50"
                    >
                      {resetM.isPending && (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      )}
                      全部重置
                    </button>
                  </Stack>
                )}
              </Section>
            </Stack>
          )}

          {section === "debug" && (
            <Stack>
              <p className="text-[12px] leading-6 text-ink-muted">
                开启后，把每次发给大模型的完整 prompt（system + 全部历史消息）与模型
                响应写到本地文件，用于排查上下文膨胀、模型答非所问、某步为什么这么慢。
                落盘结构：
                <br />
                <code>&lt;目录&gt;/&lt;sessionId&gt;/&lt;messageId&gt;/&lt;序号-步骤&gt;.json</code>
                <br />
                即「会话 → 某一轮 → 该轮每次调用」，与用量详情里的分步一一对应。
              </p>

              <Section>
                <Stack>
                  <ToggleField
                    label="开启落盘"
                    checked={!!d.enabled}
                    onChange={(v) => patch("prompt_dump", "enabled", v)}
                  />
                  <TextField
                    label="落盘目录"
                    extra="支持 ~ 开头的家目录路径"
                    placeholder="~/.keeper/prompt-dump"
                    value={d.dir}
                    onChange={(v) => patch("prompt_dump", "dir", v)}
                  />
                  <NumField
                    label="最小输入 token"
                    extra="只记录输入 token 超过该值的调用；0 = 全部记录。排查上下文膨胀时建议设 50000，避免每次调用都写几万字"
                    value={d.min_prompt_tokens}
                    onChange={(v) => patch("prompt_dump", "min_prompt_tokens", v)}
                    min={0}
                    step={1000}
                  />
                  <NumField
                    label="保留天数"
                    extra="过期文件自动清理（最多每小时扫一次）"
                    value={d.retain_days}
                    onChange={(v) => patch("prompt_dump", "retain_days", v)}
                    min={0}
                    max={365}
                  />
                </Stack>
              </Section>
            </Stack>
          )}

          {section === "delegatee" && <DelegateeSettings />}

          {section === "mention" && <MentionSettings />}

          {section === "space" && <MySpaceSettings />}
        </div>
      )}
    </Modal>
  );
}