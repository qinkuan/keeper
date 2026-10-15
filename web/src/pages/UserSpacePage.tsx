import { useState } from "react";
import {
  ChevronRight,
  File as FileIcon,
  Folder,
  FolderOpen,
  Loader2,
  TriangleAlert,
} from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  apiBrowseDir,
  apiCreateUserSpace,
  apiDeleteUserSpace,
  apiListUserSpaces,
  apiUpdateUserSpace,
} from "../api/client";
import type { DirBrowseResp, UserSpace, UserSpaceInput } from "../api/types";
import { Field, TextInput, ToggleField } from "../components/ui/Form";
import { Hint, PageHeader, Section } from "../components/ui/Page";
import { Table } from "../components/ui/Table";
import type { Column } from "../components/ui/Table";
import { Modal } from "../components/ui/Modal";
import { Pill } from "../components/ui/Pill";
import { ConfirmButton, DANGER_BTN, PLAIN_BTN } from "../components/ui/Confirm";
import { toast } from "../components/ui/Toast";
import { cn } from "../lib/utils";

const DEFAULT_FORM: UserSpaceInput & { id?: string } = {
  name: "",
  path: "",
  read_only: true,
  description: null,
};

/** 树节点：自建（替代 antd Tree）。懒加载，children 为 undefined 表示"还没展开过" */
interface TreeNode {
  title: string;
  key: string;
  children?: TreeNode[];
  /** 已确认没有子目录，用来隐藏展开箭头 */
  leaf?: boolean;
}

/** 在树里按 key 递归地把 children 塞进去（懒加载用）。 */
function injectTreeChildren(
  nodes: TreeNode[],
  key: string,
  children: TreeNode[]
): TreeNode[] {
  return nodes.map((n) =>
    n.key === key
      ? { ...n, children, leaf: children.length === 0 }
      : n.children
      ? { ...n, children: injectTreeChildren(n.children, key, children) }
      : n
  );
}

/** 在树里按 key 递归查找节点。 */
function findNode(nodes: TreeNode[], key: string): TreeNode | undefined {
  for (const n of nodes) {
    if (n.key === key) return n;
    if (n.children) {
      const f = findNode(n.children, key);
      if (f) return f;
    }
  }
  return undefined;
}

