import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { apiListPlugins } from "../api/client";
import type { LibraryPlugin } from "../api/types";
import { Hint, PageHeader, Section, Stack } from "../components/ui/Page";
import { Table } from "../components/ui/Table";
import type { Column } from "../components/ui/Table";
import { toast } from "../components/ui/Toast";

/** 行内代码：所有「路径 / 版本号 / 目录名」统一这个观感 */
function Code({ children }: { children: React.ReactNode }) {
  return (
    <code className="rounded bg-surface px-1.5 py-0.5 font-mono text-[12px] text-ink">
      {children}
    </code>
  );
}

function Copyable({ text }: { text?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <span className="inline-flex items-center gap-1.5">
      <Code>{text ?? "读取中…"}</Code>
      {text && (
        <button
          type="button"
          title="复制路径"
          onClick={() => {
            navigator.clipboard?.writeText(text);
            toast.success("已复制");
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          }}
          className="text-[12px] text-ink-faint transition-colors hover:text-brand"
        >
          {copied ? "已复制" : "复制"}
        </button>
      )}
    </span>
  );
}

/** 能力类型 → 颜色。分不清的话宁可都显示成同一个中性色。 */
function kindPill(k: string) {
  const cls =
    k === "bin"
      ? "bg-blue-500/15 text-blue-600 dark:text-blue-300"
      : k === "mcp"
      ? "bg-purple-500/15 text-purple-600 dark:text-purple-300"
      : "bg-black/[0.06] text-ink-muted dark:bg-white/[0.08]";
  return (
    <span
      key={k}
      className={`shrink-0 rounded-md px-1.5 py-0.5 text-[11px] leading-4 ${cls}`}
    >
      {k}
    </span>
  );
}

/** 包格式徽标：只给非 keeper 格式（Claude 布局外部包）打标，原生格式不添噪 */
function formatBadge(f: string | undefined) {
  if (!f || f === "keeper") return null;
  return (
    <span
      className="shrink-0 rounded-md bg-amber-500/15 px-1.5 py-0.5 text-[11px] leading-4 text-amber-600 dark:text-amber-300"
      title={`${f} 布局的外部插件包（按特征标志识别，零改装兼容）`}
    >
      {f}
    </span>
  );
}

