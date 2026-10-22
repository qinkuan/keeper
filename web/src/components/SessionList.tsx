import { useCallback, useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Loader2,
  Pencil,
  Plus,
  Search,
  Trash2,
  X,
} from "lucide-react";
import { cn } from "../lib/utils";
import {
  apiDeleteSession,
  apiRenameSession,
  apiSessions,
  apiListTasks,
  apiCreateTask,
} from "../api/client";
import type { SessionItem, SessionKind } from "../api/types";
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
  kind = "chat",
  onSelect,
  onNew,
  onNewTask,
  hideCreate,
}: {
  agentId: string;
  activeId?: string | null;
  /**
   * 只列这一类会话：「对话」页按当前 tab 传 chat / task。
   *
   * 为什么用类别而不是「要不要任务」这种布尔：两类是**互斥**的，任务会话不该
   * 出现在「会话」tab 里。而布尔只能表达二分，`kind !== "task"` 会把别的一并放
   * 进去——一个不报错的静默串类。
   */
  kind?: SessionKind;
  onSelect: (sessionId: string) => void;
  onNew: () => void;
  onNewTask?: (sessionId: string) => void;
  /** 隐藏「新建」入口 */
  hideCreate?: boolean;
}) {
  const taskMode = kind === "task";
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
      /* 类别过滤交给后端（apiSessions 的 kind 参数），这里不再二次筛。
         前端筛是错的：列表分页在服务端就已经切好了，前端再筛等于「这一页 50 条里
         挑出属于这一类的那几条」，同类会话一多就会把另一页挤成空洞。 */
      const resp = await apiSessions(agentId, undefined, 50, 0, kind);
      setItems(resp.sessions || []);
      if (kind === "task") {
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
  }, [agentId, kind]);

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
  const filterKw = (list: SessionItem[]) =>
    kw2 ? list.filter((s) => (s.title || "").toLowerCase().includes(kw2)) : list;
  const shown = filterKw(items);

  return (
    <div className="flex h-full flex-col">
      {/* 顶部：新建入口 */}
      {!hideCreate && (
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
      )}

      {/* 搜索 */}
      <div className="px-2 pb-2">
        <div className="flex h-[28px] items-center gap-1.5 rounded-[7px] border border-line bg-panel px-2.5 shadow-[0_1px_1px_rgba(0,0,0,0.03)] transition-all focus-within:border-brand focus-within:shadow-[0_0_0_3px_rgba(66,133,244,0.48)]">
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
          shown.map((s) => (
            <SessionRow
              key={s.id}
              session={s}
              agentId={agentId}
              active={s.id === activeId}
              taskMode={taskMode}
              status={taskMode ? taskStatusMap[s.id] : undefined}
              onSelect={onSelect}
              /* 删掉的正是当前会话时不能停在一条已不存在的会话上——
                 这里复用「新建」，把视图挪到一个干净的新会话。 */
              onDeleted={() => {
                if (s.id === activeId) onNew();
              }}
              onChanged={load}
            />
          ))}
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
              className="w-full rounded-[7px] border border-line bg-panel px-2.5 py-2 text-[12.5px] text-ink shadow-[0_1px_1px_rgba(0,0,0,0.03)] outline-none transition-all duration-150 placeholder:text-ink-faint focus:border-brand focus:shadow-[0_0_0_3px_rgba(66,133,244,0.48)]"
            />
          </div>
          <div>
            <div className="mb-1 text-[13px] text-ink">描述（可选）</div>
            <textarea
              rows={4}
              value={createDesc}
              onChange={(e) => setCreateDesc(e.target.value)}
              placeholder="想让 agent 完成什么，越具体越好"
              className="w-full resize-none rounded-[7px] border border-line bg-panel px-2.5 py-2 text-[12.5px] leading-6 text-ink shadow-[0_1px_1px_rgba(0,0,0,0.03)] outline-none transition-all duration-150 placeholder:text-ink-faint focus:border-brand focus:shadow-[0_0_0_3px_rgba(66,133,244,0.48)]"
            />
          </div>
        </div>
      </Modal>
    </div>
  );
}

/**
 * 一行会话 / 任务：标题 + 状态 + 时间，hover 时露出**重命名 / 删除**。
 *
 * 这两个操作原先只在侧栏的会话轨里，而那里已经搬走了。会话的新建、查询本来就
 * 在这个面板（也就是"会话管理 / 任务管理"），把改和删补齐之后，增删改查四件事
 * 才真正聚在**一处**——否则用户管一个会话要跨两个地方找动作。
 *
 * 拆成独立组件：它自己要管两份局部状态（是否在改名、输入框内容），塞进
 * SessionList 会让那个组件同时管「列表与筛选」和「某一项的编辑」两件事。
 */
