import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { cn } from "../lib/utils";
import { Drawer } from "./ui/Drawer";
import { Tabs } from "./ui/Tabs";
import { Hint } from "./ui/Page";
import { SelectInput, TextInput, Toggle } from "./ui/Form";
import { toast } from "./ui/Toast";
import { useDebug } from "../hooks/useDebug";
import {
  apiConfig,
  apiSkills,
  apiPeers,
  apiAddPeer,
  apiDeletePeer,
  apiPatchPeer,
  apiEffectiveTools,
  apiListAgents,
  apiCapabilities,
  apiToggleCapability,
  apiListModels,
  apiGetAgentModel,
  apiSetAgentModel,
  apiListPlugins,
  apiAgentPlugins,
  apiSetLocalAgentPlugins,
} from "../api/client";
import type { CapabilityItem, LibraryPlugin } from "../api/types";

/** 传给后端的能力类型：内置工具 / 插件（插件统一承载 MCP、工具、技能） */
type CapKind = "builtin" | "plugin";

/** 能力模块的 tab key → 后端 kind */
const CAP_TABS: { key: string; label: string; kind: CapKind }[] = [
  { key: "builtin", label: "内置工具", kind: "builtin" },
  { key: "plugin", label: "插件", kind: "plugin" },
];

/**
 * 只有调试模式才展示的 tab：内置工具 / A2A 对端 / 生效工具。
 *
 * 这三个分区面向排查（这台 agent 到底装了什么、能调谁、最终生效的工具表），
 * 日常使用用不到；关掉 debug 就只留「概览」和「插件」两个入口。
 */
const DEBUG_ONLY_TABS = new Set(["builtin", "peers", "effective"]);

/** 状态 → 配色。antd 的 Tag 色板是写死的，深色下会糊，所以自己给语义色。 */
const PILL_TONES = {
  green: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-300",
  red: "bg-red-500/15 text-red-600 dark:text-red-300",
  orange: "bg-orange-500/15 text-orange-600 dark:text-orange-300",
  blue: "bg-blue-500/15 text-blue-600 dark:text-blue-300",
  purple: "bg-purple-500/15 text-purple-600 dark:text-purple-300",
  cyan: "bg-cyan-500/15 text-cyan-600 dark:text-cyan-300",
  warn: "bg-amber-500/15 text-amber-600 dark:text-amber-300",
  muted: "bg-black/[0.06] text-ink-muted dark:bg-white/[0.08]",
} as const;

type PillTone = keyof typeof PILL_TONES;

const STATE_TONE: Record<string, PillTone> = {
  已装配: "green",
  已关闭: "muted",
  只读工作区跳过: "orange",
  未绑定: "muted",
  依赖缺失: "red",
  已停用: "muted",
};

function Pill({
  tone = "muted",
  title,
  onClick,
  children,
}: {
  tone?: PillTone;
  title?: string;
  onClick?: () => void;
  children: ReactNode;
}) {
  return (
    <span
      title={title}
      onClick={onClick}
      className={cn(
        "inline-flex shrink-0 items-center rounded-md px-1.5 py-0.5 text-[11px] leading-4",
        PILL_TONES[tone],
        onClick && "cursor-pointer transition-opacity hover:opacity-70"
      )}
    >
      {children}
    </span>
  );
}

/** 基本信息的一行：标签固定宽度，值占满剩余 */
function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-start gap-3 border-b border-line-soft py-2 last:border-0">
      <span className="w-[130px] shrink-0 text-[12px] text-ink-muted">
        {label}
      </span>
      <span className="min-w-0 flex-1 text-[13px] text-ink">{children}</span>
    </div>
  );
}

/** 插件能力类型（bin / mcp / skill …）小标签，照搬库页的观感 */
function KindPill({ k }: { k: string }) {
  const cls =
    k === "bin"
      ? "bg-blue-500/15 text-blue-600 dark:text-blue-300"
      : k === "mcp"
        ? "bg-purple-500/15 text-purple-600 dark:text-purple-300"
        : "bg-black/[0.06] text-ink-muted dark:bg-white/[0.08]";
  return (
    <span
      className={`shrink-0 rounded-md px-1.5 py-0.5 text-[11px] leading-4 ${cls}`}
    >
      {k}
    </span>
  );
}

