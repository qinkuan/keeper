import { useCallback, useEffect, useRef, useState } from "react";

const BTN =
  "inline-flex h-6 items-center gap-1 rounded-md border border-line px-1.5 text-[12px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong";

// 滚轮/捏合缩放灵敏度：越小越「钝」。约 130 像素滚动 ≈ 缩放 12%。
// 旧实现固定每次 ±10%，触控板捏合连续冒小事件会累加失控，这里改成按幅度指数映射。
const ZOOM_SENSITIVITY = 0.0009;

interface PanZoomStageProps {
  children: React.ReactNode;
  height?: number;
  hint?: React.ReactNode;
}

/**
 * 通用缩放/平移画布：滚轮或触控板捏合缩放（指向哪放大哪）、拖拽上下左右平移、
 * 双击放大（已放大则复位到 fit）。内容任意（mermaid 的 SVG、vega 图表、<img> 等）。
 *
 * 首次量到内容尺寸后自动按容器 fit，使整张默认可见；之后靠 ResizeObserver
 * 捕捉异步内容（vega 渲染、图片加载、mermaid 注入）的尺寸变化重算。
 */
export default function PanZoomStage({ children, height = 360, hint }: PanZoomStageProps) {
  const [zoom, setZoom] = useState(1);
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const [pan, setPan] = useState<{ x: number; y: number }>({ x: 0, y: 0 });

  const containerRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const didFitRef = useRef(false);
  const dragState = useRef({ active: false, sx: 0, sy: 0, px: 0, py: 0 });

  // 供原生事件闭包读取最新值（事件在 effect 里绑定，闭包捕获不到最新 state）
  const zoomRef = useRef(zoom);
  zoomRef.current = zoom;
  const panRef = useRef(pan);
  panRef.current = pan;
  const naturalRef = useRef(natural);
  naturalRef.current = natural;
  const fitZoomRef = useRef(1);

  // 平移限制在「内容不拖出容器」的范围；内容比容器小则居中
  const clampPan = useCallback(
    (x: number, y: number, z: number) => {
      const nw = naturalRef.current?.w ?? 0;
      const nh = naturalRef.current?.h ?? 0;
      // 自然尺寸未知时不做任何约束，否则会被强制居中到右下角（stageW=0<=cw）
      if (!nw || !nh) return { x, y };
      const stageW = nw * z;
      const stageH = nh * z;
      const cw = containerRef.current?.clientWidth ?? stageW;
      const ch = height;
      const nx = stageW <= cw ? (cw - stageW) / 2 : Math.min(0, Math.max(cw - stageW, x));
      const ny = stageH <= ch ? (ch - stageH) / 2 : Math.min(0, Math.max(ch - stageH, y));
      return { x: nx, y: ny };
    },
    [height],
  );

  // 以容器内某点（cx,cy）为锚点缩放，保持该点下的内容不动
  const zoomAt = useCallback(
    (cx: number, cy: number, factor: number) => {
      const z1 = zoomRef.current;
      const z2 = Math.min(5, Math.max(0.2, z1 * factor));
      if (z2 === z1) return;
      const p = panRef.current;
      const contentX = (cx - p.x) / z1;
      const contentY = (cy - p.y) / z1;
      setZoom(z2);
      setPan(clampPan(cx - contentX * z2, cy - contentY * z2, z2));
    },
    [clampPan],
  );

  // 复位到 fit 视图（整张内容居中可见）
  const resetView = useCallback(() => {
    const fz = fitZoomRef.current;
    setZoom(fz);
    const nw = naturalRef.current?.w ?? 0;
    const nh = naturalRef.current?.h ?? 0;
    const cw = containerRef.current?.clientWidth ?? 0;
    setPan(clampPan((cw - nw * fz) / 2, (height - nh * fz) / 2, fz));
  }, [clampPan, height]);

  // 量内容自然尺寸：优先取 svg/img/canvas 本身，回退到包裹层。
  // 注意：SVG 元素没有可靠的 offsetWidth，必须用 getBoundingClientRect，
  // 再除以当前 zoom 还原自然尺寸（rect 含祖先 transform 的缩放）。
  const measure = useCallback(() => {
    const wrap = contentRef.current;
    const cw = containerRef.current?.clientWidth ?? 0;
    if (!wrap || !cw) return;
    const el = (wrap.querySelector("svg, img, canvas") as HTMLElement | null) ?? wrap;
    const rect = el.getBoundingClientRect();
    const z = zoomRef.current || 1;
    const w = rect.width / z;
    const h = rect.height / z;
    if (!w || !h || !isFinite(w) || !isFinite(h)) return;
    setNatural({ w, h });
    if (!didFitRef.current) {
      const fit = Math.max(0.2, Math.min(1, cw / w, height / h));
      fitZoomRef.current = fit;
      setZoom(fit);
      setPan({ x: (cw - w * fit) / 2, y: (height - h * fit) / 2 });
      didFitRef.current = true;
    }
  }, [height]);

  // 挂载后量一次；之后内容尺寸变化由 ResizeObserver 捕捉
  useEffect(() => {
    const wrap = contentRef.current;
    if (!wrap) return;
    measure();
    const ro = new ResizeObserver(() => measure());
    ro.observe(wrap);
    return () => ro.disconnect();
  }, [measure]);

  // 缩放或内容尺寸变化时，重新把平移约束在可视范围内
  useEffect(() => {
    setPan((p) => clampPan(p.x, p.y, zoom));
  }, [zoom, natural, clampPan]);

  // 滚轮缩放（含触控板捏合——其本质也是 wheel 事件）。
  // 用指数映射 + deltaMode 归一：幅度越大缩放越多（与地图/图片查看器一致手感），
  // 小幅度滚动只微调，避免「固定 10%/次」在触控板连续小事件下累加失控。
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      // deltaMode 1=行、2=页，统一换算成像素量级，跨浏览器一致
      let dy = e.deltaY;
      if (e.deltaMode === 1) dy *= 16;
      else if (e.deltaMode === 2) dy *= 100;
      // 指数映射：正负对称、手感线性；系数 ZOOM_SENSITIVITY 越小越「钝」
      const factor = Math.exp(-dy * ZOOM_SENSITIVITY);
      const rect = el.getBoundingClientRect();
      zoomAt(e.clientX - rect.left, e.clientY - rect.top, factor);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [zoomAt]);

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    dragState.current = {
      active: true,
      sx: e.clientX,
      sy: e.clientY,
      px: pan.x,
      py: pan.y,
    };
    e.currentTarget.setPointerCapture(e.pointerId);
    e.currentTarget.style.cursor = "grabbing";
  };
  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = dragState.current;
    if (!d.active) return;
    setPan(clampPan(d.px + (e.clientX - d.sx), d.py + (e.clientY - d.sy), zoom));
  };
  const endDrag = (e: React.PointerEvent<HTMLDivElement>) => {
    dragState.current.active = false;
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* noop */
    }
    e.currentTarget.style.cursor = "grab";
  };

  // 双击：在 fit 视图下放大（锚定光标）；已放大则复位到 fit
  const onDoubleClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const el = containerRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const cx = e.clientX - rect.left;
    const cy = e.clientY - rect.top;
    if (zoomRef.current <= fitZoomRef.current * 1.05) {
      zoomAt(cx, cy, 1.4);
    } else {
      resetView();
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column" }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          padding: "4px 8px",
          borderBottom: "1px solid #eee",
        }}
      >
        <button className={BTN} onClick={() => setZoom((z) => Math.max(0.2, z * 0.9))}>
          －
        </button>
        <span
          style={{
            minWidth: 44,
            textAlign: "center",
            fontSize: 12,
            color: "var(--kp-text-muted)",
          }}
        >
          {Math.round(zoom * 100)}%
        </span>
        <button className={BTN} onClick={() => setZoom((z) => Math.min(5, z * 1.1))}>
          ＋
        </button>
        <button className={BTN} onClick={resetView}>
          重置
        </button>
        <span
          style={{
            marginLeft: "auto",
            fontSize: 11,
            color: "var(--kp-text-muted)",
          }}
        >
          {hint ?? "滚轮/捏合缩放 · 拖拽平移 · 双击放大(已放大双击复位)"}
        </span>
      </div>
      <div
        ref={containerRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerLeave={endDrag}
        onPointerCancel={endDrag}
        onDoubleClick={onDoubleClick}
        style={{
          height,
          overflow: "hidden",
          background: "#fff",
          cursor: "grab",
          userSelect: "none",
          touchAction: "none",
        }}
      >
        <div
          style={{
            width: natural ? natural.w * zoom : "100%",
            height: natural ? natural.h * zoom : "auto",
            position: "relative",
          }}
        >
          <div
            ref={contentRef}
            style={{
              transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
              transformOrigin: "top left",
              // 明确按自然尺寸撑开内容盒，避免 inline-block 的 shrink-to-fit 把
              // SVG 宽高重新解析掉（原 MermaidBlock 即如此，缩放/量测都稳定）
              width: natural ? natural.w : "100%",
              height: natural ? natural.h : "auto",
            }}
          >
            {children}
          </div>
        </div>
      </div>
    </div>
  );
}
