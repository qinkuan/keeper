import { cn } from "../../lib/utils";

/**
 * 分段控件（替代 antd Segmented）。
 *
 * 之前这段「灰底胶囊 + 白底选中项 + 轻阴影」的模式在 ChatPage 顶栏、可观测页、
 * 会话抽屉里各手写了一遍，这里收成一个组件——三处的圆角、内边距、字号当时
 * 都不完全一致。
 */
export function SegmentedControl<T extends string | number>({
  value,
  onChange,
  options,
  className,
}: {
  value: T;
  onChange: (v: T) => void;
  options: { label: string; value: T }[];
  className?: string;
}) {
  return (
    <div
      className={cn(
        // 凹槽底 + 内阴影：分段控件的"槽"感全靠这层，比单纯换个灰底立体
        "flex w-fit items-center gap-0.5 rounded-[7px] bg-black/[0.05] p-[3px] shadow-[inset_0_1px_2px_rgba(0,0,0,0.05)] dark:bg-white/[0.07]",
        className
      )}
    >
      {options.map((o) => (
        <button
          key={String(o.value)}
          type="button"
          onClick={() => onChange(o.value)}
          className={cn(
            "rounded-[5px] px-2.5 py-[3px] text-[12px] transition-all duration-150 ease-apple",
            value === o.value
              ? "bg-panel font-medium text-ink-strong shadow-[0_1px_2px_rgba(0,0,0,0.10),0_0_0_0.5px_rgba(0,0,0,0.04)]"
              : "text-ink-muted hover:text-ink"
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
