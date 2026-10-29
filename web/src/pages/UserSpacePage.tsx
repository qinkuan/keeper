import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent, type MutableRefObject, type ReactNode } from "react";
import {
  ChevronRight,
  ExternalLink,
  Eye,
  Plus,
  ArrowLeft,
  FileText,
  Folder,
  FolderOpen,
  FolderPlus,
  Loader2,
  MessageSquarePlus,
  Minus,
  PanelLeft,
  PanelRight,
  Pencil,
  RefreshCw,
  Search as SearchIcon,
  Trash2,
  TriangleAlert,
  Upload,
  X,
} from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGetDocxIr, apiGetMySpace, apiSearchQuery, apiSearchStats, apiSetMySpace } from "../api/client";
import type { MySpaceFile, SearchHit } from "../api/types";
import { makeSpaceApi, spaceFileUrl, type SpaceApi } from "../api/spaceNative";
import {
  subscribeOpenFile,
  takePendingOpenFile,
  type OpenFileTarget,
} from "../lib/openFile";
import { Hint } from "../components/ui/Page";
import { toast } from "../components/ui/Toast";
import { cn, isComposingKey } from "../lib/utils";
import MarkdownView from "../components/MarkdownView";
import { addQuoteToChat, addPendingFileRef } from "../lib/quote";
import ImageViewer from "../components/ImageViewer";
import Editor from "@monaco-editor/react";
import { applyKeeperTheme, KEEPER_THEME } from "../monacoTheme";
import { monacoLangOf } from "../monacoLang";
import { viewerKindOf, isSheetName, diagramKindOf } from "../components/viewers";
import { officeKindOf } from "../components/officeTypes";
import { ChartView, MermaidView } from "../components/DiagramView";
import SheetViewer from "../components/SheetViewer";
import PdfViewer from "../components/PdfViewer";
import { DocxViewer, type DocxCtl, type DocxInfo } from "../components/DocxViewer";
import FileTypeIcon from "../components/FileTypeIcon";

/** 树节点：既能是目录（可展开）也能是文件（叶子）。 */
interface TNode {
  title: string;
  key: string;
  isDir: boolean;
  leaf?: boolean;
  children?: TNode[];
}

const basename = (p: string) => {
  const t = p.replace(/\/+$/, "");
  const i = t.lastIndexOf("/");
  return i >= 0 ? t.slice(i + 1) : t;
};

/** 在树里按 key 递归查找节点。 */
function findNode(nodes: TNode[], key: string): TNode | undefined {
  for (const n of nodes) {
    if (n.key === key) return n;
    if (n.children) {
      const f = findNode(n.children, key);
      if (f) return f;
    }
  }
  return undefined;
}

/** 在树里按 key 递归把 children 塞进去（懒加载用）。 */
function injectChildren(nodes: TNode[], key: string, children: TNode[]): TNode[] {
  return nodes.map((n) =>
    n.key === key
      ? { ...n, children, leaf: children.length === 0 }
      : n.children
        ? { ...n, children: injectChildren(n.children, key, children) }
        : n
  );
}

/** 拉取某目录的子项（目录 + 文件，目录在前、按名排序），用于左侧树。走桌面原生层。 */
async function loadChildren(space: SpaceApi, p: string): Promise<TNode[]> {
  const r = await space.list(p);
  return r.items.map((it) => ({
    title: it.name,
    key: it.path,
    isDir: it.isDir,
    ...(it.isDir ? {} : { leaf: true }),
  }));
}

const fmtSize = (size: number) => {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  return `${(size / 1024 / 1024 / 1024).toFixed(1)} GB`;
};
const fmtMtime = (mtime: number) =>
  new Date(mtime * 1000).toLocaleString("zh-CN", { hour12: false });

/** 把命中的词标出来（搜索结果摘要用）。 */
function Highlight({ text, terms }: { text: string; terms: string[] }) {
  const usable = terms.map((t) => t.trim()).filter((t) => t.length > 0);
  if (!usable.length) return <>{text}</>;
  const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  // 长词优先：「缓存穿透」要先于「缓存」被匹配，否则高亮会断成一截一截
  const alt = [...new Set(usable)]
    .sort((a, b) => b.length - a.length)
    .map(esc)
    .join("|");
  const re = new RegExp(`(${alt})`, "g");
  const out: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(
      <mark
        key={out.length}
        className="rounded bg-brand/15 px-0.5 text-ink-strong"
      >
        {m[0]}
      </mark>
    );
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return <>{out}</>;
}

/**
 * 搜索结果里的一个文件：同一文件可能命中好几段，这里合并成一条，
 * 取分数最高的那段当摘要，并记下命中了几段。
 */
interface FileHit {
  path: string;
  rel: string;
  name: string;
  score: number;
  text: string;
  titlepath: string;
  locator: string | null;
  start_line: number;
  end_line: number;
  /** PDF 的起始页码（1-based）；Markdown 为 null */
  page: number | null;
  /** markdown / pdf / docx —— 决定定位串怎么写（"第 N 行" / "第 N 页" / "第 N 段"） */
  source_kind: string | null;
  count: number;
  /** 这个文件里实际命中的词（后端分词后给的）——用来高亮 */
  matched: string[];
}

/** 把片段级结果聚合成文件级结果（按路径去重，按最高分排序）。 */
function groupByFile(hits: SearchHit[], root: string): FileHit[] {
  const map = new Map<string, FileHit>();
  for (const h of hits) {
    if (!h.path) continue;
    // 索引里可能有「换过空间」之前的旧文件，点开会打不开 → 直接不展示
    if (root && !h.path.startsWith(root)) continue;
    const rel = h.path.slice(root.length).replace(/^\//, "") || h.path;
    const cur = map.get(h.path);
    if (!cur) {
      map.set(h.path, {
        path: h.path,
        rel,
        name: basename(h.path),
        score: h.score,
        text: h.text || "",
        titlepath: h.titlepath || "",
        locator: h.locator,
        start_line: h.start_line,
        end_line: h.end_line,
        page: h.page ?? null,
        source_kind: h.source_kind ?? null,
        count: 1,
        matched: [...(h.matched_terms ?? [])],
      });
    } else {
      cur.count += 1;
      for (const t of h.matched_terms ?? [])
        if (!cur.matched.includes(t)) cur.matched.push(t);
      if (h.score > cur.score) {
        cur.score = h.score;
        cur.text = h.text || "";
        cur.titlepath = h.titlepath || "";
        cur.locator = h.locator;
        cur.start_line = h.start_line;
        cur.end_line = h.end_line;
        cur.page = h.page ?? cur.page;
        cur.source_kind = h.source_kind ?? cur.source_kind;
      }
    }
  }
  return [...map.values()].sort((a, b) => b.score - a.score);
}

/** 搜索结果列表：一个文件一条（同一文件的多个片段已合并）。 */
function SearchResults({
  hits,
  terms,
  loading,
  error,
  noIndex,
  degraded,
  onOpen,
}: {
  hits: FileHit[];
  terms: string[];
  loading: boolean;
  error?: string;
  noIndex: boolean;
  degraded?: boolean;
  onOpen: (hit: FileHit) => void;
}) {
  if (loading)
    return (
      <div className="flex h-full items-center justify-center gap-2 text-[13px] text-ink-muted">
        <Loader2 className="h-4 w-4 animate-spin" />
        搜索中…
      </div>
    );
  if (error)
    return (
      <Hint tone="danger" title="搜索失败">
        {error}
      </Hint>
    );
  if (noIndex)
    return (
      <Hint tone="warn" title="还没有建立索引">
        搜索依赖本地索引，当前还没有索引任何内容。到「搜索」页点「建立索引」，
        索引完成后回到这里就能搜了。
      </Hint>
    );
  if (!hits.length)
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 text-center text-ink-faint">
        <SearchIcon className="h-8 w-8" />
        <div className="text-[13px]">没有匹配的文件</div>
        <div className="text-[12px]">只有被索引过的文件（Markdown / PDF / Word）能被搜到</div>
      </div>
    );
  return (
    <div className="flex flex-col gap-2 pb-2">
      {degraded && (
        <div className="rounded-md border border-amber-500/25 bg-amber-500/[0.08] px-2.5 py-1.5 text-[11.5px] leading-5 text-amber-700 dark:text-amber-300">
          语义模型未就绪，本次只按字面匹配（关键词通道）搜索。
        </div>
      )}
      {hits.map((h) => (
        <button
          key={h.path}
          type="button"
          onClick={() => onOpen(h)}
          title={
            h.page
              ? `打开并跳到第 ${h.page} 页`
              : h.start_line
                ? h.source_kind === "docx"
                  ? `打开并跳到第 ${h.start_line} 段`
                  : `打开并跳到第 ${h.start_line} 行`
                : "打开这个文件"
          }
          className="flex flex-col gap-1.5 rounded-lg border border-line-soft bg-surface px-3 py-2.5 text-left transition-colors hover:border-brand/40 hover:bg-panel"
        >
          <div className="flex items-center gap-2">
            <FileTypeIcon name={h.name} size="xs" />
            <span className="truncate text-[13px] font-medium text-ink-strong">
              {h.name}
            </span>
            <span className="tabular-nums text-[11px] text-ink-faint">
              {h.score.toFixed(3)}
            </span>
            {h.count > 1 && (
              <span className="shrink-0 text-[11px] text-ink-faint">
                {h.count} 处命中
              </span>
            )}
          </div>
          <div className="truncate font-mono text-[11px] text-ink-muted" title={h.rel}>
            {h.rel}
          </div>
          {h.text && (
            <div className="line-clamp-2 text-[12px] leading-5 text-ink-muted">
              <Highlight text={h.text} terms={[...h.matched, ...terms]} />
            </div>
          )}
          {(h.titlepath || h.locator || h.start_line) && (
            <div className="truncate text-[11px] text-ink-faint">
              {h.titlepath}
              {h.locator
                ? ` · ${h.locator}`
                : h.start_line
                  ? h.source_kind === "docx"
                    ? ` · 第 ${h.start_line}-${h.end_line} 段`
                    : ` · 第 ${h.start_line}-${h.end_line} 行`
                  : ""}
            </div>
          )}
        </button>
      ))}
    </div>
  );
}

