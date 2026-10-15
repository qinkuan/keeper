/**
 * 状态标签（替代 antd Tag）。
 *
 * 不用 antd 的色板（green/blue/orange/geekblue…）是因为那些颜色是写死的
 * hex，深色主题下对比度会塌。这里每个 tone 都给一对「浅色底 + dark 变体」，
 * 两种主题下都读得清。
 *
 * 之前这套色板在 ChatBubble、SessionList、AgentInfoDrawer、AgentUsagePanel
 * 里各写了一遍，改一处忘一处，所以收成组件。
 */
import type { ReactNode } from "react";
import { cn } from "../../lib/utils";

/**
 * 状态色板。
 *
 * 「蓝」这一档刻意**不用** Tailwind 的 blue（带紫调），改用品牌色本身：
 * 主色已经是 Chrome 蓝，旁边再放一个相近但不同的蓝，界面就会出现"两种蓝
 * 说不清谁是谁"的问题。品牌蓝就是这一档的蓝。
 * 其余状态色保留 Tailwind 的原生色——它们是语义色（成功/危险/警告），
 * 与品牌色不构成竞争关系。
 */
const TONES = {
  muted: "bg-black/[0.05] text-ink-muted dark:bg-white/[0.08]",
  green: "bg-emerald-500/12 text-emerald-700 dark:text-emerald-300",
  red: "bg-red-500/12 text-red-600 dark:text-red-300",
  orange: "bg-orange-500/12 text-orange-700 dark:text-orange-300",
  amber: "bg-amber-500/14 text-amber-700 dark:text-amber-300",
  blue: "bg-brand/10 text-brand",
  indigo: "bg-indigo-500/12 text-indigo-700 dark:text-indigo-300",
  purple: "bg-purple-500/12 text-purple-700 dark:text-purple-300",
  cyan: "bg-cyan-500/12 text-cyan-700 dark:text-cyan-300",
  brand: "bg-brand/10 text-brand",
} as const;

export type PillTone = keyof typeof TONES;

/** 状态文案 → 颜色。分不清的一律走 muted，别硬给个色。 */
export const STATE_TONE: Record<string, PillTone> = {
  已装配: "green",
  已关闭: "muted",
  只读工作区跳过: "orange",
  未绑定: "muted",
  依赖缺失: "red",
  已停用: "muted",
  启用: "green",
  停用: "muted",
  已装载: "blue",
  未装载: "muted",
  正常: "green",
  已熔断: "red",
};

export function Pill({
  tone = "muted",
  title,
  onClick,
  className,
  children,
}: {
  tone?: PillTone;
  title?: string;
  /** 给了就是可点的（如「走代理 / 直连」切换） */
  onClick?: () => void;
  className?: string;
  children: ReactNode;
}) {
  const cls = cn(
    // 字重 500 + 6px 圆角：11px 的字太细会糊，600 又太抢
    "inline-flex shrink-0 items-center rounded-[5px] px-1.5 py-px text-[11px] font-medium leading-[16px] tracking-[0.01em]",
    TONES[tone],
    onClick && "cursor-pointer transition-all duration-150 hover:brightness-95",
    className
  );
  if (onClick) {
    return (
      <button type="button" title={title} onClick={onClick} className={cls}>
        {children}
      </button>
    );
  }
  return (
    <span title={title} className={cls}>
      {children}
    </span>
  );
}
