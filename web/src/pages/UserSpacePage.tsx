import { useEffect, useRef, useState } from "react";
import {
  ChevronRight,
  Download,
  File as FileIcon,
  FileText,
  Folder,
  FolderOpen,
  FolderPlus,
  Loader2,
  Pencil,
  RefreshCw,
  Trash2,
  Upload,
} from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  apiBrowseDir,
  apiDeleteMySpaceFile,
  apiDownloadMySpace,
  apiGetMySpace,
  apiMkdirMySpace,
  apiMySpaceFile,
  apiOpenMySpace,
  apiRenameMySpace,
  apiSetMySpace,
  apiUploadMySpace,
} from "../api/client";
import type { DirBrowseResp, MySpaceFile } from "../api/types";
import { Hint, PageHeader } from "../components/ui/Page";
import { Modal } from "../components/ui/Modal";
import { toast } from "../components/ui/Toast";
import { cn } from "../lib/utils";
import MarkdownView from "../components/MarkdownView";

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

/** 拉取某目录的子项（目录 + 文件，目录在前、按名排序），用于左侧树。 */
async function loadChildren(p: string): Promise<TNode[]> {
  const r = await apiBrowseDir(p);
  const dirs: TNode[] = r.dirs.map((d) => ({
    title: d.name,
    key: d.path,
    isDir: true,
  }));
  const files: TNode[] = r.files.map((f) => ({
    title: f.name,
    key: f.path,
    isDir: false,
    leaf: true,
  }));
  return [...dirs, ...files].sort(
    (a, b) => Number(b.isDir) - Number(a.isDir) || a.title.localeCompare(b.title)
  );
}

/** 在树里把 target 路径的祖先链都展开、确保中间节点已加载。 */
async function syncTreeToPath(
  target: string,
  data0: TNode[]
): Promise<{ data: TNode[]; expanded: Set<string> }> {
  const segs = target.split("/").filter(Boolean);
  const chain: string[] = [
    "/",
    ...segs.map((_, i) => "/" + segs.slice(0, i + 1).join("/")),
  ];
  let data = data0;
  for (let i = 0; i < chain.length - 1; i++) {
    const key = chain[i];
    const node = findNode(data, key);
    if (!node || node.children == null) {
      const kids = await loadChildren(key);
      data = injectChildren(data, key, kids);
    }
  }
  return { data, expanded: new Set(chain.slice(0, -1)) };
}

