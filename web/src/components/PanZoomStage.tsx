import { useCallback, useEffect, useRef, useState } from "react";

const BTN =
  "inline-flex h-6 items-center gap-1 rounded-md border border-line px-1.5 text-[12px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong";

// 滚轮/捏合缩放灵敏度：越小越「钝」。约 130 像素滚动 ≈ 缩放 12%。
// 旧实现固定每次 ±10%，触控板捏合连续冒小事件会累加失控，这里改成按幅度指数映射。
const ZOOM_SENSITIVITY = 0.0009;

interface PanZoomStageProps {
  children: React.ReactNode;
  /** 画布高度：数字 = 固定像素；"100%" = 填满父容器（父容器需要有确定高度） */
  height?: number | "100%";
  hint?: React.ReactNode;
}

/**
 * 通用缩放/平移画布：滚轮或触控板捏合缩放（指向哪放大哪）、拖拽上下左右平移、
 * 双击放大（已放大则复位到 fit）。内容任意（mermaid 的 SVG、vega 图表、<img> 等）。
 *
 * 首次量到内容尺寸后自动按容器 fit，使整张默认可见；之后靠 ResizeObserver
 * 捕捉异步内容（vega 渲染、图片加载、mermaid 注入）的尺寸变化重算。
 *
 * **两个尺寸都要盯**：
 * - 内容（wrap）：vega 异步渲染、图片加载完成 → 内容尺寸变了，要重新 fit；
 * - 容器：面板被拖宽、窗口缩放 → 内容虽然没变，但"居中"是相对容器的算的，
 *   不重新居中的话，图会停在旧的位置上（表现为偏到一边）。
 */
