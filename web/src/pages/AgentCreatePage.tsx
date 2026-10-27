import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Loader2 } from "lucide-react";
import { apiCreateLocalAgent, apiListPlugins } from "../api/client";
import type { LibraryPlugin } from "../api/types";
import { Field, SelectInput, TextInput } from "../components/ui/Form";
import { Hint, PageHeader, Section } from "../components/ui/Page";
import { Pill } from "../components/ui/Pill";
import { toast } from "../components/ui/Toast";
import { cn } from "../lib/utils";

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

/** 表单状态：整份 agent 配置就是一个对象，保存时整体提交 */
interface CreateForm {
  name: string;
  description: string;
  status: string;
  persona: string;
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
  const [plugins, setPlugins] = useState<string[]>([]);
  const qc = useQueryClient();

  // 状态直接放在 useState，不再走 antd Form 实例：整份表单就是一个对象，
  // 校验也只有「名称必填」一条，用 Form 反而要把值在两处之间同步。
  const [form, setForm] = useState<CreateForm>({
    name: "",
    description: "",
    status: "active",
    persona: "",
  });
  const patch = (k: keyof CreateForm, v: string) =>
    setForm((p) => ({ ...p, [k]: v }));
  const [nameErr, setNameErr] = useState("");

  const libQ = useQuery({ queryKey: ["plugins"], queryFn: apiListPlugins });

  const create = useMutation({
    mutationFn: apiCreateLocalAgent,
    onSuccess: (r) => {
      toast.success(`已创建「${r.name}」，已链接 ${r.plugins.length} 个插件`);
      qc.invalidateQueries({ queryKey: ["localAgents"] });
      onDone?.();
    },
    onError: (e: any) => {
      toast.error(String(e?.message ?? e));
    },
  });

  const lib = libQ.data;
  const libPlugins = (lib?.plugins ?? []).filter((p) => !p.error);

  const onFinish = () => {
    if (!form.name.trim()) {
      setNameErr("给 agent 起个名字");
      return;
    }
    setNameErr("");
    create.mutate({
      name: form.name.trim(),
      description: form.description || null,
      persona: form.persona || "",
      status: form.status || "active",
      // 不传工作区：工作空间是**会话级**的属性，且「我的空间」是全局唯一的空间
      // （路径在 settings.yaml），创建 agent 时无处可选。
      plugins,
    });
  };

