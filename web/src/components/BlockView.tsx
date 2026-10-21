import { useEffect, useMemo, useRef, useState } from "react";
import { Download, File, FolderOpen, Loader2 } from "lucide-react";
import type { Artifact } from "../api/types";
import {
  apiDeployInfo,
  apiRevealArtifact,
  artifactUrl,
} from "../api/client";
import { isBlockArtifact } from "./viewers";
import PanZoomStage from "./PanZoomStage";

/**
 * 非文件产物的渲染器。这里是 Path 2「带类型的块」的前端落点：
 * 后端 fs.deliver 收的是 Block[]，每种 kind 一个专属 viewer。
 *
 * 与 ArtifactCard 的关系：ArtifactCard 在顶部判断，凡是非文件块就整张交给本组件，
 * 文件块才走它自己的 Monaco / iframe 逻辑。两类渲染互不干扰。
 *
 * 重渲染库（vega-embed / mermaid）用动态 import 懒加载——它们体积大，
 * 只有真的出现对应块时才下载对应 chunk，不进首屏包。
 */

interface BlockViewProps {
  artifact: Artifact;
  agentId: string;
  messageId: string;
  isLocal?: boolean;
}

const BTN =
  "inline-flex h-6 items-center gap-1 rounded-md border border-line px-1.5 text-[12px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong";
const ICON_BTN =
  "flex h-6 w-6 items-center justify-center rounded-md border border-line text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong disabled:opacity-50";

const PLACEHOLDER_STYLE: React.CSSProperties = {
  padding: 16,
  color: "var(--kp-text-muted)",
  fontSize: 12,
  textAlign: "center",
};