/** 递归渲染目录树。缩进用 padding 而不是深层嵌套 div——省掉一层盒子。 */
function TreeView({
  nodes,
  depth,
  expanded,
  selected,
  loadingKey,
  onToggle,
  onSelect,
}: {
  nodes: TreeNode[];
  depth: number;
  expanded: Set<string>;
  selected: string;
  loadingKey: string | null;
  onToggle: (key: string) => void;
  onSelect: (key: string) => void;
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
              style={{ paddingLeft: depth * 14 + 4 }}
              className={cn(
                "flex items-center gap-1 rounded-md py-1 pr-2 transition-colors",
                isSel ? "bg-brand/10" : "hover:bg-black/[0.04] dark:hover:bg-white/5"
              )}
            >
              {/* 展开箭头：叶子节点占位保持缩进对齐 */}
              {n.leaf && n.children?.length === 0 ? (
                <span className="h-3.5 w-3.5 shrink-0" />
              ) : (
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
              )}
              <button
                type="button"
                onClick={() => onSelect(n.key)}
                className={cn(
                  "min-w-0 flex-1 truncate text-left text-[13px]",
                  isSel ? "text-brand" : "text-ink"
                )}
              >
                {n.title}
              </button>
            </div>
            {open && n.children && n.children.length > 0 && (
              <TreeView
                nodes={n.children}
                depth={depth + 1}
                expanded={expanded}
                selected={selected}
                loadingKey={loadingKey}
                onToggle={onToggle}
                onSelect={onSelect}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

/**
 * 用户空间管理：用户自管的命名文件路径。
 *
 * default 是系统默认工作空间，始终存在且不可编辑 / 删除（改它会让默认工作区
 * 指向别处，所有没显式绑定空间的会话跟着漂移）。界面上把按钮置灰而不是藏起来，
 * 是为了让"为什么这个不能删"有迹可循。
 */
export default function UserSpacePage() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<UserSpace | null>(null);
  const [form, setForm] = useState<UserSpaceInput & { id?: string }>(DEFAULT_FORM);
  const [formErr, setFormErr] = useState<Record<string, string>>({});
  const patch = (k: keyof UserSpaceInput, v: unknown) =>
    setForm((p) => ({ ...p, [k]: v }));

  // 目录浏览：左侧目录树 + 右侧当前目录内容
  const [browseOpen, setBrowseOpen] = useState(false);
  const [browsePath, setBrowsePath] = useState("");
  const [browseParent, setBrowseParent] = useState<string | null>(null);
  const [browseDirs, setBrowseDirs] = useState<DirBrowseResp["dirs"]>([]);
  const [browseFiles, setBrowseFiles] = useState<DirBrowseResp["files"]>([]);
  const [browseErr, setBrowseErr] = useState<string | null>(null);
  const [browseLoading, setBrowseLoading] = useState(false);
  const [treeData, setTreeData] = useState<TreeNode[]>([]);
  const [treeLoadingKey, setTreeLoadingKey] = useState<string | null>(null);
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(new Set());
  const [selectedKey, setSelectedKey] = useState("");

  const loadTreeChildren = async (path: string): Promise<TreeNode[]> => {
    const r = await apiBrowseDir(path);
    return r.dirs.map((d) => ({ title: d.name, key: d.path, isLeaf: false } as TreeNode));
  };

  /** 把树同步到 target 路径：确保从 "/" 起的祖先链都展开；除末节点外逐级
      确保子目录已加载（这样目标节点才会存在于树中）。 */
  const syncTreeToPath = async (
    target: string,
    data0: TreeNode[]
  ): Promise<{ data: TreeNode[]; expanded: Set<string> }> => {
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
        const kids = await loadTreeChildren(key);
        data = injectTreeChildren(data, key, kids);
      }
    }
    return { data, expanded: new Set(chain.slice(0, -1)) };
  };

  const navigateDir = async (p?: string) => {
    setBrowseLoading(true);
    try {
      const r = await apiBrowseDir(p);
      setBrowsePath(r.path);
      setBrowseParent(r.parent);
      setBrowseDirs(r.dirs);
      setBrowseFiles(r.files);
      setBrowseErr(r.error);
      // 右侧进入子目录时，左侧树同步展开 / 选中到该路径
      const syn = await syncTreeToPath(r.path, treeData);
      setTreeData(syn.data);
      setExpandedKeys(syn.expanded);
      setSelectedKey(r.path);
    } catch (e: any) {
      setBrowseErr(e?.message || "浏览失败");
    } finally {
      setBrowseLoading(false);
    }
  };

  const openBrowse = async () => {
    const start = form.path || undefined;
    setBrowseOpen(true);
    setBrowseLoading(true);
    try {
      const r = await apiBrowseDir(start);
      setBrowsePath(r.path);
      setBrowseParent(r.parent);
      setBrowseDirs(r.dirs);
      setBrowseFiles(r.files);
      setBrowseErr(r.error);
      // 树始终从系统根 "/" 起构建，再展开到 start 所在位置
      const rootKids = await loadTreeChildren("/");
      let data: TreeNode[] = [
        { title: "/", key: "/", children: rootKids, leaf: rootKids.length === 0 },
      ];
      const syn = await syncTreeToPath(r.path, data);
      data = syn.data;
      setTreeData(data);
      setExpandedKeys(syn.expanded);
      setSelectedKey(r.path);
    } catch (e: any) {
      setBrowseErr(e?.message || "浏览失败");
    } finally {
      setBrowseLoading(false);
    }
  };

  const onTreeToggle = async (key: string) => {
    const node = findNode(treeData, key);
    // 第一次展开：先把子目录拉回来，再置为展开
    if (node && node.children == null) {
      setTreeLoadingKey(key);
      try {
        const kids = await loadTreeChildren(key);
        setTreeData((prev) => injectTreeChildren(prev, key, kids));
      } finally {
        setTreeLoadingKey(null);
      }
    }
    setExpandedKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const chooseDir = () => {
    patch("path", browsePath);
    setBrowseOpen(false);
  };

  // 文件大小 / 修改时间格式化
  const fmtSize = (size: number | null) => {
    if (size == null) return "—";
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
    return `${(size / 1024 / 1024 / 1024).toFixed(1)} GB`;
  };
  const fmtMtime = (mtime: number | null) =>
    mtime == null
      ? "—"
      : new Date(mtime * 1000).toLocaleString("zh-CN", { hour12: false });

  const q = useQuery({ queryKey: ["user-spaces"], queryFn: apiListUserSpaces });

  const saveM = useMutation({
    mutationFn: (v: UserSpaceInput & { id?: string }) => {
      const { id, ...body } = v;
      return id ? apiUpdateUserSpace(id, body) : apiCreateUserSpace(body);
    },
    onSuccess: () => {
      toast.success("已保存");
      setOpen(false);
      qc.invalidateQueries({ queryKey: ["user-spaces"] });
    },
    onError: (e: any) => toast.error(e?.message || "保存失败"),
  });

  const delM = useMutation({
    mutationFn: (id: string) => apiDeleteUserSpace(id),
    onSuccess: () => {
      toast.success("已删除（引用它的会话将回落到 agent 默认目录）");
      qc.invalidateQueries({ queryKey: ["user-spaces"] });
    },
    onError: (e: any) => toast.error(e?.message || "删除失败"),
  });

  const openCreate = () => {
    setEditing(null);
    setForm({ ...DEFAULT_FORM });
    setFormErr({});
    setOpen(true);
  };
  const openEdit = (row: UserSpace) => {
    setEditing(row);
    setForm({ ...row });
    setFormErr({});
    setOpen(true);
  };

  const submit = () => {
    const errs: Record<string, string> = {};
    if (!String(form.name ?? "").trim()) errs.name = "请输入名称";
    if (!String(form.path ?? "").trim()) errs.path = "请输入绝对路径";
    setFormErr(errs);
    if (Object.keys(errs).length) return;
    saveM.mutate({ ...form, id: editing?.id });
  };

  const rows = q.data?.user_spaces ?? [];

  const columns: Column<UserSpace>[] = [
    { key: "name", title: "名称", width: 160, render: (r) => r.name },
    {
      key: "path",
      title: "路径",
      render: (r) => (
        <span className="block max-w-[360px] truncate font-mono text-[12px] text-ink-muted">
          {r.path}
        </span>
      ),
    },
    {
      key: "read_only",
      title: "只读",
      width: 80,
      render: (r) =>
        r.read_only ? (
          <Pill tone="orange">只读</Pill>
        ) : (
          <Pill tone="green">可写</Pill>
        ),
    },
    {
      key: "description",
      title: "说明",
      render: (r) => (
        <span className="block max-w-[240px] truncate text-ink-muted">
          {r.description || "—"}
        </span>
      ),
    },
    {
      key: "ops",
      title: "操作",
      width: 160,
      render: (r) => {
        const isDefault = r.name === "default";
        return (
          <span className="flex items-center gap-1">
            <button
              type="button"
              disabled={isDefault}
              title={isDefault ? "系统默认工作空间不可编辑" : undefined}
              onClick={() => openEdit(r)}
              className="inline-flex h-6 items-center rounded-md border border-line px-2 text-[12px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong disabled:cursor-not-allowed disabled:opacity-40"
            >
              编辑
            </button>
            <ConfirmButton
              resetKey={rows.length}
              disabled={isDefault}
              onConfirm={() => delM.mutate(r.id)}
              className={cn(DANGER_BTN, "h-6 text-[12px] disabled:cursor-not-allowed disabled:opacity-40")}
            >
              删除
            </ConfirmButton>
            {isDefault && (
              <span
                title="系统默认工作空间：始终存在，不可删除或编辑（除非手动改库）"
                className="inline-flex text-ink-faint"
              >
                <TriangleAlert className="h-3.5 w-3.5" />
              </span>
            )}
          </span>
        );
      },
    },
  ];

  const browseRows = [
    ...browseDirs.map((d) => ({ ...d, isDir: true as const })),
    ...browseFiles.map((f) => ({ ...f, isDir: false as const })),
  ];

  return (
    <>
      <PageHeader
        title="用户空间"
        desc="用户自管的命名文件路径：每个会话可绑定一个，作为 agent 干活的主目录。其中 default 为系统默认工作空间（~/.keeper/workspace/user/default），始终存在、不可删除。新建空间会在 user/ 下建立同名软链接，指向你填的真实文件夹。"
        actions={
          <button
            type="button"
            onClick={openCreate}
            className="rounded-lg bg-brand-gradient px-4 py-[7px] text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98]"
          >
            新建用户空间
          </button>
        }
      />

      {q.isError && (
        <div className="mb-3">
          <Hint tone="danger" title="加载失败">
            {(q.error as Error)?.message}
          </Hint>
        </div>
      )}

      <Section bodyClassName="p-0">
        <Table
          rows={rows}
          columns={columns}
          rowKey={(r) => r.id}
          loading={q.isLoading}
          minWidth={880}
          empty="还没有用户空间"
        />
      </Section>

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        width={560}
        title={editing ? `编辑：${editing.name}` : "新建用户空间"}
        footer={
          <>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className={cn(PLAIN_BTN, "h-8 px-4")}
            >
              取消
            </button>
            <button
              type="button"
              disabled={saveM.isPending}
              onClick={submit}
              className="flex h-8 items-center gap-1.5 rounded-lg bg-brand-gradient px-4 text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98] disabled:opacity-50"
            >
              {saveM.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              保存
            </button>
          </>
        }
      >
        <div className="space-y-4">
          <Field label="名称">
            <TextInput
              value={form.name}
              onChange={(v) => patch("name", v)}
              placeholder="如：我的仓库"
            />
            {formErr.name && (
              <p className="mt-1 text-[12px] text-red-600 dark:text-red-300">
                {formErr.name}
              </p>
            )}
          </Field>

          <Field
            label="绝对路径"
            extra="填真实文件夹的绝对路径；可点右侧「浏览」从服务器本机挑选；后端会在 user/ 下建立同名软链接指向它，路径不存在时自动创建（mkdir）。"
          >
            <div className="flex gap-2">
              <div className="min-w-0 flex-1">
                <TextInput
                  value={form.path}
                  onChange={(v) => patch("path", v)}
                  placeholder="/Users/me/projects/xxx"
                />
              </div>
              <button
                type="button"
                onClick={openBrowse}
                className="shrink-0 rounded-lg border border-line px-3 text-[13px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong"
              >
                浏览
              </button>
            </div>
            {formErr.path && (
              <p className="mt-1 text-[12px] text-red-600 dark:text-red-300">
                {formErr.path}
              </p>
            )}
          </Field>

          <ToggleField
            label="只读"
            extra="只读空间下，写类工具（bash__run、fs.write 等）会被自动跳过"
            checked={!!form.read_only}
            onChange={(v) => patch("read_only", v)}
          />

          <Field label="说明">
            <textarea
              rows={2}
              value={form.description ?? ""}
              onChange={(e) => patch("description", e.target.value)}
              placeholder="可选"
              className="w-full resize-none rounded-[7px] border border-line bg-panel px-2.5 py-2 text-[12.5px] leading-6 text-ink shadow-[0_1px_1px_rgba(0,0,0,0.03)] outline-none transition-all duration-150 placeholder:text-ink-faint focus:border-brand focus:shadow-[0_0_0_3px_rgba(66,133,244,0.48)]"
            />
          </Field>
        </div>
      </Modal>

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
              className={cn(PLAIN_BTN, "h-8 px-4")}
            >
              取消
            </button>
            <button
              type="button"
              onClick={chooseDir}
              className="h-8 rounded-lg bg-brand-gradient px-4 text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98]"
            >
              选择此目录
            </button>
          </>
        }
      >
        <div className="flex h-[420px] flex-col">
          {/* 路径栏：上一级 + 面包屑。面包屑自己拼，antd 的 items 协议没必要 */}
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
            {/* 左：目录树 */}
            <div className="w-[240px] shrink-0 overflow-auto rounded-lg border border-line-soft py-1">
              <TreeView
                nodes={treeData}
                depth={0}
                expanded={expandedKeys}
                selected={selectedKey}
                loadingKey={treeLoadingKey}
                onToggle={onTreeToggle}
                onSelect={(k) => void navigateDir(k)}
              />
            </div>

            {/* 右：当前目录内容（目录可点进去） */}
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
    </>
  );
}
