import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  Download,
  Eye,
  Loader2,
  RefreshCw,
  Search as SearchIcon,
  Trash2,
} from "lucide-react";
import {
  apiSearchClearIndex,
  apiSearchConfig,
  apiSearchDeleteDocument,
  apiSearchDeleteDocumentChunks,
  apiSearchDocuments,
  apiSearchIndex,
  apiSearchModelDownload,
  apiSearchQuery,
  apiSearchSaveConfig,
  apiSearchStatus,
  apiSearchVectorBackfill,
} from "../api/client";
import type { SearchHit } from "../api/types";
import { Hint, PageHeader, Section, Stack } from "../components/ui/Page";
import { Modal } from "../components/ui/Modal";
import { SegmentedControl } from "../components/ui/Segmented";
import { Stat } from "../components/ui/Stat";
import { Table, type Column } from "../components/ui/Table";
import { Field, NumberInput } from "../components/ui/Form";
import type { SearchDocument, SearchSyncConfig } from "../api/types";
import { Pill } from "../components/ui/Pill";
import { ConfirmButton, PLAIN_BTN } from "../components/ui/Confirm";
import { toast } from "../components/ui/Toast";

/** 主按钮 / 次按钮的统一样式（与其它页面一致）。 */
const BTN_PRIMARY =
  "flex h-8 items-center gap-1.5 rounded-lg bg-brand-gradient px-3.5 text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] disabled:opacity-50";
const BTN =
  "flex h-8 items-center gap-1.5 rounded-lg border border-line-soft bg-panel px-3 text-[12.5px] text-ink-muted shadow-card transition-colors duration-150 hover:border-brand/40 hover:text-brand disabled:opacity-50";

/** 检索模式。向量模式需要 embedding 模型，没装时后端会明确报错。 */
const MODES = [
  { label: "混合", value: "hybrid" },
  { label: "纯向量", value: "vector" },
  { label: "纯关键词", value: "keyword" },
] as const;

/**
 * 搜索页：两个 tab ——「索引管理」与「搜索」。
 *
 * 分成两个 tab 是因为这是**两件事**：前者管的是「索引这份数据本身的状态」
 * （模型在不在、索引建了没、哪份文档进了索引），后者是「拿这份索引去查」。
 * 混在一个页面里，查东西的时候会被一堆索引状态干扰。
 */
export default function SearchPage() {
  const [tab, setTab] = useState<"index" | "query">("index");

  return (
    <div className="h-full overflow-auto">
      <PageHeader
        title="搜索"
        desc="本地文档的语义检索：Markdown 结构化切片后建立向量与关键词索引，检索时两路召回再融合排序。索引与模型都在本机，文件内容不外传。"
        actions={
          <SegmentedControl<"index" | "query">
            value={tab}
            onChange={(v) => setTab(v)}
            options={[
              { label: "索引管理", value: "index" },
              { label: "搜索", value: "query" },
            ]}
          />
        }
      />
      {tab === "index" ? <IndexTab /> : <QueryTab />}
    </div>
  );
}

