import type { ReactNode } from "react";
import { cn } from "../../lib/utils";

/** 下划线式 Tab：比 antd Tabs 轻，且能直接用我们自己的排版变量。 */
export function Tabs({
  items,
  value,
  onChange,
  className,
}: {
  items: { key: string; label: ReactNode }[];
  value: string;
  onChange: (key: string) => void;
  className?: string;
}) {
  return (
    <div
      className={cn("flex items-center gap-1 border-b border-line-soft", className)}
    >
      {items.map((t) => (
        <button
          key={t.key}
          type="button"
          onClick={() => onChange(t.key)}
          className={cn(
            /* 下划线粗 2px 并上移 2px盖住底线，选中态才有"嵌进页头"的感觉；
               未选中时底色也给一点 hover，选中的那块始终在最上层 */
            "relative -mb-[2px] border-b-2 px-3 pb-2 pt-1.5 text-[12.5px] transition-colors duration-150",
            value === t.key
              ? "border-brand font-medium text-ink-strong"
              : "border-transparent text-ink-muted hover:bg-surface/60 hover:text-ink"
          )}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}
