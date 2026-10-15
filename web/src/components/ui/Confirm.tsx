/**
 * 按钮内二次确认（替代 antd Popconfirm / Modal.confirm）。
 *
 * 为什么不做成对话框：删除一条会话、移除一个对端、删一段价格——这些操作
 * 后果可控且可恢复，弹一层遮罩 + 焦点陷阱 + 两个按钮的手势太重，还多一次
 * 点击。把确认态**放在触发按钮原位**，既不误触，也不打断视线。
 *
 * 用 `<span>` 而不是嵌套 `<button>`：HTML 不允许按钮嵌按钮，浏览器会把
 * 内层按钮提出来，导致点击区域错乱。
 */
import { useEffect, useState } from "react";
import { cn } from "../../lib/utils";

const ACTIONS =
  "inline-flex h-6 items-center rounded-md border px-2 text-[12px] transition-colors disabled:opacity-50";
/** 危险动作：描边红 + hover 实心，视觉上就与「取消」拉开距离 */
export const DANGER_BTN = cn(
  ACTIONS,
  "border-red-500/40 text-red-600 hover:bg-red-500 hover:text-white dark:text-red-300"
);
export const PLAIN_BTN = cn(
  ACTIONS,
  "border-line text-ink-muted hover:bg-surface hover:text-ink-strong"
);

export function ConfirmButton({
  onConfirm,
  children,
  confirmText = "确认删除",
  cancelText = "取消",
  disabled,
  className,
  /** 离开页面时自动复原，避免"确认"态挂在界面上 */
  resetKey,
}: {
  onConfirm: () => void;
  children: React.ReactNode;
  confirmText?: string;
  cancelText?: string;
  disabled?: boolean;
  className?: string;
  resetKey?: unknown;
}) {
  const [armed, setArmed] = useState(false);
  useEffect(() => setArmed(false), [resetKey]);

  if (!armed) {
    return (
      <button
        type="button"
        disabled={disabled}
        onClick={() => setArmed(true)}
        className={className}
      >
        {children}
      </button>
    );
  }

  return (
    <span className="inline-flex items-center gap-1">
      <button
        type="button"
        disabled={disabled}
        onClick={() => {
          setArmed(false);
          onConfirm();
        }}
        className={DANGER_BTN}
      >
        {confirmText}
      </button>
      <button
        type="button"
        onClick={() => setArmed(false)}
        className={PLAIN_BTN}
      >
        {cancelText}
      </button>
    </span>
  );
}
