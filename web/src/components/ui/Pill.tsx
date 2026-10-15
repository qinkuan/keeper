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
 * 「蓝」这一档用品牌色本身：主色旁边再放一个相近但不同的蓝，界面就会出现
 * "两种蓝说不清谁是谁"的问题。
 *
 * 成功/警告/危险三档换成**柔和版**（#4A9C78 / #C49254 / #B85C5C），不用
 * Tailwind 原生的 emerald/amber/red——那些是为"高饱和提醒"设计的，在冷灰蓝
 * 底上会跳出来，而这三个色在这里只是"少量点缀、不抢戏"。
 * 文字色分浅深两档：浅底用深一档（11px 小字要压得住），深底用亮一档。
 *
 * indigo/purple/cyan 是**分类色**不是语义色，保持原样。
 */
const TONES = {
  muted: "bg-black/[0.05] text-ink-muted dark:bg-white/[0.08]",
  green: "bg-[#34a873]/12 text-[#2b8f60] dark:text-[#4cbd88]",
  red: "bg-[#d15b5b]/12 text-[#b04a4a] dark:text-[#dd7a7a]",
  orange: "bg-[#d49441]/12 text-[#a87433] dark:text-[#dda85c]",
  amber: "bg-[#d49441]/14 text-[#a87433] dark:text-[#dda85c]",
  /* 淡蓝底上的文字用 deep（浅底 #2962D8，5.6:1）而不是主色（3.2:1）：
     这些标签是 11px 小字，用主色会糊。深色下换 strong（Primary-light）。 */
  blue: "bg-brand/10 text-brand-deep dark:text-brand-strong",
  indigo: "bg-indigo-500/12 text-indigo-700 dark:text-indigo-300",
  purple: "bg-purple-500/12 text-purple-700 dark:text-purple-300",
  cyan: "bg-cyan-500/12 text-cyan-700 dark:text-cyan-300",
  brand: "bg-brand/10 text-brand-deep dark:text-brand-strong",
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
