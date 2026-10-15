import { useCallback, useEffect, useState } from "react";
import { Loader2, Plus, Search, X } from "lucide-react";
import { cn } from "../lib/utils";
import { apiSessions, apiListTasks, apiCreateTask } from "../api/client";
import type { SessionItem } from "../api/types";
import { Modal } from "./ui/Modal";
import { toast } from "./ui/Toast";

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
/** 状态色：走 Tailwind 语义色，随主题深浅都能读（antd 的 Tag 色板是写死的） */
const TASK_STATUS_CLASS: Record<string, string> = {
  draft: "bg-black/[0.06] text-ink-muted dark:bg-white/[0.08]",
  planning: "bg-blue-500/15 text-blue-600 dark:text-blue-300",
  plan_review: "bg-amber-500/15 text-amber-600 dark:text-amber-300",
  executing: "bg-brand/15 text-brand",
  waiting_input: "bg-cyan-500/15 text-cyan-600 dark:text-cyan-300",
  waiting_review: "bg-orange-500/15 text-orange-600 dark:text-orange-300",
  done: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-300",
  failed: "bg-red-500/15 text-red-600 dark:text-red-300",
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
      toast.error("请填写任务标题");
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
      toast.error(e?.message || "创建失败");
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
    <div className="flex h-full flex-col">
      {/* 顶部：新建入口 */}
      <div className="p-2">
        <button
          type="button"
          onClick={taskMode ? () => setCreateOpen(true) : onNew}
          className={cn(
            "flex w-full items-center justify-center gap-1.5 rounded-[7px] border border-dashed border-line px-2 py-2",
            "text-[12.5px] font-medium text-ink-muted transition-all duration-150",
            "hover:border-brand/50 hover:bg-brand/[0.05] hover:text-brand"
          )}
        >
          <Plus className="h-3.5 w-3.5" />
          {taskMode ? "新建任务" : "新对话"}
        </button>
      </div>

      {/* 搜索 */}
      <div className="px-2 pb-2">
        <div className="flex h-[28px] items-center gap-1.5 rounded-[7px] border border-line bg-panel px-2.5 shadow-[0_1px_1px_rgba(0,0,0,0.03)] transition-all focus-within:border-brand focus-within:shadow-[0_0_0_3px_rgba(91,155,245,0.48)]">
          <Search className="h-3 w-3 shrink-0 text-ink-faint" />
          <input
            value={kw}
            onChange={(e) => setKw(e.target.value)}
            placeholder={taskMode ? "搜索任务" : "搜索会话"}
            className="min-w-0 flex-1 bg-transparent text-[12px] text-ink outline-none placeholder:text-ink-faint"
          />
          {kw && (
            <button
              type="button"
              onClick={() => setKw("")}
              title="清空"
              className="shrink-0 text-ink-faint transition-colors hover:text-ink"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>

      {/* 列表 */}
      <div className="min-h-0 flex-1 overflow-auto px-1.5 pb-2">
        {loading ? (
          <div className="flex items-center justify-center py-4 text-ink-muted">
            <Loader2 className="h-4 w-4 animate-spin" />
          </div>
        ) : shown.length === 0 ? (
          <div className="mt-8 px-3 text-center text-[12px] text-ink-faint">
            {taskMode ? "暂无任务" : "暂无历史会话"}
          </div>
        ) : (
          <></>
        )}

        {/* 分组标题（截图里是「任务 (4)」这种灰字小标题）——条目多的时候
            它让人知道这一屏有多少东西 */}
        {!loading && shown.length > 3 && (
          <div className="px-2 pb-1 pt-0.5 text-[10.5px] font-medium text-ink-faint">
            {taskMode ? "任务" : "会话"}（{shown.length}）
          </div>
        )}

        {!loading &&
          shown.map((s) => {
            const status = taskMode ? taskStatusMap[s.id] : undefined;
            const active = s.id === activeId;
            return (
              <button
                key={s.id}
                type="button"
                onClick={() => onSelect(s.id)}
                className={cn(
                  "mb-0.5 block w-full rounded-[7px] px-2.5 py-[7px] text-left transition-colors duration-150",
                  // 选中态用品牌淡底而不是灰底：抽屉里灰底与白底只差一档，
                  // 扫列表时很难一眼看出当前在哪一条
                  active
                    ? "bg-brand/10 text-brand"
                    : "hover:bg-black/[0.04] dark:hover:bg-white/[0.06]"
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <span
                    className={cn(
                      "min-w-0 flex-1 truncate text-[12.5px]",
                      active ? "font-medium" : "text-ink"
                    )}
                  >
                    {s.title || (taskMode ? "未命名任务" : "新对话")}
                  </span>
                  {status && (
                    <span
                      className={cn(
                        "shrink-0 rounded-md px-1.5 py-0.5 text-[11px] leading-4",
                        TASK_STATUS_CLASS[status] ??
                          "bg-black/[0.06] text-ink-muted"
                      )}
                    >
                      {TASK_STATUS_LABEL[status] || status}
                    </span>
                  )}
                </div>
                {/* 时间戳与标题**同行右对齐**（截图里是「12分钟前」贴在右侧）：
                    两行会占掉一倍高度，而时间只是参考信息 */}
                <div className="mt-px text-[10.5px] text-ink-faint">
                  {fmt(s.updated_at)}
                </div>
              </button>
            );
          })}
      </div>

      <Modal
        title="新建任务"
        open={createOpen}
        onClose={() => !createBusy && setCreateOpen(false)}
        footer={
          <>
            <button
              type="button"
              disabled={createBusy}
              onClick={() => setCreateOpen(false)}
              className="rounded-lg border border-line bg-panel px-4 py-[7px] text-[13px] font-medium text-ink-muted shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-all duration-150 hover:bg-surface hover:text-ink-strong active:scale-[0.98] disabled:opacity-50"
            >
              取消
            </button>
            <button
              type="button"
              disabled={createBusy}
              onClick={doCreate}
              className="flex items-center gap-1.5 rounded-lg bg-brand-gradient px-4 py-[7px] text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98] disabled:opacity-50"
            >
              {createBusy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              创建
            </button>
          </>
        }
      >
        <div className="space-y-3">
          <div>
            <div className="mb-1 text-[13px] text-ink">标题</div>
            <input
              value={createTitle}
              onChange={(e) => setCreateTitle(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") doCreate();
              }}
              placeholder="给任务起个名字"
              className="w-full rounded-[7px] border border-line bg-panel px-2.5 py-2 text-[12.5px] text-ink shadow-[0_1px_1px_rgba(0,0,0,0.03)] outline-none transition-all duration-150 placeholder:text-ink-faint focus:border-brand focus:shadow-[0_0_0_3px_rgba(91,155,245,0.48)]"
            />
          </div>
          <div>
            <div className="mb-1 text-[13px] text-ink">描述（可选）</div>
            <textarea
              rows={4}
              value={createDesc}
              onChange={(e) => setCreateDesc(e.target.value)}
              placeholder="想让 agent 完成什么，越具体越好"
              className="w-full resize-none rounded-[7px] border border-line bg-panel px-2.5 py-2 text-[12.5px] leading-6 text-ink shadow-[0_1px_1px_rgba(0,0,0,0.03)] outline-none transition-all duration-150 placeholder:text-ink-faint focus:border-brand focus:shadow-[0_0_0_3px_rgba(91,155,245,0.48)]"
            />
          </div>
        </div>
      </Modal>
    </div>
  );
}
