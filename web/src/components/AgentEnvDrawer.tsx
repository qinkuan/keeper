import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { apiAgentEnv, apiUpdateAgentEnv } from "../api/client";
import type {
  AgentEnvResp,
  DetectedRuntime,
  EnvDependency,
  MarketAgent,
} from "../api/types";
import { Drawer } from "./ui/Drawer";
import { Hint } from "./ui/Page";
import { TextInput, SelectInput } from "./ui/Form";
import { toast } from "./ui/Toast";
import { cn } from "../lib/utils";

const CUSTOM = "__custom__";

/** 语义化比较两个版本号数组，a<b 返回负，a>b 返回正，相等返回 0。 */
function cmpVersion(a: number[], b: number[]): number {
  const n = Math.max(a.length, b.length);
  for (let i = 0; i < n; i++) {
    const x = a[i] ?? 0;
    const y = b[i] ?? 0;
    if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
}

/** 本机实际版本是否落在 [min_version, max_version] 区间（无上限则只看下限）。 */
export function meetsVersion(
  actual: string,
  min?: string | null,
  max?: string | null
): boolean {
  const av = actual.split(".").map((n) => parseInt(n, 10) || 0);
  if (min) {
    const mv = min.split(".").map((n) => parseInt(n, 10) || 0);
    if (cmpVersion(av, mv) < 0) return false;
  }
  if (max) {
    const xv = max.split(".").map((n) => parseInt(n, 10) || 0);
    if (cmpVersion(av, xv) > 0) return false;
  }
  return true;
}

/** python / node 解释器下拉：检测到的选项 + 「自定义路径」直接填绝对路径。 */
function RuntimeSelect({
  kind,
  detected,
  value,
  onChange,
}: {
  kind: "python" | "node";
  detected: DetectedRuntime[];
  value: string;
  onChange: (v: string) => void;
}) {
  const isCustom = value !== "" && !detected.some((d) => d.path === value);
  const options = [
    {
      value: "",
      label: kind === "python" ? "选择 Python 解释器" : "选择 Node 解释器",
    },
    ...detected.map((d) => ({
      label: `${d.version_string}  (${d.path})`,
      value: d.path,
    })),
    { label: "自定义路径…", value: CUSTOM },
  ];
  // 已选值不在候选里（比如配置文件里手写的路径）：补一项，否则下拉会显示空白
  if (value !== "" && !options.some((o) => o.value === value)) {
    options.splice(1, 0, { label: value, value });
  }

  return (
    <div className="space-y-2">
      <SelectInput
        value={isCustom ? CUSTOM : value}
        onChange={(v) => onChange(v === CUSTOM ? value : v)}
        options={options}
      />
      {isCustom && (
        <TextInput
          placeholder="填写解释器绝对路径，如 /usr/local/bin/python3"
          value={value}
          onChange={onChange}
        />
      )}
    </div>
  );
}

/** 单条环境需求的合规结果。 */
function DependencyCheck({
  dep,
  detected,
}: {
  dep: EnvDependency;
  detected: DetectedRuntime[];
}) {
  const matched = detected.filter((d) =>
    meetsVersion(d.version, dep.min_version, dep.max_version)
  );
  const ok = matched.length > 0;
  const best = matched[0]; // detected 已降序，第一个即最高可用版本

  return (
    <div
      className={cn(
        "mb-2 rounded-lg border px-3.5 py-2.5",
        ok
          ? "border-emerald-500/25 bg-emerald-500/[0.07]"
          : "border-red-500/25 bg-red-500/[0.06]"
      )}
    >
      <div className="flex items-center gap-2 text-[13px] text-ink">
        <span
          className={cn(
            "shrink-0 rounded-md px-1.5 py-0.5 text-[11px] leading-4",
            ok
              ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-300"
              : "bg-red-500/15 text-red-600 dark:text-red-300"
          )}
        >
          {ok ? "满足" : "不满足"}
        </span>
        <span className="font-medium">
          {dep.kind} ≥ {dep.min_version}
          {dep.max_version ? `, ≤ ${dep.max_version}` : ""}
        </span>
      </div>
      <div className="mt-1 text-[12px] leading-5 text-ink-muted">
        {ok
          ? `本机可用：${best.version_string}（${best.path}）`
          : "未检测到满足该版本约束的解释器，请安装对应版本或手动指定路径。"}
      </div>
    </div>
  );
}

/** 解释器标签：检测到几个就列几个，一个都没有时明确说「未检测到」 */
function DetectedList({
  label,
  items,
}: {
  label: string;
  items: DetectedRuntime[];
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="text-[12px] text-ink-muted">{label}</span>
      {items.length === 0 ? (
        <span className="text-[12px] text-amber-600 dark:text-amber-300">
          未检测到
        </span>
      ) : (
        items.map((d) => (
          <span
            key={d.path}
            title={d.path}
            className="rounded-md bg-black/[0.06] px-1.5 py-0.5 font-mono text-[11px] leading-4 text-ink-muted dark:bg-white/[0.08]"
          >
            {d.version_string}
          </span>
        ))
      )}
    </div>
  );
}

/**
 * 某个 agent 的环境配置抽屉。
 *
 * - 展示该 agent 声明的环境依赖（kind + 版本），并与本机检测到的解释器做合规检查
 *   （绿色满足 / 红色不满足）。
 * - 已装载的 agent 可额外「指定解释器」（写到 config.yaml 的 agent_overrides），
 *   未装载时只做检测，不做安装。
 */
export default function AgentEnvDrawer({
  agent,
  loaded,
  open,
  onClose,
}: {
  agent: MarketAgent;
  loaded: boolean;
  open: boolean;
  onClose: () => void;
}) {
  const agentId = agent.id;
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["agent-env", agentId],
    queryFn: () => apiAgentEnv(agentId),
    enabled: open && !!agentId,
  });

  const [python, setPython] = useState<string>("");
  const [node, setNode] = useState<string>("");

  useEffect(() => {
    if (q.data) {
      setPython(q.data.selection.python ?? "");
      setNode(q.data.selection.node ?? "");
    }
  }, [q.data]);

  const saveM = useMutation({
    mutationFn: () => apiUpdateAgentEnv(agentId, { python, node }),
    onSuccess: (d) => {
      toast.success("已保存该 Agent 的解释器选择");
      qc.setQueryData(["agent-env", agentId], d);
    },
    onError: (e: Error) => toast.error("保存失败：" + e.message),
  });

  const data: AgentEnvResp | undefined = q.data;
  const pyDetected = data?.detected.python ?? [];
  const nodeDetected = data?.detected.node ?? [];
  const deps = agent.env_dependencies ?? [];

  return (
    <Drawer
      title={`环境配置 · ${agent.name}`}
      width={560}
      open={open}
      onClose={onClose}
    >
      {q.isLoading && (
        <div className="flex items-center gap-2 text-[13px] text-ink-muted">
          <Loader2 className="h-4 w-4 animate-spin" />
          加载中…
        </div>
      )}
      {q.isError && (
        <Hint tone="danger" title="读取环境信息失败">
          {(q.error as Error)?.message}
        </Hint>
      )}

      {data && (
        <div className="space-y-5">
          <Hint title="环境需求与本地合规">
            该 Agent 的依赖（MCP / 插件）需要 Python 与 Node 解释器来执行。下方列出它声明需要的环境，以及本机检测到的可用解释器；绿色表示满足，红色表示不满足。
          </Hint>

          <div>
            <h3 className="mb-2 text-[13px] font-semibold text-ink-strong">
              该 Agent 声明需要的环境
            </h3>
            {deps.length === 0 ? (
              <p className="text-[12px] text-ink-muted">未声明环境依赖。</p>
            ) : (
              deps.map((dep, i) => (
                <DependencyCheck
                  key={i}
                  dep={dep}
                  detected={dep.kind === "python" ? pyDetected : nodeDetected}
                />
              ))
            )}
          </div>

          <div>
            <h3 className="mb-2 text-[13px] font-semibold text-ink-strong">
              本机检测到的解释器
            </h3>
            <div className="space-y-1.5">
              <DetectedList label="Python：" items={pyDetected} />
              <DetectedList label="Node：" items={nodeDetected} />
            </div>
          </div>

          {loaded && (
            <div>
              <h3 className="mb-2 text-[13px] font-semibold text-ink-strong">
                指定解释器（覆盖默认）
              </h3>
              <div className="space-y-3">
                <div>
                  <div className="mb-1 text-[12px] text-ink-muted">Python</div>
                  <RuntimeSelect
                    kind="python"
                    detected={pyDetected}
                    value={python}
                    onChange={setPython}
                  />
                </div>
                <div>
                  <div className="mb-1 text-[12px] text-ink-muted">Node</div>
                  <RuntimeSelect
                    kind="node"
                    detected={nodeDetected}
                    value={node}
                    onChange={setNode}
                  />
                </div>
              </div>
              <p className="mt-2 text-[12px] leading-5 text-ink-muted">
                指定的路径会按 agent 记录到{" "}
                <code className="rounded bg-surface px-1.5 py-0.5 font-mono text-[11px]">
                  agent_overrides
                </code>
                ，装载时优先采用。不安装任何环境。
              </p>
            </div>
          )}

          {/* 操作区：只在已装载时给「保存」，未装载时环境不可指定 */}
          <div className="flex items-center justify-end gap-2 border-t border-line-soft pt-4">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg border border-line bg-panel px-4 py-[7px] text-[13px] font-medium text-ink-muted shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-all duration-150 hover:bg-surface hover:text-ink-strong active:scale-[0.98]"
            >
              关闭
            </button>
            {loaded && (
              <button
                type="button"
                disabled={saveM.isPending}
                onClick={() => saveM.mutate()}
                className="flex items-center gap-1.5 rounded-lg bg-brand-gradient px-4 py-[7px] text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
              >
                {saveM.isPending && (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                )}
                保存
              </button>
            )}
          </div>
        </div>
      )}
    </Drawer>
  );
}
