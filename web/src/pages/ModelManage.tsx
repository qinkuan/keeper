import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import {
  apiDeleteModel,
  apiListModels,
  apiTestModel,
} from "../api/client";
import ModelPriceModal from "../components/ModelPriceModal";
import ModelCreateWizard from "../components/ModelCreateWizard";
import { ModelEditModal } from "../components/ModelEditModal";
import { PageHeader, Section } from "../components/ui/Page";
import { Table } from "../components/ui/Table";
import type { Column } from "../components/ui/Table";
import { toast } from "../components/ui/Toast";
import { cn } from "../lib/utils";
import type { ModelProfile } from "../api/types";

/** 操作按钮：删除用红色文字，与其它动作区分开 */
function ActBtn({
  onClick,
  disabled,
  tone,
  title,
  children,
}: {
  onClick: () => void;
  disabled?: boolean;
  tone?: "danger";
  /** 悬停提示（原生 title） */
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={cn(
        "inline-flex h-6 items-center gap-1 rounded-md border px-2 text-[12px] transition-colors disabled:opacity-50",
        tone === "danger"
          ? "border-red-500/30 text-red-600 hover:border-red-500 hover:bg-red-500/10 dark:text-red-300"
          : "border-line text-ink-muted hover:bg-surface hover:text-ink-strong"
      )}
    >
      {children}
    </button>
  );
}

/**
 * 模型管理：本地维护一组可复用的模型预设。
 *
 * 模型归 keeper 本机维护，纯本地 CRUD，不连任何外部服务。
 * 配好后到「智能体」页的 Agent 配置里绑定即可。
 *
 * 新增 / 编辑的表单在 ``ModelEditModal`` 里（对话页的「新建模型」也用它，
 * 两处长得一样）。
 */
export default function ModelManage() {
  const qc = useQueryClient();
  const modelsQ = useQuery({ queryKey: ["models"], queryFn: apiListModels });

  /** 新增 / 编辑弹窗 */
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<ModelProfile | null>(null);
  /** 「价格」弹窗：一个模型可配多段价（错峰），独立管理 */
  const [priceModel, setPriceModel] = useState<ModelProfile | null>(null);
  /** 「新建模型」向导：选服务商 → 填 Key → 勾模型 → 批量创建 */
  const [wizardOpen, setWizardOpen] = useState(false);
  const [confirmDel, setConfirmDel] = useState<string | null>(null);

  const delM = useMutation({
    mutationFn: (id: string) => apiDeleteModel(id),
    onSuccess: (d) => {
      // 删模型会连带解绑 agent，如实说清楚有几个被解绑
      const n = d?.unbound_agents ?? 0;
      toast.success(n ? `已删除，并解绑了 ${n} 个 agent` : "已删除");
      setConfirmDel(null);
      qc.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (e: Error) => toast.error("删除失败：" + e.message),
  });

  const testM = useMutation({
    mutationFn: (id: string) => apiTestModel(id),
    onSuccess: (d) => toast.success("连通正常：" + (d.reply ?? "")),
    onError: (e: Error) => toast.error("连通失败：" + e.message),
  });

  const openCreate = () => {
    setEditing(null);
    setOpen(true);
  };
  const openEdit = (m: ModelProfile) => {
    setEditing(m);
    setOpen(true);
  };

  const columns: Column<ModelProfile>[] = [
    { key: "name", title: "名称", render: (r) => r.name },
    {
      key: "provider",
      title: "模型服务商",
      width: 110,
      render: (r) => (
        <span className="rounded-md bg-black/[0.06] px-1.5 py-0.5 font-mono text-[11px] leading-4 text-ink-muted dark:bg-white/[0.08]">
          {r.provider}
        </span>
      ),
    },
    {
      key: "model_name",
      title: "模型",
      width: 200,
      render: (r) => <span className="font-mono text-[12px]">{r.model_name}</span>,
    },
    {
      key: "base_url",
      title: "Base URL",
      render: (r) => (
        <span className="block max-w-[220px] truncate font-mono text-[12px] text-ink-muted">
          {r.base_url || "—"}
        </span>
      ),
    },
    { key: "temperature", title: "温度", width: 70, align: "right", render: (r) => r.temperature },
    {
      key: "action",
      title: "操作",
      width: 250,
      render: (r) => (
        <div className="flex flex-wrap items-center gap-1">
          <ActBtn
            onClick={() => testM.mutate(r.id)}
            disabled={testM.isPending}
          >
            {testM.isPending && <Loader2 className="h-3 w-3 animate-spin" />}
            测试
          </ActBtn>
          <ActBtn onClick={() => setPriceModel(r)}>价格</ActBtn>
          <ActBtn onClick={() => openEdit(r)}>编辑</ActBtn>
          {/* 二次确认放在按钮内：删一个预设后果可控，不值得再弹一层对话框 */}
          {confirmDel === r.id ? (
            <>
              <ActBtn tone="danger" onClick={() => delM.mutate(r.id)}>
                确认删除
              </ActBtn>
              <ActBtn onClick={() => setConfirmDel(null)}>取消</ActBtn>
            </>
          ) : (
            <ActBtn
              tone="danger"
              title="删除会同时解除 agent 与它的绑定"
              onClick={() => setConfirmDel(r.id)}
            >
              删除
            </ActBtn>
          )}
        </div>
      ),
    },
  ];

  return (
    <>
      <PageHeader
        title="模型管理"
        desc="模型归本机维护。配置好后，到「智能体」页的 Agent 配置里绑定，即可让不同 agent 用不同模型。"
        actions={
          <button
            type="button"
            onClick={() => setWizardOpen(true)}
            className="rounded-lg bg-brand-gradient px-4 py-[7px] text-[13px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98]"
          >
            新建模型
          </button>
        }
      />
      <Section bodyClassName="p-0">
        <Table
          rows={modelsQ.data ?? []}
          columns={columns}
          rowKey={(r) => r.id}
          loading={modelsQ.isLoading}
          minWidth={900}
          empty="还没有模型预设，点右上角「新建模型」"
        />
      </Section>

      <ModelEditModal
        open={open}
        editing={editing}
        onClose={() => setOpen(false)}
      />

      <ModelPriceModal
        model={priceModel}
        open={!!priceModel}
        onClose={() => setPriceModel(null)}
      />

      {/* 新建模型向导。系统没有预设的服务商 → 「自定义」走原来那张手填表单
          （同一个 ModelEditModal，与对话页里的入口长得一样）。 */}
      <ModelCreateWizard
        open={wizardOpen}
        onClose={() => setWizardOpen(false)}
        onCustom={() => {
          setWizardOpen(false);
          openCreate();
        }}
      />
    </>
  );
}
