/**
 * 居中对话框（替代 antd Modal），底层 Radix Dialog。
 *
 * 和 Drawer 共用同一套行为来源（焦点陷阱 / Esc / 滚动锁定 / aria），
 * 区别只是位置与动效：这个从中间淡入放大，Drawer 从右侧滑入。
 */
import * as Dialog from "@radix-ui/react-dialog";
import type { ReactNode } from "react";
import { X } from "lucide-react";
import { cn } from "../../lib/utils";

export function Modal({
  open,
  onClose,
  title,
  footer,
  width = 520,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  /** 底部操作区；不传则只有内容（对应 antd 的 footer={null}） */
  footer?: ReactNode;
  width?: number;
  children: ReactNode;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay
          className={cn(
            /* macOS 的模态遮罩很淡（约 25%）且带一层模糊：模糊把背后的界面
               "推远"，比单纯压暗更像系统弹窗，弹窗自身也显得更靠前 */
            "fixed inset-0 z-[1500] bg-black/25 backdrop-blur-[3px] dark:bg-black/50",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0"
          )}
        />
        <Dialog.Content
          style={{ width }}
          className={cn(
            "fixed left-1/2 top-1/2 z-[1600] flex max-h-[88vh] max-w-[94vw] -translate-x-1/2 -translate-y-1/2",
            /* 标题栏用灰底，与内容区白底分开——macOS 的 sheet / alert 就是这个结构 */
            "flex-col overflow-hidden rounded-float border border-line-soft bg-panel shadow-pop outline-none",
            "duration-150 data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0",
            "data-[state=open]:zoom-in-[0.98] data-[state=closed]:zoom-out-[0.98]"
          )}
        >
          <div className="flex shrink-0 items-center justify-between gap-3 border-b border-line-soft bg-surface/70 px-4 py-2.5">
            <Dialog.Title className="text-[13px] font-semibold tracking-[-0.01em] text-ink-strong">
              {title}
            </Dialog.Title>
            <Dialog.Close
              title="关闭"
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[7px] text-ink-muted transition-colors duration-150 hover:bg-black/[0.06] hover:text-ink dark:hover:bg-white/10"
            >
              <X className="h-4 w-4" />
            </Dialog.Close>
          </div>

          <div className="min-h-0 flex-1 overflow-auto px-5 py-4">{children}</div>

          {footer && (
            <div className="flex shrink-0 items-center justify-end gap-2 border-t border-line-soft bg-surface/70 px-4 py-3">
              {footer}
            </div>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