/** 索引管理：模型状态 + 索引维护 + 已索引文档。 */
function IndexTab() {
  const qc = useQueryClient();
  // 索引在后台跑，跑的时候要能看出「还在动」——所以轮询。
  const statusQ = useQuery({
    queryKey: ["search", "status"],
    queryFn: apiSearchStatus,
    refetchInterval: (q) => (q.state.data?.index.running ? 2000 : false),
  });
  const docsQ = useQuery({
    queryKey: ["search", "documents"],
    queryFn: () => apiSearchDocuments(200),
  });

  const status = statusQ.data;
  const model = status?.model;
  const stats = status?.stats;
  const idx = status?.index;
  const sync = status?.sync;
  /** 向量索引现状（有没有向量、缺多少、要不要补建） */
  const vecState = status?.vector;

  // 「索引配置」区块：读当前生效值 → 本地草稿 → 保存
  const cfgQ = useQuery({
    queryKey: ["search", "config"],
    queryFn: apiSearchConfig,
  });
  const [cfgDraft, setCfgDraft] = useState<SearchSyncConfig>({
    sync_enabled: true,
    sync_interval: 30,
    sync_full_interval: 1800,
  });
  const [cfgLoaded, setCfgLoaded] = useState(false);
  useEffect(() => {
    if (cfgQ.data && !cfgLoaded) {
      setCfgDraft(cfgQ.data);
      setCfgLoaded(true);
    }
  }, [cfgQ.data, cfgLoaded]);

  const cfgM = useMutation({
    mutationFn: (body: SearchSyncConfig) => apiSearchSaveConfig(body),
    onSuccess: (saved) => {
      setCfgDraft(saved);
      toast.success("索引配置已保存并立即生效");
      void qc.invalidateQueries({ queryKey: ["search"] });
    },
    onError: (e: Error) => toast.error("保存失败：" + e.message),
  });

  const cfgDirty =
    !!cfgQ.data &&
    (cfgDraft.sync_enabled !== cfgQ.data.sync_enabled ||
      cfgDraft.sync_interval !== cfgQ.data.sync_interval ||
      cfgDraft.sync_full_interval !== cfgQ.data.sync_full_interval);

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["search"] });
  };

  const downloadM = useMutation({
    mutationFn: () => apiSearchModelDownload("auto"),
    onSuccess: () => {
      toast.success("已开始下载模型（几百 MB，后台进行）");
      invalidate();
    },
    onError: (e: Error) => toast.error("下载失败：" + e.message),
  });

  const indexM = useMutation({
    mutationFn: (v: { force?: boolean; kinds?: "auto" | "keyword" }) =>
      apiSearchIndex({ force: v.force, index_kinds: v.kinds ?? "auto" }),
    onSuccess: (res) => {
      // 自动同步正在跑时内核会回 already_running —— 别谎报"已开始"
      if (res?.status === "already_running") {
        toast.info("已有索引任务在跑，本次跳过（自动同步会补上）");
      } else {
        toast.success("已开始建立索引（后台进行）");
      }
      invalidate();
    },
    onError: (e: Error) => toast.error("建索引失败：" + e.message),
  });

  /**
   * 补建向量索引：只补**缺向量**的文档。
   *
   * 典型场景：用户先建了关键词索引（那时还没下载向量模型），后来下载了模型——
   * 索引里的向量还是空的，而文档没变，普通增量扫描会跳过它们。点这个按钮
   * 把语义检索补回来。
   */
  const backfillM = useMutation({
    mutationFn: () => apiSearchVectorBackfill(),
    onSuccess: (res) => {
      if (res?.status === "already_running") {
        toast.info("已有索引任务在跑，本次跳过");
      } else {
        toast.success("已开始补建向量索引（后台进行）");
      }
      invalidate();
    },
    onError: (e: Error) => toast.error("补建向量索引失败：" + e.message),
  });

  const clearM = useMutation({
    mutationFn: (dropDocuments: boolean) => apiSearchClearIndex(dropDocuments),
    onSuccess: (_d, dropDocuments) => {
      toast.success(dropDocuments ? "索引已清空" : "片段已清空（保留文件清单）");
      invalidate();
    },
    onError: (e: Error) => toast.error("清索引失败：" + e.message),
  });

  const delDocM = useMutation({
    mutationFn: (id: number) => apiSearchDeleteDocument(id),
    onSuccess: () => {
      toast.success("已从索引中删除（磁盘文件未动）");
      invalidate();
    },
    onError: (e: Error) => toast.error("删除失败：" + e.message),
  });

  const delChunksM = useMutation({
    mutationFn: (id: number) => apiSearchDeleteDocumentChunks(id),
    onSuccess: () => {
      toast.success("已删除该文档的片段（保留文件记录）");
      invalidate();
    },
    onError: (e: Error) => toast.error("删除失败：" + e.message),
  });

  const modelReady = model?.ready ?? false;
  const modelUsable = model?.available ?? false;
  /** 「索引库」详情弹窗 */
  const [dbInfoOpen, setDbInfoOpen] = useState(false);

  /** 复制索引库路径 */
  const copyDbPath = async () => {
    const p = stats?.db_path ?? "";
    if (!p) return;
    try {
      await navigator.clipboard.writeText(p);
      toast.success("索引库路径已复制");
    } catch {
      toast.error("复制失败，请手动选中路径复制");
    }
  };

  return (
    <Stack>
      {!!statusQ.error && (
        <Hint tone="danger" title="读不到索引状态">
          {String((statusQ.error as Error)?.message ?? statusQ.error)}
        </Hint>
      )}

      {/* ---------------- 模型 ---------------- */}
      <Section
        title="Embedding 模型"
        extra={
          <button
            type="button"
            className={BTN}
            disabled={downloadM.isPending || model?.status === "downloading"}
            onClick={() => downloadM.mutate()}
          >
            {downloadM.isPending || model?.status === "downloading" ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Download className="h-3.5 w-3.5" />
            )}
            {modelReady ? "重新下载" : "下载模型"}
          </button>
        }
      >
        {/* 这个模型是干什么的 —— 不说清楚，用户只会以为"下完就能搜得更好"，
            不知道关键词检索本来就不需要它、也不知道改了模型要重建索引。 */}
        <p className="mb-3 text-[12px] leading-5 text-ink-muted">
          用于把文档切片转成向量，支持
          <span className="font-medium text-ink-strong">语义检索</span>
          ——能搜到「意思相近、用词不同」的内容。关键词检索不需要它。
          <span className="text-ink-faint">
            下载或更新模型后需要重建索引才会生效。
          </span>
        </p>

        <div className="grid gap-3 sm:grid-cols-3">
          <Stat
            label="模型状态"
            value={
              modelReady ? "已就绪" : model?.status === "downloading" ? "下载中" : "未就绪"
            }
            tone={modelReady ? "success" : model?.status === "downloading" ? "warn" : undefined}
            sub={model?.message || "本地推理，不外传文件内容"}
          />
          <Stat label="模型目录" value={<span className="text-[13px]">{model?.model_file ?? "—"}</span>} sub={model?.model_dir ?? ""} />
          <Stat
            label="模型预热"
            value={
              model?.warmed ? "已预热" : modelReady ? "待预热" : "—"
            }
            sub={
              model?.warmed
                ? "启动时已后台加载，首次查询无感"
                : "首次查询会等模型加载"
            }
          />
          <Stat
            label="推理后端"
            value={
              model?.backend_active
                ? model.backend_active === "gpu"
                  ? "GPU 加速"
                  : "CPU"
                : model?.backend_config === "auto"
                  ? "自动（待加载）"
                  : (model?.backend ?? "—")
            }
            sub={
              model?.backend_note
                || model?.gpu_probe?.reason
                || model?.model_repo
                || ""
            }
          />
        </div>

        {/* GPU 不可用而回退到 CPU 时要说清楚——不然用户只会觉得「怎么变慢了」 */}
        {!!model?.backend_note && (
          <div className="mt-3">
            <Hint tone="warn" title="已回退到 CPU 推理">
              {model.backend_note}
            </Hint>
          </div>
        )}

        {!modelUsable && (
          <div className="mt-3">
            <Hint tone="warn" title="向量通道不可用">
              {model?.unavailable_reason || "向量模型未就绪"} —— 只能做
              <span className="font-medium">纯关键词检索</span>（mode=keyword）。
              {model?.unavailable_reason?.includes("litert-lm") ? (
                <>
                  想要语义召回，先装它：
                  <code className="ml-1 font-mono">pip install litert-lm==0.18.0</code>
                </>
              ) : null}
            </Hint>
          </div>
        )}

        {/* 模型在、但向量索引还没建全 —— 语义检索实际上是关着的，
            却容易被"模型已就绪"掩盖过去。这里明确说出来，并给一键补建。 */}
        {modelReady && vecState?.needs_backfill && (
          <div className="mt-3">
            <Hint tone="warn" title="向量索引还没建全">
              模型已就绪，但还有 {vecState.missing} 个片段没有向量，语义检索暂时用不上。
              补建只处理缺向量的片段，不重扫全库。
              <button
                type="button"
                className="ml-2 inline-flex items-center gap-1 rounded-md border border-line-soft bg-panel px-2 py-0.5 text-[12px] text-ink-muted transition-colors hover:border-brand/40 hover:text-brand disabled:opacity-50"
                disabled={backfillM.isPending || idx?.running}
                onClick={() => backfillM.mutate()}
              >
                {backfillM.isPending ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <RefreshCw className="h-3 w-3" />
                )}
                补建向量索引
              </button>
            </Hint>
          </div>
        )}
      </Section>

      {/* ---------------- 索引维护 ---------------- */}
      <Section
        title="索引维护"
        extra={
          <div className="flex items-center gap-2">
            <button
              type="button"
              className={BTN}
              disabled={indexM.isPending || idx?.running}
              onClick={() => indexM.mutate({ force: false })}
              title="有向量模型就建关键词+向量索引，没有就只建关键词索引"
            >
              <RefreshCw className={idx?.running ? "h-3.5 w-3.5 animate-spin" : "h-3.5 w-3.5"} />
              建立索引
            </button>
            <button
              type="button"
              className={BTN}
              disabled={indexM.isPending || idx?.running}
              onClick={() => indexM.mutate({ force: false, kinds: "keyword" })}
              title="只建关键词索引，不加载向量模型（快，无需下载模型）"
            >
              仅关键词
            </button>
            <button
              type="button"
              className={BTN}
              disabled={indexM.isPending || idx?.running}
              onClick={() => indexM.mutate({ force: true })}
              title="忽略内容哈希，强制重新嵌入（慢）"
            >
              强制重建
            </button>
          </div>
        }
      >
        <div className="grid gap-3 sm:grid-cols-5">
          <Stat label="已索引文档" value={stats?.documents ?? 0} />
          <Stat label="片段总数" value={stats?.chunks ?? 0} sub={`叶子 ${stats?.leaves ?? 0} · 父块 ${stats?.parents ?? 0}`} />
          <Stat label="关键词条目" value={stats?.keywords ?? 0} />
          <Stat
            label="向量索引"
            value={
              vecState?.state === "full"
                ? "已建全"
                : vecState?.state === "partial"
                  ? "不完整"
                  : "未建立"
            }
            tone={
              vecState?.state === "full"
                ? "success"
                : vecState?.state === "partial"
                  ? "warn"
                  : undefined
            }
            sub={
              vecState
                ? `有向量 ${vecState.with_vector} / 共 ${vecState.leaves}${
                    vecState.missing ? ` · 缺 ${vecState.missing}` : ""
                  }`
                : ""
            }
          />
          {/* 索引库路径很长（~/.keeper/search/search.db），直接铺在卡片里会
              撑破卡片、把整行挤歪。改成「查看」：点开看完整路径 + 说明。 */}
          <Stat
            label="索引库"
            value={<span className="text-[13px]">SQLite</span>}
            sub={
              <button
                type="button"
                className="inline-flex items-center gap-1 rounded text-[11px] text-brand transition-colors hover:underline"
                onClick={() => setDbInfoOpen(true)}
              >
                <Eye className="h-3 w-3" />
                查看
              </button>
            }
          />
        </div>

        <div className="mt-3 text-[12px] text-ink-muted">
          索引目录：{status?.config.roots?.length ? status.config.roots.join("、") : "（未配置「我的空间」）"}
        </div>

        {idx?.running && (
          <div className="mt-3">
            <Hint tone="info" title="正在建立索引">
              <div className="space-y-2">
                <div className="flex items-center justify-between text-[12.5px]">
                  <span>
                    {idx.stage === "scanning"
                      ? "扫描文件（识别移动 / 算内容指纹）"
                      : idx.stage === "indexing"
                        ? "切片并嵌入（建索引）"
                        : "准备中"}
                    {idx.total > 0 && (
                      <>：第 {idx.current} / 共 {idx.total} 个文件</>
                    )}
                  </span>
                  {idx.total > 0 && (
                    <span className="tabular-nums text-ink-faint">
                      {idx.pct.toFixed(0)}%
                    </span>
                  )}
                </div>
                {idx.total > 0 && (
                  <div className="h-1.5 w-full overflow-hidden rounded-full bg-line-soft">
                    <div
                      className="h-full rounded-full bg-brand transition-[width] duration-200"
                      style={{ width: `${Math.min(100, Math.max(0, idx.pct))}%` }}
                    />
                  </div>
                )}
                {idx.current_name && (
                  <div className="truncate font-mono text-[11.5px] text-ink-faint">
                    {idx.current_name}
                  </div>
                )}
              </div>
            </Hint>
          </div>
        )}
        {!!idx?.error && (
          <div className="mt-3">
            <Hint tone="danger" title="上次索引失败">{idx.error}</Hint>
          </div>
        )}
        {!!idx?.stats && !idx.running && (
          <div className="mt-3">
            <Hint tone="info">
              上次结果：新增 {idx.stats.indexed} · 跳过 {idx.stats.skipped} · 删除 {idx.stats.deleted} · 移动 {idx.stats.moved}
            </Hint>
          </div>
        )}

        {/* 自动同步：索引会自己跟着文件更新，不必手动点 */}
        {!!sync && (
          <div className="mt-3">
            {sync.enabled ? (
              <Hint tone="info" title="索引会自动跟着文件更新">
                每 {Math.round(sync.interval)} 秒扫描一次「我的空间」——只重切重嵌变化了的文件，
                没动过的文件连打开都不打开。
                {sync.last_scan_at
                  ? `上次同步 ${new Date(sync.last_scan_at * 1000).toLocaleTimeString()}`
                  : "尚未同步"}
                {sync.last_stats
                  ? `（新增 ${sync.last_stats.indexed ?? 0} · 跳过 ${sync.last_stats.skipped ?? 0} · 删除 ${sync.last_stats.deleted ?? 0}）`
                  : ""}
                {sync.watched_paths > 0 ? ` · 文件监听已通知 ${sync.watched_paths} 次` : ""}
              </Hint>
            ) : (
              <Hint tone="info" title="索引自动同步已关闭">
                改完文件后要手动点「建立索引」才会更新（可用 KEEPER_SEARCH_SYNC_ENABLED=1 打开）。
              </Hint>
            )}
          </div>
        )}
        {!!sync?.last_error && (
          <div className="mt-3">
            <Hint tone="danger" title="上次自动同步失败">{sync.last_error}</Hint>
          </div>
        )}

        <div className="mt-4 flex items-center gap-2 border-t border-line-soft pt-4">
          <span className="text-[12px] text-ink-muted">清空索引：</span>
          <ConfirmButton
            onConfirm={() => clearM.mutate(false)}
            confirmText="只清片段"
            className={PLAIN_BTN}
            resetKey={stats?.chunks}
          >
            只清片段（保留文件清单）
          </ConfirmButton>
          <ConfirmButton
            onConfirm={() => clearM.mutate(true)}
            confirmText="全部清空"
            className={PLAIN_BTN}
            resetKey={stats?.chunks}
          >
            全部清空
          </ConfirmButton>
          <span className="text-[11.5px] text-ink-faint">
            只删索引，不动磁盘上的文件
          </span>
        </div>
      </Section>

      {/* ---------------- 索引配置 ---------------- */}
      <Section
        title="索引配置"
        extra={
          <button
            type="button"
            className={BTN}
            disabled={cfgM.isPending || !cfgDirty}
            onClick={() => cfgM.mutate(cfgDraft)}
          >
            {cfgM.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Check className="h-3.5 w-3.5" />
            )}
            保存
          </button>
        }
      >
        <div className="grid gap-4 sm:grid-cols-3">
          <Field
            label="自动同步"
            extra="关掉后索引只在你手动点「建立索引」时更新"
          >
            <label className="flex items-center gap-2 text-[12.5px] text-ink-muted">
              <input
                type="checkbox"
                checked={cfgDraft.sync_enabled}
                onChange={(e) =>
                  setCfgDraft((d) => ({ ...d, sync_enabled: e.target.checked }))
                }
                className="h-3.5 w-3.5 accent-[var(--brand)]"
              />
              {cfgDraft.sync_enabled ? "已开启" : "已关闭"}
            </label>
          </Field>

          <Field
            label="增量扫描间隔（秒）"
            extra="每隔多久扫一次「我的空间」。扫描只看文件有没有变，不读内容"
          >
            <NumberInput
              value={cfgDraft.sync_interval}
              onChange={(v) =>
                setCfgDraft((d) => ({ ...d, sync_interval: v }))
              }
              min={5}
              max={3600}
              step={5}
              disabled={!cfgDraft.sync_enabled}
            />
          </Field>

          <Field
            label="全量校准间隔（分钟）"
            extra="每隔多久把所有文件重读一遍，确认内容真的没变（防止 mtime 撒谎）"
          >
            <NumberInput
              value={Math.round(cfgDraft.sync_full_interval / 60)}
              onChange={(v) =>
                setCfgDraft((d) => ({ ...d, sync_full_interval: v * 60 }))
              }
              min={1}
              max={1440}
              step={1}
              disabled={!cfgDraft.sync_enabled}
            />
          </Field>
        </div>
        <div className="mt-3 text-[12px] text-ink-muted">
          扫描间隔调小 = 索引更新更快、后台更忙；语料大（几万文件）时建议 30 秒以上。
        </div>
      </Section>

      {/* ---------------- 已索引文档 ---------------- */}
      <Section title="已索引文档" extra={<span className="text-[12px] text-ink-faint">共 {docsQ.data?.total ?? 0} 份</span>}>
        <Table
          rows={docsQ.data?.documents ?? []}
          loading={docsQ.isLoading}
          minWidth={720}
          rowKey={(r) => String(r.id)}
          empty={<span className="text-[12.5px] text-ink-faint">还没有索引任何文档——点上面的「建立索引」</span>}
          columns={docColumns({
            onDelete: (id) => delDocM.mutate(id),
            onDeleteChunks: (id) => delChunksM.mutate(id),
          })}
        />
      </Section>

      {/* 索引库详情：路径长，放弹窗里看（还能一键复制） */}
      <Modal
        open={dbInfoOpen}
        onClose={() => setDbInfoOpen(false)}
        title="索引库"
        width={520}
        footer={
          <>
            <button type="button" className={BTN} onClick={copyDbPath}>
              <Copy className="h-3.5 w-3.5" />
              复制路径
            </button>
            <button
              type="button"
              className={BTN_PRIMARY}
              onClick={() => setDbInfoOpen(false)}
            >
              知道了
            </button>
          </>
        }
      >
        <div className="space-y-3">
          <div>
            <div className="mb-1 text-[11.5px] text-ink-faint">文件位置</div>
            <div className="select-all break-all rounded-lg border border-line-soft bg-surface px-3 py-2 font-mono text-[12px] leading-5 text-ink-strong">
              {stats?.db_path ?? "—"}
            </div>
          </div>
          <p className="text-[12px] leading-5 text-ink-muted">
            索引库是一个 SQLite 文件：里面是 <code>documents</code> /{" "}
            <code>chunks</code> 两张业务表，加上 <code>chunks_fts</code>
            （FTS5 关键词索引）与向量列。备份这一个文件就等于备份整个索引；
            删掉它索引会重新建（要重新切片与嵌入，慢）。
          </p>
        </div>
      </Modal>
    </Stack>
  );
}

