/**
 * 思考模式选择器（对话输入框下方），与工作空间选择器同形：图标 + 当前值，点它向上弹出。
 *
 * 4 个选项合成一个下拉（而不是"开关 + 强度"两排）：
 *   关闭 = 思考关；低 / 高 / 最高 = 思考开 + 对应强度。
 * 每次选中都会带出 (thinking, reasoning_effort) 两个值，由调用方决定写不写后台——
 * 已有会话改一下即写库，新会话只存本地 pending（见 ChatPage.applyThinking）。
 */
import { useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Brain, Check, ChevronUp } from "lucide-react";
import { cn } from "../lib/utils";
import type { ReasoningEffort } from "../api/types";

/** 选项 key：off=关；其余=开 + 强度 */
type ThinkKey = "off" | ReasoningEffort;

const OPTIONS: {
  value: ThinkKey;
  label: string;
  thinking: boolean;
  effort: ReasoningEffort;
}[] = [
  { value: "off", label: "关闭", thinking: false, effort: "low" },
  { value: "low", label: "低", thinking: true, effort: "low" },
  { value: "high", label: "高", thinking: true, effort: "high" },
  { value: "max", label: "最高", thinking: true, effort: "max" },
];

export function ThinkingPicker({
  thinking,
  effort,
  onChange,
}: {
  /** 当前是否开启思考 */
  thinking: boolean;
  /** 当前强度（关时无所谓，默认 low） */
  effort: ReasoningEffort;
  onChange: (thinking: boolean, effort: ReasoningEffort) => void;
}) {
  const [open, setOpen] = useState(false);
  // 关 → "关闭"；开 → 按强度匹配。匹配不到（异常强度）时回落"低"，别显示空白。
  const cur =
    OPTIONS.find((o) =>
      thinking ? o.thinking && o.effort === effort : o.value === "off"
    ) ?? OPTIONS[1];

  return (
    <DropdownMenu.Root open={open} onOpenChange={setOpen}>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          title={cur.thinking ? `思考模式：${cur.label}` : "思考模式：关闭"}
          className={cn(
            "flex h-7 shrink-0 items-center gap-1.5 rounded-[7px] border border-line-soft bg-panel px-2 text-[12px] font-medium shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-colors duration-150",
            cur.thinking
              ? "text-brand hover:border-brand/40"
              : "text-ink-muted hover:text-ink"
          )}
        >
          <Brain className="h-3.5 w-3.5 shrink-0" />
          <span className="hidden lg:inline">思考</span>
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
          className="z-[1700] min-w-[120px] rounded-[10px] border border-line-soft bg-panel p-1 shadow-pop"
        >
          {OPTIONS.map((o) => (
            <DropdownMenu.Item
              key={o.value}
              onSelect={() => {
                onChange(o.thinking, o.effort);
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
              <span
                className={cn(
                  "truncate text-[12px]",
                  cur.value === o.value && "font-medium text-ink-strong"
                )}
              >
                {o.label}
              </span>
            </DropdownMenu.Item>
          ))}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
