import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
import type { KeyboardEvent } from "react";

/** shadcn/ui 的 cn：合并条件类名，并让后写的 Tailwind 类覆盖先写的同类类。 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * 这次按键是不是**输入法正在拼字**时按的（中文拼音 / 日文罗马字等）。
 *
 * 为什么每个「回车即提交」的输入框都得先问这一句：拼音打到一半按回车，
 * 用户的意思是「确认候选词」，不是「发送」。不拦的话，半截拼音会被直接
 * 提交上去，候选词也没选上——macOS 上每次打中文都要踩一次。
 *
 * 组合期的**所有**键都该让给输入法：回车=确认候选、上下键=翻页候选、
 * 空格=上屏第一个候选。所以这个判断要放在 keydown 处理的最前面。
 *
 * 两个来源都要看：
 * - ``isComposing``：标准属性，Chromium / Firefox 的 keydown 上可靠。
 * - ``keyCode === 229``：组合期按键的统一 keyCode。Safari（以及某些
 *   Electron 版本）在 keydown 里 ``isComposing`` 可能还是 false，只有这个
 *   兜得住。
 *
 * 注意：判成立时只 return、**不要** preventDefault——把输入法正在处理的
 * 键否掉，候选词反而上不了屏。
 */
export function isComposingKey(e: KeyboardEvent): boolean {
  return e.nativeEvent?.isComposing === true || e.keyCode === 229;
}

