import { cn } from "../lib/utils";

/** `/` 唤出的「未加载插件」候选项（来自 GET /manage/plugins/available）。 */
export type AvailPlugin = {
  name: string;
  dirname: string;
  description: string;
  kinds: string[];
  version?: string;
  error?: string;
};

/**
 * `/` 快捷项弹出列表：纯展示 + 选中回调，键盘导航由父组件（输入框）接管。
 *
 * 选中用 onMouseDown + preventDefault：避免输入框先失焦、把 `/查询` 残留在文本里
 * （与 MentionPicker 同一套道理）。
 */
export default function PluginPicker({
  items,
  activeIndex,
  onSelect,
  onHover,
}: {
  items: AvailPlugin[];
  activeIndex: number;
  onSelect: (p: AvailPlugin) => void;
  onHover: (i: number) => void;
}) {
  return (
    <div className="max-h-[264px] w-[min(440px,90vw)] overflow-auto rounded-xl border border-line-soft bg-panel p-1 shadow-[0_12px_36px_-10px_rgba(0,0,0,0.28)]">
      {items.length === 0 ? (
        <div className="px-2.5 py-2 text-[12.5px] text-ink-muted">
          没有可动态加载的插件（库里没有未链接的 bin 类插件）
        </div>
      ) : (
        items.map((p, i) => (
          <button
            key={p.name}
            type="button"
            onMouseDown={(e) => {
              e.preventDefault();
              onSelect(p);
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
              /{p.name}
            </span>
            <span className="min-w-0 flex-1 truncate text-[12.5px] text-ink-muted">
              {p.description || p.dirname}
            </span>
          </button>
        ))
      )}
    </div>
  );
}