  return (
    <div className="mx-auto w-full max-w-[720px]">
      <PageHeader
        title="创建 Agent"
        desc="只做创建。创建完去「智能体」页管理它的启用开关与插件绑定。"
        actions={
          <button
            type="button"
            onClick={onDone}
            className="flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-[13px] text-ink-muted transition-colors hover:bg-surface hover:text-ink"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            返回
          </button>
        }
      />

      <Section>
        <div className="space-y-4">
          <Field label="名称" extra="本机唯一">
            <TextInput
              value={form.name}
              onChange={(v) => {
                patch("name", v);
                if (nameErr) setNameErr("");
              }}
              placeholder="比如 code-reviewer"
            />
            {nameErr && (
              <p className="mt-1 text-[12px] text-red-600 dark:text-red-300">
                {nameErr}
              </p>
            )}
          </Field>

          <Field label="说明">
            <TextInput
              value={form.description}
              onChange={(v) => patch("description", v)}
              placeholder="一句话说明它做什么（可空）"
            />
          </Field>

          <div className="w-[180px]">
            <Field label="状态">
              <SelectInput
                value={form.status}
                onChange={(v) => patch("status", v)}
                options={[
                  { value: "active", label: "active" },
                  { value: "disabled", label: "disabled" },
                  { value: "archived", label: "archived" },
                ]}
              />
            </Field>
          </div>

          <Field label="画像" extra="每次对话都会带上。可以留空。">
            <textarea
              rows={4}
              value={form.persona}
              onChange={(e) => patch("persona", e.target.value)}
              placeholder="角色、语气、该关注什么…"
              className="w-full resize-none rounded-[7px] border border-line bg-panel px-2.5 py-2 text-[12.5px] leading-6 text-ink shadow-[0_1px_1px_rgba(0,0,0,0.03)] outline-none transition-all duration-150 placeholder:text-ink-faint focus:border-brand focus:shadow-[0_0_0_3px_rgba(66,133,244,0.48)]"
            />
          </Field>

          <div>
            <div className="mb-1.5 flex items-center gap-2">
              <span className="text-[13px] font-medium text-ink">
                能力绑定（插件）
              </span>
              {plugins.length > 0 && (
                <Pill tone="brand">已选 {plugins.length} 个</Pill>
              )}
            </div>
            {/* 复选框列表而不是多选下拉：插件库通常只有几个，一屏列出来比
                「点开下拉 → 逐个勾 → 关掉」少三步操作，还能顺带看到环境要求 */}
            <div className="max-h-[300px] space-y-1 overflow-auto rounded-lg border border-line-soft p-2">
              {libQ.isLoading ? (
                <div className="flex items-center justify-center py-6 text-ink-muted">
                  <Loader2 className="h-4 w-4 animate-spin" />
                </div>
              ) : libPlugins.length === 0 ? (
                <div className="py-6 text-center text-[13px] text-ink-faint">
                  {lib?.plugins.length
                    ? "插件库里没有可用的插件（清单都坏了）"
                    : "插件库是空的——先往插件库里放插件包"}
                </div>
              ) : (
                libPlugins.map((p) => {
                  const on = plugins.includes(p.dirname);
                  const hint = envHint(p);
                  return (
                    <label
                      key={p.dirname}
                      className={cn(
                        "flex cursor-pointer items-start gap-2.5 rounded-lg px-2.5 py-2 transition-colors",
                        on ? "bg-brand/[0.07]" : "hover:bg-surface"
                      )}
                    >
                      <input
                        type="checkbox"
                        checked={on}
                        onChange={(e) =>
                          setPlugins((s) =>
                            e.target.checked
                              ? [...s, p.dirname]
                              : s.filter((x) => x !== p.dirname)
                          )
                        }
                        className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-[var(--kp-primary)]"
                      />
                      <span className="min-w-0 flex-1">
                        <span className="flex flex-wrap items-center gap-1.5">
                          <span className="font-mono text-[13px] text-ink">
                            {p.dirname}
                          </span>
                          {p.version && <Pill tone="muted">{p.version}</Pill>}
                          {p.kinds.length > 0 && (
                            <Pill tone="indigo">{p.kinds.join("/")}</Pill>
                          )}
                        </span>
                        {hint && (
                          <span className="mt-0.5 block text-[12px] text-amber-600 dark:text-amber-400">
                            {hint}
                          </span>
                        )}
                      </span>
                    </label>
                  );
                })
              )}
            </div>
            <p className="mt-1.5 text-[12px] leading-5 text-ink-faint">
              插件本体不在 agent 目录里，而是在{" "}
              <code className="rounded bg-surface px-1.5 py-0.5 font-mono text-[11px]">
                ~/.keeper/agents/&lt;id&gt;/plugins/
              </code>{" "}
              下建一个<strong>符号链接</strong>指回库里的真实目录——所以库里改了，
              所有 agent 立刻生效，不用重新装。
            </p>
          </div>

          {lib && !lib.exists ? (
            <Hint tone="warn" title="插件库目录不存在">
              <code className="rounded bg-surface px-1.5 py-0.5 font-mono text-[12px]">
                {lib.root}
              </code>{" "}
              还没建，可以先不选插件创建，之后再补。
            </Hint>
          ) : null}

          <p className="text-[12px] leading-6 text-ink-muted">
            提示：选了环境要求满足不了的插件，创建会直接被拒绝并告诉你缺什么——
            这是故意的。环境不满足的症状是子进程起不来、命令 not found，跟
            "插件坏了"长得很像，等到真去调的时候根本想不起来是创建时就没验过。
          </p>

          <div className="flex items-center gap-2 pt-1">
            <button
              type="button"
              disabled={create.isPending}
              onClick={onFinish}
              className="flex items-center gap-1.5 rounded-lg bg-brand-gradient px-4 py-[7px] text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
            >
              {create.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              创建
            </button>
            <button
              type="button"
              onClick={onDone}
              className="rounded-lg border border-line bg-panel px-4 py-[7px] text-[13px] font-medium text-ink-muted shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-all duration-150 hover:bg-surface hover:text-ink-strong active:scale-[0.98]"
            >
              取消
            </button>
          </div>
        </div>
      </Section>
    </div>
  );
}
