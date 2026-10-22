import { useEffect, useRef, useState } from "react";
import * as pdfjsLib from "pdfjs-dist";
// Vite 以 URL 方式引入 worker（?url 由 vite/client 类型支持），避免运行时找不到 worker。
import PdfWorker from "pdfjs-dist/build/pdf.worker.min.mjs?url";

pdfjsLib.GlobalWorkerOptions.workerSrc = PdfWorker;

/**
 * 「我的空间」PDF 预览：pdfjs-dist 渲染到 canvas，纵向滚动翻页。
 *
 * 之前 PDF 是「在新窗口打开」（keeperfile:// 交给系统查看器）；这里改成内联，和表格
 * 预览一致的体验。只读预览：页数封顶，超大 PDF 仍建议下载。
 */

const MAX_PAGES = 50;
const SCALE = 1.2;

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

  // 文档就绪后把各页渲染进 canvas（封顶 MAX_PAGES）
  useEffect(() => {
    if (loading || !pages) return;
    let dead = false;
    const host = hostRef.current;
    const doc = pdfRef.current;
    if (!host || !doc) return;
    host.innerHTML = "";
    const dpr = window.devicePixelRatio || 1;
    const renderAll = async () => {
      const max = Math.min(pages, MAX_PAGES);
      for (let p = 1; p <= max; p++) {
        if (dead) return;
        const page = await doc.getPage(p);
        // 逻辑宽度固定，按 DPR 提高位图分辨率，避免高分屏发虚
        const viewport = page.getViewport({ scale: SCALE * dpr });
        const wrap = document.createElement("div");
        wrap.className = "mb-3 flex justify-center";
        const canvas = document.createElement("canvas");
        canvas.width = viewport.width;
        canvas.height = viewport.height;
        canvas.style.width = viewport.width / dpr + "px";
        canvas.style.height = viewport.height / dpr + "px";
        canvas.className = "rounded border border-line-soft bg-white shadow-card";
        wrap.appendChild(canvas);
        host.appendChild(wrap);
        await page.render({ canvasContext: canvas.getContext("2d")!, viewport }).promise;
      }
    };
    renderAll();
    return () => {
      dead = true;
    };
  }, [pages, loading]);

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
        {name} · {pages} 页{pages > MAX_PAGES ? `（仅预览前 ${MAX_PAGES} 页）` : ""}
      </div>
      <div
        ref={hostRef}
        className="min-h-0 flex-1 overflow-auto bg-black/[0.04] p-3"
      />
    </div>
  );
}
