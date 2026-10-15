/**
 * 表单基础件（替代 antd Form / Form.Item / Input / InputNumber / Switch / Select）。
 *
 * 为什么不用「表单实例」那一套（把值存进 Form、再用 set/get 读写）：它带
 * 一整套校验调度与栅格布局，而我们这里只需要「一个标签 + 一个控件 + 一句
 * 说明」。为了这点东西背上一套表单状态机，配置读写反而要在组件状态和表单
 * 实例之间同步两遍——不值当。
 *
 * 状态就用普通的 useState：设置页的整份配置本来就是一个对象，改哪一段就
 * 改哪一段，保存时整体提交——比从表单实例里再捞一遍更直接。
 */
import type { ReactNode } from "react";
import { cn } from "../../lib/utils";

/**
 * 输入控件统一样式。
 *
 * 桌面应用的输入框比网页矮一档（30px vs 36px+）、圆角更小（7px）、`py` 更窄，
 * 聚焦时的光晕也更收敛——网页爱用 4px 的彩色 ring，macOS 是一条细描边加
 * 很淡的外发光。
 *
 * **导出**（而不只是在文件内部用）：TaskPage / ModelManage 等页面各自又写
 * 了一份"本地 INPUT 常量"，几份之间已经漂移（有的还是旧的粗 ring）。控件
 * 规格只有这一处，改观感时它们一起跟着变。
 */
export const inputCls =
  "h-[30px] w-full rounded-[7px] border border-line bg-panel px-2.5 text-[12.5px] text-ink shadow-[0_1px_1px_rgba(0,0,0,0.03)] outline-none transition-all duration-150 placeholder:text-ink-faint hover:border-line focus:border-brand focus:shadow-[0_0_0_3px_rgba(91,155,245,0.48)] disabled:cursor-not-allowed disabled:bg-surface disabled:opacity-60";

/** 多行输入：高度交给调用点（style / rows），其余规格与单行一致 */
export const textareaCls =
  "w-full resize-y rounded-[7px] border border-line bg-panel px-2.5 py-2 text-[12.5px] leading-6 text-ink shadow-[0_1px_1px_rgba(0,0,0,0.03)] outline-none transition-all duration-150 placeholder:text-ink-faint focus:border-brand focus:shadow-[0_0_0_3px_rgba(91,155,245,0.48)] disabled:cursor-not-allowed disabled:bg-surface disabled:opacity-60";

/** 标签 + 控件 + 说明。说明写在控件下方（antd 叫 extra，这里同理）。 */
export function Field({
  label,
  extra,
  children,
  className,
}: {
  label: ReactNode;
  extra?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("space-y-1", className)}>
      <div className="text-[12.5px] font-medium text-ink">{label}</div>
      {children}
      {extra && (
        <p className="text-[11.5px] leading-[1.5] text-ink-faint">{extra}</p>
      )}
    </div>
  );
}

export function TextInput({
  value,
  onChange,
  placeholder,
  disabled,
}: {
  value?: string | null;
  onChange: (v: string) => void;
  placeholder?: string;
  disabled?: boolean;
}) {
  return (
    <input
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      disabled={disabled}
      className={inputCls}
    />
  );
}

export function NumberInput({
  value,
  onChange,
  min,
  max,
  step,
  disabled,
}: {
  value?: number | null;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
  disabled?: boolean;
}) {
  return (
    <input
      type="number"
      value={value ?? 0}
      min={min}
      max={max}
      step={step}
      disabled={disabled}
      // 清空输入框时给 0 而不是 NaN：NaN 会被 JSON 序列化成 null，后端直接报错
      onChange={(e) => {
        const n = e.target.value === "" ? 0 : Number(e.target.value);
        onChange(Number.isNaN(n) ? 0 : n);
      }}
      className={inputCls}
    />
  );
}

/** 原生 select：值统一按字符串处理，需要数字的调用点自己 Number() 转 */
export function SelectInput({
  value,
  onChange,
  options,
  disabled,
}: {
  value?: string | number;
  onChange: (v: string) => void;
  options: { value: string | number; label: string }[];
  disabled?: boolean;
}) {
  return (
    <select
      value={String(value ?? "")}
      onChange={(e) => onChange(e.target.value)}
      disabled={disabled}
      className={cn(inputCls, "cursor-pointer pr-8")}
    >
      {options.map((o) => (
        <option key={String(o.value)} value={String(o.value)}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

export function Toggle({
  checked,
  onChange,
  disabled,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      /* 开关收小到 32×18：macOS 的开关比网页的小一号。开启态走品牌渐变，
         滑块带一道很淡的描边 + 双层投影（深色下纯白滑块会"飘"在背景上） */
      className={cn(
        "relative h-[18px] w-8 shrink-0 rounded-full transition-all duration-200 ease-apple disabled:opacity-50",
        checked
          ? "bg-brand-gradient shadow-[0_1px_3px_rgba(91,155,245,0.48)]"
          : "bg-line shadow-[inset_0_1px_2px_rgba(0,0,0,0.08)]"
      )}
    >
      <span
        className={cn(
          "absolute top-[2px] h-3.5 w-3.5 rounded-full bg-white shadow-[0_1px_2px_rgba(0,0,0,0.28),0_0_0_0.5px_rgba(0,0,0,0.06)] transition-all duration-200 ease-apple",
          checked ? "left-[16px]" : "left-[2px]"
        )}
      />
    </button>
  );
}

/** 数字字段：表单里数量最多的一类，合成一个组件省掉重复的三行结构 */
export function NumField({
  label,
  extra,
  value,
  onChange,
  min,
  max,
  step,
  disabled,
}: {
  label: ReactNode;
  extra?: ReactNode;
  value?: number | null;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
  disabled?: boolean;
}) {
  return (
    <Field label={label} extra={extra}>
      <NumberInput
        value={value}
        onChange={onChange}
        min={min}
        max={max}
        step={step}
        disabled={disabled}
      />
    </Field>
  );
}

export function TextField({
  label,
  extra,
  value,
  onChange,
  placeholder,
  disabled,
}: {
  label: ReactNode;
  extra?: ReactNode;
  value?: string | null;
  onChange: (v: string) => void;
  placeholder?: string;
  disabled?: boolean;
}) {
  return (
    <Field label={label} extra={extra}>
      <TextInput
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        disabled={disabled}
      />
    </Field>
  );
}

export function SelectField({
  label,
  extra,
  value,
  onChange,
  options,
  disabled,
}: {
  label: ReactNode;
  extra?: ReactNode;
  value?: string | number;
  onChange: (v: string) => void;
  options: { value: string | number; label: string }[];
  disabled?: boolean;
}) {
  return (
    <Field label={label} extra={extra}>
      <SelectInput
        value={value}
        onChange={onChange}
        options={options}
        disabled={disabled}
      />
    </Field>
  );
}

/** 开关字段：整行是一个可点的条，比「标签 + 右边一个开关」更好点中 */
export function ToggleField({
  label,
  extra,
  checked,
  onChange,
  disabled,
}: {
  label: ReactNode;
  extra?: ReactNode;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex items-center justify-between gap-4 rounded-card border border-line-soft bg-surface/50 px-3 py-2">
      <div className="min-w-0">
        <div className="text-[12.5px] font-medium text-ink">{label}</div>
        {extra && (
          <p className="mt-0.5 text-[11.5px] leading-[1.5] text-ink-faint">
            {extra}
          </p>
        )}
      </div>
      <Toggle checked={checked} onChange={onChange} disabled={disabled} />
    </div>
  );
}
