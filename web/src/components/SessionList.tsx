import { useCallback, useEffect, useState } from "react";
import { Button, Empty, Input, Modal, Spin, Tag, Typography, message } from "antd";
import { PlusOutlined, SearchOutlined } from "@ant-design/icons";
import { apiSessions, apiListTasks, apiCreateTask } from "../api/client";
import type { SessionItem } from "../api/types";

const TASK_STATUS_LABEL: Record<string, string> = {
  draft: "草稿",
  planning: "规划中",
  plan_review: "待审计划",
  executing: "执行中",
  waiting_input: "等待输入",
  waiting_review: "待审结果",
  done: "已完成",
  failed: "失败",
};
const TASK_STATUS_COLOR: Record<string, string> = {
  draft: "default",
  planning: "blue",
  plan_review: "gold",
  executing: "processing",
  waiting_input: "cyan",
  waiting_review: "orange",
  done: "green",
  failed: "red",
};

export default function SessionList({
  agentId,
  activeId,
  taskMode = false,
  onSelect,
  onNew,
  onNewTask,
}: {
  agentId: string;
  activeId?: string | null;
  taskMode?: boolean;
  onSelect: (sessionId: string) => void;
  onNew: () => void;
  onNewTask?: (sessionId: string) => void;
}) {
  const [items, setItems] = useState<SessionItem[]>([]);
  const [taskStatusMap, setTaskStatusMap] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  const [kw, setKw] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [createTitle, setCreateTitle] = useState("");
  const [createDesc, setCreateDesc] = useState("");
  const [createBusy, setCreateBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await apiSessions(agentId);
      const list = (resp.sessions || []).filter((s) =>
        taskMode ? s.kind === "task" : s.kind !== "task"
      );
      setItems(list);
      if (taskMode) {
        try {
          const tr = await apiListTasks(agentId);
          const map: Record<string, string> = {};
          (tr.tasks || []).forEach((t) => {
            if (t.session_id) map[t.session_id] = t.status;
          });
          setTaskStatusMap(map);
        } catch {
          /* ignore */
        }
      }
    } catch {
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [agentId, taskMode]);

  useEffect(() => {
    load();
  }, [load]);

  const doCreate = async () => {
    if (!createTitle.trim()) {
      message.warning("请填写任务标题");
      return;
    }
    setCreateBusy(true);
    try {
      const r = await apiCreateTask(
        agentId,
        createTitle.trim(),
        createDesc.trim() || undefined
      );
      setCreateOpen(false);
      setCreateTitle("");
      setCreateDesc("");
      onNewTask?.(r.task.session_id ?? "");
      await load();
    } catch (e: any) {
      message.error(e?.message || "创建失败");
    } finally {
      setCreateBusy(false);
    }
  };

  const fmt = (s?: string | null) =>
    s
      ? new Date(s).toLocaleString("zh-CN", {
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
        })
      : "";

  const kw2 = kw.trim().toLowerCase();
  const shown = kw2
    ? items.filter((s) => (s.title || "").toLowerCase().includes(kw2))
    : items;

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      {/* 顶部：新建入口（与「新建会话」对称） */}
      <div style={{ padding: "10px 12px" }}>
        {taskMode ? (
          <Button
            block
            type="dashed"
            size="small"
            icon={<PlusOutlined />}
            onClick={() => setCreateOpen(true)}
          >
            + 新建任务
          </Button>
        ) : (
          <Button
            block
            type="dashed"
            size="small"
            icon={<PlusOutlined />}
            onClick={onNew}
          >
            + 新对话
          </Button>
        )}
      </div>

      {/* 搜索 */}
      <div style={{ padding: "0 12px 8px" }}>
        <Input
          size="small"
          allowClear
          prefix={<SearchOutlined />}
          placeholder={taskMode ? "搜索任务" : "搜索会话"}
          value={kw}
          onChange={(e) => setKw(e.target.value)}
        />
      </div>

      {/* 列表 */}
      <div style={{ flex: 1, minHeight: 0, overflow: "auto", padding: "0 8px 8px" }}>
        {loading ? (
          <div style={{ padding: 16, textAlign: "center" }}>
            <Spin size="small" />
          </div>
        ) : shown.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={taskMode ? "暂无任务" : "暂无历史会话"}
            style={{ marginTop: 40 }}
          />
        ) : (
          shown.map((s) => {
            const status = taskMode ? taskStatusMap[s.id] : undefined;
            return (
              <div
                key={s.id}
                onClick={() => onSelect(s.id)}
                style={{
                  cursor: "pointer",
                  padding: "8px 10px",
                  marginBottom: 4,
                  borderRadius: 6,
                  background: s.id === activeId ? "var(--kp-primary-soft)" : "transparent",
                }}
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: 8,
                  }}
                >
                  <Typography.Text ellipsis style={{ fontSize: 13 }}>
                    {s.title || (taskMode ? "未命名任务" : "新对话")}
                  </Typography.Text>
                  {status && (
                    <Tag
                      color={TASK_STATUS_COLOR[status]}
                      style={{ fontSize: 11, marginRight: 0, flex: "0 0 auto" }}
                    >
                      {TASK_STATUS_LABEL[status] || status}
                    </Tag>
                  )}
                </div>
                <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                  {fmt(s.updated_at)}
                </Typography.Text>
              </div>
            );
          })
        )}
      </div>

      {/* 新建任务弹窗 */}
      <Modal
        title="新建任务"
        open={createOpen}
        confirmLoading={createBusy}
        onOk={doCreate}
        onCancel={() => !createBusy && setCreateOpen(false)}
        okText="创建"
        cancelText="取消"
        destroyOnClose
      >
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 10,
            marginTop: 8,
          }}
        >
          <div>
            <Typography.Text>标题</Typography.Text>
            <Input
              value={createTitle}
              onChange={(e) => setCreateTitle(e.target.value)}
              placeholder="给任务起个名字"
              onPressEnter={doCreate}
            />
          </div>
          <div>
            <Typography.Text>描述（可选）</Typography.Text>
            <Input.TextArea
              rows={4}
              value={createDesc}
              onChange={(e) => setCreateDesc(e.target.value)}
              placeholder="想让 agent 完成什么，越具体越好"
            />
          </div>
        </div>
      </Modal>
    </div>
  );
}
