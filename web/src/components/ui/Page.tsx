/**
 * 页面级排版基础件。
 *
 * 为什么要有它：设置 / 模型管理 / 插件 / 可观测这四页原来各自用 antd 的
 * `Typography.Title` + `Card` + `Space` 拼页面头，结果每一页的标题字号、卡片
 * 圆角、区块间距都不一样。统一走这三个组件后，页面结构（标题层 / 区块层 /
 * 提示层）只有一种写法，改观感时只改这里。
 *
 * 它们**只管排版，不碰数据**——表单用 `Form.tsx`、表格用 `Table.tsx`、
 * 弹窗用 `Modal.tsx` / `Drawer.tsx`，都是同目录下的兄弟件，各管一层。
 */
import type { ReactNode } from "react";
import { cn } from "../../lib/utils";

/** 页头：标题 + 说明 + 右侧操作区 */
export function PageHeader({
  title,
  desc,
  actions,
  className,
}: {
  title: ReactNode;
  desc?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn("mb-6 flex items-start justify-between gap-4", className)}
    >
      <div className="min-w-0">
        {/* 标题 20px / 字重 600 / 负字距：三者一起才像"应用标题"而不是网页 H1 */}
        <h1 className="text-[20px] font-semibold leading-tight tracking-[-0.02em] text-ink-strong">
          {title}
        </h1>
        {desc && (
          <p className="mt-1.5 max-w-[680px] text-[12.5px] leading-5 text-ink-muted">
            {desc}
          </p>
        )}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}

/** 区块：圆角卡片 + 可选标题栏。表格 / 表单都塞进它里面。 */
export function Section({
  title,
  extra,
  children,
  className,
  bodyClassName,
}: {
  title?: ReactNode;
  extra?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section
      className={cn(
        // 静置就带一档极淡阴影：卡片不投影的话会"贴"在白底上，看起来像没做完
        "overflow-hidden rounded-float border border-line-soft bg-panel shadow-card",
        className
      )}
    >
      {(title || extra) && (
        /* 区块标题栏用灰底（--kp-surface），与内容区白底分开——macOS 分组列表
           （NSBox / grouped style）的通行做法，比只加一条边框清楚。
           底色用半透明而非实色，卡片若放在灰底页面上也不会显出方块。 */
        <div className="flex items-center justify-between gap-3 border-b border-line-soft bg-surface/70 px-4 py-2.5">
          <h2 className="text-[13px] font-semibold tracking-[-0.01em] text-ink-strong">
            {title}
          </h2>
          {extra && <div className="flex items-center gap-2">{extra}</div>}
        </div>
      )}
      <div className={cn("p-[18px]", bodyClassName)}>{children}</div>
    </section>
  );
}

/** 区块之间的垂直间距 */
export function Stack({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return <div className={cn("space-y-[18px]", className)}>{children}</div>;
}

/**
 * 行内提示（替代 antd Alert 的轻量版）。
 *
 * 不用 antd Alert 的原因：它自带 12px 内边距 + 图标槽 + 圆角，在密集表单里
 * 会占掉两行高度，比它提示的那句话还显眼。这里只保留「一条淡色底 + 一句话」。
 */
export function Hint({
  tone = "info",
  title,
  children,
  className,
}: {
  tone?: "info" | "warn" | "danger";
  title?: ReactNode;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-card border px-3 py-2 text-[12.5px] leading-5",
        tone === "info" && "border-brand/20 bg-brand/[0.06] text-ink-muted",
        tone === "warn" &&
          "border-amber-500/25 bg-amber-500/[0.08] text-amber-700 dark:text-amber-300",
        tone === "danger" &&
          "border-red-500/25 bg-red-500/[0.06] text-red-600 dark:text-red-300",
        className
      )}
    >
      {title && <div className="font-medium text-ink-strong">{title}</div>}
      {children}
    </div>
  );
}
