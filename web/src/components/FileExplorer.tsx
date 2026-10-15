import { useEffect, useState } from "react";
import {
  ChevronRight,
  File as FileIcon,
  Folder,
  FolderOpen,
  FolderPlus,
  Loader2,
  Pencil,
  Plus,
  Trash2,
} from "lucide-react";
import { Modal } from "./ui/Modal";
import { Pill } from "./ui/Pill";
import { ConfirmButton } from "./ui/Confirm";
import { toast } from "./ui/Toast";
import { cn } from "../lib/utils";

/** 行内小操作按钮（图标态，hover 才显眼） */
const ACT_BTN =
  "flex h-5 w-5 shrink-0 items-center justify-center rounded text-ink-faint transition-colors hover:bg-surface hover:text-ink";
const HEAD_BTN =
  "flex h-6 w-6 items-center justify-center rounded-md text-ink-muted transition-colors hover:bg-surface hover:text-ink disabled:cursor-not-allowed disabled:opacity-40";

/** 弹窗底部按钮 */
const CANCEL_BTN =
  "h-8 rounded-lg border border-line px-4 text-[13px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong disabled:opacity-50";
const OK_BTN =
  "flex h-8 items-center gap-1.5 rounded-lg bg-brand-gradient px-4 text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98] disabled:opacity-50";

/** 输入框：弹窗里新建文件 / 文件夹、重命名共用 */
function NameInput({
  value,
  onChange,
  onEnter,
  placeholder,
  icon,
}: {
  value: string;
  onChange: (v: string) => void;
  onEnter: () => void;
  placeholder: string;
  icon: React.ReactNode;
}) {
  return (
    /* 输入组：聚焦给「边框变品牌 + 一圈淡外发光」，与 Form.tsx 的 inputCls 同一规格 */
    <div className="mt-2 flex items-center gap-2 rounded-[7px] border border-line bg-panel px-2.5 py-1.5 shadow-[0_1px_1px_rgba(0,0,0,0.03)] transition-all duration-150 focus-within:border-brand focus-within:shadow-[0_0_0_3px_rgba(91,155,245,0.48)]">
      <span className="shrink-0 text-ink-faint">{icon}</span>
      <input
        autoFocus
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") onEnter();
        }}
        className="min-w-0 flex-1 bg-transparent text-[13px] text-ink outline-none placeholder:text-ink-faint"
      />
    </div>
  );
}
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  apiWorkspaceTree,
  apiSaveWorkspaceFile,
  apiDeleteWorkspaceFile,
  apiCreateWorkspaceFolder,
  apiRenameWorkspacePath,
} from "../api/client";
import type { WorkspaceEntry } from "../api/types";

interface FileExplorerProps {
  agentId: string;
  sessionId?: string;
  /** 打开某文件（相对工作空间根的路径） */
  onOpenFile: (relPath: string) => void;
  /** 删除回调（用于清空正在查看的已删文件） */
  onDeleted?: (relPath: string) => void;
  /** 重命名回调（用于同步正在查看的文件路径） */
  onRenamed?: (oldPath: string, newPath: string) => void;
  /** 保存后自增以触发目录刷新 */
  refreshSignal?: number;
  /** 树顶端根节点显示的空间名（如 product），缺省取工作空间路径末段 */
  rootName?: string;
}

function gitColor(code: string): string | undefined {
  if (code.includes("M")) return "orange";
  if (code.includes("A")) return "green";
  if (code === "??") return "blue";
  if (code.includes("D")) return "red";
  if (code.includes("R")) return "purple";
  return undefined;
}

function gitLabel(code: string): string {
  if (code === "??") return "新增";
  if (code.includes("M")) return "修改";
  if (code.includes("A")) return "已加";
  if (code.includes("D")) return "删除";
  if (code.includes("R")) return "重命名";
  return code;
}

interface TreeNodeProps {
  agentId: string;
  sessionId: string;
  entry: WorkspaceEntry;
  depth: number;
  readOnly: boolean;
  selectedDir: string;
  onSelectDir: (p: string) => void;
  onOpenFile: (p: string) => void;
  onDeleted?: (p: string) => void;
  onRenamed?: (o: string, n: string) => void;
  invalidateTree: () => void;
}

