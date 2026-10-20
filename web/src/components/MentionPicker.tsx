import { cn } from "../lib/utils";
import type { MentionItem } from "../lib/mention";

/**
 * `@` 快捷项弹出列表：纯展示 + 选中回调，键盘导航由父组件（输入框）接管。
 *
 * 选中用 onMouseDown + preventDefault：避免输入框先失焦、把 `@查询` 残留在文本里。
 */
export default function MentionPicker({
  items,
  activeIndex,
  onSelect,
  onHover,
}: {
  items: MentionItem[];
  activeIndex: number;
  onSelect: (item: MentionItem) => void;
  onHover: (i: number) => void;
}) {
  return (
    <div className="max-h-[264px] w-[min(440px,90vw)] overflow-auto rounded-xl border border-line-soft bg-panel p-1 shadow-[0_12px_36px_-10px_rgba(0,0,0,0.28)]">
      {items.map((it, i) => (
        <button
          key={it.key}
          type="button"
          onMouseDown={(e) => {
            e.preventDefault();
            onSelect(it);
          }}
          onMouseEnter={() => onHover(i)}
          className={cn(
            "flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left transition-colors",
            i === activeIndex
              ? "bg-brand/10"
              : "hover:bg-black/[0.04] dark:hover:bg-white/[0.06]"
          )}
        >
          <span className="shrink-0 rounded-md bg-brand/12 px-1.5 py-0.5 text-[11px] font-medium text-brand">
            @{it.label}
          </span>
          <span className="min-w-0 flex-1 truncate text-[12.5px] text-ink-muted">
            {it.snippet}
          </span>
        </button>
      ))}
    </div>
  );
}
