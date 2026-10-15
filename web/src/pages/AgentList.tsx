import { useState } from "react";
import { Loader2, RefreshCw } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  apiDeleteLocalAgent,
  apiListAgents,
  apiListAllAgents,
  apiListPlugins,
  apiSetAgentEnabled,
  apiSetLocalAgentPlugins,
  unloadAgent,
} from "../api/client";
import type { LocalAgent } from "../api/types";
import { Hint, PageHeader, Section } from "../components/ui/Page";
import { Table } from "../components/ui/Table";
import type { Column } from "../components/ui/Table";
import { Toggle } from "../components/ui/Form";
import { Modal } from "../components/ui/Modal";
import { Pill } from "../components/ui/Pill";
import { ConfirmButton, DANGER_BTN, PLAIN_BTN } from "../components/ui/Confirm";
import { toast } from "../components/ui/Toast";
import { cn } from "../lib/utils";

/** 行内小按钮（表格操作列统一这个规格） */
const ROW_BTN =
  "inline-flex h-6 items-center gap-1 rounded-md border border-line px-2 text-[12px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong disabled:opacity-50";

/**
 * 智能体 →「我的智能体」tab：**我已有的**（本地创建的 + 从平台装载的）。
 *
 * 平台上「有什么、还没拿过来的」在同页的「智能体市场」tab；这里只管库存。
 * 两边按 origin 天然分界，所以同一个 agent 不会显示两遍。
 *
 * 每行一个**启用开关**（Switch），它是决定 agent 跑不跑、进不进概览的唯一开关，
 * 缺省是开的。停用不会删任何东西，也不会被下一次平台同步撤销——写的是
 * override 表，不是 status。
 *
 * 只有本地创建的（origin=local）能删：平台 agent 的配置归平台，删了还得重装。
 */
