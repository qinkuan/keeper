import type { ReactNode } from "react";
import { cn } from "../../lib/utils";

/**
 * 指标小卡（替代 antd Statistic / Card 组合）。
 *
 * 表格与指标的区别在于**扫描方式**：指标是横着扫一排、比大小，所以数字要比
 * 标签大得多、并且对齐；标签只是说明文字，不该抢注意力。
 */
export function Stat({
  label,
  value,
  sub,
  tone,
  className,
}: {
  label: ReactNode;
  value: ReactNode;
  /** 数字下方的小字（如「3/12 轮」） */
  sub?: ReactNode;
  tone?: "warn" | "danger" | "success";
  className?: string;
}) {
  return (
    <div
      className={cn(
        // 白卡 + 微阴影（而不是灰底）：指标是"浮在页面上的数"，灰底会让它
        // 沉进背景里，扫视时跳不出来
        "rounded-float border border-line-soft bg-panel px-4 py-3 shadow-card",
        className
      )}
    >
      <div className="truncate text-[11.5px] font-medium tracking-[0.01em] text-ink-muted">
        {label}
      </div>
      <div
        className={cn(
          // 数字用等宽字形并收紧字距：一排指标要对齐，字距负值才不"散"
          "mt-1 text-[22px] font-semibold leading-none tracking-[-0.02em] tabular-nums",
          tone === "danger"
            ? "text-red-600 dark:text-red-400"
            : tone === "warn"
            ? "text-amber-600 dark:text-amber-400"
            : tone === "success"
            ? "text-emerald-600 dark:text-emerald-400"
            : "text-ink-strong"
        )}
      >
        {value}
      </div>
      {/* sub 加 truncate：里面常放路径（索引库路径、模型目录），一长就会把
          卡片撑破、把整行挤歪。截断比溢出好，完整内容另有地方看。 */}
      {sub && (
        <div className="mt-1.5 truncate text-[11px] text-ink-faint">{sub}</div>
      )}
    </div>
  );
}