/** 已索引文档的表格列。 */
function docColumns(handlers: {
  onDelete: (id: number) => void;
  onDeleteChunks: (id: number) => void;
}): Column<SearchDocument>[] {
  return [
    {
      key: "name",
      title: "文档",
      render: (r) => (
        <div className="min-w-0">
          <div className="truncate text-[13px] text-ink-strong">{r.name}</div>
          <div className="mt-0.5 truncate font-mono text-[11px] text-ink-faint">{r.dir}</div>
        </div>
      ),
    },
    {
      key: "status",
      title: "状态",
      width: 96,
      render: (r) => (
        <Pill tone={r.index_status === "done" ? "green" : "orange"}>
          {r.index_status === "done" ? "已索引" : "待重建"}
        </Pill>
      ),
    },
    {
      key: "mtime",
      title: "修改时间",
      width: 150,
      render: (r) => (
        <span className="text-[12px] text-ink-muted">
          {r.mtime ? new Date(r.mtime * 1000).toLocaleString("zh-CN") : "—"}
        </span>
      ),
    },
    {
      key: "ops",
      title: "操作",
      width: 210,
      render: (r) => (
        <div className="flex items-center gap-1.5">
          <ConfirmButton
            onConfirm={() => handlers.onDeleteChunks(r.id)}
            confirmText="确认"
            className={PLAIN_BTN}
            resetKey={r.id}
          >
            只删片段
          </ConfirmButton>
          <ConfirmButton
            onConfirm={() => handlers.onDelete(r.id)}
            confirmText="确认删除"
            className={PLAIN_BTN}
            resetKey={r.id}
          >
            <Trash2 className="h-3 w-3" />
            移除
          </ConfirmButton>
        </div>
      ),
    },
  ];
}

