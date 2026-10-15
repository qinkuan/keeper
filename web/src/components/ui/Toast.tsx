/**
 * 极简 Toast。
 *
 * 需求只有「底部居中飘一条，2.6 秒后自己消失」。引入一个组件库去做这件事，
 * 代价是一个函数调用就把整个库拖进产物；手写则不到 50 行。
 *
 * 做成模块级单例 + 一个 <ToastHost/>：调用点是命令式的（toast.success(...)），
 * 渲染点集中在一处，不必把 Provider 穿进组件树。
 */
import { useEffect, useState } from "react";
import { Check, Info, X } from "lucide-react";
import { cn } from "../../lib/utils";

type Kind = "success" | "error" | "info";
type Item = { id: string; kind: Kind; text: string };

let items: Item[] = [];
const listeners = new Set<() => void>();

function emit() {
  listeners.forEach((l) => l());
}

function push(kind: Kind, text: string) {
  const id = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
  items = [...items, { id, kind, text }];
  emit();
  setTimeout(() => {
    items = items.filter((i) => i.id !== id);
    emit();
  }, 2600);
}

export const toast = {
  success: (text: string) => push("success", text),
  error: (text: string) => push("error", text),
  info: (text: string) => push("info", text),
};

export function ToastHost() {
  const [, force] = useState(0);
  useEffect(() => {
    const l = () => force((n) => n + 1);
    listeners.add(l);
    return () => {
      listeners.delete(l);
    };
  }, []);

  if (!items.length) return null;

  return (
    <div className="pointer-events-none fixed bottom-6 left-1/2 z-[2000] flex -translate-x-1/2 flex-col items-center gap-2">
      {items.map((i) => (
        /* 统一的深色玻璃 + 一枚状态图标，比三条纯色横幅安静：
           提示是"插话"，不该比正文更抢眼；颜色只留给那枚图标 */
        <div
          key={i.id}
          className={cn(
            "animate-fade-up flex items-center gap-2.5 rounded-full py-2 pl-2.5 pr-4 text-[12.5px] font-medium text-white",
            "bg-[rgba(0,0,0,0.92)] shadow-pop backdrop-blur-md"
          )}
        >
          <span
            className={cn(
              "flex h-4 w-4 shrink-0 items-center justify-center rounded-full",
              i.kind === "success" && "bg-emerald-500/25 text-emerald-300",
              i.kind === "error" && "bg-red-500/25 text-red-300",
              i.kind === "info" && "bg-white/20 text-white"
            )}
          >
            {i.kind === "success" ? (
              <Check className="h-2.5 w-2.5" strokeWidth={3} />
            ) : i.kind === "error" ? (
              <X className="h-2.5 w-2.5" strokeWidth={3} />
            ) : (
              <Info className="h-2.5 w-2.5" strokeWidth={3} />
            )}
          </span>
          {i.text}
        </div>
      ))}
    </div>
  );
}
