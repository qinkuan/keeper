/**
 * 运行模式选择器（对话输入框下方，思考选择器的左边）。
 *
 * 与工作空间 / 思考选择器同形：图标 + 当前值，点它向上弹出。两个模式：
 *   普通 —— 完整 ReAct + 最近历史 + 长期记忆 + 全量工具（现状）
 *   快捷 —— 一次性任务（翻译 / OCR / 总结），无循环、无记忆、无 planner
 *
 * 「程序化（ptc）」原来是个预留项：后端并没有真的实现这套执行器
 * （agent/keeper.py 的 mode→Process 映射里没有 ptc，落回普通流程），
 * 所以这里不再给用户一个「选了也没区别」的选项。
 *
 * 每次选中即带出 mode：新会话存本地 pending（发第一条消息时落库），
 * 已有会话直接写库（后端每轮都从会话行现读，下一轮起生效）。
 */
import { useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Check, ChevronUp, Workflow, Zap } from "lucide-react";
import { cn } from "../lib/utils";
import type { RunMode } from "../api/types";

const OPTIONS: {
  value: RunMode;
  label: string;
  desc: string;
  icon: typeof Workflow;
}[] = [
  { value: "normal", label: "普通", desc: "完整 ReAct + 记忆 + 工具", icon: Workflow },
  { value: "simple", label: "快捷", desc: "一次性任务，无循环无记忆", icon: Zap },
];

export function ModePicker({
  value,
  onChange,
}: {
  value: RunMode;
  onChange: (value: RunMode) => void;
}) {
  const [open, setOpen] = useState(false);
  const cur = OPTIONS.find((o) => o.value === value) ?? OPTIONS[0];
  const Icon = cur.icon;

  return (
    <DropdownMenu.Root open={open} onOpenChange={setOpen}>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          title={`运行模式：${cur.label}（${cur.desc}）`}
          className={cn(
            "flex h-7 shrink-0 items-center gap-1.5 rounded-[7px] border border-line-soft bg-panel px-2 text-[12px] font-medium shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-colors duration-150",
            cur.value === "normal"
              ? "text-ink-muted hover:text-ink"
              : "text-brand hover:border-brand/40"
          )}
        >
          <Icon className="h-3.5 w-3.5 shrink-0" />
          <span className="hidden lg:inline">模式</span>
          <span className="min-w-0 truncate">{cur.label}</span>
          <ChevronUp className="h-3 w-3 shrink-0 opacity-55" />
        </button>
      </DropdownMenu.Trigger>

      <DropdownMenu.Portal>
        <DropdownMenu.Content
          side="top"
          align="start"
          sideOffset={6}
          collisionPadding={8}
          className="z-[1700] min-w-[200px] rounded-[10px] border border-line-soft bg-panel p-1 shadow-pop"
        >
          {OPTIONS.map((o) => {
            const OIcon = o.icon;
            return (
              <DropdownMenu.Item
                key={o.value}
                onSelect={() => {
                  onChange(o.value);
                  setOpen(false);
                }}
                className="flex cursor-pointer items-center gap-2 rounded-[7px] px-2 py-1.5 outline-none transition-colors duration-100 data-[highlighted]:bg-brand/12"
              >
                <Check
                  className={cn(
                    "h-3 w-3 shrink-0",
                    cur.value === o.value ? "opacity-100" : "opacity-0"
                  )}
                />
                <OIcon className="h-3.5 w-3.5 shrink-0 text-ink-faint" />
                <span className="flex min-w-0 flex-1 flex-col">
                  <span
                    className={cn(
                      "truncate text-[12px]",
                      cur.value === o.value && "font-medium text-ink-strong"
                    )}
                  >
                    {o.label}
                  </span>
                  <span className="truncate text-[11px] text-ink-faint">
                    {o.desc}
                  </span>
                </span>
              </DropdownMenu.Item>
            );
          })}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