function EmptyHint({ text = "无" }: { text?: string }) {
  return <span className="text-[13px] text-ink-faint">{text}</span>;
}

/**
 * Agent 配置面板。
 *
 * 分区：概览 / 插件 始终可见；内置工具 / A2A 对端 / 生效工具 **只在 debug
 * 模式出现**（排查用：这台 agent 装了什么、能调谁、最终生效的工具表）。
 * 能力开关写本地 override，切换后服务端**重建实例**生效。
 * 插件从本机插件库里挑 MCP server、可执行工具（bin）与技能（skill），可按需开关。
 */
export default function AgentInfoDrawer({
  open,
  onClose,
  agentId,
}: {
  open: boolean;
  onClose: () => void;
  agentId: string;
}) {
  const qc = useQueryClient();
  const [tab, setTab] = useState("overview");
  const debug = useDebug();

  /* debug 关掉后，如果正停在只在 debug 可见的 tab（内置工具 / A2A 对端 /
     生效工具）上，就退回「概览」——否则会看到一个没有 tab 头、内容还挂着的
     "幽灵页"。 */
  useEffect(() => {
    if (debug) return;
    if (DEBUG_ONLY_TABS.has(tab)) setTab("overview");
  }, [debug, tab]);
  const [peerName, setPeerName] = useState("");
  const [peerUrl, setPeerUrl] = useState("");
  const [peerSource, setPeerSource] = useState<"local" | "external">("local");
  const [pickedAgentId, setPickedAgentId] = useState<string | null>(null);
  const [peerTrustEnv, setPeerTrustEnv] = useState(true);
  // MCP server 展开状态：记录哪些 server 被点击展开以查看其下工具
  const [expandedMcp, setExpandedMcp] = useState<Record<string, boolean>>({});
  // 移除对端的二次确认：确认态放在按钮上，比弹一层对话框轻
  const [confirmDel, setConfirmDel] = useState<string | null>(null);

  const configQ = useQuery({
    queryKey: ["config", agentId],
    queryFn: () => apiConfig(agentId),
    enabled: open,
  });
  const skillsQ = useQuery({
    queryKey: ["skills", agentId],
    queryFn: () => apiSkills(agentId),
    enabled: open,
  });
  const peersQ = useQuery({
    queryKey: ["peers", agentId],
    queryFn: () => apiPeers(agentId),
    // 「A2A 对端」只在 debug 下展示 → 非 debug 模式不必去拉这份数据
    enabled: open && debug,
  });
  const toolsQ = useQuery({
    queryKey: ["effective-tools", agentId],
    queryFn: () => apiEffectiveTools(agentId),
    // 同上：「生效工具」是排查用的视图
    enabled: open && debug,
  });
  const capsQ = useQuery({
    queryKey: ["capabilities", agentId],
    queryFn: () => apiCapabilities(agentId),
    enabled: open,
  });
  const agentsQ = useQuery({
    queryKey: ["agents"],
    queryFn: apiListAgents,
    enabled: open,
  });
  const modelsQ = useQuery({
    queryKey: ["models"],
    queryFn: apiListModels,
    enabled: open,
  });
  const agentModelQ = useQuery({
    queryKey: ["agent-model", agentId],
    queryFn: () => apiGetAgentModel(agentId),
    enabled: open,
  });
  // 插件库（本地磁盘目录，只读）与「本 agent 已绑定的插件」：用于插件 tab 的绑定 / 解绑。
  const libQ = useQuery({
    queryKey: ["plugin-lib"],
    queryFn: apiListPlugins,
    enabled: open,
  });
  const boundQ = useQuery({
    queryKey: ["agent-plugins", agentId],
    queryFn: () => apiAgentPlugins(agentId),
    enabled: open,
  });
  // 这个 agent 是否能在本地增删插件绑定。
  // 核心 agent 同样可以（它从本地文件构建、不在 agents 表里），其 id 由
  // /agents 的 core_agent_id 字段给出。
  // （早期有「平台下发 agent」那一类，它的插件由平台管、本地不可增删；
  //  平台整体移除后不存在这种 agent 了。）
  const isLocal =
    (agentsQ.data?.agents ?? []).some((a) => a.id === agentId) ||
    (agentsQ.data?.core_agent_id ?? null) === agentId;
  // 已绑定且仍在库里的插件名（悬空链接不含，避免提交时被 _resolve_plugins 拒绝）。
  const libDirs = new Set((libQ.data?.plugins ?? []).map((p) => p.dirname));
  const boundNames = (boundQ.data?.plugins ?? [])
    .map((p) => p.name)
    .filter((n) => libDirs.has(n));
  const setModelM = useMutation({
    mutationFn: (llmProfileId: string | null) =>
      apiSetAgentModel(agentId, llmProfileId),
    onSuccess: () => {
      toast.success("模型绑定已更新");
      qc.invalidateQueries({ queryKey: ["agent-model", agentId] });
      qc.invalidateQueries({ queryKey: ["config", agentId] });
    },
    onError: (e: Error) => toast.error("绑定失败：" + e.message),
  });
  const setPluginsM = useMutation({
    mutationFn: (names: string[]) => apiSetLocalAgentPlugins(agentId, names),
    onSuccess: () => {
      toast.success("插件绑定已更新（已重建实例）");
      qc.invalidateQueries({ queryKey: ["agent-plugins", agentId] });
      qc.invalidateQueries({ queryKey: ["config", agentId] });
      qc.invalidateQueries({ queryKey: ["capabilities", agentId] });
      qc.invalidateQueries({ queryKey: ["effective-tools", agentId] });
      qc.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: (e: Error) => toast.error("插件绑定失败：" + e.message),
  });
  const togglePlugin = (p: LibraryPlugin) => {
    if (!isLocal) {
      toast.error("这个 agent 不在本地库里，无法绑定插件");
      return;
    }
    const next = new Set(boundNames);
    if (next.has(p.dirname)) next.delete(p.dirname);
    else next.add(p.dirname);
    setPluginsM.mutate([...next]);
  };

  const localAgents = (agentsQ.data?.agents ?? []).filter((a) => a.id !== agentId);

  const refreshAfterCapChange = () => {
    qc.invalidateQueries({ queryKey: ["capabilities", agentId] });
    qc.invalidateQueries({ queryKey: ["effective-tools", agentId] });
    qc.invalidateQueries({ queryKey: ["skills", agentId] });
  };

  const toggleCapM = useMutation({
    mutationFn: (v: { kind: CapKind; name: string; enabled: boolean }) =>
      apiToggleCapability(agentId, v),
    onSuccess: (_d, v) => {
      toast.success(`${v.name} 已${v.enabled ? "启用" : "关闭"}（已重建实例）`);
      refreshAfterCapChange();
    },
    onError: (e: Error) => toast.error("切换失败：" + e.message),
  });

  const refreshPeers = () => {
    qc.invalidateQueries({ queryKey: ["peers", agentId] });
    qc.invalidateQueries({ queryKey: ["effective-tools", agentId] });
  };

  const handlePickAgent = (id: string) => {
    setPickedAgentId(id);
    const a = localAgents.find((x) => x.id === id);
    if (!a) return;
    setPeerName(a.name);
    setPeerUrl(a.a2a_url ?? "");
  };

  const handleSourceChange = (v: "local" | "external") => {
    setPeerSource(v);
    setPickedAgentId(null);
    setPeerName("");
    setPeerUrl("");
    setPeerTrustEnv(true);
  };

  const addM = useMutation({
    mutationFn: () =>
      apiAddPeer(agentId, {
        name: peerName.trim(),
        a2a_url: peerUrl.trim(),
        source: peerSource,
        trust_env: peerSource === "external" ? peerTrustEnv : false,
      }),
    onSuccess: () => {
      toast.success(`已添加对端：${peerName}`);
      setPeerName("");
      setPeerUrl("");
      setPickedAgentId(null);
      refreshPeers();
    },
    onError: (e: Error) => toast.error("添加失败：" + e.message),
  });

  const patchM = useMutation({
    mutationFn: (v: { name: string; trustEnv: boolean }) =>
      apiPatchPeer(agentId, v.name, v.trustEnv),
    onSuccess: (_d, v) => {
      toast.success(v.trustEnv ? "已改为走代理" : "已改为直连");
      refreshPeers();
    },
    onError: (e: Error) => toast.error("修改失败：" + e.message),
  });

  const delM = useMutation({
    mutationFn: (name: string) => apiDeletePeer(agentId, name),
    onSuccess: () => {
      toast.success("已移除对端");
      setConfirmDel(null);
      refreshPeers();
    },
    onError: (e: Error) => toast.error("移除失败：" + e.message),
  });

  const cfg = configQ.data;
  const llm = cfg?.llm;
  const peers = peersQ.data?.peers ?? [];
  const effectiveTools = toolsQ.data?.tools ?? [];
  const busy = addM.isPending || delM.isPending || patchM.isPending;
  const canAdd = peerName.trim() !== "" && peerUrl.trim() !== "";

  // 能力列表：内置工具 / 插件 共用。
  // 插件项若带 tools（其下已连上的 MCP 工具）可点击展开查看。
  const CapList = ({ items, kind }: { items: CapabilityItem[]; kind: CapKind }) => {
    if (capsQ.isLoading)
      return (
        <div className="flex items-center gap-2 text-[13px] text-ink-muted">
          <Loader2 className="h-4 w-4 animate-spin" />
          加载中…
        </div>
      );
    if (capsQ.isError)
      return (
        <span className="text-[13px] text-red-600 dark:text-red-300">
          读取失败：{(capsQ.error as Error)?.message}
        </span>
      );
    if (!items.length) return <EmptyHint />;
    return (
      <div className="divide-y divide-line-soft">
        {items.map((it) => {
          const tools = it.tools ?? [];
          const hasTools = tools.length > 0;
          const open = !!expandedMcp[it.name];
          return (
            <div key={it.name} className="py-2.5">
              <div className="flex items-center justify-between gap-3">
                <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                  <span className="text-[13px] font-medium text-ink">
                    {it.name}
                  </span>
                  <Pill tone={STATE_TONE[it.state] ?? "muted"}>{it.state}</Pill>
                  {it.mutating && <Pill tone="red">写操作</Pill>}
                  {hasTools && (
                    <button
                      type="button"
                      onClick={() =>
                        setExpandedMcp((s) => ({ ...s, [it.name]: !open }))
                      }
                      className="text-[12px] text-ink-faint transition-colors hover:text-brand"
                    >
                      {open ? "收起" : "展开"} {tools.length} 个工具
                    </button>
                  )}
                </div>
                <Toggle
                  checked={it.enabled}
                  disabled={toggleCapM.isPending}
                  onChange={(checked) =>
                    toggleCapM.mutate({ kind, name: it.name, enabled: checked })
                  }
                />
              </div>
              {it.description && (
                <p className="mt-1 text-[12px] leading-5 text-ink-muted">
                  {it.description}
                </p>
              )}
              {open && hasTools && (
                <div className="mt-2 space-y-1 rounded-lg border border-line-soft bg-surface px-3 py-2">
                  {tools.map((tl) => (
                    <div key={tl.name}>
                      <div className="font-mono text-[12px] text-ink">
                        {tl.name}
                      </div>
                      <div className="text-[12px] text-ink-faint">
                        {tl.description || "—"}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    );
  };

  const caps = capsQ.data;

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title="Agent 配置"
      subtitle={agentId}
      width={720}
    >
      <Tabs
        className="mb-4"
        value={tab}
        onChange={setTab}
        items={[
          { key: "overview", label: "概览" },
          // 能力 tab：内置工具只在 debug 下出现，插件始终可见
          ...CAP_TABS.filter(
            (t) => debug || !DEBUG_ONLY_TABS.has(t.key)
          ).map((t) => ({ key: t.key, label: t.label })),
          ...(debug
            ? [
                { key: "peers", label: "A2A 对端" },
                { key: "effective", label: "生效工具" },
              ]
            : []),
        ]}
      />

      {tab === "overview" && (
        <>
          {configQ.isLoading && (
            <div className="flex items-center gap-2 text-[13px] text-ink-muted">
              <Loader2 className="h-4 w-4 animate-spin" />
              加载中…
            </div>
          )}
          {configQ.isError && (
            <span className="text-[13px] text-red-600 dark:text-red-300">
              配置读取失败：{(configQ.error as Error)?.message}
            </span>
          )}
          {!cfg && !configQ.isLoading && <EmptyHint text="暂无配置" />}
          {cfg && (
            <>
              <div className="rounded-card border border-line-soft px-4 py-1">
                <Row label="名称">{cfg.name}</Row>
                <Row label="工作区">
                  {/* 只读：工作空间是**会话级**属性，不是 agent 的。agent 装配期
                      没有工作区（多空间下线后不再有"默认目录"兜底），会话实际读写
                      哪里由「我的空间」决定。给入口反而会让人以为"改了这里会话
                      就跟着变"。要换会话的工作空间，用会话页顶栏那个下拉。 */}
                  {cfg.workspace || "会话级（在会话页切换）"}
                </Row>
                <Row label="MCP 连接">
                  <Pill tone={cfg.mcp_connected ? "green" : "red"}>
                    {cfg.mcp_connected ? "已连接" : "未连接"}
                  </Pill>
                </Row>
                <Row label="已注册 MCP server">
                  {cfg.mcp_servers?.length
                    ? cfg.mcp_servers.map((s: string) => (
                        <Pill key={s} tone="blue">
                          {s}
                        </Pill>
                      ))
                    : "无"}
                </Row>
              </div>

              <div className="mt-5 flex flex-wrap items-center gap-2">
                <span className="text-[13px] font-medium text-ink">大模型：</span>
                {llm?.provider ? (
                  <>
                    <Pill tone="muted">{llm.provider}</Pill>
                    <Pill tone="muted">{llm.model_name || "-"}</Pill>
                    <Pill tone={llm.has_api_key ? "green" : "muted"}>
                      {llm.has_api_key ? "已配置 Key" : "未提供 Key"}
                    </Pill>
                  </>
                ) : (
                  <>
                    <Pill tone="warn">未绑定模型</Pill>
                    <span className="text-[12px] text-ink-muted">
                      该 agent 不会调用模型（纯索引模式）
                    </span>
                  </>
                )}
              </div>

              <div className="mt-4">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-[13px] font-medium text-ink">
                    绑定模型预设：
                  </span>
                  {agentModelQ.isLoading ? (
                    <Loader2 className="h-4 w-4 animate-spin text-ink-muted" />
                  ) : (
                    <div className="w-[280px]">
                      <SelectInput
                        value={agentModelQ.data?.llm_profile_id ?? ""}
                        onChange={(v) => setModelM.mutate(v || null)}
                        options={[
                          { value: "", label: "未绑定（无法调用模型）" },
                          ...(modelsQ.data ?? []).map((m) => ({
                            value: m.id,
                            label: `${m.name}（${m.provider}/${m.model_name}）`,
                          })),
                        ]}
                      />
                    </div>
                  )}
                </div>
                <p className="mt-1.5 text-[12px] leading-5 text-ink-muted">
                  模型必须绑定才能使用：未绑定时该 agent 不会调用模型（纯索引模式）
                  {agentModelQ.data?.hint && !agentModelQ.data.bound && (
                    <span className="ml-1.5 text-amber-600 dark:text-amber-300">
                      {agentModelQ.data.hint}
                    </span>
                  )}
                </p>
              </div>

              <div className="mt-4 flex flex-wrap items-center gap-2">
                <span className="text-[13px] font-medium text-ink">技能：</span>
                {skillsQ.data?.skills?.length
                  ? skillsQ.data.skills.map((s: string) => (
                      <Pill key={s} tone="purple">
                        {s}
                      </Pill>
                    ))
                  : "无"}
              </div>
            </>
          )}
        </>
      )}

      {CAP_TABS.map(
        (t) =>
          tab === t.key && (
            <div key={t.key}>
              {t.key === "builtin" && (
                <Hint title="内置工具归 keeper 本地">
                  代码在 keeper 本地，不需要下载也不需要安装；默认启用，可按需关闭（写类工具在只读工作区下会自动跳过）。
                </Hint>
              )}
              {t.key === "builtin" && (
                <div className="mt-3">
                  <CapList
                    items={(caps?.[t.key] ?? []) as CapabilityItem[]}
                    kind={t.kind}
                  />
                </div>
              )}
              {t.key === "plugin" && (
                <Hint title="本地插件库">
                  从本机插件库里挑插件挂到这个 agent 上：勾上即建符号链接、重建实例生效，取消即解绑。
                </Hint>
              )}

              {/* 本地插件库：所有 agent 都展示，便于看到本机有哪些可挂的插件。
                  每个插件只显示一条，右侧开关直接管「绑定 / 解绑」——
                  不另起「启用 / 停用」行，避免重复。 */}
              {t.key === "plugin" && (
                <div className="mt-3">
                  {libQ.isLoading ? (
                    <div className="flex items-center gap-2 text-[13px] text-ink-muted">
                      <Loader2 className="h-4 w-4 animate-spin" />
                      加载插件库…
                    </div>
                  ) : (
                    <div className="divide-y divide-line-soft">
                      {(libQ.data?.plugins ?? []).map((p: LibraryPlugin) => {
                        const bound = boundNames.includes(p.dirname);
                        return (
                          <div key={p.dirname} className="py-2.5">
                            <div className="flex items-center justify-between gap-3">
                              <div className="min-w-0">
                                <div className="flex flex-wrap items-center gap-1.5">
                                  <span className="text-[13px] font-medium text-ink">
                                    {p.name}
                                  </span>
                                  {p.version && (
                                    <code className="rounded bg-surface px-1.5 py-0.5 font-mono text-[12px] text-ink">
                                      {p.version}
                                    </code>
                                  )}
                                  {p.kinds?.map((k) => (
                                    <KindPill key={k} k={k} />
                                  ))}
                                  {/* 非 keeper 格式（Claude 布局外部包）打来源标 */}
                                  {p.format && p.format !== "keeper" && (
                                    <span
                                      className="shrink-0 rounded-md bg-amber-500/15 px-1.5 py-0.5 text-[11px] leading-4 text-amber-600 dark:text-amber-300"
                                      title={`${p.format} 布局的外部插件包（按特征标志识别，零改装兼容）`}
                                    >
                                      {p.format}
                                    </span>
                                  )}
                                </div>
                                {p.description && (
                                  <p className="mt-0.5 text-[12px] leading-5 text-ink-muted">
                                    {p.description}
                                  </p>
                                )}
                              </div>
                              <div className="flex shrink-0 items-center gap-2">
                                <span
                                  className={
                                    bound
                                      ? "text-[12px] text-emerald-600 dark:text-emerald-300"
                                      : "text-[12px] text-ink-faint"
                                  }
                                >
                                  {bound ? "已绑定" : "未绑定"}
                                </span>
                                <Toggle
                                  checked={bound}
                                  disabled={!isLocal || setPluginsM.isPending}
                                  onChange={() => togglePlugin(p)}
                                />
                              </div>
                            </div>
                          </div>
                        );
                      })}
                      {(libQ.data?.plugins ?? []).length === 0 && (
                        <div className="py-3 text-[13px] text-ink-faint">
                          插件库是空的——把插件包放进库目录（见「插件库」页）后刷新即可
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}

              {/* 不在本地库里的 agent（理论上只有「还没装载的」）：不能增删插件绑定，
                  但仍展示能力列表，只读。 */}
              {t.key === "plugin" && !isLocal && (
                <div className="mt-5">
                  <div className="mb-1.5 text-[13px] font-semibold text-ink-strong">
                    能力
                  </div>
                  <CapList
                    items={(caps?.[t.key] ?? []) as CapabilityItem[]}
                    kind={t.kind}
                  />
                </div>
              )}
            </div>
          )
      )}

      {tab === "peers" && (
        <>
          <Hint title="运行时可增删，立即生效并持久化">
            本进程内 agent 走下拉选择（直连）；外部 agent 手填地址，默认走系统代理。
          </Hint>

          <div className="mt-3 space-y-2">
            {peers.length === 0 && <EmptyHint text="暂无对端" />}
            {peers.map((p) => (
              <div
                key={p.name}
                className="flex items-center gap-2 rounded-lg border border-line-soft px-3 py-2"
              >
                <Pill tone="blue">{p.name}</Pill>
                <Pill tone={p.source === "local" ? "cyan" : "orange"}>
                  {p.source === "local" ? "本进程" : "外部"}
                </Pill>
                {p.source === "external" && (
                  <Pill
                    tone={p.trust_env ? "blue" : "muted"}
                    title="点击切换：走系统代理 / 直连"
                    onClick={() =>
                      patchM.mutate({ name: p.name, trustEnv: !p.trust_env })
                    }
                  >
                    {p.trust_env ? "走代理" : "直连"}
                  </Pill>
                )}
                <span className="min-w-0 flex-1 truncate text-[12px] text-ink-muted">
                  {p.a2a_url}
                </span>
                {confirmDel === p.name ? (
                  <span className="flex shrink-0 items-center gap-1">
                    <button
                      type="button"
                      onClick={() => delM.mutate(p.name)}
                      disabled={busy}
                      className="rounded-md bg-red-500 px-2 py-1 text-[12px] text-white transition hover:bg-red-400 disabled:opacity-50"
                    >
                      确认移除
                    </button>
                    <button
                      type="button"
                      onClick={() => setConfirmDel(null)}
                      className="rounded-md px-2 py-1 text-[12px] text-ink-muted transition hover:text-ink"
                    >
                      取消
                    </button>
                  </span>
                ) : (
                  <button
                    type="button"
                    onClick={() => setConfirmDel(p.name)}
                    className="shrink-0 rounded-md px-2 py-1 text-[12px] text-ink-muted transition hover:bg-red-500/10 hover:text-red-600 dark:hover:text-red-300"
                  >
                    移除
                  </button>
                )}
              </div>
            ))}

            <div className="flex items-center gap-0.5 rounded-lg bg-surface p-0.5">
              {(["local", "external"] as const).map((v) => (
                <button
                  key={v}
                  type="button"
                  onClick={() => handleSourceChange(v)}
                  className={cn(
                    "rounded-md px-2.5 py-1 text-[12px] transition-all",
                    peerSource === v
                      ? "bg-panel text-ink-strong shadow-[var(--kp-shadow-sm)]"
                      : "text-ink-muted hover:text-ink"
                  )}
                >
                  {v === "local" ? "本进程" : "外部"}
                </button>
              ))}
            </div>

            <div className="flex gap-2">
              <div className="w-[30%]">
                <TextInput
                  value={peerName}
                  onChange={setPeerName}
                  placeholder="对端名"
                />
              </div>
              {peerSource === "local" ? (
                <div className="flex-1">
                  <SelectInput
                    value={pickedAgentId ?? ""}
                    onChange={handlePickAgent}
                    options={[
                      {
                        value: "",
                        label: localAgents.length
                          ? "选择本进程已装载的 agent"
                          : "本进程暂无其他 agent",
                      },
                      ...localAgents.map((a) => ({
                        value: a.id,
                        label: `${a.name}  (${a.id.slice(0, 8)}…)`,
                      })),
                    ]}
                  />
                </div>
              ) : (
                <div className="flex-1">
                  <TextInput
                    value={peerUrl}
                    onChange={setPeerUrl}
                    placeholder="http://host:8080/agents/{id}/a2a"
                  />
                </div>
              )}
              <button
                type="button"
                disabled={!canAdd || busy}
                onClick={() => addM.mutate()}
                className="flex shrink-0 items-center gap-1.5 rounded-lg bg-brand-gradient px-4 py-[7px] text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
              >
                {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                添加
              </button>
            </div>

            {peerSource === "external" && (
              <div className="flex items-center gap-2">
                <label className="flex cursor-pointer items-center gap-1.5 text-[13px] text-ink">
                  <input
                    type="checkbox"
                    checked={peerTrustEnv}
                    onChange={(e) => setPeerTrustEnv(e.target.checked)}
                    className="h-3.5 w-3.5 accent-[var(--kp-primary)]"
                  />
                  走系统代理
                </label>
                <span className="text-[12px] text-ink-muted">
                  外部对端通常需要；若对端在内网可直连，取消勾选
                </span>
              </div>
            )}
          </div>
        </>
      )}

      {tab === "effective" && (
        <>
          {toolsQ.isLoading && (
            <div className="flex items-center gap-2 text-[13px] text-ink-muted">
              <Loader2 className="h-4 w-4 animate-spin" />
              加载中…
            </div>
          )}
          {toolsQ.isError && (
            <span className="text-[13px] text-red-600 dark:text-red-300">
              读取失败：{(toolsQ.error as Error)?.message}
            </span>
          )}
          {!toolsQ.isLoading && effectiveTools.length === 0 && <EmptyHint />}
          <div className="flex flex-wrap gap-1.5">
            {effectiveTools.map((t, i) => (
              <Pill key={`${t.name}-${i}`} tone="muted">
                {t.name}
              </Pill>
            ))}
          </div>
        </>
      )}

      {tab === "usage" && (
        <Hint title="用量数据已移到侧边栏「可观测」">
          那边同时提供用量趋势、工具统计、重复调用 / 空转、错误聚合、能力加载与解析结构，并且可以按时间范围对照查看。
        </Hint>
      )}
    </Drawer>
  );
}