export default function AgentList({
  onCreate,
  embedded,
}: {
  onCreate?: () => void;
  /**
   * 嵌在「智能体」页的 tab 里时为 true：外层已经渲染了页头与 tab 栏，这里
   * 不再重复一个大标题，只把操作按钮留在内容上方右对齐。
   */
  embedded?: boolean;
}) {
  const qc = useQueryClient();
  const [pluginEdit, setPluginEdit] = useState<LocalAgent | null>(null);
  const [pluginSel, setPluginSel] = useState<string[]>([]);
  /** 让每行的「删除」确认态在切页/刷新后自动复原 */
  const [confirmDelReset, setConfirmDelReset] = useState(0);

  const listQ = useQuery({ queryKey: ["allAgents"], queryFn: apiListAllAgents });
  const libQ = useQuery({ queryKey: ["plugins"], queryFn: apiListPlugins });
  // 本进程实际跑着哪些（/agents 是运行时视角）
  const runningQ = useQuery({ queryKey: ["agents"], queryFn: apiListAgents });
  const runningIds = new Set((runningQ.data?.agents ?? []).map((a) => a.id));

  const refresh = () => {
    setConfirmDelReset((n) => n + 1);
    qc.invalidateQueries({ queryKey: ["allAgents"] });
    qc.invalidateQueries({ queryKey: ["agents"] });
    qc.invalidateQueries({ queryKey: ["health"] });
  };

  const enableM = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      apiSetAgentEnabled(id, enabled),
    onSuccess: (d, v) => {
      toast.success(
        v.enabled ? (d.running ? "已启用，正在运行" : "已启用") : "已停用"
      );
      refresh();
    },
    onError: (e: Error) => toast.error("操作失败：" + e.message),
  });

  const unloadM = useMutation({
    mutationFn: (id: string) => unloadAgent(id),
    onSuccess: () => {
      toast.success("已卸载（配置缓存保留，重新装载即可再跑）");
      refresh();
    },
    onError: (e: Error) => toast.error("卸载失败：" + e.message),
  });

  const setPluginsM = useMutation({
    mutationFn: ({ id, plugins }: { id: string; plugins: string[] }) =>
      apiSetLocalAgentPlugins(id, plugins),
    onSuccess: (d: any) => {
      toast.success(d?.restarted ? "已更新插件绑定，实例已重建" : "已更新插件绑定");
      refresh();
    },
    onError: (e: Error) => toast.error("更新失败：" + e.message),
  });

  const deleteM = useMutation({
    mutationFn: (id: string) => apiDeleteLocalAgent(id),
    onSuccess: () => {
      toast.success("已删除");
      refresh();
    },
    onError: (e: Error) => toast.error("删除失败：" + e.message),
  });

  const cols: Column<LocalAgent>[] = [
    {
      key: "name",
      title: "名称",
      width: 180,
      render: (r) => (
        <span className="flex items-center gap-1.5">
          <span className="truncate font-medium text-ink-strong">{r.name}</span>
          <Pill tone={r.origin === "local" ? "green" : "blue"}>
            {r.origin === "local" ? "本地创建" : "平台装载"}
          </Pill>
        </span>
      ),
    },
    {
      key: "description",
      title: "说明",
      render: (r) => (
        <span className="block max-w-[280px] truncate text-ink-muted">
          {r.description || "—"}
        </span>
      ),
    },
    {
      key: "plugins",
      title: "插件",
      width: 220,
      render: (r) => {
        const ps = r.plugins ?? [];
        if (!ps.length) return <span className="text-ink-faint">未绑定</span>;
        const missing = new Set(r.missing_plugins ?? []);
        return (
          <span className="flex flex-wrap gap-1">
            {ps.map((p) =>
              missing.has(p) ? (
                // 库里目录没了但绑定还在：装载时会被跳过，保存一次即可清掉
                <Pill
                  key={p}
                  tone="red"
                  title="插件库里已经没有这个目录，装载时会跳过"
                >
                  {p} · 缺失
                </Pill>
              ) : (
                <Pill key={p} tone="muted">
                  {p}
                </Pill>
              )
            )}
          </span>
        );
      },
    },
    {
      key: "enabled",
      title: "启用",
      width: 150,
      render: (r) => {
        const busy = enableM.isPending && enableM.variables?.id === r.id;
        return (
          <span className="flex items-center gap-2">
            <Toggle
              checked={r.enabled}
              disabled={busy}
              onChange={(on) => enableM.mutate({ id: r.id, enabled: on })}
            />
            {runningIds.has(r.id) ? (
              <Pill tone="green">运行中</Pill>
            ) : r.enabled ? (
              <Pill tone="muted">已启用</Pill>
            ) : (
              <Pill tone="muted">已停用</Pill>
            )}
          </span>
        );
      },
    },
    {
      key: "ops",
      title: "操作",
      width: 210,
      render: (r) => (
        <span className="flex flex-wrap items-center gap-1">
          {r.origin === "platform" ? (
            // 只有「卸载」。卸载后它就从本页消失（本地配置一并清掉），装回去去
            // 「智能体市场」——那边按本地装载状态显示「装载」/「重新装载」。
            <button
              type="button"
              disabled={unloadM.isPending && unloadM.variables === r.id}
              onClick={() => unloadM.mutate(r.id)}
              className={ROW_BTN}
            >
              {unloadM.isPending && unloadM.variables === r.id && (
                <Loader2 className="h-3 w-3 animate-spin" />
              )}
              卸载
            </button>
          ) : (
            <button
              type="button"
              onClick={() => {
                setPluginEdit(r);
                setPluginSel(r.plugins ?? []);
              }}
              className={ROW_BTN}
            >
              管理插件
            </button>
          )}
          {r.origin === "local" && (
            <ConfirmButton
              resetKey={confirmDelReset}
              disabled={deleteM.isPending && deleteM.variables === r.id}
              onConfirm={() => deleteM.mutate(r.id)}
              className={cn(DANGER_BTN, "h-6 text-[12px]")}
            >
              删除
            </ConfirmButton>
          )}
        </span>
      ),
    },
  ];

  const agents = listQ.data?.agents ?? [];
  const libPlugins = (libQ.data?.plugins ?? []).filter((p) => !p.error);

  // 页头操作区：独立成页时挂在 PageHeader 右侧，嵌在 tab 里时单独占一行右对齐
  const headerActions = (
    <>
      <button
        type="button"
        onClick={() => listQ.refetch()}
        className="flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-[13px] text-ink-muted transition-colors hover:bg-surface hover:text-ink"
      >
        <RefreshCw className="h-3.5 w-3.5" />
        刷新
      </button>
      <button
        type="button"
        onClick={onCreate}
        className="rounded-lg bg-brand-gradient px-4 py-[7px] text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98]"
      >
        创建 Agent
      </button>
    </>
  );

  return (
    <>
      {embedded ? (
        <div className="mb-3 flex justify-end gap-2">{headerActions}</div>
      ) : (
        <PageHeader
          title="智能体"
          desc="这里是我已有的 agent：本地创建的 + 从平台装载的"
          actions={headerActions}
        />
      )}

      <div className="mb-3">
        <Hint>
          启用开关决定它跑不跑、进不进概览，缺省是开的；停用不会删东西，也不会被下一次平台同步撤销。
          平台上还没拿过来的 agent 在
          <strong className="text-ink-strong">智能体市场</strong>那个 tab 里。
        </Hint>
      </div>

      <Section bodyClassName="p-0">
        <Table
          rows={agents}
          columns={cols}
          rowKey={(r) => r.id}
          loading={listQ.isLoading}
          minWidth={980}
          empty="还没有 agent——点右上角「创建 Agent」，或去智能体市场装载"
        />
      </Section>

      <Modal
        open={!!pluginEdit}
        onClose={() => setPluginEdit(null)}
        width={560}
        title={`插件绑定：${pluginEdit?.name ?? ""}`}
        footer={
          <>
            <button
              type="button"
              onClick={() => setPluginEdit(null)}
              className={cn(PLAIN_BTN, "h-8 px-4")}
            >
              取消
            </button>
            <button
              type="button"
              disabled={setPluginsM.isPending}
              onClick={() => {
                if (!pluginEdit) return;
                setPluginsM.mutate(
                  { id: pluginEdit.id, plugins: pluginSel },
                  { onSuccess: () => setPluginEdit(null) }
                );
              }}
              className="flex h-8 items-center gap-1.5 rounded-lg bg-brand-gradient px-4 text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98] disabled:opacity-50"
            >
              {setPluginsM.isPending && (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              )}
              保存
            </button>
          </>
        }
      >
        <div className="space-y-3">
          <p className="text-[12px] leading-5 text-ink-muted">
            插件本体在插件库里，这里改的是「勾哪些」。保存后链接立刻建好 / 删掉，agent
            在跑的话会重建实例。
          </p>

          {/* 多选插件：用复选框列表而不是多选下拉——插件库通常只有几个，
              一屏列出来比"点开下拉、逐个勾、再关掉"少三步操作 */}
          <div className="max-h-[320px] space-y-1 overflow-auto rounded-lg border border-line-soft p-2">
            {libQ.isLoading ? (
              <div className="flex items-center justify-center py-6 text-ink-muted">
                <Loader2 className="h-4 w-4 animate-spin" />
              </div>
            ) : libPlugins.length === 0 ? (
              <div className="py-6 text-center text-[13px] text-ink-faint">
                插件库是空的
              </div>
            ) : (
              libPlugins.map((p) => {
                const on = pluginSel.includes(p.dirname);
                return (
                  <label
                    key={p.dirname}
                    className={cn(
                      "flex cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-2 transition-colors",
                      on ? "bg-brand/[0.07]" : "hover:bg-surface"
                    )}
                  >
                    <input
                      type="checkbox"
                      checked={on}
                      onChange={(e) =>
                        setPluginSel((s) =>
                          e.target.checked
                            ? [...s, p.dirname]
                            : s.filter((x) => x !== p.dirname)
                        )
                      }
                      className="h-3.5 w-3.5 shrink-0 accent-[var(--kp-primary)]"
                    />
                    <span className="min-w-0 flex-1 truncate font-mono text-[13px] text-ink">
                      {p.dirname}
                    </span>
                    {p.version && <Pill tone="muted">{p.version}</Pill>}
                  </label>
                );
              })
            )}
          </div>

          {(pluginEdit?.missing_plugins ?? []).length ? (
            <Hint
              tone="warn"
              title={`库里已经找不到：${pluginEdit!.missing_plugins.join("、")}`}
            >
              这些绑定还在，但插件目录没了，装载时会跳过。保存一次即可清掉。
            </Hint>
          ) : null}
        </div>
      </Modal>
    </>
  );
}
