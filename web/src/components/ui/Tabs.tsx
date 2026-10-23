import type { ReactNode } from "react";
import { cn } from "../../lib/utils";

/**
 * 下划线式 Tab：比 antd Tabs 轻，且能直接用我们自己的排版变量。
 *
 * tab 多到放不下时**横向滚动**，而不是把每个挤扁
 * --------------------------------------------------
 * 原来是一层 `flex` 容器：tab 多了以后 flex 会压缩子项（默认 flex-shrink:1），
 * 而「第 12 步」这种中文标签在窄按钮里会逐字换行——实测 32 个 tab 时每个被压到
 * 37px 宽、70px 高，「第 1 步」竖着排三行，看起来像一排竖条。
 *
 * 现在分两层：
 * - **外层**只管横向滚动（``overflow-x-auto``）
 * - **内层**画底线，并让内容按自身宽度铺开（``w-max min-w-full``）：tab 少时
 *   底线横贯整栏，tab 多时底线跟着内容一起横向滚动
 * - 每个 button 加 ``shrink-0 whitespace-nowrap``：保持自身宽度，标签不换行
 *
 * 为什么底线用 box-shadow 而不是 border
 * ------------------------------------
 * 选中态的下划线要**正好压在这条线上**。用 border 画底线就得让 button 下移
 * 1px 去盖它，而那 1px 溢出会在滚动容器里变成一条多余的纵向滚动条。
 * ``inset`` 阴影既不占布局空间、也不溢出，正好干这件事。
 */
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
    <div className={cn("overflow-x-auto", className)}>
      <div className="flex w-max min-w-full items-center gap-1 shadow-[inset_0_-1px_0_rgb(var(--kp-border-soft-rgb))]">
        {items.map((t) => (
          <button
            key={t.key}
            type="button"
            onClick={() => onChange(t.key)}
            className={cn(
              /* relative + 绝对定位的下划线：不占布局空间，所以选中与否
                 不影响 tab 自身高度，选中态切换时整排不会跳动 */
              "relative shrink-0 whitespace-nowrap px-3 pb-2 pt-1.5 text-[12.5px] transition-colors duration-150",
              value === t.key
                ? "font-medium text-ink-strong"
                : "text-ink-muted hover:bg-surface/60 hover:text-ink"
            )}
          >
            {t.label}
            {value === t.key && (
              <span className="pointer-events-none absolute inset-x-0 bottom-0 h-[2px] rounded-full bg-brand" />
            )}
          </button>
        ))}
      </div>
    </div>
  );
}