/** 搜索 tab：输入查询 → 拿命中列表。 */
function QueryTab() {
  const [q, setQ] = useState("");
  const [mode, setMode] = useState<"hybrid" | "vector" | "keyword">("hybrid");
  const [topK, setTopK] = useState(10);
  const [neighborSpan, setNeighborSpan] = useState(0);
  const [hits, setHits] = useState<SearchHit[] | null>(null);

  const searchM = useMutation({
    mutationFn: () =>
      apiSearchQuery({
        query: q,
        top_k: topK,
        mode,
        neighbor_span: neighborSpan,
      }),
    onSuccess: (d) => setHits(d.results),
    onError: (e: Error) => toast.error("检索失败：" + e.message),
  });

  const canSearch = q.trim().length > 0 && !searchM.isPending;

  return (
    <Stack>
      <Section title="检索">
        <div className="flex flex-col gap-3">
          <div className="flex gap-2">
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && canSearch) searchM.mutate();
              }}
              placeholder="用一句话描述你要找的内容，例如「讲缓存穿透的那篇」"
              className="h-9 min-w-0 flex-1 rounded-lg border border-line-soft bg-panel px-3 text-[13px] text-ink shadow-card outline-none placeholder:text-ink-faint focus:border-brand/50"
            />
            <button
              type="button"
              className={BTN_PRIMARY}
              disabled={!canSearch}
              onClick={() => searchM.mutate()}
            >
              {searchM.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <SearchIcon className="h-4 w-4" />
              )}
              搜索
            </button>
          </div>

          <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-[12px] text-ink-muted">
            <label className="flex items-center gap-1.5">
              检索通道
              <SegmentedControl<"hybrid" | "vector" | "keyword">
                value={mode}
                onChange={(v) => setMode(v)}
                options={MODES.map((m) => ({ label: m.label, value: m.value }))}
              />
            </label>
            <label className="flex items-center gap-1.5">
              返回条数
              <input
                type="number"
                min={1}
                max={100}
                value={topK}
                onChange={(e) =>
                  setTopK(Math.max(1, Math.min(100, Number(e.target.value) || 1)))
                }
                className="h-6 w-16 rounded-md border border-line-soft bg-panel px-2 text-[12px] tabular-nums text-ink outline-none focus:border-brand/50"
              />
            </label>
            <label className="flex items-center gap-1.5">
              附带相邻片段
              <input
                type="number"
                min={0}
                max={5}
                value={neighborSpan}
                onChange={(e) =>
                  setNeighborSpan(Math.max(0, Math.min(5, Number(e.target.value) || 0)))
                }
                className="h-6 w-14 rounded-md border border-line-soft bg-panel px-2 text-[12px] tabular-nums text-ink outline-none focus:border-brand/50"
              />
            </label>
          </div>

          {mode === "keyword" && (
            <Hint tone="info">
              纯关键词模式只匹配字面词（BM25），不加载模型——查不到「说法不同但意思相近」的内容。
            </Hint>
          )}
        </div>
      </Section>

      {hits && (
        <Section title={`命中 ${hits.length} 条`}>
          {hits.length === 0 ? (
            <div className="py-6 text-center text-[12.5px] text-ink-faint">
              没有命中。换个说法，或切到「混合」通道再试。
            </div>
          ) : (
            <div className="space-y-2">
              {hits.map((h) => (
                <HitItem key={h.chunk_id} hit={h} />
              ))}
            </div>
          )}
        </Section>
      )}
    </Stack>
  );
}

