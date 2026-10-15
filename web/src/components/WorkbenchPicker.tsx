/**
 * 工作台选择器（对话输入框右侧，工作空间的左边）。
 *
 * 它一开始是个**开关按钮**：只能"编码工作台 / 不开"。但右侧那块区域其实不止一种
 * 用法——文件浏览只是其中最常用的一种。所以改成和工作空间同形的**下拉可选项**。
 *
 * 曾有第三个选项「Agent 浏览器」（Playwright 自带的独立 Chromium 窗口），**已下架**：
 * 它够不到用户自己 Chrome 里的页面，而那才是用户实际在用的浏览器。那条能力现在由
 * 浏览器扩展承担——装一次扩展、填一次连接码，见 keeper「浏览器」页。
 *
 * 之所以做成"选择"而不是再加一个开关按钮：右侧一行已经有工作空间、模型、发送钮，
 * 再塞一颗按钮只会让这行越来越挤；而开关式控件也表达不了"多选一"这个语义
 * ——用户点开关时无法预期点了之后会得到什么。
 */
import { useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Check, ChevronDown, Code2, Globe, X } from "lucide-react";
import { cn } from "../lib/utils";

export type WorkbenchType = "off" | "coding" | "browser";

const {
  Root: MenuRoot,
  Trigger: MenuTrigger,
  Content: MenuContent,
  Item: MenuItem,
} = DropdownMenu;

const OPTIONS: {
  value: WorkbenchType;
  label: string;
  desc: string;
  icon: typeof Code2;
  /** 已下架：代码与分支都还在，只是不在下拉里露出。把 hidden 去掉即恢复。 */
  hidden?: boolean;
}[] = [
  {
    value: "off",
    label: "关闭工作台",
    desc: "只聊天，不占右侧空间",
    icon: X,
  },
  {
    value: "coding",
    label: "编码工作台",
    desc: "文件浏览 + 代码编辑",
    icon: Code2,
  },
  {
    /* 已下架：Electron 内嵌（WebContentsView）那一份。它装不了扩展、建不了多标签，
       接不上用户自己那个 Chrome 里的页面 —— 而那才是用户实际在用的浏览器。留着它
       只会让人以为是两个浏览器，实际是一个半成品。代码仍在，随时可恢复。
       真正能用的那条在keeper「浏览器」页：装扩展、填连接码。 */
    value: "browser",
    label: "浏览器",
    desc: "输入网址，在会话里看网页",
    icon: Globe,
    hidden: true,
  },
];

const VISIBLE = OPTIONS.filter((o) => !o.hidden);

export function WorkbenchPicker({
  value,
  onChange,
}: {
  value: WorkbenchType;
  onChange: (v: WorkbenchType) => void;
}) {
  const [open, setOpen] = useState(false);
  const cur = VISIBLE.find((o) => o.value === value) ?? VISIBLE[0];
  const Icon = cur.icon;
  const on = value !== "off";

  return (
    <MenuRoot open={open} onOpenChange={setOpen}>
      <MenuTrigger asChild>
        <button
          type="button"
          title={`工作台：${cur.label}`}
          className={cn(
            "flex h-7 shrink-0 items-center gap-1.5 rounded-[7px] px-2 text-[12px] font-medium transition-colors duration-150",
            on
              ? "bg-brand/12 text-brand"
              : "text-ink-muted hover:bg-surface hover:text-ink"
          )}
        >
          <Icon className="h-3.5 w-3.5 shrink-0" />
          {/* 窄屏只留图标：这一行右边还有工作空间、模型、发送钮，全展开会挤到换行 */}
          <span className="hidden lg:inline">{cur.label}</span>
          <ChevronDown className="hidden h-3 w-3 shrink-0 opacity-55 lg:inline" />
        </button>
      </MenuTrigger>

      <MenuContent
        /* 向上弹：这行贴着输入框底部，下面是免责声明，往上才不会挡住别的内容 */
        side="top"
        align="start"
        sideOffset={6}
        className="z-[1700] min-w-[220px] rounded-[10px] border border-line-soft bg-panel p-1 shadow-pop"
      >
        {VISIBLE.map((o) => {
          const OIcon = o.icon;
          return (
            <MenuItem
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
              <OIcon className="h-3.5 w-3.5 shrink-0 text-ink-faint" />
              <span className="flex min-w-0 flex-1 flex-col">
                <span
                  className={cn(
                    "truncate text-[12px]",
                    value === o.value && "font-medium text-ink-strong"
                  )}
                >
                  {o.label}
                </span>
                <span className="truncate text-[11px] text-ink-faint">
                  {o.desc}
                </span>
              </span>
            </MenuItem>
          );
        })}
      </MenuContent>
    </MenuRoot>
  );
}