function TreeNode({
  agentId,
  sessionId,
  entry,
  depth,
  readOnly,
  selectedDir,
  onSelectDir,
  onOpenFile,
  onDeleted,
  onRenamed,
  invalidateTree,
}: TreeNodeProps) {
  const [expanded, setExpanded] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameName, setRenameName] = useState("");

  // 懒加载：仅当展开时才请求该目录的子项
  const q = useQuery({
    queryKey: ["ws-tree", agentId, sessionId, entry.path],
    queryFn: () => apiWorkspaceTree(agentId, sessionId, entry.path),
    enabled: expanded,
    staleTime: 30_000,
  });

  const openRename = () => {
    setRenameName(entry.name);
    setRenameOpen(true);
  };

  const handleRename = async () => {
    const name = renameName.trim();
    if (!name || name === entry.name) {
      setRenameOpen(false);
      return;
    }
    try {
      const res = await apiRenameWorkspacePath(
        agentId,
        sessionId,
        entry.path,
        name
      );
      toast.success("已重命名");
      setRenameOpen(false);
      invalidateTree();
      onRenamed?.(entry.path, res.path);
    } catch (e: any) {
      toast.error(e?.message || "重命名失败");
    }
  };

  const handleDelete = async () => {
    try {
      await apiDeleteWorkspaceFile(agentId, sessionId, entry.path);
      toast.success("已删除");
      invalidateTree();
      onDeleted?.(entry.path);
    } catch (e: any) {
      toast.error(e?.message || "删除失败");
    }
  };

  const children: WorkspaceEntry[] = q.data?.entries ?? [];
  const color = gitColor(entry.git);
  const selected = selectedDir === entry.path;

  return (
    <div>
      {/* 行高与缩进：缩进靠 paddingLeft 而不是嵌套盒子，深层目录也不会多出 DOM 层 */}
      <div
        onClick={() => {
          if (entry.is_dir) {
            setExpanded((v) => !v);
            onSelectDir(entry.path);
          } else {
            onOpenFile(entry.path);
          }
        }}
        style={{ paddingLeft: 8 + depth * 14 }}
        className={cn(
          "group flex cursor-pointer items-center gap-1.5 py-[5px] pr-2.5 text-[13px] transition-colors",
          selected
            ? "bg-brand/10"
            : "hover:bg-black/[0.04] dark:hover:bg-white/5"
        )}
      >
        {entry.is_dir ? (
          <ChevronRight
            className={cn(
              "h-2.5 w-2.5 shrink-0 text-ink-faint transition-transform",
              expanded && "rotate-90"
            )}
          />
        ) : (
          <span className="inline-block w-2.5 shrink-0" />
        )}
        {entry.is_dir ? (
          expanded ? (
            <FolderOpen className="h-3.5 w-3.5 shrink-0 text-amber-500" />
          ) : (
            <Folder className="h-3.5 w-3.5 shrink-0 text-amber-500" />
          )
        ) : (
          <FileIcon className="h-3.5 w-3.5 shrink-0 text-ink-faint" />
        )}
        <span
          className={cn(
            "min-w-0 flex-1 truncate",
            selected ? "text-brand" : "text-ink"
          )}
        >
          {entry.name}
        </span>
        {color && <Pill tone="muted">{gitLabel(entry.git)}</Pill>}
        {!readOnly && (
          /* 操作按钮平时淡出，hover 或键盘聚焦时才显眼——文件树的主任务是浏览，
             每行常驻两个图标会把名字挤短 */
          <span className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
            <button
              type="button"
              title="重命名"
              onClick={(ev) => {
                ev.stopPropagation();
                openRename();
              }}
              className={ACT_BTN}
            >
              <Pencil className="h-3 w-3" />
            </button>
            <ConfirmButton
              resetKey={entry.path}
              confirmText="删除"
              cancelText="取消"
              onConfirm={handleDelete}
              className={cn(ACT_BTN, "hover:text-red-600 dark:hover:text-red-400")}
            >
              <Trash2 className="h-3 w-3" />
            </ConfirmButton>
          </span>
        )}
      </div>

      {expanded && (
        <div>
          {q.isLoading ? (
            <div
              style={{ paddingLeft: 8 + (depth + 1) * 14 }}
              className="flex items-center gap-1.5 py-1 text-[12px] text-ink-faint"
            >
              <Loader2 className="h-3 w-3 animate-spin" />
              加载中…
            </div>
          ) : children.length === 0 ? (
            <div
              style={{ paddingLeft: 8 + (depth + 1) * 14 }}
              className="py-1 text-[12px] text-ink-faint"
            >
              空目录
            </div>
          ) : (
            children.map((c) => (
              <TreeNode
                key={c.path}
                agentId={agentId}
                sessionId={sessionId}
                entry={c}
                depth={depth + 1}
                readOnly={readOnly}
                selectedDir={selectedDir}
                onSelectDir={onSelectDir}
                onOpenFile={onOpenFile}
                onDeleted={onDeleted}
                onRenamed={onRenamed}
                invalidateTree={invalidateTree}
              />
            ))
          )}
        </div>
      )}

      <Modal
        title="重命名"
        open={renameOpen}
        onClose={() => setRenameOpen(false)}
        width={420}
        footer={
          <>
            <button
              type="button"
              onClick={() => setRenameOpen(false)}
              className={CANCEL_BTN}
            >
              取消
            </button>
            <button type="button" onClick={handleRename} className={OK_BTN}>
              确定
            </button>
          </>
        }
      >
        <NameInput
          value={renameName}
          onChange={setRenameName}
          onEnter={handleRename}
          placeholder="新名称"
          icon={<Pencil className="h-3.5 w-3.5" />}
        />
      </Modal>
    </div>
  );
}

