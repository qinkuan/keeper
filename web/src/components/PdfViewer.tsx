import { useCallback, useEffect, useRef, useState } from "react";
import * as pdfjsLib from "pdfjs-dist";
// Vite 以 URL 方式引入 worker（?url 由 vite/client 类型支持），避免运行时找不到 worker。
import PdfWorker from "pdfjs-dist/build/pdf.worker.min.mjs?url";

pdfjsLib.GlobalWorkerOptions.workerSrc = PdfWorker;

/**
 * 「我的空间」PDF 预览：pdfjs-dist 渲染到 canvas，纵向滚动翻页。
 *
 * 三个关键设计：
 * 1. **宽度按容器自适应**（fit-width）：固定 scale 会让宽页面超出面板，被 flex 居中后
 *    左侧溢出被裁——表现为「左边一条展示不到」。
 * 2. **虚拟滚动**：只渲染视口附近（rootMargin ±1.5 屏）的页；滚远的页卸掉 canvas（留
 *    同高占位，滚动条位置不乱），滚回去再按需重绘。否则滚得越多常驻 canvas 越多，
 *    大 PDF 能把内存吃到 GB 级——这正是「已加载的页面能否卸载」要解决的问题。
 * 3. **占位高度用基准值，不逐页量**：同一份 PDF 的页高基本一致（幻灯片 PDF 完全一致），
 *    所以只量第 1 页，其余页的占位直接用这个高度建出来——**不用等 getPage**。
 *    这样「跳到第 500 页」也是瞬时的：先建占位（瞬间），再后台补齐精确高度。
 * 4. **滚动续页**：占位先建一批，滚到底附近再放一批；占位很便宜（一个 div），
 *    但 DOM 节点不能无限多，MAX_PAGES 封顶，超出引导下载 / 用系统查看器。
 */

const PAGE_BATCH = 200; // 每批创建占位的页数（首批同量）
const MAX_PAGES = 2000; // 预览硬上限：占位 DOM 节点数封顶
const ROOT_MARGIN = "150% 0px"; // 视口上下各 1.5 屏内保留渲染，之外卸载

