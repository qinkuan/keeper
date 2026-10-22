import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RotateCcw, Trash2 } from "lucide-react";
import { apiRestoreSession, apiTrashSessions } from "../api/client";
import type { TrashSessionItem } from "../api/types";
import { Hint, PageHeader, Section } from "../components/ui/Page";
import { Table } from "../components/ui/Table";
import type { Column } from "../components/ui/Table";
import { Pill } from "../components/ui/Pill";
import { toast } from "../components/ui/Toast";

/** 一页多少条。回收站是"偶尔找回一条东西"的地方，不做无限滚动（见后端说明）。 */
const PAGE_SIZE = 50;

/**
 * 回收站：已删除会话的列表 + 恢复。
 *
 * 删除是**软删除**（后端只把 `status` 置成 `deleted`、记下删除时间，消息和用量
 * 一行都没删），所以这里能把会话捞回来，而且恢复后能接着聊——不需要重建任何东西。
 *
 * 跨 agent 展示：删掉的会话可能属于任何一个 agent，甚至属于一个已经卸掉的 agent，
 * 所以每一行都标出它原本属于谁，而不是按当前 agent 过滤掉一批（那样用户会以为
 * 会话凭空消失了）。
 */
export default function RecycleBinPage() {
  const qc = useQueryClient();
  /** 已展示的条数；点「显示更多」加大，而不是翻页——回收站不常用，翻页反而更绕 */
  const [limit, setLimit] = useState(PAGE_SIZE);

  const trashQ = useQuery({
    queryKey: ["trash", limit],
    queryFn: () => apiTrashSessions(limit),
  });

  const restoreM = useMutation({
    mutationFn: (id: string) => apiRestoreSession(id),
    onSuccess: (_d, id) => {
      toast.success("已恢复，可在会话列表里继续对话");
      qc.invalidateQueries({ queryKey: ["trash"] });
      // 恢复后它会重新出现在侧栏会话列表里，不刷的话要切走再回来才看得见
      qc.invalidateQueries({ queryKey: ["sessions"] });
      void id;
    },
    onError: (e: Error) => toast.error("恢复失败：" + e.message),
  });

  const rows = trashQ.data?.sessions ?? [];
  const total = trashQ.data?.total ?? 0;

  const columns: Column<TrashSessionItem>[] = [
    {
      key: "title",
      title: "会话",
      render: (r) => (
        <div className="min-w-0">
          <div className="truncate text-[13px] text-ink-strong">
            {r.title || "新对话"}
          </div>
          <div className="mt-0.5 font-mono text-[11px] text-ink-faint">
            {r.id}
          </div>
        </div>
      ),
    },
    {
      key: "agent",
      title: "所属智能体",
      width: 160,
      render: (r) => (
        <span className="block max-w-[160px] truncate text-ink-muted">
          {r.agent_name || r.agent_id || "—"}
        </span>
      ),
    },
    {
      key: "kind",
      title: "类型",
      width: 88,
      /* 两类都要显式列出来。未知取值（老数据 / 后端加了新类别）兜底成「对话」，
         而不是显示空白。 */
      render: (r) => (
        <Pill tone={r.kind === "task" ? "blue" : "muted"}>
          {r.kind === "task" ? "任务" : "对话"}
        </Pill>
      ),
    },
    {
      key: "messages",
      title: "消息",
      width: 70,
      align: "right",
      render: (r) => `${r.message_count} 条`,
    },
    {
      key: "updated",
      title: "最后更新",
      width: 130,
      render: (r) => (
        <span className="text-ink-muted">{fmt(r.updated_at)}</span>
      ),
    },
    {
      key: "deleted",
      title: "删除时间",
      width: 130,
      render: (r) => (
        <span className={r.deleted_at ? "text-ink-muted" : "text-ink-faint"}>
          {r.deleted_at ? fmt(r.deleted_at) : "未知"}
        </span>
      ),
    },
    {
      key: "ops",
      title: "",
      width: 92,
      render: (r) => (
        <button
          type="button"
          disabled={restoreM.isPending && restoreM.variables === r.id}
          onClick={() => restoreM.mutate(r.id)}
          title="恢复这条会话"
          className="flex h-7 w-full items-center justify-center gap-1 rounded-[7px] border border-line text-[12px] text-ink-muted transition-colors duration-150 hover:border-brand/50 hover:bg-brand/[0.06] hover:text-brand disabled:cursor-not-allowed disabled:opacity-50"
        >
          <RotateCcw className="h-3.5 w-3.5" />
          恢复
        </button>
      ),
    },
  ];

  return (
    <div className="mx-auto w-full max-w-[1080px]">
      <PageHeader
        title="回收站"
        desc="删除会话只是把它从列表里收起来，消息和记录都还在这里。恢复后可以接着聊；彻底清理由保留策略负责，不在删除那一刻发生。"
      />

      <div className="space-y-[18px]">
        <Hint tone="info" title="回收站跨所有智能体">
          这里列出的是<b>全部</b>已删除会话，不管它属于哪个智能体（含已卸载的）。
          每行标出它原本属于谁，免得恢复后找不到回去的地方。
        </Hint>

        <Section
          title={total ? `已删除的会话（${total}）` : "已删除的会话"}
          bodyClassName="p-[18px]"
        >
          <Table
            rows={rows}
            columns={columns}
            rowKey={(r) => r.id}
            loading={trashQ.isLoading}
            empty={
              <div className="flex flex-col items-center gap-2">
                <Trash2 className="h-5 w-5 text-ink-faint" />
                <span>回收站是空的，没有已删除的会话</span>
              </div>
            }
          />
          {rows.length < total && (
            <div className="mt-3 flex justify-center">
              <button
                type="button"
                onClick={() => setLimit((n) => n + PAGE_SIZE)}
                disabled={trashQ.isFetching}
                className="rounded-lg border border-line px-3 py-1.5 text-[12.5px] text-ink-muted transition-colors duration-150 hover:bg-surface hover:text-ink-strong disabled:opacity-50"
              >
                {trashQ.isFetching ? "加载中…" : `显示更多（还有 ${total - rows.length} 条）`}
              </button>
            </div>
          )}
        </Section>
      </div>
    </div>
  );
}

/** 短时间戳：回收站要一眼看出"多久前删的"，月日时分足够，秒没用还占地方。 */
function fmt(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}