function RootNode({
  label,
  agentId,
  sessionId,
  entries,
  readOnly,
  selectedDir,
  onSelectDir,
  onOpenFile,
  onDeleted,
  onRenamed,
  invalidateTree,
}: {
  label: string;
  agentId: string;
  sessionId: string;
  entries: WorkspaceEntry[];
  readOnly: boolean;
  selectedDir: string;
  onSelectDir: (p: string) => void;
  onOpenFile: (p: string) => void;
  onDeleted?: (p: string) => void;
  onRenamed?: (o: string, n: string) => void;
  invalidateTree: () => void;
}) {
  // 默认展开（VSCode 风格：项目根默认展开）
  const [expanded, setExpanded] = useState(true);
  const selected = selectedDir === "";

  return (
    <div>
      {/* 空间根节点：比普通行更重（加粗 + 主色），一眼能分清"根在哪" */}
      <div
        onClick={() => {
          setExpanded((v) => !v);
          onSelectDir("");
        }}
        className={cn(
          "flex cursor-pointer items-center gap-1.5 py-[5px] pl-2 pr-2.5 text-[13px] font-semibold transition-colors",
          selected
            ? "bg-brand/10"
            : "hover:bg-black/[0.04] dark:hover:bg-white/5"
        )}
      >
        <ChevronRight
          className={cn(
            "h-2.5 w-2.5 shrink-0 text-ink-faint transition-transform",
            expanded && "rotate-90"
          )}
        />
        {expanded ? (
          <FolderOpen className="h-3.5 w-3.5 shrink-0 text-amber-500" />
        ) : (
          <Folder className="h-3.5 w-3.5 shrink-0 text-amber-500" />
        )}
        <span
          className={cn(
            "min-w-0 flex-1 truncate",
            selected ? "text-brand" : "text-ink-strong"
          )}
        >
          {label}
        </span>
      </div>

      {expanded && (
        <div>
          {entries.length === 0 ? (
            <div
              style={{
                paddingTop: 4,
                paddingBottom: 4,
                paddingLeft: 22,
                color: "#999",
                fontSize: 12,
              }}
            >
              空目录
            </div>
          ) : (
            entries.map((c) => (
              <TreeNode
                key={c.path}
                agentId={agentId}
                sessionId={sessionId}
                entry={c}
                depth={1}
                readOnly={readOnly}
                selectedDir={selectedDir}
                onSelectDir={onSelectDir}
                onOpenFile={onOpenFile}
                onDeleted={onDeleted}
                onRenamed={onRenamed}
                invalidateTree={invalidateTree}
              />
            ))
          )}
        </div>
      )}
    </div>
  );
}