export default function PdfViewer({
  name,
  loadBytes,
  jumpToPage,
}: {
  name: string;
  loadBytes: () => Promise<ArrayBuffer>;
  /**
   * 跳到第几页（1-based）。搜索结果点进来时用。
   * seq 递增——同一页连点两次也要重新跳，靠它触发 effect。
   */
  jumpToPage?: { page?: number; seq?: number } | null;
}) {
  const [pages, setPages] = useState(0);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const pdfRef = useRef<pdfjsLib.PDFDocumentProxy | null>(null);
  const hostRef = useRef<HTMLDivElement>(null);
  // 宿主容器宽度：占位高度按它算；面板 resize 时整体重排。
  const [hostW, setHostW] = useState(0);
  // 已创建占位的页数（滚到底续页就是加它）。
  const [builtCount, setBuiltCount] = useState(0);
  // 页码 → 占位 wrap；页码 → PDFPageProxy（proxy 不持有位图，缓存可减小反复 getPage 开销）。
  const wrapsRef = useRef<Map<number, HTMLDivElement>>(new Map());
  // 各页的**精确**内容高度（px）。没量过的页用 baseHeight 兜底。
  const heightsRef = useRef<Map<number, number>>(new Map());
  // 基准页高：量第 1 页得到，同一份 PDF 的页高基本一致，其余页直接用它建占位——
  // 这样建 2000 页占位也只是一瞬间，不必串行 getPage 2000 次。
  const baseHeightRef = useRef<number>(0);
  const pageCacheRef = useRef<Map<number, pdfjsLib.PDFPageProxy>>(new Map());
  // 「文档+宽度」签名：变了才整体重建占位，否则只往后追加。
  const sigRef = useRef("");
  const ioRef = useRef<IntersectionObserver | null>(null);

  // 载入文档
  useEffect(() => {
    let dead = false;
    (async () => {
      setLoading(true);
      setErr("");
      try {
        const buf = await loadBytes();
        const doc = await pdfjsLib.getDocument({ data: buf }).promise;
        if (dead) {
          doc.destroy();
          return;
        }
        pdfRef.current = doc;
        setPages(doc.numPages);
      } catch (e: any) {
        if (!dead) setErr(e?.message || "PDF 解析失败");
      } finally {
        if (!dead) setLoading(false);
      }
    })();
    return () => {
      dead = true;
      pdfRef.current?.destroy();
      pdfRef.current = null;
    };
  }, [name]);

  // 换文档：重置占位与缓存，先放第一批
  useEffect(() => {
    wrapsRef.current.clear();
    pageCacheRef.current.clear();
    heightsRef.current.clear();
    baseHeightRef.current = 0;
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
  // 占位现在很便宜（一个 div，不用等 getPage），所以允许直接建到目标页——
  // 200 页、2000 页都一样是瞬时的。
  useEffect(() => {
    if (!jumpPage || !pages) return;
    if (jumpPage > builtCount) {
      setBuiltCount(Math.min(jumpPage, Math.min(pages, MAX_PAGES)));
    }
  }, [jumpPage, pages, builtCount]);

  // 建占位 wrap（增量）；签名变=整体重建。渲染交给下面的 IntersectionObserver。
  useEffect(() => {
    if (loading || !pages) return;
    let dead = false;
    const host = hostRef.current;
    const doc = pdfRef.current;
    if (!host || !doc) return;

    const avail = Math.max(120, (hostW || host.clientWidth) - 24);

    /** 第 p 页的内容高度（px）：已知精确值用精确值，否则用基准页高。 */
    const heightOf = (p: number) =>
      heightsRef.current.get(p) ?? (baseHeightRef.current || 0);

    /**
     * 把某页占位的高度改成真实高度，并**补偿滚动位置**。
     * 改的是视口上方的页时，下面整体移位了，视口也要跟着移同样的距离，
     * 否则用户会看到内容"跳一下"。
     */
    const applyHeight = (wrap: HTMLDivElement, realH: number) => {
      const oldH = parseFloat(wrap.style.height || "0") || 0;
      if (Math.abs(oldH - realH) < 0.5) return;
      const wasAbove = wrap.offsetTop < host.scrollTop;
      wrap.style.height = realH + "px";
      if (wasAbove) host.scrollTop += realH - oldH;
    };

    // 把一页画进它的占位 wrap（已渲染 / 渲染中 / 期间被卸载 都直接跳过）。
    const renderIfNeeded = async (wrap: HTMLDivElement, p: number) => {
      if (wrap.dataset.busy || wrap.querySelector("canvas")) return;
      wrap.dataset.busy = "1";
      try {
        const page = await getPageCached(doc, p);
        const base = page.getViewport({ scale: 1 });
        const dpr = window.devicePixelRatio || 1;
        // 逻辑宽 = min(面板可用宽, 页面原始宽)：宽页面适配面板，窄页面不放大（免糊）。
        const target = Math.min(avail, base.width);
        const scale = (target * dpr) / base.width;
        const viewport = page.getViewport({ scale });
        const canvas = document.createElement("canvas");
        canvas.width = Math.floor(viewport.width);
        canvas.height = Math.floor(viewport.height);
        canvas.style.width = target + "px";
        canvas.style.height =
          Math.floor((target * base.height) / base.width) + "px";
        canvas.className =
          "rounded border border-line-soft bg-white shadow-card";
        await page.render({
          canvasContext: canvas.getContext("2d")!,
          viewport,
        }).promise;
        // 渲染期间被卸载 / 重排：丢掉结果，不往旧位置塞
        if (!wrap.isConnected || wrap.querySelector("canvas")) return;
        // 真实高度可能和占位（基准）高度不同 → 修正占位 + 补偿滚动位置
        applyHeight(wrap, Math.floor((target * base.height) / base.width));
        wrap.appendChild(canvas);
      } catch {
        // 渲染被打断（卸载 / 切文件），属正常
      } finally {
        delete wrap.dataset.busy;
      }
    };

    // 卸载：移除 canvas 释放位图；wrap 的显式高度撑住滚动条，位置不乱。
    const unloadWrap = (wrap: HTMLDivElement) => {
      const c = wrap.querySelector("canvas");
      if (c) c.remove();
    };

    const onIntersect: IntersectionObserverCallback = (entries) => {
      for (const e of entries) {
        const wrap = e.target as HTMLDivElement;
        const p = Number(wrap.dataset.page);
        if (e.isIntersecting) void renderIfNeeded(wrap, p);
        else unloadWrap(wrap);
      }
    };

    const sig = `${pages}:${hostW}`;
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

    const getPageCached = (d: pdfjsLib.PDFDocumentProxy, p: number) => {
      const cached = pageCacheRef.current.get(p);
      if (cached) return Promise.resolve(cached);
      return d.getPage(p).then((pg) => {
        pageCacheRef.current.set(p, pg);
        return pg;
      });
    };

    const end = Math.min(builtCount, pages, MAX_PAGES);
    /**
     * 给第 p 页建占位 wrap。**不等 getPage**：高度先用基准页高，
     * 真实高度后台补齐时再修正。这样建 2000 页占位也只是一瞬间。
     */
    const createWrap = (p: number) => {
      if (wrapsRef.current.has(p)) return;
      const wrap = document.createElement("div");
      wrap.className = "mb-3 flex justify-center";
      wrap.style.height = heightOf(p) + "px";
      wrap.dataset.page = String(p);
      wrapsRef.current.set(p, wrap);
      host.appendChild(wrap);
      io?.observe(wrap);
    };
    /** 后台补齐各页的精确高度（并发），并修正已建好的占位。 */
    const refineHeights = async (from: number, to: number) => {
      const BATCH = 32;
      for (let s = from; s <= to; s += BATCH) {
        if (dead) return;
        const ps: number[] = [];
        for (let p = s; p <= Math.min(s + BATCH - 1, to); p++) ps.push(p);
        await Promise.all(
          ps.map(async (p) => {
            if (heightsRef.current.has(p)) return;
            const pg = await getPageCached(doc, p);
            const base = pg.getViewport({ scale: 1 });
            const realH = Math.floor(
              (Math.min(avail, base.width) * base.height) / base.width
            );
            heightsRef.current.set(p, realH);
            const w = wrapsRef.current.get(p);
            if (w) applyHeight(w, realH);
          })
        );
      }
    };
    const ensure = async () => {
      // 基准页高：只量第 1 页（同一份 PDF 页高基本一致），
      // 其余页直接用它建占位 → 「跳到第 500 页」也是瞬时的。
      if (!baseHeightRef.current) {
        const pg = await getPageCached(doc, 1);
        if (dead) return;
        const base = pg.getViewport({ scale: 1 });
        const h = Math.floor(
          (Math.min(avail, base.width) * base.height) / base.width
        );
        baseHeightRef.current = h;
        heightsRef.current.set(1, h);
      }
      // 建到 builtCount 为止（builtCount 已经保证 >= 目标页，见上面的 effect）
      const limit = end;
      for (let p = 1; p <= limit; p++) {
        if (dead) return;
        createWrap(p);
      }
      // 占位齐了（瞬间）→ 跳
      tryJump();
      // 后台补齐精确高度（不阻塞跳转）；补齐后落点可能偏一点，再精确跳一次
      await refineHeights(1, limit);
      tryJump();
    };
    void ensure();
    return () => {
      dead = true;
    };
  }, [pages, loading, hostW, builtCount, tryJump, jumpSeq, jumpPage]);

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