/** 把内联内容包成可下载的 blob（如 mermaid 源码）。 */
function downloadBlob(content: string, filename: string, mime: string) {
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function BlockView({ artifact, agentId, messageId, isLocal }: BlockViewProps) {
  const [local, setLocal] = useState<boolean | null>(isLocal ?? null);
  const [revealing, setRevealing] = useState(false);

  useEffect(() => {
    if (isLocal !== undefined) {
      setLocal(isLocal);
      return;
    }
    let active = true;
    apiDeployInfo(agentId)
      .then((d) => active && setLocal(d.local))
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [agentId, isLocal]);

  const reveal = async () => {
    if (revealing) return;
    setRevealing(true);
    try {
      await apiRevealArtifact(agentId, messageId, artifact.id);
    } catch {
      /* 忽略：前端已降级为只显示下载 */
    } finally {
      setRevealing(false);
    }
  };

  // 内联块（如 mermaid src）没有后端文件，下载走 blob；其余走文件 URL。
  const inline = !!artifact.src;
  const downloadHref = artifactUrl(agentId, messageId, artifact.id, true);

  return (
    <div style={{ marginTop: 6 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: 8,
          border: "1px solid var(--kp-border-soft)",
          borderRadius: 8,
          background: "var(--kp-surface)",
        }}
      >
        <File className="h-[18px] w-[18px] shrink-0" style={{ color: "var(--kp-primary)" }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            style={{
              fontWeight: 500,
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {artifact.name}
          </div>
          <div
            style={{
              fontSize: 12,
              color: "var(--kp-text-muted)",
            }}
          >
            {artifact.kind}
            {artifact.engine ? ` · ${artifact.engine}` : ""}
          </div>
        </div>
        <div style={{ display: "flex", gap: 4 }}>
          {inline ? (
            <button
              type="button"
              title="下载"
              className={BTN}
              onClick={() => downloadBlob(artifact.src!, artifact.name, "text/plain")}
            >
              <Download className="h-3 w-3" />
              下载
            </button>
          ) : (
            <a href={downloadHref} title="下载" className={BTN}>
              <Download className="h-3 w-3" />
              下载
            </a>
          )}
          {local && (
            <button
              type="button"
              title="在文件管理器打开所在目录"
              disabled={revealing}
              onClick={reveal}
              className={ICON_BTN}
            >
              {revealing ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <FolderOpen className="h-3 w-3" />
              )}
            </button>
          )}
        </div>
      </div>

      <div
        style={{
          marginTop: 8,
          border: "1px solid #eee",
          borderRadius: 6,
          overflow: "hidden",
          background: "#fff",
        }}
      >
        {artifact.kind === "chart" ? (
          <ChartBlock artifact={artifact} agentId={agentId} messageId={messageId} />
        ) : artifact.kind === "mermaid" ? (
          <MermaidBlock artifact={artifact} agentId={agentId} messageId={messageId} />
        ) : artifact.kind === "table" ? (
          <TableBlock artifact={artifact} agentId={agentId} messageId={messageId} />
        ) : (
          <div style={PLACEHOLDER_STYLE}>
            该类型（{artifact.kind}）暂不支持对话内预览，请下载后查看。
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * 图表渲染（src 直渲版，供制品卡片与 markdown 内联共用）。
 * engine=vega-lite 走 vega-embed；engine=echarts 走 echarts.init + setOption。
 * 容器常驻，加载/失败以浮层提示，不卸载容器。
 */
export function ChartView({
  src,
  engine = "vega-lite",
}: {
  src: string;
  engine?: "vega-lite" | "echarts";
}) {
  const [state, setState] = useState<{ loading: boolean; err: string | null }>({
    loading: true,
    err: null,
  });
  const chartRef = useRef<HTMLDivElement>(null);
  const instRef = useRef<any>(null);
  const resizeRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    let active = true;
    const el = chartRef.current;
    if (!el) return;
    (async () => {
      try {
        const spec = JSON.parse(src);
        if (engine === "echarts") {
          const echarts = (await import("echarts")).default;
          instRef.current = echarts.init(el);
          instRef.current.setOption(spec);
          // echarts 不随容器尺寸自适应，渲染后补一次 resize 以铺满给定高度
          instRef.current.resize();
          // 窗口 resize 时也跟随：监听一次，卸载时移除，避免泄漏。
          const onResize = () => instRef.current?.resize();
          window.addEventListener("resize", onResize);
          resizeRef.current = onResize;
        } else {
          const vegaEmbed = (await import("vega-embed")).default;
          el.innerHTML = "";
          await vegaEmbed(el, spec, { renderer: "svg", actions: false });
        }
        if (active) setState({ loading: false, err: null });
      } catch (e) {
        if (active)
          setState({
            loading: false,
            err: e instanceof Error ? e.message : String(e),
          });
      }
    })();
    return () => {
      active = false;
      if (resizeRef.current) {
        window.removeEventListener("resize", resizeRef.current);
        resizeRef.current = null;
      }
      if (instRef.current) {
        instRef.current.dispose?.();
        instRef.current = null;
      }
      el.innerHTML = "";
    };
  }, [src, engine]);

  const overlay = (text: string) => (
    <div
      style={{
        position: "absolute",
        inset: 22,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        color: "var(--kp-text-muted)",
        fontSize: 12,
        textAlign: "center",
        background: "#fff",
        padding: 8,
      }}
    >
      {text}
    </div>
  );

  return (
    <div style={{ position: "relative" }}>
      <PanZoomStage height={360}>
        <div
          ref={chartRef}
          style={{
            display: "inline-block",
            width: "100%",
            // echarts 需要显式高度；vega 按内容自适应，给 width 即可
            ...(engine === "echarts" ? { height: 320 } : {}),
          }}
        />
      </PanZoomStage>
      {state.loading && overlay("渲染图表中…")}
      {state.err && overlay(`图表渲染失败：${state.err}（可下载 spec 查看）`)}
    </div>
  );
}

/** 图表：制品卡片入口，先取 spec（内联 src 或文件），再交给 ChartView 直渲。 */
function ChartBlock({
  artifact,
  agentId,
  messageId,
}: {
  artifact: Artifact;
  agentId: string;
  messageId: string;
}) {
  const [src, setSrc] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        let s = artifact.src;
        if (!s) {
          // artifactUrl 已把 token 拼进查询参数（和下载同一套），无需额外自定义头，
          // 避免跨域时触发 CORS 预检（OPTIONS）被本地部署拦掉而 fetch 失败。
          const res = await fetch(artifactUrl(agentId, messageId, artifact.id));
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          s = await res.text();
        }
        if (active) setSrc(s);
      } catch (e) {
        if (active) setErr(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      active = false;
    };
  }, [artifact.id, agentId, messageId]);

  if (err)
    return <div style={PLACEHOLDER_STYLE}>图表渲染失败：{err}（可下载 spec 查看）</div>;
  if (!src) return <div style={PLACEHOLDER_STYLE}>加载图表中…</div>;
  return <ChartView src={src} engine={(artifact.engine as any) || "vega-lite"} />;
}

let mermaidReady = false;
/** 自增 id：mermaid.render 要求每次调用 id 唯一，否则报「id 已存在」。 */
let mermaidSeq = 0;

/** 流程图渲染（src 直渲版，供制品卡片与 markdown 内联共用）。 */
export function MermaidView({ src, refMark }: { src: string; refMark?: string }) {
  const [state, setState] = useState<{
    loading: boolean;
    err: string | null;
    svg: string | null;
  }>({ loading: true, err: null, svg: null });

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        let s = src;
        // 时序图（refMark==="seq"）：agent 在 mermaid 源码里用 [seq] 占「这一步序号」的位，
        // 渲染前统一替换成递增序号（1,2,3…）。对实时渲染和读历史一致生效，且对已经带好
        // 序号的源码无副作用（匹配不到就不替换）。markdown 内联一般不带 ref，跳过此步。
        if (refMark === "seq" && s) {
          let _n = 0;
          s = s.replace(/\[seq\]/g, () => String(++_n));
        }
        const mermaid = (await import("mermaid")).default;
        if (!mermaidReady) {
          mermaid.initialize({ startOnLoad: false, theme: "default", securityLevel: "strict" });
          mermaidReady = true;
        }
        const { svg } = await mermaid.render("m" + ++mermaidSeq, s);
        // 交给 React 受控渲染：SVG 随组件挂载写入，避免手动 innerHTML 时 ref 尚未挂载（白屏）。
        if (active) setState({ loading: false, err: null, svg });
      } catch (e) {
        if (active)
          setState({
            loading: false,
            err: e instanceof Error ? e.message : String(e),
            svg: null,
          });
      }
    })();
    return () => {
      active = false;
    };
  }, [src, refMark]);

  if (state.loading) return <div style={PLACEHOLDER_STYLE}>渲染流程图中…</div>;
  if (state.err)
    return (
      <div style={PLACEHOLDER_STYLE}>
        流程图渲染失败：{state.err}（可下载源码查看）
      </div>
    );

  // 缩放/平移/双击交互统一交给可复用的 PanZoomStage
  return (
    <PanZoomStage height={360}>
      <div dangerouslySetInnerHTML={{ __html: state.svg || "" }} />
    </PanZoomStage>
  );
}

