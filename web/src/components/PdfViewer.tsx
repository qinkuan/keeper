/**
 * 「我的空间」PDF 预览：服务端渲染整页 → PNG，逐页贴上去，纵向滚动翻页。
 *
 * 为什么不用浏览器里的 pdf.js
 * -------------------------
 * pdf.js 碰到**未嵌入字体**的 PDF 时只能画出 ASCII，中文整片消失——实测网易云
 * 音乐半年报.pdf：繁体中文、标题、表格标签全没了，只剩数字（pdf.js 没有 CJK
 * 回退字体可用，浏览器沙箱的硬限制）。
 *
 * 内核那边用 PDFium（Chrome 看 PDF 就是它）渲染，自带 CJK 回退，同一份文件
 * 渲染出来是完整的。所以预览改成「服务端渲染成 PNG → 这里贴 <img>」。
 *
 * 保留的三件事（和原来一样）：
 * 1. **宽度按容器自适应**：页面逻辑宽 = min(容器可用宽, 页面原始宽)。
 * 2. **虚拟滚动**：只保留视口附近（上下各 1.5 屏）的页；滚远的页把 <img>
 *    摘掉（留同高占位，滚动条位置不乱），滚回去再挂上。否则滚得越多常驻的
 *    位图越多，大 PDF 能把内存吃到 GB 级。
 * 3. **占位高度用基准值**：同一份 PDF 页高基本一致，只按首页比例算，其余页
 *    直接建占位——不用等每页都请求完，「跳到第 500 页」也是瞬时的。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { apiGetPdfInfo, apiPdfPageUrl } from "../api/client";

const PAGE_BATCH = 200; // 每批创建占位的页数（首批同量）
const MAX_PAGES = 2000; // 预览硬上限：占位 DOM 节点数封顶
const ROOT_MARGIN = "150% 0px"; // 视口上下各 1.5 屏内保留渲染，之外卸载
const MAX_DPR = 2; // 高清屏最多按 2 倍渲染，再高只是白费带宽

export default function PdfViewer({
  name,
  path,
  jumpToPage,
}: {
  name: string;
  /** PDF 的绝对路径（服务端渲染端点按它取文件） */
  path: string;
  /**
   * 跳到第几页（1-based）。搜索结果点进来时用。
   * seq 递增——同一页连点两次也要重新跳，靠它触发 effect。
   */
  jumpToPage?: { page?: number; seq?: number } | null;
}) {
  const [pages, setPages] = useState(0);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  // 首页尺寸（pt）：占位高度按它算。
  const [pageSize, setPageSize] = useState<{ w: number; h: number } | null>(null);
  const hostRef = useRef<HTMLDivElement>(null);
  // 宿主容器宽度：页面逻辑宽按它算；面板 resize 时整体重排。
  const [hostW, setHostW] = useState(0);
  // 已创建占位的页数（滚到底续页就是加它）。
  const [builtCount, setBuiltCount] = useState(0);
  // 页码 → 占位 wrap。
  const wrapsRef = useRef<Map<number, HTMLDivElement>>(new Map());
  // 「文档+宽度」签名：变了才整体重建占位，否则只往后追加。
  const sigRef = useRef("");
  const ioRef = useRef<IntersectionObserver | null>(null);

  // 载入文档元信息
  useEffect(() => {
    let dead = false;
    (async () => {
      setLoading(true);
      setErr("");
      try {
        const info = await apiGetPdfInfo(path);
        if (dead) return;
        setPages(info.pages);
        setPageSize({ w: info.width, h: info.height });
      } catch (e: any) {
        if (!dead) setErr(e?.message || "PDF 解析失败");
      } finally {
        if (!dead) setLoading(false);
      }
    })();
    return () => {
      dead = true;
    };
  }, [path, name]);

  // 换文档：重置占位与缓存，先放第一批
  useEffect(() => {
    wrapsRef.current.clear();
    sigRef.current = "";
    setBuiltCount(pages ? Math.min(pages, PAGE_BATCH) : 0);
  }, [pages]);

  // 文档就绪（host 已挂上 DOM）后监听宽度：立即同步一次 + resize 重排。
  useEffect(() => {
    if (loading) return;
    const host = hostRef.current;
    if (!host) return;
    setHostW(Math.floor(host.clientWidth));
    const ro = new ResizeObserver((entries) => {
      const w = Math.floor(entries[0].contentRect.width);
      setHostW((prev) => (Math.abs(prev - w) > 1 ? w : prev));
    });
    ro.observe(host);
    return () => ro.disconnect();
  }, [loading]);

  // 组件卸载：收掉观察器
  useEffect(() => () => ioRef.current?.disconnect(), []);

  /** 跳到指定页：滚到那一页 + 闪一下。占位还没建好时什么也不做（稍后重试）。 */
  const tryJump = useCallback(() => {
    const p = jumpToPage?.page;
    if (!p || p < 1) return;
    const host = hostRef.current;
    const wrap = wrapsRef.current.get(p);
    if (!host || !wrap) return;
    // wrap 的 offsetTop 相对 host（host 是 relative）→ 直接就是可滚动距离
    const top = Math.max(0, wrap.offsetTop - host.clientHeight / 3);
    // 精确高度补齐后会再跳一次；位置没变就别再闪一遍高亮
    const moved = Math.abs(top - host.scrollTop) > 4;
    host.scrollTop = top;
    if (!moved) return;
    wrap.classList.remove("kp-page-hit");
    // 强制重排再重新加类：连点两次也能重新触发。
    // 不用 requestAnimationFrame——窗口不在前台时 rAF 不执行，高亮就永远加不上。
    void wrap.offsetWidth;
    wrap.classList.add("kp-page-hit");
    window.setTimeout(() => wrap.classList.remove("kp-page-hit"), 1800);
  }, [jumpToPage?.page, jumpToPage?.seq]);

  // 目标页在预览上限之外（MAX_PAGES）时只能作罢
  const jumpPage = jumpToPage?.page ?? 0;
  const jumpSeq = jumpToPage?.seq ?? 0;

  // 目标页还没建占位：先把占位建到那一页（否则无处可滚）。
  useEffect(() => {
    if (!jumpPage || !pages) return;
    if (jumpPage > builtCount) {
      setBuiltCount(Math.min(jumpPage, Math.min(pages, MAX_PAGES)));
    }
  }, [jumpPage, pages, builtCount]);

  // 建占位 wrap（增量）；签名变=整体重建。渲染交给下面的 IntersectionObserver。
  useEffect(() => {
    if (loading || !pages || !pageSize) return;
    let dead = false;
    const host = hostRef.current;
    if (!host) return;

    const avail = Math.max(120, (hostW || host.clientWidth) - 24);
    /* 页面逻辑宽：min(容器可用宽, 页面原始宽)。
     * 宽页面适配面板，窄页面不放大（免得糊）。 */
    const target = Math.min(avail, (pageSize.w * 96) / 72);
    /* 页面高：按首页比例算（同一份 PDF 页高基本一致）。
     * 这样建 2000 页占位也只是一瞬间，不必逐页量。 */
    const targetH = Math.round((target * pageSize.h) / pageSize.w);
    const dpr = Math.min(MAX_DPR, window.devicePixelRatio || 1);

    const sig = `${pages}:${target}`;
    if (sig !== sigRef.current) {
      // 重排：清空旧占位与旧观察器，按新宽度重建
      host.innerHTML = "";
      wrapsRef.current.clear();
      sigRef.current = sig;
      ioRef.current?.disconnect();
      ioRef.current = new IntersectionObserver(onIntersect, {
        root: host,
        rootMargin: ROOT_MARGIN,
      });
    }
    const io = ioRef.current;

    /* 把一页贴进它的占位 wrap（已渲染 / 渲染中 / 期间被卸载 都直接跳过）。 */
    const mountIfNeeded = (wrap: HTMLDivElement, p: number) => {
      if (wrap.dataset.busy || wrap.querySelector("img")) return;
      wrap.dataset.busy = "1";
      const img = document.createElement("img");
      img.alt = `第 ${p} 页`;
      img.decoding = "async";
      img.className = "rounded border border-line-soft bg-white shadow-card";
      img.style.width = target + "px";
      img.style.height = targetH + "px";
      img.addEventListener("load", () => {
        delete wrap.dataset.busy;
      });
      img.addEventListener("error", () => {
        delete wrap.dataset.busy;
        // 这一页渲染不出来：留个占位说明，别让用户以为是空白页
        const tip = document.createElement("div");
        tip.className =
          "flex items-center justify-center rounded border border-line-soft bg-white text-[12px] text-ink-faint";
        tip.style.width = target + "px";
        tip.style.height = targetH + "px";
        tip.textContent = `第 ${p} 页渲染失败`;
        wrap.appendChild(tip);
      });
      img.src = apiPdfPageUrl(path, p, target * dpr);
      wrap.appendChild(img);
    };

    // 卸载：摘掉 img 释放位图；wrap 的显式高度撑住滚动条，位置不乱。
    const unmount = (wrap: HTMLDivElement) => {
      const el = wrap.querySelector("img");
      if (el) el.remove();
    };

    function onIntersect(entries: IntersectionObserverEntry[]) {
      for (const e of entries) {
        const wrap = e.target as HTMLDivElement;
        const p = Number(wrap.dataset.page);
        if (e.isIntersecting) mountIfNeeded(wrap, p);
        else unmount(wrap);
      }
    }

    const end = Math.min(builtCount, pages, MAX_PAGES);
    /**
     * 给第 p 页建占位 wrap。**不等渲染**：高度按页面比例直接算好，
     * 所以建 2000 页占位也只是一瞬间。
     */
    const createWrap = (p: number) => {
      if (wrapsRef.current.has(p)) return;
      const wrap = document.createElement("div");
      wrap.className = "mb-3 flex justify-center";
      wrap.style.height = targetH + "px";
      wrap.dataset.page = String(p);
      wrapsRef.current.set(p, wrap);
      host.appendChild(wrap);
      io?.observe(wrap);
    };

    for (let p = 1; p <= end; p++) {
      if (dead) return;
      createWrap(p);
    }
    // 占位齐了（瞬间）→ 跳
    tryJump();
    return () => {
      dead = true;
    };
  }, [
    pages,
    loading,
    hostW,
    builtCount,
    tryJump,
    jumpSeq,
    jumpPage,
    pageSize,
    path,
  ]);

  // 滚到底部附近：再建一批占位（新占位进入缓冲范围会被 IO 自动渲染）。
  useEffect(() => {
    if (loading || !pages) return;
    const host = hostRef.current;
    if (!host) return;
    const onScroll = () => {
      if (host.scrollTop + host.clientHeight >= host.scrollHeight - 400) {
        setBuiltCount((c) =>
          Math.min(c + PAGE_BATCH, Math.min(pages, MAX_PAGES))
        );
      }
    };
    host.addEventListener("scroll", onScroll, { passive: true });
    return () => host.removeEventListener("scroll", onScroll);
  }, [loading, pages]);

  const capped = builtCount >= MAX_PAGES && pages > MAX_PAGES;
  // 目标页在预览上限之外：跳不过去，得说清楚，而不是让人干瞪眼
  const jumpOutOfRange = jumpPage > MAX_PAGES;

  if (loading)
    return (
      <div className="flex h-full items-center justify-center text-[13px] text-ink-muted">
        加载 PDF…
      </div>
    );
  if (err)
    return <div className="p-4 text-[13px] text-red-500">无法预览该 PDF：{err}</div>;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 border-b border-line-soft px-3 py-1.5 text-[11px] text-ink-faint">
        {name} · 共 {pages} 页
        {pages > builtCount
          ? capped
            ? `（仅预览前 ${MAX_PAGES} 页，更多请下载或用系统查看器）`
            : `（已加载前 ${builtCount} 页，滚动到底部继续）`
          : "（已全部加载）"}
        {jumpOutOfRange && (
          <span className="ml-1.5 text-amber-600 dark:text-amber-400">
            （命中在第 {jumpPage} 页，超出前 {MAX_PAGES} 页的预览范围，无法跳转）
          </span>
        )}
      </div>
      <div
        ref={hostRef}
        className="relative min-h-0 flex-1 overflow-auto bg-black/[0.04] p-3"
      />
    </div>
  );
}
