import { useEffect, useRef, useState } from "react";
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
 * 3. **滚动续页**：不一次性建全部占位（那也是几百次 getPage），首批 50 页，滚到底
 *    附近再放一批；MAX_PAGES 封顶，超出引导下载 / 用系统查看器。
 */

const PAGE_BATCH = 50; // 每批创建占位的页数（首批同量）
const MAX_PAGES = 200; // 预览硬上限：占位与缓存随页数线性涨，见模块注释
const ROOT_MARGIN = "150% 0px"; // 视口上下各 1.5 屏内保留渲染，之外卸载

export default function PdfViewer({
  name,
  loadBytes,
}: {
  name: string;
  loadBytes: () => Promise<ArrayBuffer>;
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

  // 建占位 wrap（增量）；签名变=整体重建。渲染交给下面的 IntersectionObserver。
  useEffect(() => {
    if (loading || !pages) return;
    let dead = false;
    const host = hostRef.current;
    const doc = pdfRef.current;
    if (!host || !doc) return;

    const avail = Math.max(120, (hostW || host.clientWidth) - 24);

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
    const ensure = async () => {
      for (let p = 1; p <= end; p++) {
        if (dead) return;
        if (wrapsRef.current.has(p)) continue;
        const page = await getPageCached(doc, p);
        const base = page.getViewport({ scale: 1 });
        const target = Math.min(avail, base.width);
        const wrap = document.createElement("div");
        wrap.className = "mb-3 flex justify-center";
        wrap.style.height =
          Math.floor((target * base.height) / base.width) + "px";
        wrap.dataset.page = String(p);
        wrapsRef.current.set(p, wrap);
        host.appendChild(wrap);
        io?.observe(wrap);
      }
    };
    void ensure();
    return () => {
      dead = true;
    };
  }, [pages, loading, hostW, builtCount]);

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
      </div>
      <div
        ref={hostRef}
        className="min-h-0 flex-1 overflow-auto bg-black/[0.04] p-3"
      />
    </div>
  );
}