/** 流程图：制品卡片入口，先取源码（内联 src 或文件），再交给 MermaidView 直渲。 */
function MermaidBlock({
  artifact,
  agentId,
  messageId,
}: {
  artifact: Artifact;
  agentId: string;
  messageId: string;
}) {
  const [src, setSrc] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        let s = artifact.src;
        if (!s) {
          // artifactUrl 已把 token 拼进查询参数（和下载同一套），无需额外自定义头，
          // 避免跨域预检被本地部署拦掉。
          const res = await fetch(artifactUrl(agentId, messageId, artifact.id));
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          s = await res.text();
        }
        if (active) setSrc(s);
      } catch (e) {
        if (active) setErr(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      active = false;
    };
  }, [artifact.id, agentId, messageId]);

  if (err)
    return <div style={PLACEHOLDER_STYLE}>流程图渲染失败：{err}（可下载源码查看）</div>;
  if (!src) return <div style={PLACEHOLDER_STYLE}>渲染流程图中…</div>;
  return <MermaidView src={src} refMark={artifact.ref} />;
}

/** 表格：取 CSV / JSON 文件，解析成可排序表格。数字列按数值排序，避免 10,2,9。 */
function TableBlock({
  artifact,
  agentId,
  messageId,
}: {
  artifact: Artifact;
  agentId: string;
  messageId: string;
}) {
  const [state, setState] = useState<{
    loading: boolean;
    err: string | null;
    headers: string[];
    rows: string[][];
  }>({ loading: true, err: null, headers: [], rows: [] });

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const res = await fetch(artifactUrl(agentId, messageId, artifact.id));
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const text = await res.text();
        const isJson = /\.json$/i.test(artifact.name);
        const { headers, rows } = isJson ? parseJsonTable(text) : parseCsv(text);
        if (!headers.length) throw new Error("表格为空或无法解析表头");
        if (active) setState({ loading: false, err: null, headers, rows });
      } catch (e) {
        if (active)
          setState({
            loading: false,
            err: e instanceof Error ? e.message : String(e),
            headers: [],
            rows: [],
          });
      }
    })();
    return () => {
      active = false;
    };
  }, [artifact.id, agentId, messageId, artifact.name]);

  if (state.loading) return <div style={PLACEHOLDER_STYLE}>加载表格中…</div>;
  if (state.err)
    return (
      <div style={PLACEHOLDER_STYLE}>表格解析失败：{state.err}（可下载查看）</div>
    );

  return <SortableTable headers={state.headers} rows={state.rows} />;
}

