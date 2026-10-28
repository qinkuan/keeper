/**
 * 服务端时间的解析与展示。
 *
 * 为什么需要专门一层
 * ------------------
 * 库里的时间列一律按 UTC 落盘（写入侧是 `datetime.now(timezone.utc)`），而
 * SQLite 的 `DATETIME` **不保存 tzinfo** —— 读回来是 naive datetime，后端直接
 * `.isoformat()` 下发的是 `2026-10-27T22:11:10.682795` 这种**没有时区标记**的串。
 *
 * JS 对「有 T、无时区后缀」的 ISO 串按**本地时间**解析（ES2015 起的规定），于是
 * 这个 UTC 时刻被当成本地时刻显示，凭空快了整整一个时区偏移（UTC+8 下就是 8 小时）。
 * 表现即：消息刚返回时前端本地生成的 `created_at`（带 Z）是对的，刷新页面读历史
 * 后时间就"漂移"成了 UTC。
 *
 * 后端出口现已统一补 `+00:00`（见 `store/models.py` 的 `utc_iso`），这里再兜一层：
 * 老数据或漏改的接口下发裸串时，一律按 UTC 解释，保证展示口径一致。
 */

/** 末尾的时区标记：`Z` / `+08:00` / `+0800`。 */
const TZ_SUFFIX = /(?:Z|[+-]\d{2}:?\d{2})$/i;

/**
 * 解析服务端时间戳。
 *
 * - naive（无时区标记）的串按 **UTC** 解释，并补上标记；
 * - 空格分隔（裸 SQLite 值 / 部分老接口）先规范成 `T` 分隔，兼容 Safari；
 * - 非法值返回 `null`，调用方据此显示空串而不是 `Invalid Date`。
 */
export function parseServerTime(value?: string | number | null): Date | null {
  if (value === null || value === undefined || value === "") return null;

  if (typeof value === "number") {
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  let s = value.trim();
  const sp = s.indexOf(" ");
  if (sp > 0) s = `${s.slice(0, sp)}T${s.slice(sp + 1)}`;
  if (!TZ_SUFFIX.test(s)) s += "Z";

  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** 短格式：今天显示 `HH:mm`，更早显示 `MM-DD HH:mm`。 */
export function formatShortTime(value?: string | number | null): string {
  const d = parseServerTime(value);
  if (!d) return "";

  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();

  return sameDay ? hm : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;
}