export default function PanZoomStage({ children, height = 360, hint }: PanZoomStageProps) {
  const [zoom, setZoom] = useState(1);
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const [pan, setPan] = useState<{ x: number; y: number }>({ x: 0, y: 0 });

  const containerRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const didFitRef = useRef(false);
  const dragState = useRef({ active: false, sx: 0, sy: 0, px: 0, py: 0 });
  /** 用户手动缩放/平移过：之后内容或容器再变，也不再自动重新 fit（别把人正在看的视角抢走） */
  const userAdjustedRef = useRef(false);
  /** 上一次量到的容器宽度/高度，用来判断"容器有没有变" */
  const lastBoxRef = useRef<{ w: number; h: number } | null>(null);

  const fillHeight = typeof height === "string";

  // 供原生事件闭包读取最新值（事件在 effect 里绑定，闭包捕获不到最新 state）
  const zoomRef = useRef(zoom);
  zoomRef.current = zoom;
  const panRef = useRef(pan);
  panRef.current = pan;
  const naturalRef = useRef(natural);
  naturalRef.current = natural;
  const fitZoomRef = useRef(1);

  /** 画布的实际高度：固定像素时就是 height；填满时取容器的 clientHeight */
  const stageHeight = useCallback(() => {
    const ch = containerRef.current?.clientHeight ?? 0;
    if (ch) return ch;
    return typeof height === "number" ? height : 0;
  }, [height]);

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
      const ch = stageHeight();
      const nx = stageW <= cw ? (cw - stageW) / 2 : Math.min(0, Math.max(cw - stageW, x));
      const ny = stageH <= ch ? (ch - stageH) / 2 : Math.min(0, Math.max(ch - stageH, y));
      return { x: nx, y: ny };
    },
    [stageHeight],
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
    const ch = stageHeight();
    userAdjustedRef.current = false;
    setPan(clampPan((cw - nw * fz) / 2, (ch - nh * fz) / 2, fz));
  }, [clampPan, stageHeight]);

  // 量内容自然尺寸：**只认真正的画布元素**（svg/img/canvas）。
  //
  // 为什么不能回退到包裹层（老代码是 `?? wrap`）：
  //   内容还没渲染出来时，包裹层是一个空的 inline-block，行盒（strut）会撑出
  //   ~18px 的「假高度」。一旦把这个假尺寸当成自然尺寸写进 state，
  //   包裹层就会被固定成 18px 高 → 内容（svg）到达时包裹层尺寸**不再变化** →
  //   ResizeObserver 不再触发 → 首次 fit 的错误结果被永久锁死（表现就是图偏在右下角，
  //   只有拖动面板让容器尺寸变化才会重新居中）。
  // 所以：找不到画布元素就直接放弃这次量测，等 RO/MutationObserver 再叫。
  //
  // 另外 SVG 没有可靠的 offsetWidth，必须用 getBoundingClientRect，
  // 再除以当前 zoom 还原自然尺寸（rect 含祖先 transform 的缩放）。
  const measure = useCallback(() => {
    const wrap = contentRef.current;
    const cw = containerRef.current?.clientWidth ?? 0;
    const ch = stageHeight();
    if (!wrap || !cw || !ch) return;
    const el = wrap.querySelector("svg, img, canvas") as HTMLElement | null;
    if (!el) return; // 内容还没渲染出来：等下一次通知
    const rect = el.getBoundingClientRect();
    const z = zoomRef.current || 1;
    const w = rect.width / z;
    const h = rect.height / z;
    if (!w || !h || !isFinite(w) || !isFinite(h)) return;

    // 内容尺寸变了吗？容器尺寸变了吗？
    // 两者都会让"居中"失效 —— 居中是相对容器算的，容器一变就得重算，
    // 否则图会停在旧位置（看起来就是"偏到一边/偏右下角"）。
    const prevNat = naturalRef.current;
    const prevBox = lastBoxRef.current;
    const natChanged =
      !prevNat || Math.abs(prevNat.w - w) > 0.5 || Math.abs(prevNat.h - h) > 0.5;
    const boxChanged = !prevBox || Math.abs(prevBox.w - cw) > 0.5 || Math.abs(prevBox.h - ch) > 0.5;
    lastBoxRef.current = { w: cw, h: ch };
    naturalRef.current = { w, h };
    if (!prevNat || prevNat.w !== w || prevNat.h !== h) setNatural({ w, h });

    if (!didFitRef.current || ((natChanged || boxChanged) && !userAdjustedRef.current)) {
      const fit = Math.max(0.2, Math.min(1, cw / w, ch / h));
      fitZoomRef.current = fit;
      setZoom(fit);
      setPan({ x: (cw - w * fit) / 2, y: (ch - h * fit) / 2 });
      didFitRef.current = true;
    }
  }, [stageHeight]);

  // 挂载后量一次；之后内容尺寸变化由 ResizeObserver 捕捉
  useEffect(() => {
    const wrap = contentRef.current;
    const cont = containerRef.current;
    if (!wrap || !cont) return;
    measure();
    const ro = new ResizeObserver(() => measure());
    ro.observe(wrap); // 内容尺寸变了：vega 异步渲染、图片加载完成
    ro.observe(cont); // 容器尺寸变了：面板拖宽、窗口缩放 → 要重新居中
    // 兜底：vega/mermaid 是异步注入画布元素的，注入瞬间包裹层尺寸可能没变，
    // 只靠 ResizeObserver 会漏掉这一帧 —— 用 MutationObserver 盯一下子节点变化。
    const mo = new MutationObserver(() => measure());
    mo.observe(wrap, { childList: true, subtree: true });
    return () => {
      ro.disconnect();
      mo.disconnect();
    };
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
      // 用户开始自己控制视角了 → 之后内容/容器再变也不自动重新 fit
      userAdjustedRef.current = true;
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
    userAdjustedRef.current = true;
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
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        // height="100%" 时整块填满父容器（父容器需要有确定高度）：
        // 画布高度跟着可用空间走，图才会落在**可见区域**的正中间，
        // 而不是固定 520px 画布里居中、结果整体偏到下面去。
        ...(fillHeight ? { height: "100%", minHeight: 0 } : {}),
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          padding: "4px 8px",
          borderBottom: "1px solid #eee",
          flex: "0 0 auto",
        }}
      >
        <button
          className={BTN}
          onClick={() => {
            userAdjustedRef.current = true;
            setZoom((z) => Math.max(0.2, z * 0.9));
          }}
        >
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
        <button
          className={BTN}
          onClick={() => {
            userAdjustedRef.current = true;
            setZoom((z) => Math.min(5, z * 1.1));
          }}
        >
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
          ...(fillHeight
            ? { flex: "1 1 auto", minHeight: 0 }
            : { height: typeof height === "number" ? height : undefined }),
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
