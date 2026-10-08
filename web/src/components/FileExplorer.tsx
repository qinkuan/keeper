import { useEffect, useState } from "react";
import {
  Button,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Spin,
  Tag,
  Typography,
  message,
} from "antd";
import {
  DeleteOutlined,
  EditOutlined,
  FileOutlined,
  FileAddOutlined,
  FolderOutlined,
  FolderOpenOutlined,
  FolderAddOutlined,
  RightOutlined,
  PlusOutlined,
} from "@ant-design/icons";
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
      message.success("已重命名");
      setRenameOpen(false);
      invalidateTree();
      onRenamed?.(entry.path, res.path);
    } catch (e: any) {
      message.error(e?.message || "重命名失败");
    }
  };

  const handleDelete = async () => {
    try {
      await apiDeleteWorkspaceFile(agentId, sessionId, entry.path);
      message.success("已删除");
      invalidateTree();
      onDeleted?.(entry.path);
    } catch (e: any) {
      message.error(e?.message || "删除失败");
    }
  };

  const children: WorkspaceEntry[] = q.data?.entries ?? [];
  const color = gitColor(entry.git);
  const selected = selectedDir === entry.path;

  return (
    <div>
      <div
        onClick={() => {
          if (entry.is_dir) {
            setExpanded((v) => !v);
            onSelectDir(entry.path);
          } else {
            onOpenFile(entry.path);
          }
        }}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          paddingTop: 5,
          paddingBottom: 5,
          paddingRight: 10,
          paddingLeft: 8 + depth * 14,
          cursor: "pointer",
          fontSize: 13,
          background: selected ? "var(--kp-primary-soft)" : "transparent",
        }}
        onMouseEnter={(ev) => {
          if (!selected) ev.currentTarget.style.background = "var(--kp-surface)";
        }}
        onMouseLeave={(ev) => {
          ev.currentTarget.style.background = selected ? "var(--kp-primary-soft)" : "transparent";
        }}
      >
        {entry.is_dir ? (
          <RightOutlined
            style={{
              fontSize: 10,
              color: "#999",
              transition: "transform .15s",
              transform: expanded ? "rotate(90deg)" : "none",
            }}
          />
        ) : (
          <span style={{ width: 10, display: "inline-block" }} />
        )}
        {entry.is_dir ? (
          expanded ? (
            <FolderOpenOutlined style={{ color: "#e8b84b" }} />
          ) : (
            <FolderOutlined style={{ color: "#e8b84b" }} />
          )
        ) : (
          <FileOutlined style={{ color: "var(--kp-primary)" }} />
        )}
        <span
          style={{
            flex: 1,
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {entry.name}
        </span>
        {color && (
          <Tag
            color={color}
            style={{ marginInlineEnd: 0, fontSize: 11, lineHeight: "16px" }}
          >
            {gitLabel(entry.git)}
          </Tag>
        )}
        {!readOnly && (
          <>
            <Button
              size="small"
              type="text"
              icon={<EditOutlined />}
              onClick={(ev) => {
                ev.stopPropagation();
                openRename();
              }}
              title="重命名"
            />
            <Popconfirm
              title="确认删除？"
              description={entry.is_dir ? "将删除整个目录" : "将删除该文件"}
              okText="删除"
              okButtonProps={{ danger: true }}
              onConfirm={(e2) => {
                e2?.stopPropagation();
                handleDelete();
              }}
              onCancel={(e2) => e2?.stopPropagation()}
            >
              <Button
                size="small"
                type="text"
                danger
                icon={<DeleteOutlined />}
                onClick={(ev) => ev.stopPropagation()}
              />
            </Popconfirm>
          </>
        )}
      </div>

      {expanded && (
        <div>
          {q.isLoading ? (
            <div
              style={{
                paddingTop: 4,
                paddingBottom: 4,
                paddingLeft: 8 + (depth + 1) * 14,
                color: "#999",
                fontSize: 12,
              }}
            >
              <Spin size="small" /> 加载中…
            </div>
          ) : children.length === 0 ? (
            <div
              style={{
                paddingTop: 4,
                paddingBottom: 4,
                paddingLeft: 8 + (depth + 1) * 14,
                color: "#999",
                fontSize: 12,
              }}
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
        onOk={handleRename}
        onCancel={() => setRenameOpen(false)}
        okText="确定"
        cancelText="取消"
        destroyOnClose
      >
        <Input
          autoFocus
          placeholder="新名称"
          value={renameName}
          onChange={(e) => setRenameName(e.target.value)}
          onPressEnter={handleRename}
          prefix={<EditOutlined />}
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
      <div
        onClick={() => {
          setExpanded((v) => !v);
          onSelectDir("");
        }}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          paddingTop: 5,
          paddingBottom: 5,
          paddingRight: 10,
          paddingLeft: 8,
          cursor: "pointer",
          fontSize: 13,
          fontWeight: 600,
          background: selected ? "var(--kp-primary-soft)" : "transparent",
        }}
        onMouseEnter={(ev) => {
          if (!selected) ev.currentTarget.style.background = "var(--kp-surface)";
        }}
        onMouseLeave={(ev) => {
          ev.currentTarget.style.background = selected ? "var(--kp-primary-soft)" : "transparent";
        }}
      >
        <RightOutlined
          style={{
            fontSize: 10,
            color: "#999",
            transition: "transform .15s",
            transform: expanded ? "rotate(90deg)" : "none",
          }}
        />
        {expanded ? (
          <FolderOpenOutlined style={{ color: "#e8b84b" }} />
        ) : (
          <FolderOutlined style={{ color: "#e8b84b" }} />
        )}
        <span
          style={{
            flex: 1,
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
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
      message.success("已创建");
      setCreateOpen(false);
      setNewName("");
      invalidateTree();
      onOpenFile(path);
    } catch (e: any) {
      message.error(e?.message || "创建失败");
    }
  };

  const handleCreateFolder = async () => {
    const name = folderName.trim();
    if (!name) return;
    const path = joinPath(name);
    try {
      await apiCreateWorkspaceFolder(agentId, sessionId, path);
      message.success("已创建目录");
      setFolderOpen(false);
      setFolderName("");
      invalidateTree();
    } catch (e: any) {
      message.error(e?.message || "创建失败");
    }
  };

  if (!sessionId) {
    return (
      <div style={{ padding: 16 }}>
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="请先选择或新建会话，再浏览其工作空间"
        />
      </div>
    );
  }

  if (rootQ.isLoading) {
    return (
      <div style={{ padding: 24, textAlign: "center" }}>
        <Spin /> <Typography.Text type="secondary">加载目录…</Typography.Text>
      </div>
    );
  }
  if (rootQ.isError || !rootQ.data) {
    return (
      <div style={{ padding: 16 }}>
        <Typography.Text type="secondary">
          无法读取工作空间目录。
        </Typography.Text>
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
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        minHeight: 0,
        height: "100%",
      }}
    >
      {/* 头部：标题 + 新建 */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          padding: "6px 8px",
          borderBottom: "1px solid var(--kp-border-soft)",
          fontSize: 12,
          color: "#666",
        }}
      >
        <span>资源管理器</span>
        <Typography.Text type="secondary" style={{ marginLeft: "auto" }}>
          {readOnly ? "只读" : "可写"}
        </Typography.Text>
        <Button
          size="small"
          type="text"
          icon={<PlusOutlined />}
          disabled={readOnly}
          onClick={() => {
            setNewName("");
            setCreateOpen(true);
          }}
          title="在选中目录新建文件"
        />
        <Button
          size="small"
          type="text"
          icon={<FolderAddOutlined />}
          disabled={readOnly}
          onClick={() => {
            setFolderName("");
            setFolderOpen(true);
          }}
          title="在选中目录新建文件夹"
        />
      </div>

      {/* 新建位置提示 */}
      <Typography.Text
        type="secondary"
        style={{ fontSize: 11, padding: "4px 10px", display: "block" }}
      >
        新建位置：{selectedDir ? selectedDir : `${rootLabel}（根）`}
      </Typography.Text>

      {/* 文件树：顶端为可收缩的空间根节点 */}
      <div style={{ flex: 1, overflow: "auto", padding: "4px 0" }}>
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
        onOk={handleCreate}
        onCancel={() => setCreateOpen(false)}
        okText="创建"
        cancelText="取消"
        destroyOnClose
      >
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {selectedDir ? `位于 ${selectedDir}/` : "位于工作空间根"}
        </Typography.Text>
        <Input
          autoFocus
          placeholder="文件名，如 add.py"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          onPressEnter={handleCreate}
          style={{ marginTop: 8 }}
          prefix={<FileAddOutlined />}
        />
      </Modal>

      <Modal
        title="新建文件夹"
        open={folderOpen}
        onOk={handleCreateFolder}
        onCancel={() => setFolderOpen(false)}
        okText="创建"
        cancelText="取消"
        destroyOnClose
      >
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {selectedDir ? `位于 ${selectedDir}/` : "位于工作空间根"}
        </Typography.Text>
        <Input
          autoFocus
          placeholder="文件夹名"
          value={folderName}
          onChange={(e) => setFolderName(e.target.value)}
          onPressEnter={handleCreateFolder}
          style={{ marginTop: 8 }}
          prefix={<FolderAddOutlined />}
        />
      </Modal>
    </div>
  );
}
