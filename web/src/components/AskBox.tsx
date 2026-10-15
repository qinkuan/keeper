import { useState } from "react";

interface Props {
  /** 模型给出的候选，直接点即提交 */
  options?: string[];
  disabled?: boolean;
  onSubmit: (text: string) => void;
}

/** 模型追问时，在该条消息下方渲染的答题区（选项 + 输入框） */
export default function AskBox({ options, disabled, onSubmit }: Props) {
  const [val, setVal] = useState("");

  const submit = (text: string) => {
    const t = text.trim();
    if (!t || disabled) return;
    onSubmit(t);
    setVal("");
  };

  return (
    /* 「轮到我回答」是一个需要人介入的状态，整块用左侧品牌条 + 淡底框出来：
       混在消息流里时用户很容易滑过去继续往下看，错过这个提问。 */
    <div className="relative mb-4 overflow-hidden rounded-[10px] border border-brand/20 bg-brand-gradient-soft px-3.5 py-3">
      <span className="absolute left-0 top-0 h-full w-[2.5px] bg-brand-gradient" />

      {!!options?.length && (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {options.map((o) => (
            <button
              key={o}
              type="button"
              disabled={disabled}
              onClick={() => submit(o)}
              className="rounded-[7px] border border-line bg-panel px-2.5 py-1 text-[12.5px] font-medium text-ink shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-all duration-150 hover:border-brand/45 hover:bg-brand/[0.06] hover:text-brand active:scale-[0.98] disabled:opacity-50"
            >
              {o}
            </button>
          ))}
        </div>
      )}

      <div className="flex w-full max-w-[520px] gap-2">
        <input
          value={val}
          disabled={disabled}
          placeholder="回答上面的问题，Enter 发送"
          onChange={(e) => setVal(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit(val);
          }}
          className="min-w-0 flex-1 rounded-[7px] border border-line bg-panel px-2.5 text-[12.5px] text-ink shadow-[0_1px_1px_rgba(0,0,0,0.03)] outline-none transition-all duration-150 placeholder:text-ink-faint focus:border-brand focus:shadow-[0_0_0_3px_rgba(91,155,245,0.48)] disabled:opacity-50"
        />
        <button
          type="button"
          disabled={disabled || !val.trim()}
          onClick={() => submit(val)}
          className="shrink-0 rounded-[7px] bg-brand-gradient px-3.5 py-[5px] text-[12.5px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-[1.08] active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
        >
          回答
        </button>
      </div>

      <p className="mt-1.5 text-[11px] text-ink-muted">
        在这里回答会接上刚才的上下文；用下方输入框则开启新话题
      </p>
    </div>
  );
}
