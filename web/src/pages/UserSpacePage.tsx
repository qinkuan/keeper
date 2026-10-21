import { useEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";
import {
  ChevronRight,
  Copy,
  Eye,
  File as FileIcon,
  Plus,
  FileText,
  Folder,
  FolderOpen,
  FolderPlus,
  Loader2,
  PanelLeft,
  Pencil,
  RefreshCw,
  Trash2,
  Upload,
} from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGetMySpace, apiSetMySpace } from "../api/client";
import type { MySpaceFile } from "../api/types";
import { makeSpaceApi, spaceFileUrl, type SpaceApi } from "../api/spaceNative";
import { Hint } from "../components/ui/Page";
import { toast } from "../components/ui/Toast";
import { cn } from "../lib/utils";
import MarkdownView from "../components/MarkdownView";
import { addQuoteToChat } from "../lib/quote";
import ImageViewer from "../components/ImageViewer";
import Editor from "@monaco-editor/react";
import { applyKeeperTheme, KEEPER_THEME } from "../monacoTheme";
import { monacoLangOf } from "../monacoLang";
import { viewerKindOf } from "../components/viewers";

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
  onCopy,
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
  onCopy: (key: string) => void;
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
                  <FileIcon className="h-3.5 w-3.5 shrink-0 text-ink-faint" />
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
                  title="复制（在当前目录生成副本）"
                  onClick={() => onCopy(n.key)}
                >
                  <Copy className="h-3.5 w-3.5" />
                </RowBtn>
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
                onCopy={onCopy}
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
      <FileText className="h-8 w-8 text-ink-faint" />
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

