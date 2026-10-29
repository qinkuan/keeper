/**
 * 「我的空间」设置段：更换空间目录。
 *
 * 「我的空间」是全局唯一的空间（路径写在 settings.yaml 的 user_space.path），
 * 文件浏览、@ 检索、agent 的默认工作目录都指着它。所以「换空间」不是一个
 * 普通表单字段——换完得让各处缓存的旧路径失效，并把旧空间的检索索引清掉
 * （清完不用手动重建：内核的自动同步会按新空间根重新索引）。
 *
 * 选目录的通道分两种：
 *   · 桌面端：走 Electron 的原生目录对话框（window.keeperSpace.pickFolder），
 *     系统级的选目录体验比自造的强；
 *   · 网页形态：没有原生通道，退回内置的目录浏览器（后端 /user-spaces/browse，
 *     只读列目录）。两条路都只给「换一个目录」这一个动作，不让用户手打路径。
 */
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderOpen, Loader2 } from "lucide-react";
import { Hint, Section, Stack } from "./ui/Page";
import { Modal } from "./ui/Modal";
import { toast } from "./ui/Toast";
import {
  apiBrowseDir,
  apiGetMySpace,
  apiSearchClearIndex,
  apiSetMySpace,
} from "../api/client";

const btn =
  "flex items-center gap-1.5 rounded-lg border border-line bg-panel px-3 py-[7px] text-[13px] font-medium text-ink-muted shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-all duration-150 hover:bg-surface hover:text-ink-strong active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50";

/**
 * 内置目录浏览器（网页形态的回退通道）。
 *
 * 只列**子目录**：选的是空间根，文件不需要出现在这里。父目录那一栏由后端给
 * （已是根则为 null）。
 */
function DirBrowser({
  start,
  onCancel,
  onPick,
}: {
  /** 起始目录（一般是当前空间根）；空 = 从用户 home 开始 */
  start: string;
  onCancel: () => void;
  onPick: (path: string) => void;
}) {
  const [cur, setCur] = useState(start);
  const q = useQuery({
    queryKey: ["browse-dir", cur],
    queryFn: () => apiBrowseDir(cur),
    staleTime: 5_000,
  });
  const d = q.data;

  return (
    <Modal
      open
      onClose={onCancel}
      title="选择空间目录"
      width={560}
      footer={
        <>
          <button type="button" onClick={onCancel} className={btn}>
            取消
          </button>
          <button
            type="button"
            disabled={!d?.path}
            onClick={() => d?.path && onPick(d.path)}
            className={btn + " bg-brand-gradient text-white hover:brightness-110 active:scale-95"}
          >
            选用此目录
          </button>
        </>
      }
    >
      <Stack>
        <div className="flex items-center gap-2">
          <div
            className="min-w-0 flex-1 truncate rounded-lg border border-line-soft bg-surface px-2.5 py-1.5 font-mono text-[12px] text-ink"
            title={d?.path || ""}
          >
            {q.isLoading ? "读取中…" : d?.path || "（无法读取）"}
          </div>
          <button
            type="button"
            disabled={!d?.parent}
            onClick={() => d?.parent && setCur(d.parent)}
            className={btn + " shrink-0"}
          >
            上一级
          </button>
        </div>

        {d?.error && <Hint tone="danger">{d.error}</Hint>}

        <div className="max-h-[46vh] overflow-auto rounded-lg border border-line-soft">
          {(d?.dirs ?? []).length === 0 ? (
            <p className="py-6 text-center text-[12.5px] text-ink-faint">
              {q.isLoading ? "读取中…" : "此目录下没有子文件夹"}
            </p>
          ) : (
            (d?.dirs ?? []).map((e) => (
              <button
                key={e.path}
                type="button"
                onClick={() => setCur(e.path)}
                className="flex w-full items-center gap-2 border-b border-line-soft px-3 py-2 text-left text-[12.5px] text-ink transition-colors last:border-b-0 hover:bg-surface"
              >
                <FolderOpen className="h-3.5 w-3.5 shrink-0 text-ink-faint" />
                <span className="min-w-0 flex-1 truncate">{e.name}</span>
              </button>
            ))
          )}
        </div>
      </Stack>
    </Modal>
  );
}

export function MySpaceSettings() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["my-space"], queryFn: apiGetMySpace });
  const cur = (q.data?.path ?? "").trim();

  /** 桌面壳才有原生目录对话框（Electron 注入 window.keeperSpace） */
  const isDesktop = useMemo(
    () => typeof (window as any).keeperSpace !== "undefined",
    []
  );
  const [browserOpen, setBrowserOpen] = useState(false);

  const saveM = useMutation({
    mutationFn: async (p: string) => {
      await apiSetMySpace(p);
      /* 换空间 → 旧空间的索引条目必须作废：留着的话 @ 搜索还会搜到旧路径下的
       * 文件（内容页搜索同理），点开却是另一个目录里的东西。
       * 清空后不用手动重建——内核的自动同步每轮都按**当前**空间根扫描
       * （见 keeper/search/config.py 的 roots()），新空间会被自动索引回去。 */
      let cleared = true;
      try {
        await apiSearchClearIndex(true);
      } catch {
        cleared = false;
      }
      return { cleared };
    },
    onSuccess: ({ cleared }) => {
      toast.success(
        cleared
          ? "已更换「我的空间」（索引已清空，稍后自动重建）"
          : "已更换「我的空间」（但旧索引未能清空，检索里可能还残留旧文件）"
      );
      /* 换空间后各处缓存的旧路径都得作废：空间页 / 检索统计 / @ 检索结果 */
      qc.invalidateQueries({ queryKey: ["my-space"] });
      qc.invalidateQueries({ queryKey: ["search-stats"] });
      qc.invalidateQueries({ queryKey: ["space-file-search"] });
      qc.invalidateQueries({ queryKey: ["chat-mention-search"] });
    },
    onError: (e: Error) => toast.error("保存失败：" + e.message),
  });

  /** 桌面：用原生对话框选目录；网页：退回内置目录浏览器 */
  const choose = async () => {
    if (isDesktop) {
      try {
        const p = await (window as any).keeperSpace.pickFolder();
        if (p) saveM.mutate(p);
      } catch (e: any) {
        toast.error(e?.message || "选择目录失败");
      }
      return;
    }
    setBrowserOpen(true);
  };

  return (
    <Stack>
      <p className="text-[12px] leading-6 text-ink-muted">
        「我的空间」是全局唯一的一个文件夹：文件浏览、输入框里的 <code>@</code>{" "}
        文件检索、以及 agent 默认能访问的工作目录都指着它。换成别的目录后，这些地方
        会一起改指向新目录。
      </p>

      <Section title="当前空间">
        <Stack>
          <div className="flex items-center gap-2">
            <div
              className="min-w-0 flex-1 truncate rounded-lg border border-line-soft bg-surface px-2.5 py-2 font-mono text-[12px] text-ink"
              title={cur}
            >
              {q.isLoading ? "读取中…" : cur || "（未配置）"}
            </div>
            <button
              type="button"
              onClick={choose}
              disabled={saveM.isPending}
              className={
                btn +
                " shrink-0 bg-brand-gradient text-white hover:brightness-110 active:scale-95"
              }
            >
              {saveM.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <FolderOpen className="h-3.5 w-3.5" />
              )}
              更换空间…
            </button>
          </div>
        </Stack>
      </Section>

      {browserOpen && (
        <DirBrowser
          start={cur}
          onCancel={() => setBrowserOpen(false)}
          onPick={(p) => {
            setBrowserOpen(false);
            saveM.mutate(p);
          }}
        />
      )}
    </Stack>
  );
}