/** 一条命中：标题路径 + 文件位置 + 分数 + 正文；可展开看父块与相邻片段。 */
function HitItem({ hit }: { hit: SearchHit }) {
  const [open, setOpen] = useState(false);
  const crumbs = useMemo(
    () => (hit.title_path?.length ? hit.title_path : [hit.title || "未命名"]),
    [hit.title_path, hit.title]
  );

  return (
    <div className="rounded-card border border-line-soft bg-panel">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-start gap-2 px-3 py-2.5 text-left"
      >
        {open ? (
          <ChevronDown className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-faint" />
        ) : (
          <ChevronRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-faint" />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="truncate text-[12.5px] font-medium text-ink-strong">
              {crumbs.join(" / ")}
            </span>
            <span className="font-mono text-[11px] text-ink-faint">
              {hit.path.split("/").pop()}
              {/* PDF 显示页码（「第 12 页」），Markdown 显示行号 */}
              {hit.locator ? ` · ${hit.locator}` : `:${hit.start_line}-${hit.end_line}`}
            </span>
            <span className="ml-auto flex shrink-0 items-center gap-1.5">
              {hit.vec_score != null && (
                <Pill tone="blue">向量 {hit.vec_score.toFixed(3)}</Pill>
              )}
              {hit.kw_score != null && (
                <Pill tone="amber">关键词 {hit.kw_score.toFixed(3)}</Pill>
              )}
              <Pill tone="green">总分 {hit.score.toFixed(3)}</Pill>
            </span>
          </div>
          <div className="mt-1 line-clamp-2 whitespace-pre-wrap text-[12px] leading-5 text-ink-muted">
            {hit.text}
          </div>
        </div>
      </button>

      {open && (
        <div className="space-y-3 border-t border-line-soft px-3 py-2.5 text-[12px]">
          {hit.parent && (
            <div>
              <div className="mb-1 text-[11.5px] font-medium text-ink-strong">
                所属章节（父块）
              </div>
              <div className="rounded-md bg-surface px-2.5 py-2 font-mono text-[11.5px] leading-5 text-ink-muted">
                {hit.parent.titlepath}
              </div>
              <div className="mt-1 line-clamp-4 whitespace-pre-wrap leading-5 text-ink-muted">
                {hit.parent.text}
              </div>
            </div>
          )}
          {!!hit.siblings?.length && (
            <div>
              <div className="mb-1 text-[11.5px] font-medium text-ink-strong">相邻片段</div>
              <div className="space-y-1.5">
                {hit.siblings.map((s) => (
                  <div key={s.chunk_id} className="rounded-md bg-surface px-2.5 py-1.5">
                    <div className="font-mono text-[11px] text-ink-faint">
                      {s.titlepath} · {s.start_line}-{s.end_line}
                    </div>
                    <div className="mt-0.5 line-clamp-2 whitespace-pre-wrap leading-5 text-ink-muted">
                      {s.text}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
          <div className="font-mono text-[11px] text-ink-faint">{hit.path}</div>
        </div>
      )}
    </div>
  );
}
