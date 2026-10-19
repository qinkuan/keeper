/**
 * 工作空间选择器（对话输入框右侧，工作台的右边）。
 *
 * 保留那个文件小图标（FolderOpen）作为入口，点它从**上方**弹出可选择的工作区列表。
 * 之所以向上弹：这一行贴着输入框底部，往下会挡住免责声明/别的内容，往上才不扰民。
 *
 * 用 Radix 下拉而不是原生 <select>：原生只能向下弹、样式也不受控；这里要的是
 * "图标即入口 + 向上展出一组空间 + 勾选当前项"。与左边的 WorkbenchPicker 同形同源
 * （同一套 radix dropdown + side="top"），两个选择器看起来是一家人。
 */
import { useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Check, ChevronUp, FolderOpen } from "lucide-react";
import { cn } from "../lib/utils";

export interface WorkspaceOption {
  value: string;
  label: string;
}

export function WorkspacePicker({
  value,
  options,
  onChange,
  title = "切换工作空间",
}: {
  /** 当前生效的空间 id（"" = agent 默认目录） */
  value: string;
  /** 可选项；调用方负责把「默认」与兜底项一并给全 */
  options: WorkspaceOption[];
  onChange: (value: string) => void;
  title?: string;
}) {
  const [open, setOpen] = useState(false);
  const hit = options.find((o) => o.value === value);
  // 当前项的名字：列表里能找到就用它的；找不到（或空 = 未绑定）给个人话兜底，
  // 别让触发器显示成空白——那看着像"配置丢了"。
  const label = hit?.label || value || "默认目录";

  return (
    <DropdownMenu.Root open={open} onOpenChange={setOpen}>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          title={title}
          className="hidden h-7 w-[150px] shrink-0 items-center gap-1.5 rounded-[7px] border border-line-soft bg-panel px-2 text-[12px] font-medium text-ink-muted shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-colors duration-150 hover:border-brand/40 hover:text-brand md:flex"
        >
          <FolderOpen className="h-3.5 w-3.5 shrink-0 text-ink-faint" />
          <span className="min-w-0 flex-1 truncate text-left">{label}</span>
          <ChevronUp className="h-3 w-3 shrink-0 opacity-55" />
        </button>
      </DropdownMenu.Trigger>

      <DropdownMenu.Portal>
        <DropdownMenu.Content
          side="top"
          align="start"
          sideOffset={6}
          collisionPadding={8}
          className="z-[1700] max-h-[300px] min-w-[220px] overflow-auto rounded-[10px] border border-line-soft bg-panel p-1 shadow-pop"
        >
          {options.length === 0 && (
            <div className="px-2 py-1.5 text-[12px] text-ink-faint">
              暂无可用工作区
            </div>
          )}
          {options.map((o) => (
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
                  value === o.value ? "opacity-100" : "opacity-0"
                )}
              />
              <FolderOpen className="h-3.5 w-3.5 shrink-0 text-ink-faint" />
              <span
                className={cn(
                  "min-w-0 flex-1 truncate text-[12px]",
                  value === o.value && "font-medium text-ink-strong"
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
