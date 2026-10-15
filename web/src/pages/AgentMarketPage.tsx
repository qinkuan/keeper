import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, RefreshCw } from "lucide-react";
import {
  apiListPlatformAgents,
  loadAgent,
  platformAddAgent,
  platformListMarket,
} from "../api/client";
import type { MarketAgent } from "../api/types";
import { Hint, PageHeader, Section } from "../components/ui/Page";
import { Table } from "../components/ui/Table";
import type { Column } from "../components/ui/Table";
import { toast } from "../components/ui/Toast";
import { Pill } from "../components/ui/Pill";

/**
 * 智能体市场 —— **平台上有什么、我还没拿过来的**。
 *
 * 和「智能体」页的分工：那边是库存（本地创建的 + 已装载的），这边是目录。
 * 按 origin 天然分界，所以同一个 agent 不会在两页各出现一次。
 *
 * 这里**不显示启用/停用**——那是库存页的事。这边只有两个状态：已装载 /
 * 可装载。装载 = 从远程把配置拿到本地，之后去库存页管它。
 */
export default function AgentMarketPage() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["platform-market"], queryFn: platformListMarket });
  // 本地装载状态。用 platform（含已卸载的）而不是 all——后者是「库存」，
  // 卸载过的会被过滤掉，那就分不清「没装过」和「装过又卸了」。
  const loadedQ = useQuery({
    queryKey: ["platformAgents"],
    queryFn: apiListPlatformAgents,
  });
  const loadedIds = new Set(
    (loadedQ.data?.agents ?? []).filter((a) => a.loaded).map((a) => a.id)
  );

  // 注意：`isPending` 是这一整个 mutation 的**共享**布尔值，表格里每一行都在读
  // 它——只写 `loading={loadM.isPending}` 会导致点一行、所有行的按钮一起转圈。
  // 必须再比对 `variables`（本次 mutate 的入参）才能只让当前行动起来。
  const loadM = useMutation({
    mutationFn: async (a: MarketAgent) => {
      // 未添加就先加进「我已添加」，否则平台查不到它，装载会 404
      if (!a.added) await platformAddAgent(a.id);
      return loadAgent(a.id);
    },
    onSuccess: (d: any) => {
      const msg =
        d?.running === false
          ? `已装载，但${d?.reason ?? "未运行"}`
          : `已装载：${d?.name ?? ""}`;
      if (d?.running === false) toast.error(msg);
      else toast.success(msg);
      qc.invalidateQueries({ queryKey: ["allAgents"] });
      qc.invalidateQueries({ queryKey: ["platformAgents"] });
      qc.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: (e: Error) => toast.error("装载失败：" + e.message),
  });

  const cols: Column<MarketAgent>[] = [
    { key: "name", title: "名称", width: 180, render: (a) => a.name },
    {
      key: "description",
      title: "说明",
      render: (a) => (
        <span className="block max-w-[380px] truncate text-ink-muted">
          {a.description || "—"}
        </span>
      ),
    },
    {
      key: "status",
      title: "状态",
      width: 90,
      render: (a) =>
        a.status === "active" ? (
          <Pill tone="green">启用</Pill>
        ) : (
          <Pill tone="muted">停用</Pill>
        ),
    },
    {
      key: "local",
      title: "本地",
      width: 110,
      render: (a) =>
        loadedIds.has(a.id) ? (
          <Pill tone="blue">已装载</Pill>
        ) : (
          <Pill tone="muted">未装载</Pill>
        ),
    },
    {
      key: "action",
      title: "操作",
      width: 130,
      render: (a) => {
        const loaded = loadedIds.has(a.id);
        const busy = loadM.isPending && loadM.variables?.id === a.id;
        return (
          <button
            type="button"
            disabled={busy}
            title={
              loaded
                ? "重新从平台拉一遍配置，并重建插件链接"
                : "把配置从平台拿到本地，并按平台下发的插件名单建立链接"
            }
            onClick={() => loadM.mutate(a)}
            className="inline-flex h-6 items-center gap-1.5 rounded-md bg-brand-gradient px-2.5 text-[12px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98] disabled:opacity-50"
          >
            {busy && <Loader2 className="h-3 w-3 animate-spin" />}
            {loaded ? "重新装载" : "装载"}
          </button>
        );
      },
    },
  ];

  return (
    <>
      <PageHeader
        title="智能体市场"
        desc="装载 = 把配置从平台拿到本地"
        actions={
          <button
            type="button"
            onClick={() => q.refetch()}
            className="flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-[13px] text-ink-muted transition-colors hover:bg-surface hover:text-ink"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            刷新
          </button>
        }
      />

      <div className="mb-3">
        <Hint>
          装载完它会出现在<strong className="text-ink-strong">智能体</strong>
          页——那边的启用开关、插件绑定都在那边管。本页不显示启用状态：
          装载与否是两件事。
        </Hint>
      </div>

      {q.isError && (
        <div className="mb-3">
          <Hint tone="danger" title="读不到平台配置">
            请先启动平台服务（默认 :9095），或用环境变量指定：
            <code className="mx-1 rounded bg-surface px-1.5 py-0.5 font-mono text-[11px]">
              VITE_PLATFORM_URL=http://host:port
            </code>
            <br />
            错误：{(q.error as Error)?.message}
          </Hint>
        </div>
      )}

      <Section bodyClassName="p-0">
        <Table
          rows={q.data ?? []}
          columns={cols}
          rowKey={(a) => a.id}
          loading={q.isLoading}
          minWidth={820}
          empty="平台上没有你可见的 agent"
        />
      </Section>
    </>
  );
}