/** 目录树里每行右侧的小操作按钮（hover 才显示）。 */
function RowBtn({
  title,
  danger,
  onClick,
  children,
}: {
  title: string;
  danger?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      className={cn(
        "flex h-6 w-6 items-center justify-center rounded text-ink-faint transition-colors hover:bg-black/[0.06] hover:text-ink-strong",
        danger && "hover:bg-red-500/10 hover:text-red-500"
      )}
    >
      {children}
    </button>
  );
}

/** 左侧目录树（递归）。目录可展开 / 收起；hover 每行出现原生文件操作按钮。 */
function TreeNav({
  nodes,
  depth,
  expanded,
  selected,
  loadingKey,
  onToggle,
  onSelectFile,
  onOpen,
  onRename,
  onDelete,
  onMkdirHere,
  onContextMenu,
}: {
  nodes: TNode[];
  depth: number;
  expanded: Set<string>;
  selected: string | null;
  loadingKey: string | null;
  onToggle: (key: string) => void;
  onSelectFile: (key: string) => void;
  onOpen: (key: string, isDir: boolean) => void;
  onRename: (key: string) => void;
  onDelete: (key: string) => void;
  onMkdirHere: (key: string) => void;
  onContextMenu?: (key: string, isDir: boolean, e: MouseEvent) => void;
}) {
  return (
    <div>
      {nodes.map((n) => {
        const open = expanded.has(n.key);
        const isSel = selected === n.key;
        const loading = loadingKey === n.key;
        return (
          <div key={n.key}>
            <div
              style={{ paddingLeft: depth * 14 + 8 }}
              className={cn(
                "group flex items-center gap-1 rounded-md py-1 pr-1 transition-colors",
                isSel ? "bg-brand/10" : "hover:bg-black/[0.04] dark:hover:bg-white/5"
              )}
              onContextMenu={(e) => onContextMenu?.(n.key, n.isDir, e)}
            >
              {n.isDir ? (
                <button
                  type="button"
                  onClick={() => onToggle(n.key)}
                  className="flex h-3.5 w-3.5 shrink-0 items-center justify-center text-ink-faint transition-colors hover:text-ink"
                >
                  {loading ? (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  ) : (
                    <ChevronRight
                      className={cn(
                        "h-3.5 w-3.5 transition-transform",
                        open && "rotate-90"
                      )}
                    />
                  )}
                </button>
              ) : (
                <span className="h-3.5 w-3.5 shrink-0" />
              )}
              <button
                type="button"
                onClick={() => (n.isDir ? onToggle(n.key) : onSelectFile(n.key))}
                className={cn(
                  "flex min-w-0 flex-1 items-center gap-1.5 truncate text-left text-[13px]",
                  n.isDir ? "text-ink" : "text-ink-muted",
                  isSel && "text-brand"
                )}
              >
                {n.isDir ? (
                  <Folder className="h-3.5 w-3.5 shrink-0 text-amber-500" />
                ) : (
                  <FileTypeIcon name={n.title} size="xs" />
                )}
                <span className="truncate">{n.title}</span>
              </button>
              {/* hover 才出现：仿桌面文件管理器的右键式操作 */}
              <div className="ml-auto hidden shrink-0 items-center gap-0.5 pl-1 group-hover:flex">
                {n.isDir && (
                  <RowBtn title="在此新建子文件夹" onClick={() => onMkdirHere(n.key)}>
                    <FolderPlus className="h-3.5 w-3.5" />
                  </RowBtn>
                )}
                <RowBtn
                  title="在文件管理器中打开"
                  onClick={() => onOpen(n.key, n.isDir)}
                >
                  <FolderOpen className="h-3.5 w-3.5" />
                </RowBtn>
                <RowBtn title="重命名" onClick={() => onRename(n.key)}>
                  <Pencil className="h-3.5 w-3.5" />
                </RowBtn>
                <RowBtn title="删除" danger onClick={() => onDelete(n.key)}>
                  <Trash2 className="h-3.5 w-3.5" />
                </RowBtn>
              </div>
            </div>
            {open && n.children && n.children.length > 0 && (
              <TreeNav
                nodes={n.children}
                depth={depth + 1}
                expanded={expanded}
                selected={selected}
                loadingKey={loadingKey}
                onToggle={onToggle}
                onSelectFile={onSelectFile}
                onOpen={onOpen}
                onRename={onRename}
                onDelete={onDelete}
                onMkdirHere={onMkdirHere}
                onContextMenu={onContextMenu}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

/** 右侧文件内容：受限读取后渲染。Markdown 直接渲染，其它文本按语言高亮，
 *  二进制 / 过大文件降级为「无法预览」信息卡。 */
function AppModal({
  title,
  children,
  onClose,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      onClick={onClose}
    >
      <div
        className="w-[360px] rounded-xl border border-line bg-surface p-4 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 text-[13px] font-medium text-ink-strong">{title}</div>
        {children}
      </div>
    </div>
  );
}

/** 二进制 / 无法内联预览的文件：给信息卡（不渲染内容，下载走行内按钮）。 */
function BinaryFileCard({ file, note }: { file: MySpaceFile; note?: string }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 text-center">
      <FileTypeIcon name={file.name} size="md" />
      <div className="text-[13px] text-ink">无法预览此文件</div>
      <div className="text-[12px] text-ink-muted">
        {file.name} · {fmtSize(file.size)}
      </div>
      <div className="text-[12px] text-ink-faint">
        {note ?? (file.is_text ? "非文本文件" : "二进制文件")}
      </div>
    </div>
  );
}

/**
 * Office 文档解析失败时的降级卡。
 *
 * 为什么不显示「解析失败」就完事：docx/pptx 的格式变体极多（Word / WPS /
 * Pages / 老版本各有差异），解析器一定会有不认的文件。这时候用户至少该知道
 * 「是哪类问题」以及「下一步做什么」——所以按 code 给针对性文案，并把
 * 「用系统应用打开」作为主出口。
 */
/**
 * 图文件的画布：填满预览区（height="100%"），不再用固定像素。
 * 固定 520 时，预览区比它矮就会把图挤到下面去（还要滚动才看全），
 * 而"图落在可见区域正中"才符合预览的直觉。
 */

/**
 * 只读 Monaco 源码视图。图渲染失败时的降级形态，以及编辑态的「预览」也用不到它
 * （编辑态是可写 Editor）——它只服务「想看源码但不想进编辑」这一种情况。
 */
function SourceOnly({ name, text }: { name: string; text: string }) {
  return (
    <div className="h-full min-h-0">
      <Editor
        height="100%"
        language={monacoLangOf(name) ?? "plaintext"}
        theme={KEEPER_THEME}
        beforeMount={applyKeeperTheme}
        value={text}
        options={{
          readOnly: true,
          minimap: { enabled: false },
          fontSize: 13,
          scrollBeyondLastLine: false,
          wordWrap: "on",
        }}
      />
    </div>
  );
}

/**
 * 标题栏 → 文件视图的命令句柄。
 *
 * 「预览 / 编辑」按钮放在右侧标题栏（文件名右边），但**编辑态与保存逻辑仍归
 * FileViewer 所有**：切文件丢弃草稿、退回预览前落盘这些规则不能散到父组件。
 * 父组件拿这个句柄去触发切换，FileViewer 通过 onModeChange 把状态报回去。
 *
 * 缩放（DocxCtl）同理：控件在标题栏，"适宽"的比例由 DocxViewer 实时算，
 * 两边靠这个句柄 + onModeChange 通信。
 */
export interface FileViewerCtl extends DocxCtl {
  toggleEdit?: () => void;
  /** 用系统应用打开当前文件（docx / pptx 这类保真兜底） */
  openWithApp?: () => void;
}

function FileViewer({
  space,
  root,
  path,
  jump,
  ctl,
  onModeChange,
}: {
  space: SpaceApi;
  root: string;
  path: string;
  /**
   * 跳转目标（从搜索结果点进来时带上）：
   *   line —— Markdown 的源文件行号（1-based）
   *   page —— PDF 的页码（1-based）
   *   terms—— 实际命中的词（把位置精确到那句）
   *   seq  —— 递增序号：同一位置连点两次也要重新跳
   */
  jump?: { line?: number; page?: number; terms?: string[]; seq?: number } | null;
  /** 标题栏用它触发「预览 ↔ 编辑」切换（见 FileViewerCtl） */
  ctl?: MutableRefObject<FileViewerCtl>;
  /** 把编辑态报给标题栏：按钮文案 / 图标 / 禁用态都在那边渲染 */
  onModeChange?: (m: {
    editing: boolean;
    saving: boolean;
    editable: boolean;
    /** docx 预览的缩放状态与文档元信息（别的文件类型是 null） */
    info: DocxInfo | null;
    /** 「用系统应用打开」按钮的文案（docx → 用 Word 打开，pptx → 用 PowerPoint 打开） */
    openWithAppLabel?: string;
  }) => void;
}) {
  const qc = useQueryClient();
  const fq = useQuery({
    queryKey: ["my-space-file", path],
    queryFn: () => space.read(path),
    // 切回窗口时自动拉最新（你在别的编辑器改完、点回本页即见）
    refetchOnWindowFocus: true,
  });
  // 选中正文后浮出的"添加"按钮状态；path 即文件绝对路径（来自列表 key）。
  const [quoteBtn, setQuoteBtn] = useState<{ x: number; y: number; text: string } | null>(
    null
  );
  useEffect(() => {
    if (!quoteBtn) return;
    const clear = () => setQuoteBtn(null);
    // 任意滚动都让按钮消失，避免停在过期位置
    window.addEventListener("scroll", clear, true);
    return () => window.removeEventListener("scroll", clear, true);
  }, [quoteBtn]);
  // 编辑 / 预览切换：仅文本类、且确实能当文本读的文件可编辑。切文件时复位编辑态。
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  /* 图类文件的渲染失败原因。置空 = 正常显示图。
   *
   * 为什么不直接显示「渲染失败」卡片：.vg.json / .mmd 是**文本**文件，用户点开
   * 多半就是为了改那张图。spec 写错时给他看源码 + 失败原因，改完保存立刻重试，
   * 比一张死掉的错误卡有用得多。 */
  const [diagramErr, setDiagramErr] = useState<string | null>(null);
  useEffect(() => {
    setEditing(false);
    setDraft("");
  }, [path]);
  // 换文件 / 内容变了（保存后）就重新尝试渲染图：否则第一次失败后会一直卡在源码态。
  // 依赖取 fq.data?.text 而不是 f.text：**这个 hook 必须在下面的提前 return 之前**，
  // 而 f 要到 return 之后才声明。放到 return 后面会违反 Hooks 规则——首帧 isLoading
  // 提前返回时它不执行，数据到达后重渲染才执行，hook 数量前后不一致，React 直接抛
  // 「Rendered more hooks than during the previous render」把组件崩掉（症状：一点开
  // 文件就白屏，且与文件类型无关）。
  useEffect(() => {
    setDiagramErr(null);
  }, [path, fq.data?.text]);

  /* 能不能编辑：只看「是不是能当文本读的文件」。
   *
   * 必须放在提前 return 之前算：标题栏（文件名右边）要靠它决定
   * 「预览/编辑」按钮要不要出现——而标题栏在父组件里，得由这里报上去。 */
  const editable = !!fq.data?.is_text && !fq.data?.too_large;

  /* docx 的页面内渲染：解析在内核（keeper/docx/）里做，这里只取 IR。
   *
   * 这个 hook **必须**放在下面那些提前 return 之前：首帧 fq 还在 loading 时直接
   * 返回（不执行它），数据到达后重渲染才执行——两次渲染的 hook 数量不一致，
   * React 会抛 #310「Rendered more hooks than during the previous render」，
   * 整页白屏（症状与文件类型无关，像是"点开任何文件都崩"）。
   *
   * officeKind 从 path 算（officeKindOf 取最后一段文件名），不依赖 fq.data，
   * 所以首帧也能判定；enabled 保证只有 docx 才真的发请求。 */
  const officeKind = officeKindOf(path);
  const docxIrQ = useQuery({
    queryKey: ["docx-ir", path],
    queryFn: () => apiGetDocxIr(path),
    enabled: officeKind === "docx",
    staleTime: 60_000,
  });

  /* docx 预览报上来的缩放状态与文档元信息（标题栏的缩放组与副标题用它）。
     切文件时清掉——不然上一个文件的「适宽」标签会留在标题栏上。 */
  const [docxInfo, setDocxInfo] = useState<DocxInfo | null>(null);
  useEffect(() => {
    setDocxInfo(null);
  }, [path]);

  // 把编辑态报给标题栏：按钮文案（编辑/预览）、图标、禁用态都在那边渲染。
  useEffect(() => {
    onModeChange?.({
      editing,
      saving,
      editable,
      info: docxInfo,
      /* 「用系统应用打开」：docx 交给 Word，pptx 交给 PowerPoint——
         按钮文案跟着格式走，别让 pptx 也写「用 Word 打开」。 */
      openWithAppLabel:
        officeKind === "docx"
          ? "用 Word 打开"
          : officeKind === "pptx"
            ? "用 PowerPoint 打开"
            : undefined,
    });
  }, [editing, saving, editable, docxInfo, officeKind, onModeChange]);

  /* 预览 ↔ 编辑切换：进编辑先拿当前内容当草稿；退回预览前把草稿落盘。
   *
   * 放在提前 return 之前：标题栏按钮通过 ctl 调它，钩子顺序不能变。 */
  const toggleEdit = async () => {
    if (editing) {
      setSaving(true);
      try {
        await space.write(path, draft);
        qc.invalidateQueries({ queryKey: ["my-space-file", path] });
        toast.success("已保存");
        setEditing(false);
      } catch (e: any) {
        toast.error(e?.message || "保存失败");
      } finally {
        setSaving(false);
      }
    } else {
      setDraft(fq.data?.text ?? "");
      setEditing(true);
    }
  };
  // 句柄里放的是**最新**的 toggleEdit：用 ref 转发，避免每次渲染都重挂句柄。
  const toggleEditRef = useRef(toggleEdit);
  toggleEditRef.current = toggleEdit;
  useEffect(() => {
    if (!ctl) return;
    ctl.current.toggleEdit = () => void toggleEditRef.current();
    return () => {
      ctl.current.toggleEdit = undefined;
    };
  }, [ctl]);

  /* 「用 Word 打开」的命令：标题栏渲染按钮，点它走这里的 openWithApp
     （保真 100% 的那条路——页面内渲染毕竟还原不全）。 */
  useEffect(() => {
    if (!ctl) return;
    ctl.current.openWithApp = () => void openWithApp(path);
    return () => {
      ctl.current.openWithApp = undefined;
    };
  }, [ctl, path]);

  if (fq.isLoading)
    return (
      <div className="flex h-full items-center justify-center gap-2 text-[13px] text-ink-muted">
        <Loader2 className="h-4 w-4 animate-spin" />
        加载中…
      </div>
    );
  if (fq.isError)
    return (
      <div className="p-4">
        <Hint tone="danger">{(fq.error as Error)?.message ?? "读取失败"}</Hint>
      </div>
    );
  const f: MySpaceFile = fq.data!;
  // 与「会话 / 工作区」里的文件预览共用同一张分派表（components/viewers.ts），
  // 保证同样的扩展名在两处用同一套展示器：图片 / PDF / 视频 / Markdown / 代码 / HTML。
  const kind = viewerKindOf(f.name);
  const rawUrl = spaceFileUrl(root, path);

  // 读文件二进制字节 → ArrayBuffer（表格 / PDF 预览用）。base64 由桌面桥 readBinary 给。
  const loadBytes = async (): Promise<ArrayBuffer> => {
    const r = await space.readBinary(path);
    const bin = atob(r.base64);
    const buf = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    return buf.buffer;
  };

  // 表格编辑「保存到原文件」：csv/tsv 是文本，走 space.write；xlsx/xls 是二进制，
  // 转 base64 走 space.writeBinary（桌面壳落盘为字节）。
  /* 用系统默认应用打开（Word / PowerPoint / Excel 的「一键打开」共用这一个）。
     失败必须给反馈：用户机器可能没装对应应用、或只装了 LibreOffice，
     静默无响应用户会以为按钮坏了。 */
  const openWithApp = async (p: string) => {
    const r = await space.openWithApp(p);
    if (!r?.ok) toast.error(r?.error || "没有能打开这个文件的应用");
  };

  const persistSheet = async (p: {
    text?: string;
    binary?: ArrayBuffer;
  }) => {
    if (p.text !== undefined) {
      await space.write(path, p.text);
      return;
    }
    if (p.binary) {
      const bytes = new Uint8Array(p.binary);
      // 分块拼 base64：大文件一次性 fromCharCode.apply 会爆调用栈
      const CHUNK = 0x8000;
      let bin = "";
      for (let i = 0; i < bytes.length; i += CHUNK) {
        bin += String.fromCharCode.apply(
          null,
          Array.from(bytes.subarray(i, i + CHUNK))
        );
      }
      await space.writeBinary(path, btoa(bin));
    }
  };

  // Office 文档：
  //   docx → 内核解析成 IR 后在页面里渲染（只读）；pptx → 仍只给「用系统应用打开」。
  //
  // docx 渲染是 2026-10 起的自研方案（keeper/docx/ 解析 + DocxViewer 画）：
  // 目标是「读得懂、读得舒服」，不是 100% 还原版面——浮动定位、文本框精确位置、
  // SmartArt、公式、修订仍不还原，渲染器会把这类特性列出来提示「用系统应用看原样」。
  // pptx 是**幻灯片**（一页一张、靠版式），与 docx 的流式排版完全不同，
  // 自研渲染的性价比很低，先不动。
  if (officeKind === "docx") {
    if (docxIrQ.isLoading) {
      return (
        <div className="flex h-full items-center justify-center gap-2 text-ink-muted">
          <Loader2 className="h-4 w-4 animate-spin" />
          正在解析文档…
        </div>
      );
    }
    if (docxIrQ.isError || !docxIrQ.data) {
      return (
        <div className="flex h-full flex-col items-center justify-center gap-2 p-4 text-center">
          <FileTypeIcon name={f.name} size="md" />
          <div className="text-[13px] text-ink">读不出这个文档</div>
          <div className="max-w-[440px] text-[12px] leading-[1.7] text-ink-faint">
            {((docxIrQ.error as Error)?.message ?? "解析失败")}
            。可以下载后用系统里的 Word 打开。
          </div>
          <button
            type="button"
            onClick={() => void openWithApp(path)}
            className="mt-1 flex h-7 items-center gap-1 rounded-md border border-line px-2.5 text-[12px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong"
          >
            <ExternalLink className="h-3.5 w-3.5" />
            用系统应用打开
          </button>
        </div>
      );
    }
    return (
      <DocxViewer
        doc={docxIrQ.data}
        /* 缩放控件在标题栏：这里只给命令句柄与状态回报的回调 */
        ctl={ctl}
        onInfo={setDocxInfo}
        /* 检索命中的落点：line = 块序号（与内核切片同口径）→ 滚到那一段 */
        jump={jump}
      />
    );
  }

  if (officeKind) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 p-4 text-center">
        <FileTypeIcon name={f.name} size="md" />
        <div className="text-[13px] text-ink">PowerPoint 演示文稿</div>
        <div className="text-[12px] text-ink-muted">
          {f.name} · {fmtSize(f.size)}
        </div>
        <div className="max-w-[440px] text-[12px] leading-[1.7] text-ink-faint">
          这个格式不做页面内预览——幻灯片靠版式还原，成本高且容易失真。
          用系统里的 PowerPoint 打开看原样，改完保存后回到这里刷新即可看到新内容。
        </div>
        {/* 唯一入口。失败（没装对应应用）要给反馈，不能静默 */}
        <button
          type="button"
          title="用系统里的 PowerPoint 打开"
          onClick={() => void openWithApp(path)}
          className="mt-1 flex h-7 items-center gap-1 rounded-md border border-line px-2.5 text-[12px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong"
        >
          <ExternalLink className="h-3.5 w-3.5" />
          用系统应用打开
        </button>
      </div>
    );
  }

  // 我的空间专属内联预览（走二进制字节，故放在通用分派之前拦截）：
  //   表格 xlsx/xls/csv/tsv → SheetViewer；PDF → PdfViewer。
  // 只读预览：读取失败（过大 / 解析不了）时给出下载兜底，不白屏。
  if (isSheetName(f.name)) {
    return (
      <SheetViewer
        name={f.name}
        loadBytes={loadBytes}
        persist={persistSheet}
        onQuoteSelection={(range) => {
          // 把「文件 + 表格区域」挂进聊天输入框（ChatPage 经 ADD_QUOTE_EVENT 收）。
          // 描述只要路径 + 区域就够，AI 自己去读这个文件的哪一片。
          addQuoteToChat({
            path,
            text: `表格区域 ${range}`,
          });
        }}
        onOpenWithApp={() => void openWithApp(path)}
      />
    );
  }
  if (kind === "pdf") {
    return (
      <PdfViewer
        name={f.name}
        loadBytes={loadBytes}
        jumpToPage={jump?.page ? { page: jump.page, seq: jump.seq } : null}
      />
    );
  }

  // 二进制：不渲染，只给下载
  if (kind === "binary") return <BinaryFileCard file={f} />;

  // 文本类（Markdown / 代码 / 数据 / 纯文本 / HTML）：优先信后端 is_text 信号——
  // 解码失败（伪装成代码的二进制）或过大（> 2MB）都读不出可渲染文本，降级为下载卡。
  const textKind =
    kind === "markdown" ||
    kind === "code" ||
    kind === "data" ||
    kind === "text" ||
    kind === "html";
  if (textKind && (!f.is_text || f.too_large))
    return (
      <BinaryFileCard
        file={f}
        note={f.too_large ? "文件过大（> 2MB），请下载后查看" : "非文本文件，无法预览"}
      />
    );

  // 图片：内联缩放 / 平移预览（复用会话侧的 ImageViewer + PanZoomStage）
  if (kind === "image")
    return (
      <div className="flex h-full items-center justify-center p-4">
        <ImageViewer src={rawUrl} alt={f.name} height={560} />
      </div>
    );

  // 视频：浏览器原生播放器
  if (kind === "video")
    return (
      <div className="flex h-full items-center justify-center p-4">
        <video src={rawUrl} controls style={{ maxHeight: "82vh", maxWidth: "100%" }} />
      </div>
    );

  // HTML：交给浏览器新窗口（小框里没法看，且新窗口能缩放 / 全屏 / 另存）。
  // PDF 已改为内联预览（见上面的 PdfViewer 拦截）。
  if (kind === "html")
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
        <FileTypeIcon name={f.name} size="md" />
        <div className="text-[13px] text-ink">{f.name}</div>
        <div className="flex gap-2">
          <a
            href={rawUrl}
            target="_blank"
            rel="noreferrer"
            className="rounded-md border border-line px-3 py-1.5 text-[12px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong"
          >
            在浏览器中打开 HTML
          </a>
        </div>
      </div>
    );

  // 以下为可编辑文本类（Markdown / 代码 / 数据 / 纯文本）：预览 ↔ 编辑 切换。
  // 预览按类型选查看组件（Markdown 渲染、其余 Monaco 只读高亮）；编辑态为可写 Monaco，
  // 切回预览时把改动写回磁盘（space.write）。HTML 不在此列（预览走浏览器新窗口）。
  const onSelMouseUp = () => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.toString().trim()) {
      setQuoteBtn(null);
      return;
    }
    const rect = sel.getRangeAt(0).getBoundingClientRect();
    setQuoteBtn({
      x: Math.max(8, rect.left + rect.width / 2 - 22),
      y: Math.max(8, rect.top - 38),
      text: sel.toString(),
    });
  };
  const onAddQuote = () => {
    if (quoteBtn) addQuoteToChat({ path, text: quoteBtn.text });
    setQuoteBtn(null);
    window.getSelection()?.removeAllRanges();
  };

  /* 图类文件（.vg.json → Vega，.mmd/.mermaid → Mermaid）：渲染成图来看。
     渲染失败时降级回源码（见 diagramErr），不白屏。 */
  const diagram = diagramKindOf(f.name);

  const preview =
    diagram ? (
      diagramErr ? (
        // 降级态：图渲染不出来 → 看源码。提示条说明为什么掉下来。
        <div className="flex h-full min-h-0 flex-col">
          <div className="flex items-center gap-2 border-b border-line-soft bg-amber-500/[0.08] px-3 py-2 text-[12px] text-amber-700 dark:text-amber-300">
            <TriangleAlert className="h-3.5 w-3.5 shrink-0" />
            <span className="min-w-0 flex-1 truncate">
              {diagram === "vega" ? "图表" : "流程图"}渲染失败，已降级为源码：
              {diagramErr}
            </span>
          </div>
          <div className="min-h-0 flex-1">
            <SourceOnly name={f.name} text={f.text} />
          </div>
        </div>
      ) : diagram === "vega" ? (
        /* height="100%"：画布填满预览区，图落在**可见区域**的正中间。
           固定 520px 时，若预览区比它矮，图就会偏在下面（还要滚动才看全）。 */
        <div className="h-full min-h-0">
          <ChartView src={f.text} height="100%" onError={setDiagramErr} />
        </div>
      ) : (
        <div className="h-full min-h-0">
          <MermaidView src={f.text} height="100%" onError={setDiagramErr} />
        </div>
      )
    ) : kind === "markdown" ? (
      <div className="kp-md mx-auto max-w-[900px]" onMouseUp={onSelMouseUp}>
        <MarkdownView text={f.text} mode="static" jumpTo={jump} />
      </div>
    ) : (
      <div className="h-full min-h-0">
        <Editor
          height="100%"
          language={monacoLangOf(f.name) ?? "plaintext"}
          theme={KEEPER_THEME}
          beforeMount={applyKeeperTheme}
          value={f.text}
          options={{
            readOnly: true,
            minimap: { enabled: false },
            fontSize: 13,
            scrollBeyondLastLine: false,
            wordWrap: "on",
          }}
        />
      </div>
    );

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* 「预览 / 编辑」切换搬到了右侧标题栏（文件名右边），这里不再重复一条状态栏 */}
      <div className="min-h-0 flex-1 overflow-auto">
        {editing ? (
          <div className="h-full min-h-0">
            <Editor
              height="100%"
              language={monacoLangOf(f.name) ?? "plaintext"}
              theme={KEEPER_THEME}
              beforeMount={applyKeeperTheme}
              value={draft}
              onChange={(v) => setDraft(v ?? "")}
              options={{
                minimap: { enabled: false },
                fontSize: 13,
                scrollBeyondLastLine: false,
                wordWrap: "on",
              }}
            />
          </div>
        ) : (
          <>
            {preview}
            {kind === "markdown" && quoteBtn && (
              <button
                type="button"
                onMouseDown={(e) => e.preventDefault()}
                onClick={onAddQuote}
                style={{ position: "fixed", left: quoteBtn.x, top: quoteBtn.y, zIndex: 60 }}
                className="flex h-7 items-center gap-1 rounded-full bg-brand-gradient px-3 text-[12px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-110 active:scale-95"
              >
                <Plus className="h-3.5 w-3.5" />
                添加
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/**
 * 「我的空间」：唯一一个用户空间。
 *
 * - 没配置 → 提醒页，引导去选一个文件夹（服务端目录浏览器）；
 * - 已配置 → 仿 Obsidian 的两栏文件浏览器：左侧目录树、右侧文件内容预览
 *   （Markdown 直接渲染，其它文本按语言高亮，二进制 / 过大文件降级为信息卡）。
 * 选好的路径存 settings.yaml，立即生效。
 */
/** 竖向拖拽分隔条：左右拖动时按水平位移回调，父级据此调整左栏（目录树）宽度。 */
function HResizeBar({ onResize }: { onResize: (dx: number) => void }) {
  const dragging = useRef(false);
  useEffect(() => {
    // 注意：这里的 MouseEvent 必须是 DOM 版（本文件顶部 import 的是 React 的
    // MouseEvent<Element, ...>），原生事件监听器只认 DOM 事件。
    const move = (e: globalThis.MouseEvent) => {
      if (dragging.current) onResize(e.movementX);
    };
    const up = () => {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    return () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
  }, [onResize]);
  return (
    <div
      onMouseDown={() => {
        dragging.current = true;
        document.body.style.cursor = "col-resize";
        document.body.style.userSelect = "none";
      }}
      style={{
        width: 6,
        flex: "0 0 auto",
        cursor: "col-resize",
        background: "var(--kp-border-soft)",
      }}
      className="transition-colors hover:bg-brand/40"
    />
  );
}

export default function UserSpacePage({
  onToggleChat,
  chatCollapsed,
  initialFile,
}: {
  /** 收/展右侧对话区。对话区自己的顶栏与那条竖边都没了，
   *  按钮就放在「文件展示区」的右上角。 */
  onToggleChat?: () => void;
  /** 对话区当前是否已收起（决定按钮的提示文案）。 */
  chatCollapsed?: boolean;
  /**
   * 深链：初始就选中这个文件（绝对路径）。
   *
   * 为什么要它：网页形态没有原生文件树，「我的空间」里没有任何入口能打开
   * 某个具体文件——只能靠 `?file=<绝对路径>` 直达。桌面形态也用它做"从搜索/
   * 引用跳到某个文件"的落点。
   */
  initialFile?: string | null;
} = {}) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["my-space"], queryFn: apiGetMySpace });
  const path = (q.data?.path ?? "").trim();
  // 「我的空间」桌面原生文件操作入口（root 绑定到空间根；会话/内核侧仍走 Python）
  const space = useMemo(() => makeSpaceApi(path), [path]);

  // 已配置态：目录树
  const [treeData, setTreeData] = useState<TNode[]>([]);
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(new Set());
  const [treeLoadingKey, setTreeLoadingKey] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<string | null>(initialFile ?? null);
  /* 「预览 / 编辑」按钮放在右侧标题栏（文件名右边）：
   *   fileMode —— FileViewer 报上来的编辑态（editing/saving/editable）；
   *   fileCtl  —— 反向句柄，标题栏点按钮时用它让 FileViewer 切换。
   * 编辑逻辑仍在 FileViewer 内部，这里只负责渲染按钮。 */
  const fileCtl = useRef<FileViewerCtl>({});
  const [fileMode, setFileMode] = useState<{
    editing: boolean;
    saving: boolean;
    editable: boolean;
    /** docx 预览报上来的缩放状态与文档元信息（别的文件类型没有） */
    info: DocxInfo | null;
    /** 「用系统应用打开」按钮的文案（docx / pptx 有，别的没有） */
    openWithAppLabel?: string;
  }>({ editing: false, saving: false, editable: false, info: null });
  // 跳转目标：从搜索结果点进来时带上（行号 / 页码 / 命中词）。
  // seq 每次 +1 —— 同一个文件同一处连点两次也要重新跳（否则第二次没反应）。
  const [jump, setJump] = useState<{
    path: string;
    line?: number;
    page?: number;
    terms?: string[];
    seq: number;
  } | null>(null);

  // 目录树宽度（可拖拽调节，落 localStorage）；默认 = 原 280px 的 80% = 224px
  const [treeWidth, setTreeWidth] = useState<number>(() => {
    const saved = Number(localStorage.getItem("keeper_space_tree_width"));
    return saved >= 180 && saved <= 640 ? saved : 224;
  });
  useEffect(() => {
    localStorage.setItem("keeper_space_tree_width", String(treeWidth));
  }, [treeWidth]);
  // 目录树可整体收起为细条（仿左侧「隐藏菜单」的折叠交互）
  const [treeCollapsed, setTreeCollapsed] = useState(false);

  // ── 文件搜索（跨两栏，在文件浏览区最上方）──
  const [kw, setKw] = useState("");
  const [debouncedKw, setDebouncedKw] = useState("");
  // 输入停 400ms 才真的发请求：中文输入法拼字过程中不该一直打后端
  useEffect(() => {
    const t = setTimeout(() => setDebouncedKw(kw), 400);
    return () => clearTimeout(t);
  }, [kw]);
  // "results" = 右侧展示搜索结果；"browse" = 展示文件内容
  const [view, setView] = useState<"browse" | "results">("browse");
  useEffect(() => {
    setView(debouncedKw.trim() ? "results" : "browse");
  }, [debouncedKw]);

  // 走内核的检索接口（索引在 Python 侧）。min_score 不传 = 用 UI 门槛 0.5，
  // 低于它的结果不展示——「目录里给人看的搜索」就该是这个严格度。
  const searchQ = useQuery({
    queryKey: ["space-file-search", debouncedKw.trim()],
    queryFn: async () => {
      const q = debouncedKw.trim();
      try {
        const r = await apiSearchQuery({ query: q, top_k: 100, mode: "hybrid" });
        return { ...r, degraded: false };
      } catch (e) {
        // embedding 模型没就绪时退回纯关键词（关键词通道不需要模型），
        // 总比整个搜索不可用强——但得让用户知道这次搜的是"字面匹配"。
        const r = await apiSearchQuery({ query: q, top_k: 100, mode: "keyword" });
        return { ...r, degraded: true };
      }
    },
    enabled: debouncedKw.trim().length > 0,
    staleTime: 30_000,
  });
  // 索引有没有建：没建索引时搜索必然是空的，得说清楚原因而不是显示"没找到"
  const idxQ = useQuery({
    queryKey: ["search-stats"],
    queryFn: apiSearchStats,
    enabled: view === "results",
    staleTime: 20_000,
  });

  const fileHits = useMemo(
    () => groupByFile(searchQ.data?.results ?? [], path),
    [searchQ.data, path]
  );
  // 高亮用的词：用户输入按空格切 + 整串本身
  const kwTerms = useMemo(() => {
    const t = kw.trim();
    return t ? [t, ...t.split(/\s+/)] : [];
  }, [kw]);

  const saveM = useMutation({
    mutationFn: (p: string) => apiSetMySpace(p),
    onSuccess: () => {
      toast.success("已保存「我的空间」");
      qc.invalidateQueries({ queryKey: ["my-space"] });
    },
    onError: (e: any) => toast.error(e?.message || "保存失败"),
  });

  /* 最近一次「打开文件」的目标：空间根还没就绪时先记着，
   * 就绪后补选一次（否则首帧 path 为空会把选中态清掉）。 */
  const openTargetRef = useRef<OpenFileTarget | null>(null);

  /* 「打开某个文件（带落点）」的请求：来自对话页的 @ 检索命中。
   *
   * 两个来源都要接：
   * 1. 事件（空间页已挂载时）——立刻打开并跳到落点；
   * 2. 暂存（事件到达时空间页还没挂载，例如从整页对话切过来）——
   *    挂载后补做一次。
   * 两条路都走同一段逻辑，避免"有时跳有时不跳"。 */
  const applyOpenTarget = useCallback((t: OpenFileTarget) => {
    openTargetRef.current = t;
    setSelectedFile(t.path);
    setView("browse");
    setJump((prev) => ({
      path: t.path,
      line: t.line,
      page: t.page,
      terms: t.terms,
      seq: (prev?.seq ?? 0) + 1,
    }));
  }, []);

  useEffect(() => {
    // 订阅：空间页已挂载时，请求立刻生效
    const off = subscribeOpenFile((t) => {
      takePendingOpenFile(); // 已处理，清掉暂存
      applyOpenTarget(t);
    });
    // 补做：请求到达时本页还没挂载（从整页对话切过来）
    const t = takePendingOpenFile();
    if (t) applyOpenTarget(t);
    return off;
  }, [applyOpenTarget]);

  // path 变化时：重置选中，并加载空间根目录的一级子项
  useEffect(() => {
    if (!path) {
      setTreeData([]);
      // 深链（?file=）的首帧 path 还没回来——这时**不能**清选中，否则深链被抹掉。
      // 同理：对话页 @ 命中的落点也在这时候等着，别把它一起清了。
      if (!initialFile && !openTargetRef.current) setSelectedFile(null);
      return;
    }
    /* 深链：把 ?file= 指到的文件选上。
     *
     * 为什么需要在这里再选一次：首帧 path 为空 → 上面的分支会走掉（现在不清了），
     * 但 path 就绪这一刻才是"这个文件确实在空间内"的判定时刻——顺手在这里确认
     * 一下，避免深链指向空间外的路径时渲染出一个空预览。 */
    if (initialFile && (initialFile === path || initialFile.startsWith(path + "/"))) {
      setSelectedFile(initialFile);
    }
    /* 对话页 @ 命中的落点：空间根就绪这一刻才是"这文件确实在空间内"的判定时刻，
     * 顺手补选一次（首帧 path 为空时选中态被清掉了）。 */
    const ot = openTargetRef.current;
    if (ot && (ot.path === path || ot.path.startsWith(path + "/"))) {
      setSelectedFile(ot.path);
    }
    let cancelled = false;
    setTreeLoadingKey(path);
    (async () => {
      try {
        const kids = await loadChildren(space, path);
        if (cancelled) return;
        // 去掉冗余的根节点「keeperzone」（面板标题已显示空间名），直接把空间内
        // 容物铺在最上层；并默认展开第一层目录（预取其子项，避免展开后空着）。
        const withChildren = await Promise.all(
          kids.map(async (k) => {
            if (!k.isDir) return k;
            try {
              return { ...k, children: await loadChildren(space, k.key) };
            } catch {
              return k;
            }
          })
        );
        setTreeData(withChildren);
        setExpandedKeys(
          new Set(withChildren.filter((k) => k.isDir).map((k) => k.key))
        );
      } catch (e: any) {
        if (!cancelled) toast.error(e?.message || "加载目录失败");
      } finally {
        if (!cancelled) setTreeLoadingKey(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [path, initialFile]);

  const onToggleDir = async (key: string) => {
    const willOpen = !expandedKeys.has(key);
    setExpandedKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
    // 每次展开都重新列目录（仿原生文件管理器：外面新拷进来的文件立即出现）
    if (willOpen) {
      setTreeLoadingKey(key);
      try {
        const kids = await loadChildren(space, key);
        setTreeData((prev) => injectChildren(prev, key, kids));
      } catch (e: any) {
        toast.error(e?.message || "加载子目录失败");
      } finally {
        setTreeLoadingKey(null);
      }
    }
  };

  /** 刷新整棵已展开的树：把每个已加载节点重新列一遍目录（保留展开状态）。 */
  const refreshTree = async () => {
    if (!path) return;
    const loadedKeys: string[] = [];
    const walk = (nodes: TNode[]) =>
      nodes.forEach((n) => {
        if (n.children != null) {
          loadedKeys.push(n.key);
          walk(n.children);
        }
      });
    walk(treeData);
    setTreeLoadingKey(path);
    try {
      // 扁平化后根目录本身不在 treeData 里，必须每次都重新列根，否则在根下
      // 新建 / 上传的文件不会出现在树中（手动点「刷新」也不行）。
      const rootKids = await loadChildren(space, path);
      let data: TNode[] = rootKids;
      for (const k of loadedKeys) {
        try {
          const kids = await loadChildren(space, k);
          data = injectChildren(data, k, kids);
        } catch {
          // 该目录可能已被删除（如刚删掉一个文件夹后刷新），跳过其子树即可
        }
      }
      setTreeData(data);
    } catch (e: any) {
      toast.error(e?.message || "刷新目录失败");
    } finally {
      setTreeLoadingKey(null);
    }
  };

  // 上传 / 导入目标目录 = 当前选中文件所在目录（没选中则空间根）
  const uploadTargetDir =
    selectedFile && selectedFile.includes("/")
      ? selectedFile.replace(/\/[^/]*$/, "") || path
      : path;

  /** 用系统文件选择器选文件，拷入「我的空间」（原生）。 */
  const doPickUpload = async () => {
    const files = await space.pickFile(true);
    if (!files.length) return;
    let ok = 0;
    for (const src of files) {
      try {
        await space.import(src, uploadTargetDir);
        ok++;
      } catch (e: any) {
        toast.error(`导入失败 ${basename(src)}：${e?.message || e}`);
      }
    }
    if (ok) {
      toast.success(`已拷贝 ${ok} 个文件到「我的空间」`);
      refreshTree();
    }
  };

  const doOpen = (key: string, _isDir: boolean) => {
    space.reveal(key).catch((e) => toast.error(e?.message || "打开失败"));
  };

  // 应用内弹窗（替代 window.prompt / window.confirm：沙箱环境下后者会被禁用、静默失效）
  const [promptState, setPromptState] = useState<{
    title: string;
    value: string;
    resolve: (v: string | null) => void;
  } | null>(null);
  const [confirmState, setConfirmState] = useState<{
    message: string;
    resolve: (b: boolean) => void;
  } | null>(null);
  const askName = (title: string, def: string) =>
    new Promise<string | null>((resolve) =>
      setPromptState({ title, value: def, resolve })
    );
  const confirmDialog = (message: string) =>
    new Promise<boolean>((resolve) =>
      setConfirmState({ message, resolve })
    );

  const doRename = async (key: string) => {
    const cur = key.split("/").pop() || "";
    const name = await askName("重命名为：", cur);
    if (!name || name === cur) return;
    try {
      const r = await space.rename(key, name);
      toast.success("已重命名");
      if (selectedFile === key) setSelectedFile(r.path);
      refreshTree();
    } catch (e: any) {
      toast.error(e?.message || "重命名失败");
    }
  };

  const doDelete = async (key: string) => {
    const ok = await confirmDialog(
      `确定删除 ${key.split("/").pop()}？（空间内的文件将被永久删除）`
    );
    if (!ok) return;
    try {
      await space.remove(key);
      toast.success("已删除");
      if (
        selectedFile &&
        (selectedFile === key || selectedFile.startsWith(key + "/"))
      )
        setSelectedFile(null);
      // 清掉被删目录及其后代的展开状态，避免残留
      setExpandedKeys((prev) => {
        const next = new Set(prev);
        next.forEach((k) => {
          if (k === key || k.startsWith(key + "/")) next.delete(k);
        });
        return next;
      });
      refreshTree();
    } catch (e: any) {
      toast.error(e?.message || "删除失败");
    }
  };

  const doMkdir = async (parentKey: string) => {
    const name = await askName("新建文件夹名称：", "新建文件夹");
    if (!name) return;
    try {
      await space.mkdir(parentKey, name);
      toast.success("已新建文件夹");
      refreshTree();
    } catch (e: any) {
      toast.error(e?.message || "新建失败");
    }
  };

  // ── 粘贴（桌面原生）──
  // 从系统剪贴板粘贴（Ctrl+V）：原生选目标目录
  // 仅桌面壳有效：window.keeperHost.readClipboardFiles 由 preload 桥暴露，纯 Web 版没有。
  const hostForClipboard = (window as any).keeperHost;
  const canPaste = !!hostForClipboard?.readClipboardFiles;

  const pasteInto = async (srcs: string[], dst: string) => {
    let ok = 0;
    for (const s of srcs) {
      try {
        await space.import(s, dst);
        ok++;
      } catch (e: any) {
        toast.error(`粘贴失败 ${basename(s)}：${e?.message || e}`);
      }
    }
    if (ok) {
      toast.success(srcs.length > 1 ? `已粘贴 ${ok} 项` : "已复制");
      refreshTree();
    }
  };

  const doPaste = async () => {
    if (!hostForClipboard?.readClipboardFiles) return;
    let paths: string[] = [];
    try {
      paths = await hostForClipboard.readClipboardFiles();
    } catch {
      paths = [];
    }
    if (!paths.length) {
      toast.error("剪贴板里没有可粘贴的文件");
      return;
    }
    const dst = await space.pickFolder();
    if (!dst) return;
    await pasteInto(paths, dst);
  };

  // 全局 Ctrl/Cmd+V：仅桌面壳、且焦点不在输入框时拦截，触发粘贴
  useEffect(() => {
    if (!canPaste) return;
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && (e.key === "v" || e.key === "V")) {
        const t = e.target as HTMLElement | null;
        if (
          t &&
          (t.tagName === "INPUT" ||
            t.tagName === "TEXTAREA" ||
            t.isContentEditable)
        )
          return; // 让输入框走浏览器默认粘贴
        e.preventDefault();
        doPaste();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [doPaste]);

  // 右键菜单分发：原生 Menu 返回的动作 → 对应操作
  const handleContextMenu = async (
    key: string,
    isDir: boolean,
    e: MouseEvent
  ) => {
    e.preventDefault();
    const act = await space.contextMenu(key, isDir, canPaste);
    switch (act) {
      case "add-to-chat": {
        // 树节点的 key 是绝对路径（spaceFs.list 返回的 item.path 就是 full），
        // 直接交给对话侧，agent 拿到即可读。
        addPendingFileRef({
          path: key,
          name: key.split("/").pop() || key,
          isDir,
        });
        toast.success(isDir ? "文件夹已添加到对话" : "文件已添加到对话");
        break;
      }
      case "open":
        doOpen(key, isDir);
        break;
      case "copy":
        await doCopy(key);
        break;
      case "rename":
        await doRename(key);
        break;
      case "delete":
        await doDelete(key);
        break;
      case "mkdir":
        await doMkdir(key);
        break;
      case "paste": {
        const paths =
          (await hostForClipboard?.readClipboardFiles?.().catch(() => [])) || [];
        if (paths.length) await pasteInto(paths, key);
        break;
      }
    }
  };

  const doCopy = async (key: string) => {
    const parent = key.includes("/")
      ? key.slice(0, key.lastIndexOf("/"))
      : "";
    try {
      await space.copy(key, parent);
      toast.success("已复制");
      refreshTree();
    } catch (e: any) {
      toast.error(e?.message || "复制失败");
    }
  };

  // 选择「我的空间」根目录（配置用）：原生目录对话框
  const chooseSpaceFolder = async () => {
    const p = await space.pickFolder();
    if (p) saveM.mutate(p);
  };

  // 桌面原生能力是否可用：只有 Electron 壳会注入 window.keeperSpace。
  // 网页形态下没有原生目录对话框，「选择文件夹」点了不会有任何反应。
  const isDesktop = useMemo(
    () => typeof (window as any).keeperSpace !== "undefined",
    []
  );

  const relPath = selectedFile
    ? selectedFile.slice(path.length).replace(/^\//, "") || "/"
    : "";

  return (
    <div className="flex h-full min-w-0 flex-col">
      {q.isLoading ? (
        <div className="flex items-center gap-2 py-10 text-[13px] text-ink-muted">
          <Loader2 className="h-4 w-4 animate-spin" />
          加载中…
        </div>
      ) : q.isError ? (
        <Hint tone="danger" title="加载失败">
          {(q.error as Error)?.message}
        </Hint>
      ) : !path ? (
        /* 未配置：提醒页 */
        <div className="mt-4 max-w-[760px] rounded-xl border border-dashed border-brand/40 bg-brand/[0.04] p-6">
          <div className="flex items-center gap-3">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-brand-gradient text-white shadow-brand">
              <FolderOpen className="h-5 w-5" />
            </span>
            <div>
              <div className="text-[15px] font-semibold text-ink-strong">
                还没有配置「我的空间」
              </div>
              <div className="flex items-center gap-2 text-[12.5px] text-ink-muted">
                指定一个文件夹作为 agent 默认访问的工作目录
                <span className="shrink-0 rounded-full border border-line bg-surface px-1.5 py-0.5 text-[10.5px] leading-4 text-ink-faint">
                  仅桌面端
                </span>
              </div>
            </div>
          </div>
          <p className="mt-4 text-[13px] leading-6 text-ink-muted">
            「我的空间」是 agent 干活时安全访问的文件夹：bash 等工具可以把这里的路径当作
            合法根，输入框里用 <code className="rounded bg-surface px-1">@我的空间</code>{" "}
            也会展开成它的绝对路径。先选一个你常用的项目文件夹吧。
          </p>
          <button
            type="button"
            onClick={chooseSpaceFolder}
            disabled={!isDesktop}
            title={isDesktop ? "" : "仅桌面端可选择本地文件夹"}
            className="mt-5 inline-flex h-10 items-center gap-2 rounded-lg bg-brand-gradient px-5 text-[14px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-45 disabled:hover:brightness-100 disabled:active:scale-100"
          >
            <FolderOpen className="h-4 w-4" />
            选择文件夹
          </button>
        </div>
      ) : (
        /* 已配置：两栏文件浏览器（目录树可拖宽 / 可收起）+ 右侧展示区。
           搜索框在右侧「文件展示区」里（见下），不占左边的浏览区。 */
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          {/* 面板上方的一行：两个开关分别贴在「文件展示区」的左右上角——
              左边收起/展开目录浏览区，右边收/展对话区。
              这一行压得尽量扁（只有按钮本身的高度），别在面板上方留一条空白带。
              左边先放一个与目录树等宽的占位块，按钮才正好落在展示区的左缘；
              目录收起时占位为 0，按钮跟着贴到展示区的新左缘。 */}
          <div className="flex items-center pb-1">
            <div className="shrink-0" style={{ width: treeCollapsed ? 0 : treeWidth + 6 }} />
            <button
              type="button"
              onClick={() => setTreeCollapsed((v) => !v)}
              title={treeCollapsed ? "展开目录" : "收起目录"}
              className="flex h-6 w-6 items-center justify-center rounded-md text-ink-faint transition-colors hover:bg-surface hover:text-ink-strong"
            >
              {/* 目录浏览区：展开时向右（PanelRight），收起时向左（PanelLeft） */}
              {treeCollapsed ? (
                <PanelLeft className="h-4 w-4" />
              ) : (
                <PanelRight className="h-4 w-4" />
              )}
            </button>
            {onToggleChat && (
              <button
                type="button"
                onClick={onToggleChat}
                title={chatCollapsed ? "展开对话" : "收起对话"}
                className="ml-auto flex h-6 w-6 items-center justify-center rounded-md text-ink-faint transition-colors hover:bg-surface hover:text-ink-strong"
              >
                {/* 对话区：展开时向左（PanelLeft），收起时向右（PanelRight） */}
                {chatCollapsed ? (
                  <PanelRight className="h-4 w-4" />
                ) : (
                  <PanelLeft className="h-4 w-4" />
                )}
              </button>
            )}
          </div>
          <div className="flex min-h-0 min-w-0 flex-1">
          {/* 左侧目录树：收起时不占位（收起/展开按钮在展示区左上角那一行） */}
          {treeCollapsed ? null : (
            <>
              <div
                className="flex shrink-0 flex-col overflow-hidden rounded-xl bg-panel"
                style={{ width: treeWidth }}
              >
            {/* 目录功能区：只留操作按钮（目录名与名字左边的图标不展示了），靠左排。
                高度刻意跟右侧搜索框区一样（10 + 36 + 4 = 50px），
                两栏的内容才落在同一条水平线上。 */}
            <div className="flex items-center gap-1 px-2 pb-1 pt-2.5">
              <div className="flex h-9 w-full items-center justify-start gap-1">
                <button
                  type="button"
                  onClick={() => doOpen(path, true)}
                  title="在文件管理器中打开"
                  className="flex h-7 w-7 items-center justify-center rounded-md text-ink-faint transition-colors hover:bg-surface hover:text-ink-strong"
                >
                  <FolderOpen className="h-[17px] w-[17px]" />
                </button>
                <button
                  type="button"
                  onClick={doPickUpload}
                  title="上传文件到此处"
                  className="flex h-7 w-7 items-center justify-center rounded-md text-ink-faint transition-colors hover:bg-surface hover:text-ink-strong"
                >
                  <Upload className="h-[17px] w-[17px]" />
                </button>
                <button
                  type="button"
                  onClick={() => doMkdir(uploadTargetDir)}
                  title="在此新建文件夹"
                  className="flex h-7 w-7 items-center justify-center rounded-md text-ink-faint transition-colors hover:bg-surface hover:text-ink-strong"
                >
                  <FolderPlus className="h-[17px] w-[17px]" />
                </button>
                <button
                  type="button"
                  onClick={refreshTree}
                  title="刷新目录（列出外部新增的文件）"
                  className="flex h-7 w-7 items-center justify-center rounded-md text-ink-faint transition-colors hover:bg-surface hover:text-ink-strong"
                >
                  {treeLoadingKey === path ? (
                    <Loader2 className="h-[17px] w-[17px] animate-spin" />
                  ) : (
                    <RefreshCw className="h-[17px] w-[17px]" />
                  )}
                </button>
              </div>
            </div>
            <div className="min-h-0 flex-1 overflow-auto py-1">
              <TreeNav
                nodes={treeData}
                depth={0}
                expanded={expandedKeys}
                selected={selectedFile}
                loadingKey={treeLoadingKey}
                onToggle={onToggleDir}
                onSelectFile={(k) => setSelectedFile(k)}
                onOpen={doOpen}
                onRename={doRename}
                onDelete={doDelete}
                onMkdirHere={doMkdir}
                onContextMenu={(key, isDir, e) => handleContextMenu(key, isDir, e)}
              />
              <div className="px-3 py-2 text-[11px] leading-5 text-ink-faint">
                <div className="text-ink-muted">仅桌面端支持本地空间展示</div>
                点「上传」按钮从电脑选文件拷入「我的空间」；hover 每行可在文件管理器中打开 / 重命名 / 删除，右键菜单里还有复制 / 粘贴。
              </div>
            </div>
          </div>
          <HResizeBar onResize={(dx) => setTreeWidth((w) => Math.max(180, Math.min(640, w + dx)))} />
          </>)}

          {/* 右侧内容预览 / 搜索结果 */}
          <div className="flex min-w-0 flex-1 flex-col overflow-hidden rounded-xl bg-panel">
            {/* 搜索框：属于「文件展示区」，宽度跟着这一区走——
                搜出来的结果也展示在这一区，放一起才对得上。
                bg-surface：面板本身是 bg-panel（白），输入框得比面板深一档才看得出是个框。
                聚焦时不描蓝边（focus:border-brand 太扎眼），改成中性灰描边。 */}
            <div className="px-3 pb-1 pt-2.5">
              <div className="relative">
                <SearchIcon className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-ink-faint" />
                <input
                  value={kw}
                  onChange={(e) => setKw(e.target.value)}
                  onKeyDown={(e) => {
                    // 输入法拼字中：回车是确认候选词，不是提交
                    if (isComposingKey(e)) return;
                    if (e.key === "Enter") {
                      setDebouncedKw(kw.trim());
                      setView("results");
                    }
                  }}
                  placeholder="搜索「我的空间」里的文件…"
                  /* min-w-0：input 有默认的固有宽度（约 20 字符），不让它缩的话
                     窗口变窄时它不肯跟着缩，框会顶破容器。 */
                  className="h-9 w-full min-w-0 rounded-lg border border-line-soft bg-surface pl-8 pr-14 text-[13px] text-ink shadow-card outline-none placeholder:text-ink-faint focus:border-line"
                />
                {/* 搜索中的转圈：放在框里（绝对定位），不占布局宽度——
                    否则它一出现，搜索框就缩掉 24px，框里的文字跟着跳。 */}
                {searchQ.isFetching && (
                  <Loader2 className="absolute right-8 top-1/2 h-3.5 w-3.5 -translate-y-1/2 animate-spin text-ink-faint" />
                )}
                {kw && (
                  <button
                    type="button"
                    onClick={() => setKw("")}
                    title="清空"
                    className="absolute right-2 top-1/2 flex h-5 w-5 -translate-y-1/2 items-center justify-center rounded text-ink-faint transition-colors hover:bg-panel hover:text-ink-strong"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
            </div>
            <div className="flex items-center gap-2 px-3 py-2 text-[12px] text-ink-muted">
              {view === "results" ? (
                <span className="flex min-w-0 items-center gap-1.5 truncate">
                  <SearchIcon className="h-3.5 w-3.5 shrink-0 text-brand" />
                  <span>搜索结果</span>
                  {fileHits.length > 0 && (
                    <span className="tabular-nums text-ink-faint">
                      （{fileHits.length} 个文件）
                    </span>
                  )}
                </span>
              ) : selectedFile ? (
                /* 文件名**只在这里出现一次**：预览区自己那条工具条已经撤掉
                   （原先两处都写文件名，同一个名字前后各出现一遍）。 */
                <span className="flex min-w-0 items-center gap-1.5 truncate">
                  <FileTypeIcon name={relPath} size="xs" />
                  <span className="truncate font-mono">{relPath}</span>
                  {fileMode.info?.meta && (
                    <span className="shrink-0 text-ink-faint">
                      {fileMode.info.meta}
                    </span>
                  )}
                </span>
              ) : (
                <span className="text-ink-faint">未选择文件</span>
              )}
              <div className="ml-auto flex shrink-0 items-center gap-2">
                {view === "browse" && kw.trim() && (
                  <button
                    type="button"
                    onClick={() => setView("results")}
                    className="flex items-center gap-1 rounded-md border border-line px-2 py-1 text-[12px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong"
                  >
                    <ArrowLeft className="h-3 w-3" />
                    搜索结果
                  </button>
                )}
                {view === "browse" && selectedFile && fileMode.info && (
                  /* 缩放组：默认「适应页宽」（页面比预览区窄时整页等比缩小），
                     也可以手动放大看原始字号。原先这条控件在预览区自己的工具条上，
                     跟标题栏的「添加到对话」隔了一行——现在并到同一行。
                     命令发给 DocxViewer（它才知道"适宽"该是多少），状态由它回报。 */
                  <div className="flex shrink-0 items-center gap-0.5 rounded-lg border border-line bg-panel px-0.5 py-0.5">
                    <button
                      type="button"
                      title="缩小"
                      disabled={!fileMode.info.canOut}
                      onClick={() => fileCtl.current.zoomOut?.()}
                      className="rounded-md p-1 text-ink-muted transition-colors hover:bg-surface hover:text-ink-strong disabled:opacity-35"
                    >
                      <Minus className="h-3.5 w-3.5" />
                    </button>
                    <button
                      type="button"
                      title="适应页宽"
                      onClick={() => fileCtl.current.zoomFit?.()}
                      className={
                        "min-w-[52px] rounded-md px-1.5 py-0.5 text-[11.5px] tabular-nums transition-colors " +
                        (fileMode.info.zoom === "适宽"
                          ? "bg-brand/15 text-ink-strong"
                          : "text-ink-muted hover:bg-surface hover:text-ink-strong")
                      }
                    >
                      {fileMode.info.zoom}
                    </button>
                    <button
                      type="button"
                      title="放大"
                      disabled={!fileMode.info.canIn}
                      onClick={() => fileCtl.current.zoomIn?.()}
                      className="rounded-md p-1 text-ink-muted transition-colors hover:bg-surface hover:text-ink-strong disabled:opacity-35"
                    >
                      <Plus className="h-3.5 w-3.5" />
                    </button>
                  </div>
                )}
                {view === "browse" && selectedFile && fileMode.openWithAppLabel && (
                  /* 「用 Word 打开」：保真 100% 的那条路（页面内渲染还原不全时兜底）。
                     文案跟着格式走：pptx 是「用 PowerPoint 打开」。 */
                  <button
                    type="button"
                    onClick={() => fileCtl.current.openWithApp?.()}
                    title="用系统里的应用打开（100% 保真，可编辑）"
                    className="flex h-7 shrink-0 items-center gap-1 rounded-md border border-line px-2.5 text-[12px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong"
                  >
                    <ExternalLink className="h-3.5 w-3.5" />
                    {fileMode.openWithAppLabel}
                  </button>
                )}
                {view === "browse" && selectedFile && (
                  /* 「添加到对话」：把当前预览的文件挂进聊天输入框。
                     与目录树右键「添加到对话」走同一条路（模块级暂存），
                     两处都只是登记绝对路径，内容由 agent 自己读。 */
                  <button
                    type="button"
                    onClick={() => {
                      addPendingFileRef({
                        path: selectedFile,
                        name: selectedFile.split("/").pop() || selectedFile,
                        isDir: false,
                      });
                      toast.success("已添加到对话");
                    }}
                    title="把这个文件挂进聊天输入框"
                    className="flex h-7 shrink-0 items-center gap-1 rounded-md border border-line px-2.5 text-[12px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong"
                  >
                    <MessageSquarePlus className="h-3.5 w-3.5" />
                    添加到对话
                  </button>
                )}
                {view === "browse" && selectedFile && fileMode.editable && (
                  /* 「预览 / 编辑」：可编辑的文本类文件才出现。
                     点「编辑」进 Monaco，改完点「预览」会先落盘再切回预览态。 */
                  <button
                    type="button"
                    onClick={() => fileCtl.current.toggleEdit?.()}
                    disabled={fileMode.saving}
                    title={fileMode.editing ? "保存并回到预览" : "编辑这个文件"}
                    className={
                      fileMode.editing
                        ? "flex h-7 shrink-0 items-center gap-1 rounded-md border border-brand/50 bg-brand/[0.08] px-2.5 text-[12px] font-medium text-brand transition-all duration-150 hover:bg-brand/[0.14] disabled:opacity-50"
                        : "flex h-7 shrink-0 items-center gap-1 rounded-md border border-line px-2.5 text-[12px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong disabled:opacity-50"
                    }
                  >
                    {fileMode.saving ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : fileMode.editing ? (
                      <Eye className="h-3.5 w-3.5" />
                    ) : (
                      <Pencil className="h-3.5 w-3.5" />
                    )}
                    {fileMode.editing ? "预览" : "编辑"}
                  </button>
                )}
              </div>
            </div>
            <div className="min-h-0 flex-1 overflow-auto p-4">
              {view === "results" ? (
                <SearchResults
                  hits={fileHits}
                  terms={kwTerms}
                  loading={searchQ.isLoading || idxQ.isLoading}
                  error={(searchQ.error as Error)?.message}
                  noIndex={idxQ.data ? idxQ.data.documents === 0 : false}
                  degraded={!!searchQ.data?.degraded}
                  onOpen={(h) => {
                    setSelectedFile(h.path);
                    // 把落点带过去：Markdown 跳到那一行，PDF 跳到那一页
                    setJump((prev) => ({
                      path: h.path,
                      line: h.start_line || undefined,
                      page: h.page || undefined,
                      terms: h.matched,
                      seq: (prev?.seq ?? 0) + 1,
                    }));
                    setView("browse");
                  }}
                />
              ) : selectedFile ? (
                <FileViewer
                  space={space}
                  root={path}
                  path={selectedFile}
                  jump={jump && jump.path === selectedFile ? jump : null}
                  ctl={fileCtl}
                  onModeChange={setFileMode}
                />
              ) : (
                <div className="flex h-full flex-col items-center justify-center gap-2 text-center text-ink-faint">
                  <FileText className="h-8 w-8" />
                  <div className="text-[13px]">从左侧选择一个文件查看内容</div>
                </div>
              )}
            </div>
          </div>
          </div>
        </div>
      )}

      {promptState && (
        <AppModal
          title={promptState.title}
          onClose={() => {
            const r = promptState.resolve;
            setPromptState(null);
            r(null);
          }}
        >
          <input
            autoFocus
            value={promptState.value}
            onChange={(e) => setPromptState({ ...promptState, value: e.target.value })}
            onKeyDown={(e) => {
              // 输入法拼字中：回车是确认候选词，不是提交这个 prompt
              if (isComposingKey(e)) return;
              if (e.key === "Enter") {
                const v = promptState.value.trim();
                const r = promptState.resolve;
                setPromptState(null);
                r(v || null);
              } else if (e.key === "Escape") {
                const r = promptState.resolve;
                setPromptState(null);
                r(null);
              }
            }}
            className="w-full rounded-md border border-line bg-bg px-3 py-1.5 text-[13px] text-ink outline-none focus:border-brand"
          />
          <div className="mt-3 flex justify-end gap-2">
            <button
              type="button"
              onClick={() => {
                const r = promptState.resolve;
                setPromptState(null);
                r(null);
              }}
              className="h-8 rounded-lg border border-line px-4 text-[13px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong"
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => {
                const v = promptState.value.trim();
                const r = promptState.resolve;
                setPromptState(null);
                r(v || null);
              }}
              className="h-8 rounded-lg bg-brand-gradient px-4 text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98]"
            >
              确定
            </button>
          </div>
        </AppModal>
      )}
      {confirmState && (
        <AppModal
          title="确认"
          onClose={() => {
            const r = confirmState.resolve;
            setConfirmState(null);
            r(false);
          }}
        >
          <div className="mb-3 text-[13px] text-ink-muted">{confirmState.message}</div>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => {
                const r = confirmState.resolve;
                setConfirmState(null);
                r(false);
              }}
              className="h-8 rounded-lg border border-line px-4 text-[13px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong"
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => {
                const r = confirmState.resolve;
                setConfirmState(null);
                r(true);
              }}
              className="h-8 rounded-lg bg-brand-gradient px-4 text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98]"
            >
              确定
            </button>
          </div>
        </AppModal>
      )}

    </div>
  );
}