function FileViewer({
  space,
  root,
  path,
}: {
  space: SpaceApi;
  root: string;
  path: string;
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
  useEffect(() => {
    setEditing(false);
    setDraft("");
  }, [path]);
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

  // PDF / HTML：交给浏览器新窗口（小框里没法看，且新窗口能缩放 / 全屏 / 另存）
  if (kind === "pdf" || kind === "html")
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
        <FileText className="h-8 w-8 text-ink-faint" />
        <div className="text-[13px] text-ink">{f.name}</div>
        <div className="flex gap-2">
          <a
            href={rawUrl}
            target="_blank"
            rel="noreferrer"
            className="rounded-md border border-line px-3 py-1.5 text-[12px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong"
          >
            {kind === "pdf" ? "在浏览器中打开 PDF" : "在浏览器中打开 HTML"}
          </a>
        </div>
      </div>
    );

  // 以下为可编辑文本类（Markdown / 代码 / 数据 / 纯文本）：预览 ↔ 编辑 切换。
  // 预览按类型选查看组件（Markdown 渲染、其余 Monaco 只读高亮）；编辑态为可写 Monaco，
  // 切回预览时把改动写回磁盘（space.write）。HTML 不在此列（预览走浏览器新窗口）。
  const editable = f.is_text && !f.too_large;
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
  const toggleEdit = async () => {
    if (!editing) {
      setDraft(f.text ?? "");
      setEditing(true);
      return;
    }
    // 由编辑切回预览：落盘保存
    try {
      setSaving(true);
      await space.write(path, draft);
      qc.invalidateQueries({ queryKey: ["my-space-file", path] });
      toast.success("已保存");
    } catch (e: any) {
      toast.error(e?.message || "保存失败");
      return;
    } finally {
      setSaving(false);
    }
    setEditing(false);
  };

  const preview =
    kind === "markdown" ? (
      <div className="kp-md mx-auto max-w-[900px]" onMouseUp={onSelMouseUp}>
        <MarkdownView text={f.text} mode="static" />
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
      {editable && (
        <div className="flex items-center gap-2 border-b border-line-soft px-3 py-2 text-[12px]">
          {editing ? (
            <span className="flex items-center gap-1.5 font-medium text-brand">
              <span className="h-1.5 w-1.5 rounded-full bg-brand" />
              编辑中
            </span>
          ) : (
            <span className="truncate font-mono text-ink-muted">预览</span>
          )}
          <button
            type="button"
            onClick={toggleEdit}
            disabled={saving}
            className={
              editing
                ? "ml-auto flex h-7 shrink-0 items-center gap-1 rounded-md border border-brand/50 bg-brand/[0.08] px-2.5 text-[12px] font-medium text-brand transition-all duration-150 hover:bg-brand/[0.14] disabled:opacity-50"
                : "ml-auto flex h-7 shrink-0 items-center gap-1 rounded-md border border-line px-2.5 text-[12px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong disabled:opacity-50"
            }
          >
            {saving ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : editing ? (
              <Eye className="h-3.5 w-3.5" />
            ) : (
              <Pencil className="h-3.5 w-3.5" />
            )}
            {editing ? "预览" : "编辑"}
          </button>
        </div>
      )}
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
    const move = (e: MouseEvent) => {
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

export default function UserSpacePage() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["my-space"], queryFn: apiGetMySpace });
  const path = (q.data?.path ?? "").trim();
  // 「我的空间」桌面原生文件操作入口（root 绑定到空间根；会话/内核侧仍走 Python）
  const space = useMemo(() => makeSpaceApi(path), [path]);

  // 已配置态：目录树
  const [treeData, setTreeData] = useState<TNode[]>([]);
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(new Set());
  const [treeLoadingKey, setTreeLoadingKey] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);

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

  const saveM = useMutation({
    mutationFn: (p: string) => apiSetMySpace(p),
    onSuccess: () => {
      toast.success("已保存「我的空间」");
      qc.invalidateQueries({ queryKey: ["my-space"] });
    },
    onError: (e: any) => toast.error(e?.message || "保存失败"),
  });

  // path 变化时：重置选中，并加载空间根目录的一级子项
  useEffect(() => {
    if (!path) {
      setTreeData([]);
      setSelectedFile(null);
      return;
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
  }, [path]);

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

  /** 刷新当前打开的文件内容（外面改了、点一下立刻看到最新）。 */
  const refreshFile = () => {
    if (selectedFile)
      qc.invalidateQueries({ queryKey: ["my-space-file", selectedFile] });
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

  const relPath = selectedFile
    ? selectedFile.slice(path.length).replace(/^\//, "") || "/"
    : "";

  return (
    <div className="flex h-full flex-col">
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
        <div className="max-w-[760px] rounded-xl border border-dashed border-brand/40 bg-brand/[0.04] p-6">
          <div className="flex items-center gap-3">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-brand-gradient text-white shadow-brand">
              <FolderOpen className="h-5 w-5" />
            </span>
            <div>
              <div className="text-[15px] font-semibold text-ink-strong">
                还没有配置「我的空间」
              </div>
              <div className="text-[12.5px] text-ink-muted">
                指定一个文件夹作为 agent 默认访问的工作目录
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
            className="mt-5 inline-flex h-10 items-center gap-2 rounded-lg bg-brand-gradient px-5 text-[14px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98]"
          >
            <FolderOpen className="h-4 w-4" />
            选择文件夹
          </button>
        </div>
      ) : (
        /* 已配置：两栏文件浏览器（目录树可拖宽 / 可收起） */
        <div className="mt-4 flex min-h-0 flex-1">
          {/* 左侧目录树：收起时退化为细条 + 展开按钮 */}
          {treeCollapsed ? (
            <div className="flex w-9 shrink-0 flex-col items-center rounded-xl border border-line bg-panel py-2">
              <button
                type="button"
                onClick={() => setTreeCollapsed(false)}
                title="展开目录"
                className="flex h-7 w-7 items-center justify-center rounded-md text-ink-faint transition-colors hover:bg-surface hover:text-ink-strong"
              >
                <PanelLeft className="h-4 w-4" />
              </button>
            </div>
          ) : (
            <>
              <div
                className="flex shrink-0 flex-col overflow-hidden rounded-xl border border-line bg-panel"
                style={{ width: treeWidth }}
              >
            <div className="flex items-center gap-1 border-b border-line-soft px-2 py-1.5 text-[12px] font-medium text-ink-muted">
              <FolderOpen className="h-3.5 w-3.5 shrink-0 text-brand" />
              <span className="truncate" title={path}>{basename(path)}</span>
              <div className="ml-auto flex items-center gap-0.5">
                <button
                  type="button"
                  onClick={() => doOpen(path, true)}
                  title="在文件管理器中打开"
                  className="flex h-6 w-6 items-center justify-center rounded-md text-ink-faint transition-colors hover:bg-surface hover:text-ink-strong"
                >
                  <FolderOpen className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  onClick={doPickUpload}
                  title="上传文件到此处"
                  className="flex h-6 w-6 items-center justify-center rounded-md text-ink-faint transition-colors hover:bg-surface hover:text-ink-strong"
                >
                  <Upload className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  onClick={() => doMkdir(uploadTargetDir)}
                  title="在此新建文件夹"
                  className="flex h-6 w-6 items-center justify-center rounded-md text-ink-faint transition-colors hover:bg-surface hover:text-ink-strong"
                >
                  <FolderPlus className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  onClick={refreshTree}
                  title="刷新目录（列出外部新增的文件）"
                  className="flex h-6 w-6 items-center justify-center rounded-md text-ink-faint transition-colors hover:bg-surface hover:text-ink-strong"
                >
                  {treeLoadingKey === path ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <RefreshCw className="h-3.5 w-3.5" />
                  )}
                </button>
                <button
                  type="button"
                  onClick={() => setTreeCollapsed(true)}
                  title="收起目录"
                  className="flex h-6 w-6 items-center justify-center rounded-md text-ink-faint transition-colors hover:bg-surface hover:text-ink-strong"
                >
                  <PanelLeft className="h-3.5 w-3.5" />
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
                onCopy={doCopy}
                onContextMenu={(key, isDir, e) => handleContextMenu(key, isDir, e)}
              />
              <div className="px-3 py-2 text-[11px] leading-5 text-ink-faint">
                点「上传」按钮从电脑选文件拷入「我的空间」；hover 每行可下载 / 重命名 / 删除。
              </div>
            </div>
          </div>
          <HResizeBar onResize={(dx) => setTreeWidth((w) => Math.max(180, Math.min(640, w + dx)))} />
          </>)}

          {/* 右侧内容预览 */}
          <div className="flex min-w-0 flex-1 flex-col overflow-hidden rounded-xl border border-line bg-panel">
            <div className="flex items-center gap-2 border-b border-line-soft px-3 py-2 text-[12px] text-ink-muted">
              {selectedFile ? (
                <span className="flex min-w-0 items-center gap-1.5 truncate">
                  <FileText className="h-3.5 w-3.5 shrink-0 text-ink-faint" />
                  <span className="truncate font-mono">{relPath}</span>
                </span>
              ) : (
                <span className="text-ink-faint">未选择文件</span>
              )}
              {selectedFile && (
                <button
                  type="button"
                  onClick={() => doOpen(selectedFile, false)}
                  title="在文件管理器中打开"
                  className="ml-auto shrink-0 rounded-md border border-line px-2 py-1 text-[12px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong"
                >
                  打开
                </button>
              )}
              <button
                type="button"
                onClick={refreshFile}
                title="刷新文件（读取外部修改后的最新内容）"
                className="shrink-0 rounded-md border border-line px-2 py-1 text-[12px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong"
              >
                刷新
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-auto p-4">
              {selectedFile ? (
                <FileViewer space={space} root={path} path={selectedFile} />
              ) : (
                <div className="flex h-full flex-col items-center justify-center gap-2 text-center text-ink-faint">
                  <FileText className="h-8 w-8" />
                  <div className="text-[13px]">从左侧选择一个文件查看内容</div>
                </div>
              )}
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