const EXT_LANG: Record<string, string> = {
  ts: "typescript", tsx: "tsx", js: "javascript", jsx: "jsx", mjs: "javascript",
  py: "python", rb: "ruby", go: "go", rs: "rust", java: "java", kt: "kotlin",
  c: "c", h: "c", cpp: "cpp", cs: "csharp", php: "php", swift: "swift",
  json: "json", yml: "yaml", yaml: "yaml", toml: "toml", ini: "ini", cfg: "ini",
  sh: "bash", bash: "bash", zsh: "bash", ps1: "powershell",
  html: "html", htm: "html", css: "css", scss: "scss", less: "less",
  sql: "sql", xml: "xml", svg: "xml", md: "markdown", lua: "lua",
  dockerfile: "docker", makefile: "makefile",
};
function extLang(name: string): string {
  const ext = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  return EXT_LANG[ext] ?? "";
}
function fence(content: string, lang: string): string {
  return "```" + lang + "\n" + content + "\n```";
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
  onDownload,
  onRename,
  onDelete,
  onMkdirHere,
}: {
  nodes: TNode[];
  depth: number;
  expanded: Set<string>;
  selected: string | null;
  loadingKey: string | null;
  onToggle: (key: string) => void;
  onSelectFile: (key: string) => void;
  onOpen: (key: string, isDir: boolean) => void;
  onDownload: (key: string) => void;
  onRename: (key: string) => void;
  onDelete: (key: string) => void;
  onMkdirHere: (key: string) => void;
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
                {n.isDir ? (
                  <RowBtn title="在此新建子文件夹" onClick={() => onMkdirHere(n.key)}>
                    <FolderPlus className="h-3.5 w-3.5" />
                  </RowBtn>
                ) : (
                  <RowBtn
                    title="下载 / 用系统程序打开"
                    onClick={() => onDownload(n.key)}
                  >
                    <Download className="h-3.5 w-3.5" />
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
                onDownload={onDownload}
                onRename={onRename}
                onDelete={onDelete}
                onMkdirHere={onMkdirHere}
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
function FileViewer({ path }: { path: string }) {
  const fq = useQuery({
    queryKey: ["my-space-file", path],
    queryFn: () => apiMySpaceFile(path),
    // 切回窗口时自动拉最新（你在别的编辑器改完、点回本页即见）
    refetchOnWindowFocus: true,
  });
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
  if (!f.is_text || f.too_large)
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 text-center">
        <FileText className="h-8 w-8 text-ink-faint" />
        <div className="text-[13px] text-ink">无法预览此文件</div>
        <div className="text-[12px] text-ink-muted">
          {f.name} · {fmtSize(f.size)}
        </div>
        <div className="text-[12px] text-ink-faint">
          {f.too_large ? "文件过大（> 2MB）" : "非文本文件"}
        </div>
      </div>
    );
  const ext = f.name.includes(".") ? f.name.split(".").pop()!.toLowerCase() : "";
  const isMd = ["md", "markdown", "mdx"].includes(ext);
  const text = isMd ? f.text : fence(f.text, extLang(f.name));
  return (
    <div className="kp-md mx-auto max-w-[900px]">
      <MarkdownView text={text} mode="static" />
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
export default function UserSpacePage() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["my-space"], queryFn: apiGetMySpace });
  const path = (q.data?.path ?? "").trim();

  // 已配置态：目录树
  const [treeData, setTreeData] = useState<TNode[]>([]);
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(new Set());
  const [treeLoadingKey, setTreeLoadingKey] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);

  // 选目录弹窗
  const [browseOpen, setBrowseOpen] = useState(false);
  const [browsePath, setBrowsePath] = useState("");
  const [browseParent, setBrowseParent] = useState<string | null>(null);
  const [browseDirs, setBrowseDirs] = useState<DirBrowseResp["dirs"]>([]);
  const [browseFiles, setBrowseFiles] = useState<DirBrowseResp["files"]>([]);
  const [browseErr, setBrowseErr] = useState<string | null>(null);
  const [browseLoading, setBrowseLoading] = useState(false);
  const [browseTree, setBrowseTree] = useState<TNode[]>([]);
  const [browseExpanded, setBrowseExpanded] = useState<Set<string>>(new Set());
  const [browseSelKey, setBrowseSelKey] = useState("");

  const saveM = useMutation({
    mutationFn: (p: string) => apiSetMySpace(p),
    onSuccess: () => {
      toast.success("已保存「我的空间」");
      qc.invalidateQueries({ queryKey: ["my-space"] });
      setBrowseOpen(false);
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
        const kids = await loadChildren(path);
        if (cancelled) return;
        setTreeData([{ title: basename(path), key: path, isDir: true, children: kids }]);
        setExpandedKeys(new Set([path]));
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
        const kids = await loadChildren(key);
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
    if (loadedKeys.length === 0) loadedKeys.push(path);
    setTreeLoadingKey(path);
    try {
      let data = treeData;
      for (const k of loadedKeys) {
        const kids = await loadChildren(k);
        data = injectChildren(data, k, kids);
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

  const fileInputRef = useRef<HTMLInputElement>(null);

  // 上传目标目录 = 当前选中文件所在目录（没选中则空间根）；转成相对空间根的路径给后端
  const uploadTargetDir =
    selectedFile && selectedFile.includes("/")
      ? selectedFile.replace(/\/[^/]*$/, "") || path
      : path;
  const uploadTargetRel =
    uploadTargetDir === path
      ? ""
      : uploadTargetDir.slice(path.length).replace(/^\//, "");

  /** 把 OS 拖进来的文件写进「我的空间」（拷入）。 */
  const doUpload = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    let ok = 0;
    for (const f of Array.from(files)) {
      try {
        await apiUploadMySpace(f, uploadTargetRel);
        ok++;
      } catch (e: any) {
        toast.error(`上传失败 ${f.name}：${e?.message || e}`);
      }
    }
    if (ok) {
      toast.success(`已拷贝 ${ok} 个文件到「我的空间」`);
      refreshTree();
    }
  };

  const doDownload = (key: string) => {
    apiDownloadMySpace(key).catch((e) => toast.error(e?.message || "下载失败"));
  };

  const doOpen = (key: string, isDir: boolean) => {
    apiOpenMySpace(key, isDir).catch((e) => toast.error(e?.message || "打开失败"));
  };

  const doRename = async (key: string) => {
    const cur = key.split("/").pop() || "";
    const name = window.prompt("重命名为：", cur);
    if (!name || name === cur) return;
    try {
      const r = await apiRenameMySpace(key, name);
      toast.success("已重命名");
      if (selectedFile === key) setSelectedFile(r.path);
      refreshTree();
    } catch (e: any) {
      toast.error(e?.message || "重命名失败");
    }
  };

  const doDelete = async (key: string) => {
    if (
      !window.confirm(
        `确定删除 ${key.split("/").pop()}？（空间内的文件将被永久删除）`
      )
    )
      return;
    try {
      await apiDeleteMySpaceFile(key);
      toast.success("已删除");
      if (
        selectedFile &&
        (selectedFile === key || selectedFile.startsWith(key + "/"))
      )
        setSelectedFile(null);
      refreshTree();
    } catch (e: any) {
      toast.error(e?.message || "删除失败");
    }
  };

  const doMkdir = async (parentKey: string) => {
    const name = window.prompt("新建文件夹名称：", "新建文件夹");
    if (!name) return;
    try {
      await apiMkdirMySpace(parentKey, name);
      toast.success("已新建文件夹");
      refreshTree();
    } catch (e: any) {
      toast.error(e?.message || "新建失败");
    }
  };

  // ── 选目录弹窗逻辑 ──
  const navigateDir = async (p?: string) => {
    setBrowseLoading(true);
    try {
      const r = await apiBrowseDir(p);
      setBrowsePath(r.path);
      setBrowseParent(r.parent);
      setBrowseDirs(r.dirs);
      setBrowseFiles(r.files);
      setBrowseErr(r.error);
      const syn = await syncTreeToPath(r.path, browseTree);
      setBrowseTree(syn.data);
      setBrowseExpanded(syn.expanded);
      setBrowseSelKey(r.path);
    } catch (e: any) {
      setBrowseErr(e?.message || "浏览失败");
    } finally {
      setBrowseLoading(false);
    }
  };

  const openBrowse = async () => {
    const start = path || undefined;
    setBrowseOpen(true);
    setBrowseLoading(true);
    try {
      const r = await apiBrowseDir(start);
      setBrowsePath(r.path);
      setBrowseParent(r.parent);
      setBrowseDirs(r.dirs);
      setBrowseFiles(r.files);
      setBrowseErr(r.error);
      const rootKids = await loadChildren("/");
      let data: TNode[] = [
        { title: "/", key: "/", isDir: true, children: rootKids },
      ];
      const syn = await syncTreeToPath(r.path, data);
      data = syn.data;
      setBrowseTree(data);
      setBrowseExpanded(syn.expanded);
      setBrowseSelKey(r.path);
    } catch (e: any) {
      setBrowseErr(e?.message || "浏览失败");
    } finally {
      setBrowseLoading(false);
    }
  };

  const onBrowseToggle = async (key: string) => {
    const node = findNode(browseTree, key);
    if (node && node.children == null) {
      setTreeLoadingKey(key);
      try {
        const kids = await loadChildren(key);
        setBrowseTree((prev) => injectChildren(prev, key, kids));
      } finally {
        setTreeLoadingKey(null);
      }
    }
    setBrowseExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const chooseDir = () => {
    if (browsePath) saveM.mutate(browsePath);
  };

  const browseRows = [
    ...browseDirs.map((d) => ({ ...d, isDir: true as const })),
    ...browseFiles.map((f) => ({ ...f, isDir: false as const })),
  ];

  const relPath = selectedFile
    ? selectedFile.slice(path.length).replace(/^\//, "") || "/"
    : "";

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="我的空间"
        desc="唯一一个用户空间：agent 默认访问的文件夹。已配置后这里就是文件浏览器——左侧目录树、右侧内容预览（仿 Obsidian）。"
      />

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
            onClick={openBrowse}
            className="mt-5 inline-flex h-10 items-center gap-2 rounded-lg bg-brand-gradient px-5 text-[14px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98]"
          >
            <FolderOpen className="h-4 w-4" />
            选择文件夹
          </button>
        </div>
      ) : (
        /* 已配置：两栏文件浏览器 */
        <div className="mt-4 flex min-h-0 flex-1 gap-3">
          {/* 左侧目录树 */}
          <div className="flex w-[280px] shrink-0 flex-col overflow-hidden rounded-xl border border-line bg-panel">
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
                  onClick={() => fileInputRef.current?.click()}
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
              </div>
            </div>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="hidden"
              onChange={(e) => {
                doUpload(e.target.files);
                e.target.value = "";
              }}
            />
            <div
              className="min-h-0 flex-1 overflow-auto py-1"
              onDragOver={(e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = "copy";
              }}
              onDrop={(e) => {
                e.preventDefault();
                doUpload(e.dataTransfer.files);
              }}
            >
              <TreeNav
                nodes={treeData}
                depth={0}
                expanded={expandedKeys}
                selected={selectedFile}
                loadingKey={treeLoadingKey}
                onToggle={onToggleDir}
                onSelectFile={(k) => setSelectedFile(k)}
                onOpen={doOpen}
                onDownload={doDownload}
                onRename={doRename}
                onDelete={doDelete}
                onMkdirHere={doMkdir}
              />
              <div className="px-3 py-2 text-[11px] leading-5 text-ink-faint">
                把文件从电脑拖到这里即可拷入「我的空间」；hover 每行可下载 / 重命名 / 删除。
              </div>
            </div>
          </div>

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
              <button
                type="button"
                onClick={openBrowse}
                className="shrink-0 rounded-md border border-line px-2 py-1 text-[12px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong"
              >
                重新选择文件夹
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-auto p-4">
              {selectedFile ? (
                <FileViewer path={selectedFile} />
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

      {/* 选目录弹窗 */}
      <Modal
        open={browseOpen}
        onClose={() => setBrowseOpen(false)}
        width={860}
        title="选择目录"
        footer={
          <>
            <button
              type="button"
              onClick={() => setBrowseOpen(false)}
              className="h-8 rounded-lg border border-line px-4 text-[13px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong"
            >
              取消
            </button>
            <button
              type="button"
              disabled={saveM.isPending}
              onClick={chooseDir}
              className="h-8 rounded-lg bg-brand-gradient px-4 text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98] disabled:opacity-50"
            >
              {saveM.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              选择此目录
            </button>
          </>
        }
      >
        <div className="flex h-[420px] flex-col">
          <div className="mb-2 flex items-center gap-2">
            <button
              type="button"
              disabled={!browseParent}
              onClick={() => navigateDir(browseParent || undefined)}
              className="flex h-6 shrink-0 items-center gap-1 rounded-md border border-line px-2 text-[12px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong disabled:cursor-not-allowed disabled:opacity-40"
            >
              <FolderOpen className="h-3 w-3" />
              上一级
            </button>
            <div className="flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto whitespace-nowrap text-[12px]">
              {(() => {
                const segs = browsePath.split("/").filter((s) => s !== "");
                if (segs.length === 0)
                  return <span className="text-ink">/</span>;
                return segs.map((name, i) => {
                  const cum = "/" + segs.slice(0, i + 1).join("/");
                  const isLast = i === segs.length - 1;
                  return (
                    <span key={cum} className="flex items-center gap-0.5">
                      {i > 0 && <span className="text-ink-faint">/</span>}
                      {isLast ? (
                        <span className="text-ink">{name}</span>
                      ) : (
                        <button
                          type="button"
                          onClick={() => navigateDir(cum)}
                          className="text-ink-muted transition-colors hover:text-brand"
                        >
                          {name}
                        </button>
                      )}
                    </span>
                  );
                });
              })()}
            </div>
            {browseLoading && (
              <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-ink-faint" />
            )}
          </div>

          {browseErr && (
            <div className="mb-2">
              <Hint tone="danger">{browseErr}</Hint>
            </div>
          )}

          <div className="flex min-h-0 flex-1 gap-3">
            <div className="w-[240px] shrink-0 overflow-auto rounded-lg border border-line-soft py-1">
              <TreeNav
                nodes={browseTree}
                depth={0}
                expanded={browseExpanded}
                selected={browseSelKey}
                loadingKey={treeLoadingKey}
                onToggle={onBrowseToggle}
                onSelectFile={(k) => void navigateDir(k)}
              />
            </div>

            <div className="min-w-0 flex-1 overflow-auto rounded-lg border border-line-soft">
              {browseLoading ? (
                <div className="flex items-center justify-center py-10 text-ink-muted">
                  <Loader2 className="h-4 w-4 animate-spin" />
                </div>
              ) : browseRows.length === 0 ? (
                <div className="py-10 text-center text-[13px] text-ink-faint">
                  （空目录）
                </div>
              ) : (
                <table className="w-full border-collapse">
                  <thead>
                    <tr className="border-b border-line-soft bg-surface text-left text-[12px] text-ink-muted">
                      <th className="px-3 py-2 font-medium">名称</th>
                      <th className="w-[110px] px-3 py-2 text-right font-medium">
                        大小
                      </th>
                      <th className="w-[170px] px-3 py-2 font-medium">修改时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {browseRows.map((r) => (
                      <tr
                        key={r.path}
                        onClick={r.isDir ? () => navigateDir(r.path) : undefined}
                        className={cn(
                          "border-b border-line-soft last:border-0",
                          r.isDir
                            ? "cursor-pointer transition-colors hover:bg-brand/[0.06]"
                            : ""
                        )}
                      >
                        <td className="px-3 py-1.5 text-[13px]">
                          <span className="flex items-center gap-2">
                            {r.isDir ? (
                              <Folder className="h-3.5 w-3.5 shrink-0 text-amber-500" />
                            ) : (
                              <FileIcon className="h-3.5 w-3.5 shrink-0 text-ink-faint" />
                            )}
                            <span className={r.isDir ? "text-ink" : "text-ink-muted"}>
                              {r.name}
                            </span>
                          </span>
                        </td>
                        <td className="px-3 py-1.5 text-right text-[12px] text-ink-muted">
                          {r.isDir ? "—" : fmtSize(r.size)}
                        </td>
                        <td className="px-3 py-1.5 text-[12px] text-ink-muted">
                          {fmtMtime(r.mtime)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </div>
      </Modal>
    </div>
  );
}
