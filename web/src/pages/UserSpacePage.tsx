import { useState } from "react";
import {
  Breadcrumb,
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Switch,
  Table,
  Tag,
  Tree,
  message,
} from "antd";
import type { DataNode } from "antd/es/tree";
import { FileOutlined, FolderOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  apiBrowseDir,
  apiCreateUserSpace,
  apiDeleteUserSpace,
  apiListUserSpaces,
  apiUpdateUserSpace,
} from "../api/client";
import type { DirBrowseResp, UserSpace, UserSpaceInput } from "../api/types";

const DEFAULT_FORM: UserSpaceInput & { id?: string } = {
  name: "",
  path: "",
  read_only: true,
  description: null,
};

/** 用户空间管理页：增删改查用户自管的命名文件路径。 */
export default function UserSpacePage() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<UserSpace | null>(null);
  const [form] = Form.useForm<UserSpaceInput>();

  // 目录浏览（选工作目录）：左侧目录树 + 右侧当前目录内容
  const [browseOpen, setBrowseOpen] = useState(false);
  const [browsePath, setBrowsePath] = useState("");
  const [browseParent, setBrowseParent] = useState<string | null>(null);
  const [browseDirs, setBrowseDirs] = useState<DirBrowseResp["dirs"]>([]);
  const [browseFiles, setBrowseFiles] = useState<DirBrowseResp["files"]>([]);
  const [browseErr, setBrowseErr] = useState<string | null>(null);
  const [browseLoading, setBrowseLoading] = useState(false);
  const [treeData, setTreeData] = useState<DataNode[]>([]);
  const [treeLoadingKey, setTreeLoadingKey] = useState<string | null>(null);
  const [expandedKeys, setExpandedKeys] = useState<React.Key[]>([]);
  const [selectedKeys, setSelectedKeys] = useState<React.Key[]>([]);

  /** 把一个路径下的子目录取出来，转成 Tree 节点（子节点先标记为可展开）。 */
  const loadTreeChildren = async (path: string): Promise<DataNode[]> => {
    const r = await apiBrowseDir(path);
    return r.dirs.map((d) => ({
      title: d.name,
      key: d.path,
      isLeaf: false,
    }));
  };
  /** 在树里按 key 递归地把 children 塞进去（懒加载用）。 */
  const injectTreeChildren = (
    nodes: DataNode[],
    key: string,
    children: DataNode[]
  ): DataNode[] =>
    nodes.map((n) =>
      n.key === key
        ? { ...n, children }
        : n.children
          ? { ...n, children: injectTreeChildren(n.children, key, children) }
          : n
    );
  /** 在树里按 key 递归查找节点。 */
  const findNode = (nodes: DataNode[], key: string): DataNode | undefined => {
    for (const n of nodes) {
      if (n.key === key) return n;
      if (n.children) {
        const f = findNode(n.children, key);
        if (f) return f;
      }
    }
    return undefined;
  };

  /** 把树同步到 target 路径：确保从 "/" 起的祖先链都展开、目标节点被选中。
      除末节点外逐级确保子目录已加载（这样目标节点才会存在于树中）。 */
  const syncTreeToPath = async (
    target: string,
    data0: DataNode[]
  ): Promise<{ data: DataNode[]; expanded: React.Key[] }> => {
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
    return { data, expanded: chain.slice(0, -1) };
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
      setSelectedKeys([r.path]);
    } catch (e: any) {
      setBrowseErr(e?.message || "浏览失败");
    } finally {
      setBrowseLoading(false);
    }
  };
  const openBrowse = async () => {
    const start = (form.getFieldValue("path") as string) || undefined;
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
      let data: DataNode[] = [{ title: "/", key: "/", children: rootKids }];
      const syn = await syncTreeToPath(r.path, data);
      data = syn.data;
      setTreeData(data);
      setExpandedKeys(syn.expanded);
      setSelectedKeys([r.path]);
    } catch (e: any) {
      setBrowseErr(e?.message || "浏览失败");
    } finally {
      setBrowseLoading(false);
    }
  };
  const onTreeLoad = async (node: { key: React.Key; children?: DataNode[] }) => {
    if (node.children) return;
    const key = String(node.key);
    setTreeLoadingKey(key);
    try {
      const kids = await loadTreeChildren(key);
      setTreeData((prev) => injectTreeChildren(prev, key, kids));
    } finally {
      setTreeLoadingKey(null);
    }
  };
  const onTreeSelect = (keys: React.Key[]) => {
    if (keys[0] != null) void navigateDir(keys[0] as string);
  };
  const onTreeExpand = (keys: React.Key[]) => {
    setExpandedKeys(keys);
  };
  const chooseDir = () => {
    form.setFieldsValue({ path: browsePath });
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
      return id
        ? apiUpdateUserSpace(id, body)
        : apiCreateUserSpace(body);
    },
    onSuccess: () => {
      message.success("已保存");
      setOpen(false);
      qc.invalidateQueries({ queryKey: ["user-spaces"] });
    },
    onError: (e: any) => message.error(e?.message || "保存失败"),
  });

  const delM = useMutation({
    mutationFn: (id: string) => apiDeleteUserSpace(id),
    onSuccess: () => {
      message.success("已删除（引用它的会话将回落到 agent 默认目录）");
      qc.invalidateQueries({ queryKey: ["user-spaces"] });
    },
    onError: (e: any) => message.error(e?.message || "删除失败"),
  });

  const openCreate = () => {
    setEditing(null);
    form.setFieldsValue(DEFAULT_FORM);
    setOpen(true);
  };
  const openEdit = (row: UserSpace) => {
    setEditing(row);
    form.setFieldsValue(row);
    setOpen(true);
  };

  const submit = async () => {
    const v = await form.validateFields();
    saveM.mutate({ ...v, id: editing?.id });
  };

  return (
    <div>
      <Space style={{ justifyContent: "space-between", width: "100%", marginBottom: 12 }}>
        <div>
          <h3 style={{ margin: 0 }}>用户空间</h3>
          <span style={{ color: "#888" }}>
            用户自管的命名文件路径：每个会话可绑定一个，作为 agent 干活的主目录。其中
            <b> default </b> 为系统默认工作空间（~/.keeper/workspace/user/default），始终存在、不可删除。新建空间会在 user/ 下建立同名软链接，指向你填的真实文件夹。
          </span>
        </div>
        <Button type="primary" onClick={openCreate}>
          新建用户空间
        </Button>
      </Space>

      {q.isLoading && <span>加载中…</span>}
      {q.isError && <span style={{ color: "red" }}>加载失败：{(q.error as Error)?.message}</span>}

      <Table<UserSpace>
        rowKey="id"
        dataSource={q.data?.user_spaces ?? []}
        pagination={false}
        columns={[
          { title: "名称", dataIndex: "name" },
          { title: "路径", dataIndex: "path", ellipsis: true },
          {
            title: "只读",
            dataIndex: "read_only",
            width: 80,
            render: (v: boolean) =>
              v ? <Tag color="orange">只读</Tag> : <Tag color="green">可写</Tag>,
          },
          { title: "说明", dataIndex: "description", ellipsis: true, render: (v) => v || "—" },
          {
            title: "操作",
            width: 140,
            render: (_, row) => {
              const isDefault = row.name === "default";
              return (
                <Space>
                  <Button size="small" disabled={isDefault} onClick={() => openEdit(row)}>
                    编辑
                  </Button>
                  <Popconfirm
                    title="删除该用户空间？"
                    description={
                      isDefault
                        ? "系统默认工作空间不可删除或编辑（除非手动改库）。"
                        : "引用它的会话将回落到默认用户空间。"
                    }
                    onConfirm={() => delM.mutate(row.id)}
                  >
                    <Button size="small" danger disabled={isDefault}>
                      删除
                    </Button>
                  </Popconfirm>
                </Space>
              );
            },
          },
        ]}
      />

      <Modal
        open={open}
        title={editing ? `编辑：${editing.name}` : "新建用户空间"}
        onCancel={() => setOpen(false)}
        onOk={submit}
        confirmLoading={saveM.isPending}
        destroyOnClose
      >
        <Form form={form} layout="vertical" initialValues={DEFAULT_FORM}>
          <Form.Item
            name="name"
            label="名称"
            rules={[{ required: true, message: "请输入名称" }]}
          >
            <Input placeholder="如：我的仓库" />
          </Form.Item>
          <Form.Item
            name="path"
            label="绝对路径"
            rules={[{ required: true, message: "请输入绝对路径" }]}
            extra="填真实文件夹的绝对路径；可点右侧「浏览」从服务器本机挑选；后端会在 user/ 下建立同名软链接指向它，路径不存在时自动创建（mkdir）。"
          >
            <Input
              placeholder="/Users/me/projects/xxx"
              addonAfter={
                <Button type="link" size="small" style={{ padding: 0 }} onClick={openBrowse}>
                  浏览
                </Button>
              }
            />
          </Form.Item>
          <Form.Item name="read_only" label="只读" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item name="description" label="说明">
            <Input.TextArea rows={2} placeholder="可选" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={browseOpen}
        title="选择目录"
        onCancel={() => setBrowseOpen(false)}
        onOk={chooseDir}
        okText="选择此目录"
        width={760}
        destroyOnClose
      >
        <style>{`.browse-row:hover{background:var(--kp-primary-soft);cursor:pointer;} .browse-row.file{cursor:default;}`}</style>
        <Space style={{ marginBottom: 8 }}>
          <Button
            size="small"
            disabled={!browseParent}
            onClick={() => navigateDir(browseParent || undefined)}
          >
            上一级
          </Button>
        </Space>
        <Breadcrumb
          style={{ marginBottom: 8 }}
          items={(() => {
            const segs = browsePath.split("/").filter((s) => s !== "");
            if (segs.length === 0) return [{ title: "/" }];
            return segs.map((name, i) => {
              const cum = "/" + segs.slice(0, i + 1).join("/");
              const isLast = i === segs.length - 1;
              return {
                title: isLast ? name : <a onClick={() => navigateDir(cum)}>{name}</a>,
              };
            });
          })()}
        />
        {browseErr && <div style={{ color: "red", marginBottom: 8 }}>{browseErr}</div>}
        <div style={{ display: "flex", gap: 12, height: 360 }}>
          {/* 左：目录树 */}
          <div
            style={{
              width: 240,
              border: "1px solid var(--kp-border-soft)",
              borderRadius: 4,
              overflow: "auto",
              padding: "4px 0",
            }}
          >
            <Tree
              treeData={treeData}
              selectedKeys={selectedKeys}
              expandedKeys={expandedKeys}
              onExpand={onTreeExpand}
              loadData={onTreeLoad}
              onSelect={onTreeSelect}
              blockNode
            />
          </div>
          {/* 右：当前目录内容（名称 / 大小 / 修改时间） */}
          <div
            style={{
              flex: 1,
              border: "1px solid var(--kp-border-soft)",
              borderRadius: 4,
              overflow: "auto",
            }}
          >
            <Table<DirBrowseResp["dirs"][number] & { isDir: boolean }>
              rowKey="path"
              size="small"
              pagination={false}
              loading={browseLoading}
              dataSource={[
                ...browseDirs.map((d) => ({ ...d, isDir: true })),
                ...browseFiles.map((f) => ({ ...f, isDir: false })),
              ]}
              onRow={(rec) => ({
                className: `browse-row ${rec.isDir ? "" : "file"}`,
                onClick: rec.isDir ? () => navigateDir(rec.path) : undefined,
                onDoubleClick: rec.isDir ? () => navigateDir(rec.path) : undefined,
              })}
              columns={[
                {
                  title: "名称",
                  dataIndex: "name",
                  render: (name: string, rec) => (
                    <span
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 8,
                        color: rec.isDir ? "#333" : "#999",
                      }}
                    >
                      {rec.isDir ? (
                        <FolderOutlined style={{ color: "#e8b84b" }} />
                      ) : (
                        <FileOutlined style={{ color: "var(--kp-text-muted)" }} />
                      )}
                      {name}
                    </span>
                  ),
                },
                {
                  title: "大小",
                  dataIndex: "size",
                  width: 110,
                  render: (s: number | null) => fmtSize(s),
                },
                {
                  title: "修改时间",
                  dataIndex: "mtime",
                  width: 160,
                  render: (m: number | null) => fmtMtime(m),
                },
              ]}
            />
            {!browseLoading &&
              browseDirs.length === 0 &&
              browseFiles.length === 0 &&
              !browseErr && (
                <div style={{ padding: 12, color: "#888" }}>(空目录)</div>
              )}
          </div>
        </div>
      </Modal>
    </div>
  );
}