/** 极简 CSV 解析：支持引号包裹与字段内逗号/换行。 */
function parseCsv(text: string): { headers: string[]; rows: string[][] } {
  const lines = text.split(/\r?\n/).filter((l) => l.length > 0);
  if (!lines.length) return { headers: [], rows: [] };
  const splitLine = (line: string): string[] => {
    const out: string[] = [];
    let cur = "";
    let quoted = false;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (quoted) {
        if (c === '"') {
          if (line[i + 1] === '"') {
            cur += '"';
            i++;
          } else quoted = false;
        } else cur += c;
      } else if (c === '"') quoted = true;
      else if (c === ",") {
        out.push(cur);
        cur = "";
      } else cur += c;
    }
    out.push(cur);
    return out;
  };
  const headers = splitLine(lines[0]).map((h) => h.trim());
  const rows = lines.slice(1).map((l) => splitLine(l));
  return { headers, rows };
}

/** JSON → 表格：支持「对象数组」「二维数组」「{columns,rows}」三种形态。 */
function parseJsonTable(text: string): { headers: string[]; rows: string[][] } {
  const data = JSON.parse(text);
  // 对象数组：列 = 所有键的并集
  if (Array.isArray(data) && data.length && typeof data[0] === "object" && data[0] !== null) {
    const headers: string[] = [];
    for (const o of data) {
      for (const k of Object.keys(o as object)) {
        if (!headers.includes(k)) headers.push(k);
      }
    }
    const rows = (data as Record<string, unknown>[]).map((o) =>
      headers.map((h) => String(o[h] ?? "")),
    );
    return { headers, rows };
  }
  // 二维数组：[表头, ...行]
  if (Array.isArray(data) && data.length && Array.isArray(data[0])) {
    const headers = (data[0] as unknown[]).map((c) => String(c));
    const rows = (data.slice(1) as unknown[][]).map((r) => r.map((c) => String(c)));
    return { headers, rows };
  }
  // {columns:[{key}|string], rows:[[...]] | [{...}]}
  if (data && Array.isArray((data as any).rows)) {
    const cols = (data as any).columns ?? [];
    const headers = cols.map((c: any) => (typeof c === "string" ? c : c.key ?? String(c)));
    const rows = ((data as any).rows as unknown[]).map((r) =>
      Array.isArray(r)
        ? (r as unknown[]).map((c) => String(c))
        : headers.map((h: string) => String((r as Record<string, unknown>)[h] ?? "")),
    );
    return { headers, rows };
  }
  return { headers: [], rows: [] };
}

function SortableTable({
  headers,
  rows,
}: {
  headers: string[];
  rows: string[][];
}) {
  const [sortKey, setSortKey] = useState<number | null>(null);
  const [asc, setAsc] = useState(true);

  // 数字列检测：该列所有非空单元格都能转成有限数字 → 按数值排序
  const numericCols = useMemo(
    () =>
      headers.map((_, ci) =>
        rows.every((r) => {
          const v = r[ci];
          if (v === "" || v == null) return true;
          return Number.isFinite(Number(v));
        }),
      ),
    [headers, rows],
  );

  const sorted = useMemo(() => {
    if (sortKey == null) return rows;
    const num = numericCols[sortKey];
    return [...rows].sort((a, b) => {
      const av = num ? Number(a[sortKey]) : a[sortKey];
      const bv = num ? Number(b[sortKey]) : b[sortKey];
      if (av < bv) return asc ? -1 : 1;
      if (av > bv) return asc ? 1 : -1;
      return 0;
    });
  }, [rows, sortKey, asc, numericCols]);

  const onSort = (ci: number) => {
    if (sortKey === ci) setAsc((v) => !v);
    else {
      setSortKey(ci);
      setAsc(true);
    }
  };

  return (
    <div style={{ maxHeight: 360, overflow: "auto", background: "#fff" }}>
      <table
        style={{
          borderCollapse: "collapse",
          width: "100%",
          fontSize: 12,
          fontVariantNumeric: "tabular-nums",
        }}
      >
        <thead>
          <tr>
            {headers.map((h, ci) => (
              <th
                key={ci}
                onClick={() => onSort(ci)}
                title="点击排序"
                style={{
                  position: "sticky",
                  top: 0,
                  background: "#f5f5f5",
                  border: "1px solid #eee",
                  padding: "4px 8px",
                  textAlign: numericCols[ci] ? "right" : "left",
                  cursor: "pointer",
                  userSelect: "none",
                }}
              >
                {h}
                {sortKey === ci ? (asc ? " ▲" : " ▼") : ""}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((r, ri) => (
            <tr key={ri}>
              {headers.map((_, ci) => (
                <td
                  key={ci}
                  style={{
                    border: "1px solid #eee",
                    padding: "3px 8px",
                    textAlign: numericCols[ci] ? "right" : "left",
                  }}
                >
                  {r[ci] ?? ""}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default BlockView;
