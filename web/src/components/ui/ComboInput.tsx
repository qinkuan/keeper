/**
 * 可输入的下拉框（Combo）：既能像普通输入框一样手填，也能从候选里挑。
 *
 * 为什么不用原生 <datalist>：它长什么样由浏览器决定，Chromium 里是一块
 * 灰白相间、字号失控的系统面板，和桌面应用的观感完全不搭，而且没法显示
 * 候选的副标题（比如「上下文 1,048,576」）。这里自己画一层浮层：候选的
 * 主行是标识（等宽字体），副行是说明，右侧再挂一小段备注。
 *
 * 为什么不用 <select>：它不能手填。用户有时要填预设里没有的服务商 / 模型名
 * （自建网关、内网代理），"只能选"就太死。
 *
 * 浮层用 Portal 挂到 body 上：弹窗的内容区是 overflow-auto 的，绝对定位的
 * 下拉会被它裁掉；而且 Dialog 用了 transform 做居中，transform 会给后代
 * 建立新的包含块，fixed 定位会跟着它跑——挂到 body 才不会。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ChevronDown, X } from "lucide-react";
import { cn } from "../../lib/utils";
import { inputCls } from "./Form";

export interface ComboOption {
  /** 真正写回表单的值（服务商标识 / 模型名） */
  value: string;
  /** 副标题：服务商的中文名、模型的展示名 */
  label?: string;
  /** 右侧备注：Base URL、上下文上限等 */
  desc?: string;
}

export function ComboInput({
  value,
  onChange,
  options,
  placeholder,
  disabled,
  onClear,
  emptyText = "没有匹配的候选，可直接手填",
}: {
  value?: string | null;
  onChange: (v: string) => void;
  options: ComboOption[];
  placeholder?: string;
  disabled?: boolean;
  /** 右侧 ✕（清空） */
  onClear?: () => void;
  emptyText?: string;
}) {
  const [open, setOpen] = useState(false);
  /** 键盘高亮项的下标 */
  const [active, setActive] = useState(0);
  /** 输入框的位置：浮层挂到 body 上，得自己算 */
  const [rect, setRect] = useState<DOMRect | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  /** 浮层本体（Portal 出去的）：判"点到外面"时要用 */
  const panelRef = useRef<HTMLDivElement>(null);

  /* 候选排序：命中的排前面（保持原有相对顺序），其余照旧。
     全部保留而不是只留命中的——用户是"看一眼有哪些"，不是"只看筛过的"。 */
  const keyword = (value ?? "").trim().toLowerCase();
  const list = useMemo(() => {
    if (!keyword) return options;
    const hit: ComboOption[] = [];
    const rest: ComboOption[] = [];
    for (const o of options) {
      const hay = `${o.value} ${o.label ?? ""}`.toLowerCase();
      (hay.includes(keyword) ? hit : rest).push(o);
    }
    return [...hit, ...rest];
  }, [options, keyword]);

  useEffect(() => setActive(0), [keyword, open]);

  /* 点外面收起。
     浮层是 Portal 出去的，不在 rootRef 里——所以要单独判一次，否则在浮层上
     按下鼠标的瞬间浮层就被当成"点到外面"关掉了，后面的 click 也就落空，
     表现为"候选项点不动"。 */
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (rootRef.current?.contains(t)) return;
      if (panelRef.current?.contains(t)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  /* 浮层贴输入框下沿；页面（或弹窗内容区）一滚就收起，免得错位 */
  useEffect(() => {
    if (!open) return;
    const el = inputRef.current;
    if (!el) return;
    setRect(el.getBoundingClientRect());
    const onScroll = () => setOpen(false);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onScroll);
    return () => {
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onScroll);
    };
  }, [open]);

  const pick = (o: ComboOption) => {
    onChange(o.value);
    setOpen(false);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!open) setOpen(true);
      else setActive((a) => Math.min(a + 1, list.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter") {
      if (open && list[active]) {
        e.preventDefault();
        pick(list[active]);
      }
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  };

  return (
    <div ref={rootRef} className="relative">
      <input
        ref={inputRef}
        value={value ?? ""}
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKeyDown}
        placeholder={placeholder}
        disabled={disabled}
        className={cn(inputCls, "pr-11")}
      />
      <div className="absolute right-1 top-1/2 flex -translate-y-1/2 items-center gap-0.5">
        {onClear && (
          <button
            type="button"
            onClick={onClear}
            disabled={disabled}
            title="清空"
            className="rounded p-1 text-ink-faint transition-colors hover:bg-black/[0.06] hover:text-ink dark:hover:bg-white/10 disabled:opacity-50"
          >
            <X className="h-3 w-3" />
          </button>
        )}
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          disabled={disabled}
          title={open ? "收起候选" : "展开候选"}
          className="rounded p-1 text-ink-faint transition-colors hover:bg-black/[0.06] hover:text-ink dark:hover:bg-white/10 disabled:opacity-50"
        >
          <ChevronDown
            className={cn(
              "h-3.5 w-3.5 transition-transform duration-150",
              open && "rotate-180"
            )}
          />
        </button>
      </div>

      {open &&
        rect &&
        createPortal(
          <div
            ref={panelRef}
            style={{ left: rect.left, top: rect.bottom + 4, width: rect.width }}
            /* pointer-events-auto 不能省：Radix 的 Dialog 打开时会给 <body> 加
               pointer-events:none（防止打开动画期间点到外面），而 body 上的
               pointer-events 是**继承**的——浮层不显式写 auto 就会跟着变成
               "点不到"，表现就是"候选看得见却选不了"。 */
            className="pointer-events-auto fixed z-[2400] max-h-[240px] overflow-y-auto rounded-[9px] border border-line-soft bg-panel p-1 shadow-pop"
          >
            {list.length === 0 ? (
              <div className="px-2 py-2.5 text-[11.5px] text-ink-faint">
                {emptyText}
              </div>
            ) : (
              list.map((o, i) => (
                <button
                  key={o.value}
                  type="button"
                  /* 给自动化留个抓手（真实鼠标点击回归时用） */
                  data-combo-option={o.value}
                  onMouseEnter={() => setActive(i)}
                  onClick={() => pick(o)}
                  className={cn(
                    /* 选项的 hover 观感跟 keeper 里其它下拉菜单一致（App.tsx 的
                       MenuLink / DropdownMenuItem）：淡蓝底 + 品牌色描边。
                       ring 是外扩阴影，不占布局，所以 hover 时不会把行挤动。 */
                    "flex w-full items-center gap-2.5 rounded-[6px] px-2 py-1.5 text-left transition-colors duration-100",
                    i === active
                      ? "bg-brand/12 ring-1 ring-brand/40"
                      : "hover:bg-brand/12 hover:ring-1 hover:ring-brand/40"
                  )}
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-mono text-[12px] leading-4 text-ink-strong">
                      {o.value}
                    </span>
                    {o.label && o.label !== o.value && (
                      <span className="block truncate text-[11px] leading-4 text-ink-muted">
                        {o.label}
                      </span>
                    )}
                  </span>
                  {o.desc && (
                    <span className="shrink-0 text-[11px] text-ink-faint">
                      {o.desc}
                    </span>
                  )}
                </button>
              ))
            )}
          </div>,
          document.body
        )}
    </div>
  );
}