export default function FileExplorer({
  agentId,
  sessionId,
  onOpenFile,
  onDeleted,
  onRenamed,
  refreshSignal = 0,
  rootName,
}: FileExplorerProps) {
  const queryClient = useQueryClient();
  const invalidateTree = () =>
    queryClient.invalidateQueries({ queryKey: ["ws-tree", agentId, sessionId] });

  const [selectedDir, setSelectedDir] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [folderOpen, setFolderOpen] = useState(false);
  const [folderName, setFolderName] = useState("");

  const rootQ = useQuery({
    queryKey: ["ws-tree", agentId, sessionId, ""],
    queryFn: () => apiWorkspaceTree(agentId, sessionId!, ""),
    enabled: !!sessionId,
    staleTime: 30_000,
  });

  // 保存后刷新整棵树的 git 角标
  useEffect(() => {
    if (sessionId && refreshSignal) invalidateTree();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshSignal]);

  const readOnly = rootQ.data?.read_only ?? true;
  const joinPath = (name: string) =>
    selectedDir ? `${selectedDir}/${name}` : name;

  const handleCreate = async () => {
    const name = newName.trim();
    if (!name) return;
    const path = joinPath(name);
    try {
      await apiSaveWorkspaceFile(agentId, sessionId, path, "");
      toast.success("已创建");
      setCreateOpen(false);
      setNewName("");
      invalidateTree();
      onOpenFile(path);
    } catch (e: any) {
      toast.error(e?.message || "创建失败");
    }
  };

  const handleCreateFolder = async () => {
    const name = folderName.trim();
    if (!name) return;
    const path = joinPath(name);
    try {
      await apiCreateWorkspaceFolder(agentId, sessionId, path);
      toast.success("已创建目录");
      setFolderOpen(false);
      setFolderName("");
      invalidateTree();
    } catch (e: any) {
      toast.error(e?.message || "创建失败");
    }
  };

  if (!sessionId) {
    return (
      <div className="p-4 text-center text-[13px] text-ink-muted">
        请先选择或新建会话，再浏览其工作空间
      </div>
    );
  }

  if (rootQ.isLoading) {
    return (
      <div className="flex items-center justify-center gap-2 p-6 text-[13px] text-ink-muted">
        <Loader2 className="h-4 w-4 animate-spin" />
        加载目录…
      </div>
    );
  }
  if (rootQ.isError || !rootQ.data) {
    return (
      <div className="p-4 text-[13px] text-ink-muted">
        无法读取工作空间目录。
      </div>
    );
  }

  const entries: WorkspaceEntry[] = rootQ.data.entries;

  // 根节点标签：优先用传入的空间名，否则取工作空间路径末段
  const rootPath: string = rootQ.data.root ?? "";
  const rootLabel: string =
    rootName ||
    (rootPath ? rootPath.split(/[\\/]/).pop() || "工作空间" : "工作空间");

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* 头部：标题 + 权限 + 新建 */}
      <div className="flex items-center gap-1.5 border-b border-line-soft px-2 py-1.5 text-[12px]">
        <span className="text-ink-muted">资源管理器</span>
        <span className="ml-auto">
          <Pill tone={readOnly ? "orange" : "green"}>
            {readOnly ? "只读" : "可写"}
          </Pill>
        </span>
        <button
          type="button"
          disabled={readOnly}
          title="在选中目录新建文件"
          onClick={() => {
            setNewName("");
            setCreateOpen(true);
          }}
          className={HEAD_BTN}
        >
          <Plus className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          disabled={readOnly}
          title="在选中目录新建文件夹"
          onClick={() => {
            setFolderName("");
            setFolderOpen(true);
          }}
          className={HEAD_BTN}
        >
          <FolderPlus className="h-3.5 w-3.5" />
        </button>
      </div>

      {/* 新建位置提示：常驻一行，避免"新建到哪去了"的疑问 */}
      <div className="truncate px-2.5 py-1 text-[11px] text-ink-faint">
        新建位置：{selectedDir ? selectedDir : `${rootLabel}（根）`}
      </div>

      {/* 文件树：顶端为可收缩的空间根节点 */}
      <div className="flex-1 overflow-auto py-1">
        <RootNode
          label={rootLabel}
          agentId={agentId}
          sessionId={sessionId!}
          entries={entries}
          readOnly={readOnly}
          selectedDir={selectedDir}
          onSelectDir={setSelectedDir}
          onOpenFile={onOpenFile}
          onDeleted={onDeleted}
          onRenamed={onRenamed}
          invalidateTree={invalidateTree}
        />
      </div>

      <Modal
        title="新建文件"
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        width={420}
        footer={
          <>
            <button
              type="button"
              onClick={() => setCreateOpen(false)}
              className={CANCEL_BTN}
            >
              取消
            </button>
            <button type="button" onClick={handleCreate} className={OK_BTN}>
              创建
            </button>
          </>
        }
      >
        <p className="text-[12px] text-ink-muted">
          {selectedDir ? `位于 ${selectedDir}/` : "位于工作空间根"}
        </p>
        <NameInput
          value={newName}
          onChange={setNewName}
          onEnter={handleCreate}
          placeholder="文件名，如 add.py"
          icon={<FileIcon className="h-3.5 w-3.5" />}
        />
      </Modal>

      <Modal
        title="新建文件夹"
        open={folderOpen}
        onClose={() => setFolderOpen(false)}
        width={420}
        footer={
          <>
            <button
              type="button"
              onClick={() => setFolderOpen(false)}
              className={CANCEL_BTN}
            >
              取消
            </button>
            <button
              type="button"
              onClick={handleCreateFolder}
              className={OK_BTN}
            >
              创建
            </button>
          </>
        }
      >
        <p className="text-[12px] text-ink-muted">
          {selectedDir ? `位于 ${selectedDir}/` : "位于工作空间根"}
        </p>
        <NameInput
          value={folderName}
          onChange={setFolderName}
          onEnter={handleCreateFolder}
          placeholder="文件夹名"
          icon={<Folder className="h-3.5 w-3.5" />}
        />
      </Modal>
    </div>
  );
}
