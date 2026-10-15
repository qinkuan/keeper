/**
 * 右侧抽屉（替代 antd Drawer），底层是 Radix Dialog。
 *
 * 用 Radix 而不是自己写：焦点陷阱、Esc 关闭、滚动锁定、`aria-*` 这套
 * 无障碍行为自己实现一遍不划算，而且容易漏。Radix 只提供行为，样式全在我们
 * 这里，观感不会跟着它走。
 */
import * as Dialog from "@radix-ui/react-dialog";
import type { ReactNode } from "react";
import { X } from "lucide-react";
import { cn } from "../../lib/utils";

export function Drawer({
  open,
  onClose,
  title,
  subtitle,
  width = 720,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  subtitle?: ReactNode;
  width?: number;
  children: ReactNode;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay
          className={cn(
            "fixed inset-0 z-[1500] bg-black/25 backdrop-blur-[3px] dark:bg-black/50",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0"
          )}
        />
        <Dialog.Content
          style={{ width }}
          className={cn(
            "fixed right-0 top-0 z-[1600] flex h-full max-w-[92vw] flex-col",
            /* 左侧一道发丝线 + 向左扩散的投影：抽屉是从右边"盖"过来的，
               投影方向必须跟着来，不然它会显得是浮在页面中间 */
            "border-l border-line-soft bg-panel shadow-[-16px_0_48px_-16px_rgba(0,0,0,0.22)] outline-none",
            "duration-200 data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=open]:slide-in-from-right data-[state=closed]:slide-out-to-right"
          )}
        >
          {/* 抽屉头也用灰底，与内容区分开——和 Modal 的标题栏保持一致的语言 */}
          <div className="flex shrink-0 items-center justify-between gap-3 border-b border-line-soft bg-surface/70 px-4 py-3">
            <div className="min-w-0">
              <Dialog.Title className="text-[13px] font-semibold tracking-[-0.01em] text-ink-strong">
                {title}
              </Dialog.Title>
              {subtitle && (
                <Dialog.Description className="mt-0.5 truncate font-mono text-[11.5px] text-ink-muted">
                  {subtitle}
                </Dialog.Description>
              )}
            </div>
            <Dialog.Close
              title="关闭"
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[7px] text-ink-muted transition-colors duration-150 hover:bg-black/[0.06] hover:text-ink dark:hover:bg-white/10"
            >
              <X className="h-4 w-4" />
            </Dialog.Close>
          </div>

          <div className="min-h-0 flex-1 overflow-auto p-4">{children}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