function fmtSize(n: number): string {
  if (!n) return "—";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)} ${u[i]}`;
}

/** 环境要求：把「要求」和「本机实况」并排写出来，缺什么一眼能看到。 */
function EnvCell({ items }: { items: LibraryPlugin["envDependencies"] }) {
  if (!items.length) return <span className="text-ink-faint">—</span>;
  return (
    <div className="space-y-0.5">
      {items.map((d, i) => (
        <div key={i} className="text-[12px] text-ink-muted">
          <Code>{d.kind}</Code>{" "}
          {d.minVersion ? `≥ ${d.minVersion}` : "不限"}
          {d.maxVersion ? ` ≤ ${d.maxVersion}` : ""}
        </div>
      ))}
    </div>
  );
}

/**
 * 插件库（只读）。
 *
 * **这里没有任何"安装 / 卸载"按钮，这是设计如此。** 插件库是一个目录：
 * 手写一个插件包丢进去、或用 plugin-authoring 写完放进去，都只是"往这个
 * 目录里放东西"。给安装按钮等于凭空加一套包格式、版本和冲突处理，而它带来
 * 的能力（放文件）你本来就有，而且更可控。
 *
 * 所以这一页的职责只有两个：让人看见库里有什么（名字、版本、能力类型、
 * 环境要求），以及告诉人**东西该往哪放**（库根路径）。
 */
export default function PluginPage({
  embedded,
}: {
  /** 嵌在「智能体」页的 tab 里时为 true：不重复渲染页头，只留操作按钮 */
  embedded?: boolean;
}) {
  const q = useQuery({ queryKey: ["plugins"], queryFn: apiListPlugins });

  const cols: Column<LibraryPlugin>[] = [
    {
      key: "name",
      title: "名称",
      width: 200,
      render: (r) => (
        <span className="flex items-center gap-1.5">
          {r.name}
          {formatBadge(r.format)}
        </span>
      ),
    },
    {
      key: "version",
      title: "版本",
      width: 90,
      render: (r) => (r.version ? <Code>{r.version}</Code> : "—"),
    },
    {
      key: "kinds",
      title: "能力",
      width: 150,
      render: (r) =>
        r.kinds?.length ? (
          <span className="flex flex-wrap gap-1">{r.kinds.map(kindPill)}</span>
        ) : (
          <span className="text-ink-faint">—</span>
        ),
    },
    {
      key: "env",
      title: "环境要求",
      render: (r) => <EnvCell items={r.envDependencies} />,
    },
    {
      key: "size",
      title: "体积",
      width: 90,
      align: "right",
      render: (r) => <span className="text-ink-muted">{fmtSize(r.sizeBytes)}</span>,
    },
    {
      key: "desc",
      title: "说明",
      render: (r) => (
        <span className="block max-w-[320px] truncate text-ink-muted">
          {r.description || "—"}
        </span>
      ),
    },
    {
      key: "dirname",
      title: "目录名",
      width: 170,
      render: (r) =>
        r.dirname === r.name ? (
          <Code>{r.dirname}</Code>
        ) : (
          // 目录名和清单名不一致时要说清楚：创建 agent 时用的是目录名
          <span className="text-[12px]">
            <Code>{r.dirname}</Code>{" "}
            <span className="text-ink-faint">（清单名 {r.name}）</span>
          </span>
        ),
    },
  ];

  const data = q.data;

  return (
    <>
      {embedded ? (
        <div className="mb-3 flex items-center justify-end gap-3">
          <span className="text-[13px] text-ink-muted">
            共 {data?.plugins.length ?? 0} 个
          </span>
          <button
            type="button"
            onClick={() => q.refetch()}
            className="flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-[13px] text-ink-muted transition-colors hover:bg-surface hover:text-ink"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            刷新
          </button>
        </div>
      ) : (
        <PageHeader
          title="插件库"
          desc="库里有什么、东西该往哪放。这一页只读——加插件就是往下面的目录里放文件。"
          actions={
            <>
              <span className="text-[13px] text-ink-muted">
                共 {data?.plugins.length ?? 0} 个
              </span>
              <button
                type="button"
                onClick={() => q.refetch()}
                className="flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-[13px] text-ink-muted transition-colors hover:bg-surface hover:text-ink"
              >
                <RefreshCw className="h-3.5 w-3.5" />
                刷新
              </button>
            </>
          }
        />
      )}

      <Stack>
        <Section title="库根路径">
          <Copyable text={data?.root} />
        </Section>

        <Hint>
          <div className="space-y-1">
            <div>
              手写插件包（根目录放 <Code>keeper-plugin.json</Code>）、用{" "}
              <Code>plugin-authoring</Code> 写完导出、或直接放入 Claude 布局的外部技能包
             （<Code>.claude-plugin/plugin.json</Code>，零改装兼容），放进该目录即生效——
              不需要重启，刷新本页就能看到。
            </div>
            <div className="text-ink-faint">
              这个路径来自 config.yaml 的 plugins.root，是元配置；它不是项目结构的一部分，
              请自己决定要不要纳入版本管理。
            </div>
          </div>
        </Hint>

        {!data?.exists && (
          <Hint tone="warn" title="插件库目录不存在">
            <Code>{data?.root}</Code> 还没建。创建后会重建，或直接把插件包放进去。
          </Hint>
        )}

        <Section bodyClassName="p-0">
          <Table
            rows={data?.plugins ?? []}
            columns={cols}
            rowKey={(r) => r.dirname}
            loading={q.isLoading}
            minWidth={980}
            empty="插件库是空的——把插件包放进上面的目录"
            renderExpanded={(r) =>
              r.error ? (
                <span className="text-[12px] text-red-600 dark:text-red-300">
                  {r.error}
                </span>
              ) : (
                <div className="text-[12px] text-ink-muted">
                  实际路径：<Copyable text={r.path} />
                </div>
              )
            }
          />
        </Section>
      </Stack>
    </>
  );
}