function SessionRow({
  session,
  agentId,
  active,
  taskMode,
  status,
  onSelect,
  onDeleted,
  onChanged,
}: {
  session: SessionItem;
  agentId: string;
  active: boolean;
  taskMode: boolean;
  status?: string;
  onSelect: (sessionId: string) => void;
  /** 删掉的是当前会话时调用：让上层离开这条已不存在的会话 */
  onDeleted: () => void;
  onChanged: () => Promise<void> | void;
}) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(session.title ?? "");

  const renameM = useMutation({
    mutationFn: (title: string) => apiRenameSession(agentId, session.id, title),
    onSuccess: async () => {
      setEditing(false);
      qc.invalidateQueries({ queryKey: ["sessions"] });
      // 本面板是自管数据的（load() 拉列表），react-query 的失效不会自动刷新它
      await onChanged();
      toast.success("已重命名");
    },
    onError: (e: Error) => toast.error("重命名失败：" + e.message),
  });

  const delM = useMutation({
    mutationFn: () => apiDeleteSession(agentId, session.id),
    onSuccess: async () => {
      qc.invalidateQueries({ queryKey: ["sessions"] });
      if (active) onDeleted();
      await onChanged();
      toast.success("已删除，可在「回收站」恢复");
    },
    onError: (e: Error) => toast.error("删除失败：" + e.message),
  });

  if (editing) {
    return (
      <form
        className="mb-0.5 flex items-center gap-1 rounded-[7px] bg-brand/10 px-2 py-[6px]"
        onSubmit={(e) => {
          e.preventDefault();
          const t = draft.trim();
          // 允许清空标题：回落到后端给的默认名，而不是拦下这次提交
          if (t) renameM.mutate(t);
          else setEditing(false);
        }}
      >
        <input
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") setEditing(false);
          }}
          className="min-w-0 flex-1 bg-transparent text-[12.5px] text-ink outline-none placeholder:text-ink-faint"
          placeholder={taskMode ? "任务标题" : "会话标题"}
        />
        <button
          type="submit"
          disabled={renameM.isPending}
          className="shrink-0 text-[11.5px] font-medium text-brand disabled:opacity-50"
        >
          {renameM.isPending ? "…" : "存"}
        </button>
      </form>
    );
  }

  return (
    <div
      className={cn(
        "group mb-0.5 flex w-full items-center rounded-[7px] transition-colors duration-150",
        // 选中态用品牌淡底而不是灰底：抽屉里灰底与白底只差一档，
        // 扫列表时很难一眼看出当前在哪一条
        active
          ? "bg-brand/10"
          : "hover:bg-black/[0.04] dark:hover:bg-white/[0.06]"
      )}
    >
      <button
        type="button"
        onClick={() => onSelect(session.id)}
        className="min-w-0 flex-1 px-2.5 py-[7px] text-left"
      >
        <div className="flex items-center justify-between gap-2">
          <span
            className={cn(
              "min-w-0 flex-1 truncate text-[12.5px]",
              active ? "font-medium text-brand" : "text-ink"
            )}
          >
            {session.title || (taskMode ? "未命名任务" : "新对话")}
          </span>
          {status && (
            <span
              className={cn(
                "shrink-0 rounded-md px-1.5 py-0.5 text-[11px] leading-4",
                TASK_STATUS_CLASS[status] ?? "bg-black/[0.06] text-ink-muted"
              )}
            >
              {TASK_STATUS_LABEL[status] || status}
            </span>
          )}
        </div>
        {/* 时间戳与标题**分行**：两行会占掉一倍高度，所以这里压成一行 */}
        <div className="mt-px text-[10.5px] text-ink-faint">
          {fmtTime(session.updated_at)}
        </div>
      </button>

      {/* hover 才显形：常驻会把标题挤没 */}
      <span className="flex shrink-0 items-center pr-1 opacity-0 transition-opacity duration-150 focus-within:opacity-100 group-hover:opacity-100">
        <button
          type="button"
          title="重命名"
          onClick={() => {
            setDraft(session.title ?? "");
            setEditing(true);
          }}
          className="flex h-6 w-6 items-center justify-center rounded-[6px] text-ink-faint transition-colors hover:bg-black/[0.06] hover:text-ink dark:hover:bg-white/10"
        >
          <Pencil className="h-3 w-3" />
        </button>
        <button
          type="button"
          title="删除"
          disabled={delM.isPending}
          onClick={() => {
            const name = session.title || (taskMode ? "未命名任务" : "新对话");
            if (
              confirm(
                `删除${taskMode ? "任务" : "会话"}「${name}」？删除后它会从列表里消失，可在「回收站」恢复。`
              )
            )
              delM.mutate();
          }}
          className="flex h-6 w-6 items-center justify-center rounded-[6px] text-ink-faint transition-colors hover:bg-red-500/12 hover:text-red-600 disabled:opacity-50 dark:hover:bg-red-500/20 dark:hover:text-red-300"
        >
          <Trash2 className="h-3 w-3" />
        </button>
      </span>
    </div>
  );
}

/** 短时间戳：抽屉宽度有限，秒没用还占地方。 */
function fmtTime(iso?: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}


