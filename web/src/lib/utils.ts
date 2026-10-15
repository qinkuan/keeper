import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn/ui 的 cn：合并条件类名，并让后写的 Tailwind 类覆盖先写的同类类。 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
