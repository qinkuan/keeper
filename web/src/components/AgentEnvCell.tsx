import { useQuery } from "@tanstack/react-query";
import { CircleCheck, CircleX, Loader2 } from "lucide-react";
import { apiAgentEnv } from "../api/client";
import type { MarketAgent } from "../api/types";
import { meetsVersion } from "./AgentEnvDrawer";

/**
 * 智能体列表的「环境」列单元格。
 *
 * - 已装载：显示「配置」按钮（抽屉内可指定解释器并保存）。
 * - 未装载：显示「查看」按钮（抽屉只读，只做检测）。
 * - 合规状态（按钮旁）：
 *   未声明依赖 → 绿勾（无要求即无需检查）；
 *   全部满足 → 绿勾；任一不满足 → 红叉。
 */
export default function AgentEnvCell({
  agent,
  loaded,
  onOpen,
}: {
  agent: MarketAgent;
  loaded: boolean;
  onOpen: () => void;
}) {
  // 与抽屉共用 queryKey，列表预取后抽屉打开会直接复用缓存
  const q = useQuery({
    queryKey: ["agent-env", agent.id],
    queryFn: () => apiAgentEnv(agent.id),
  });

  const deps = agent.env_dependencies ?? [];
  const noDeps = deps.length === 0;
  let status: "ok" | "bad" | "loading" = "loading";
  if (q.isLoading) {
    status = "loading";
  } else if (noDeps) {
    // 没声明依赖 => 没有需要满足的要求，视为通过
    status = "ok";
  } else {
    const detected = q.data?.detected ?? { python: [], node: [] };
    const ok = deps.every((dep) =>
      (detected[dep.kind] ?? []).some((r) =>
        meetsVersion(r.version, dep.min_version, dep.max_version)
      )
    );
    status = ok ? "ok" : "bad";
  }

  return (
    <span className="inline-flex items-center gap-1.5">
      <button
        type="button"
        onClick={onOpen}
        className={
          loaded
            ? "text-[13px] text-brand transition-opacity hover:opacity-75"
            : "inline-flex h-6 items-center rounded-md border border-line px-2 text-[12px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong"
        }
      >
        {loaded ? "配置" : "查看"}
      </button>

      {status === "loading" && (
        <Loader2 className="h-3.5 w-3.5 animate-spin text-ink-faint" />
      )}
      {status === "ok" && (
        <span
          title={
            noDeps ? "未声明环境依赖，无需检查" : "本地环境满足该 Agent 的要求"
          }
          className="inline-flex"
        >
          <CircleCheck className="h-4 w-4 text-emerald-500" />
        </span>
      )}
      {status === "bad" && (
        <span title="本地环境不满足该 Agent 的要求" className="inline-flex">
          <CircleX className="h-4 w-4 text-red-500" />
        </span>
      )}
    </span>
  );
}
