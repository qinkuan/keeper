/**
 * 可折叠区块（替代 antd Collapse）。
 *
 * 与 antd 的差别：展开态**可选受控**——不传 `open` 就由本组件内部管理，
 * 传了就完全听调用方的。antd 的 Collapse 把展开态藏在内部，想「流式时自动
 * 展开最后一条、结束后收起」这类联动就得跟它的受控协议绕。
 */
import { useState } from "react";
import type { ReactNode } from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "../../lib/utils";

export function CollapseItem({
  label,
  open,
  onToggle,
  children,
  className,
}: {
  label: ReactNode;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        // 圆角 7px + 静置微投影：折叠块在页面里往往连着好几个，不投影会糊成一片
        "overflow-hidden rounded-[7px] border border-line-soft bg-panel shadow-[0_1px_2px_rgba(0,0,0,0.04)]",
        className
      )}
    >
      <button
        type="button"
        onClick={onToggle}
        className={cn(
          "flex w-full items-center gap-2 px-3 py-2 text-left transition-colors duration-150",
          open ? "bg-surface" : "hover:bg-surface/70"
        )}
      >
        <ChevronDown
          className={cn(
            "h-3.5 w-3.5 shrink-0 text-ink-faint transition-transform duration-200 ease-apple",
            open && "rotate-180"
          )}
        />
        <span className="min-w-0 flex-1 text-[12.5px] font-medium text-ink-strong">
          {label}
        </span>
      </button>
      {open && (
        <div className="border-t border-line-soft px-3 py-2.5">{children}</div>
      )}
    </div>
  );
}

/** 折叠组：各项独立展开，默认全收起 */
export function Collapse({
  items,
  className,
}: {
  items: {
    key: string;
    label: ReactNode;
    children: ReactNode;
  }[];
  className?: string;
}) {
  const [openKeys, setOpenKeys] = useState<Record<string, boolean>>({});
  return (
    <div className={cn("space-y-2", className)}>
      {items.map((it) => (
        <CollapseItem
          key={it.key}
          label={it.label}
          open={!!openKeys[it.key]}
          onToggle={() =>
            setOpenKeys((s) => ({ ...s, [it.key]: !s[it.key] }))
          }
        >
          {it.children}
        </CollapseItem>
      ))}
    </div>
  );
}
