/**
 * 核心 agent 编辑器。
 *
 * **没有"选哪个 agent 当主脑"这一步**：核心 agent 从本地目录构建（见
 * ``agent/core.py``），它是那个目录里唯一的一个角色，不存在"选谁"的问题。
 * 给一个下拉会让人以为核心 agent 可以在数据库里的 agent 之间挑——而那其实是
 * 普通 agent 的编排问题，不是核心角色的属性。
 *
 * 只有两件东西可以改，各自对应一个文件：
 *   - **画像** → ``agent.yaml``（名字 + 一句话说明）
 *   - **灵魂** → ``soul.md``（人格全文，每次对话都带上）
 *
 * 刻意没有"画像文件"这个第三入口：人格只能有一个来源，否则用户得猜"我在设置里
 * 写的这段到底生效了没有"——而这类"改了却不生效"的问题最难排查。
 *
 * 两段各自独立保存，因为它们是两份文件，没有共同的操作上下文。
 */
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { Field, TextInput } from "./ui/Form";
import { Section, Stack, Hint } from "./ui/Page";
import { toast } from "./ui/Toast";
import {
  apiGetCoreAgent,
  apiUpdateCoreAgent,
  apiUpdateCoreAgentSoul,
} from "../api/client";
import type { CoreAgentResp } from "../api/client";

export function CoreAgentEditor() {
  const qc = useQueryClient();
  const coreQ = useQuery({ queryKey: ["core-agent"], queryFn: apiGetCoreAgent });

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [soul, setSoul] = useState("");

  useEffect(() => {
    if (coreQ.data) {
      setName(coreQ.data.name);
      setDescription(coreQ.data.description);
      setSoul(coreQ.data.soul);
    }
  }, [coreQ.data]);

  const afterSaved = () => {
    qc.invalidateQueries({ queryKey: ["core-agent"] });
    // 核心 agent 已装载的实例要重建才生效，所以顺带刷新 agent 清单
    qc.invalidateQueries({ queryKey: ["agents"] });
  };

  /**
   * 保存后的反馈。后端保存时会**自动重新装配**，所以这里要区分三种结果：
   * 1. 装配成功 → "已保存并生效"；
   * 2. 文件存了但装配失败 → 明说"下次启动生效"+ 原因。这是最容易让人困惑的
   *    一种：界面说保存成功，对话里却还是旧的，不说清楚就会变成"改了没用"；
   * 3. 写文件就失败 → 直接报错。
   */
  const onSaved = (file: string, data: CoreAgentResp) => {
    if (data.reload && !data.reload.ok) {
      toast.error(
        `已保存到 ${file}，但重新装配失败（下次启动生效）：${
          data.reload.reason ?? "未知原因"
        }`
      );
    } else {
      toast.success(`已保存到 ${file}，已生效`);
    }
    afterSaved();
  };

  const defM = useMutation({
    mutationFn: () => apiUpdateCoreAgent({ name, description }),
    onSuccess: (d: CoreAgentResp) => onSaved("agent.yaml", d),
    onError: (e: Error) => toast.error("保存失败：" + e.message),
  });

  const soulM = useMutation({
    mutationFn: () => apiUpdateCoreAgentSoul({ soul }),
    onSuccess: (d: CoreAgentResp) => onSaved("soul.md", d),
    onError: (e: Error) => toast.error("保存失败：" + e.message),
  });

  if (coreQ.isLoading) {
    return (
      <div className="flex items-center justify-center py-16 text-ink-muted">
        <Loader2 className="h-5 w-5 animate-spin" />
      </div>
    );
  }

  if (coreQ.isError) {
    return (
      <Hint tone="danger" title="读取失败">
        {(coreQ.error as Error)?.message}
      </Hint>
    );
  }

  const resolved = coreQ.data?.resolved;
  // 名字是必填：空名字会让消息头、列表、日志全都显示空白
  const nameEmpty = !name.trim();

  return (
    <Stack>
      <p className="text-[12px] leading-6 text-ink-muted">
        核心 agent 由本地目录构建，不在数据库里——所以它不能被卸载、也不参与平台同步。
        它就是那个目录里唯一的一个角色。
      </p>

      <Section
        title="画像"
        extra={
          <button
            type="button"
            disabled={defM.isPending || nameEmpty}
            onClick={() => defM.mutate()}
            className="flex items-center gap-1.5 rounded-lg bg-brand-gradient px-3 py-[5px] text-[12.5px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
          >
            {defM.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            保存
          </button>
        }
      >
        <Stack>
          <Field
            label="名字"
            extra="对话里怎么称呼这个角色"
            error={nameEmpty ? "不能为空" : undefined}
          >
            <TextInput
              value={name}
              onChange={setName}
              placeholder="例如：Keeper"
            />
          </Field>
          <Field label="说明" extra="一句话说明它是谁">
            <TextInput
              value={description}
              onChange={setDescription}
              placeholder="例如：常驻的主脑，陪你把事情做完"
            />
          </Field>
          <p className="text-[11.5px] text-ink-muted">
            运行中：{resolved ? resolved.name : "—"}
            {resolved && !resolved.model ? " · 未配置模型（纯索引模式）" : ""}
          </p>
        </Stack>
      </Section>

      <Section
        title="灵魂"
        extra={
          <button
            type="button"
            disabled={soulM.isPending}
            onClick={() => soulM.mutate()}
            className="flex items-center gap-1.5 rounded-lg bg-brand-gradient px-3 py-[5px] text-[12.5px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
          >
            {soulM.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            保存
          </button>
        }
      >
        <Stack>
          <textarea
            rows={12}
            value={soul}
            onChange={(e) => setSoul(e.target.value)}
            placeholder={"# 灵魂\n\n你是谁、怎么说话、在意什么、不做什么…"}
            className="w-full resize-y rounded-[7px] border border-line bg-panel px-2.5 py-2 text-[12.5px] leading-6 text-ink shadow-[0_1px_1px_rgba(0,0,0,0.03)] outline-none transition-all duration-150 placeholder:text-ink-faint focus:border-brand focus:shadow-[0_0_0_3px_rgba(66,133,244,0.48)]"
          />
          <p className="text-[11.5px] leading-5 text-ink-muted">
            每次对话都会带上它。这是这个角色人格的**唯一来源**——没有第二处可以写。
          </p>
        </Stack>
      </Section>

      <p className="font-mono text-[11px] text-ink-faint">{coreQ.data?.dir}</p>
    </Stack>
  );
}
