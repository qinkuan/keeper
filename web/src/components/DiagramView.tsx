import { useEffect, useRef, useState } from "react";

import PanZoomStage from "./PanZoomStage";

/**
 * 图渲染直渲：Vega / Vega-Lite 图表（ChartView）与 Mermaid 流程图（MermaidView）。
 *
 * 为什么单独一个文件
 * ------------------
 * 会话制品卡片（BlockView）、Markdown 内联代码块（MarkdownView）、「我的空间」
 * 文件预览（UserSpacePage）三处都要把「图的描述」变成「图」，但**容器**和
 * **失败处理**完全不同：制品卡片里要错误浮层，文件预览里要降级回源码编辑器。
 * 所以这里只负责渲染，错误通过 onError 交出去；不传 onError 就自己画浮层
 * （前两处的老 行为，保持不变）。
 *
 * 渲染库（vega-embed / mermaid / echarts）体积大，一律动态 import 懒加载：
 * 只有真的打开一个图时才下载对应 chunk，不进首屏包。
 */

const PLACEHOLDER_STYLE: React.CSSProperties = {
  padding: 16,
  color: "var(--kp-text-muted)",
  fontSize: 12,
  textAlign: "center",
};

/** 把 onError 存进 ref：调用方常传内联函数，进 deps 会让 effect 反复重跑。 */
function useLatest<T>(v: T) {
  const r = useRef(v);
  r.current = v;
  return r;
}

/**
 * 图表渲染（src 直渲版）。
 * engine=vega-lite 走 vega-embed；engine=echarts 走 echarts.init + setOption。
 * 容器常驻，加载/失败以浮层提示，不卸载容器。
 */
export function ChartView({
  src,
  engine = "vega-lite",
  height = 360,
  onError,
}: {
  src: string;
  engine?: "vega-lite" | "echarts";
  /** 画布高度：数字 = 固定像素；"100%" = 填满父容器（父容器需要有确定高度） */
  height?: number | "100%";
  /** 渲染失败时回调（消息已成人话）；不传则组件自己画错误浮层 */
  onError?: (message: string) => void;
}) {
  const [state, setState] = useState<{ loading: boolean; err: string | null }>({
    loading: true,
    err: null,
  });
  const chartRef = useRef<HTMLDivElement>(null);
  const instRef = useRef<any>(null);
  const resizeRef = useRef<(() => void) | null>(null);
  const errRef = useLatest(onError);

  useEffect(() => {
    let active = true;
    const el = chartRef.current;
    if (!el) return;
    (async () => {
      try {
        const spec = JSON.parse(src);
        if (engine === "echarts") {
          // echarts 的类型定义没写 default 导出（运行时是有的），取不到就退回命名空间
          const mod: any = await import("echarts");
          const echarts = mod.default ?? mod;
          instRef.current = echarts.init(el);
          instRef.current.setOption(spec);
          // echarts 不随容器尺寸自适应，渲染后补一次 resize 以铺满给定高度
          instRef.current.resize();
          // 窗口 resize 时也跟随：监听一次，卸载时移除，避免泄漏。
          const onResize = () => instRef.current?.resize();
          window.addEventListener("resize", onResize);
          resizeRef.current = onResize;
        } else {
          const vegaEmbed = (await import("vega-embed")).default;
          el.innerHTML = "";
          await vegaEmbed(el, spec, { renderer: "svg", actions: false });
        }
        if (active) setState({ loading: false, err: null });
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        if (active) {
          errRef.current?.(msg);
          setState({ loading: false, err: msg });
        }
      }
    })();
    return () => {
      active = false;
      if (resizeRef.current) {
        window.removeEventListener("resize", resizeRef.current);
        resizeRef.current = null;
      }
      if (instRef.current) {
        instRef.current.dispose?.();
        instRef.current = null;
      }
      el.innerHTML = "";
    };
  }, [src, engine]);

  const overlay = (text: string) => (
    <div
      style={{
        position: "absolute",
        inset: 22,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        // 用面板底色而不是硬编码白：深色主题下白块会糊成一片
        color: "var(--kp-text-muted)",
        fontSize: 12,
        textAlign: "center",
        background: "var(--kp-bg)",
        padding: 8,
      }}
    >
      {text}
    </div>
  );

  return (
    <div style={{ position: "relative", ...(typeof height === "string" ? { height: "100%" } : {}) }}>
      <PanZoomStage height={height}>
        <div
          ref={chartRef}
          style={{
            display: "inline-block",
            width: "100%",
            // echarts 需要显式高度；vega 按内容自适应，给 width 即可
            ...(engine === "echarts" && typeof height === "number" ? { height: height - 40 } : {}),
          }}
        />
      </PanZoomStage>
      {state.loading && overlay("渲染图表中…")}
      {state.err && overlay(`图表渲染失败：${state.err}（可下载 spec 查看）`)}
    </div>
  );
}

let mermaidReady = false;
/** 自增 id：mermaid.render 要求每次调用 id 唯一，否则报「id 已存在」。 */
let mermaidSeq = 0;

/** 流程图渲染（src 直渲版）。 */
export function MermaidView({
  src,
  refMark,
  height = 360,
  onError,
}: {
  src: string;
  refMark?: string;
  /** 画布高度：数字 = 固定像素；"100%" = 填满父容器（父容器需要有确定高度） */
  height?: number | "100%";
  onError?: (message: string) => void;
}) {
  const [state, setState] = useState<{
    loading: boolean;
    err: string | null;
    svg: string | null;
  }>({ loading: true, err: null, svg: null });
  const errRef = useLatest(onError);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        let s = src;
        // 时序图（refMark==="seq"）：agent 在 mermaid 源码里用 [seq] 占「这一步序号」的位，
        // 渲染前统一替换成递增序号（1,2,3…）。对实时渲染和读历史一致生效，且对已经带好
        // 序号的源码无副作用（匹配不到就不替换）。markdown 内联一般不带 ref，跳过此步。
        if (refMark === "seq" && s) {
          let _n = 0;
          s = s.replace(/\[seq\]/g, () => String(++_n));
        }
        const mermaid = (await import("mermaid")).default;
        if (!mermaidReady) {
          mermaid.initialize({ startOnLoad: false, theme: "default", securityLevel: "strict" });
          mermaidReady = true;
        }
        const { svg } = await mermaid.render("m" + ++mermaidSeq, s);
        // 交给 React 受控渲染：SVG 随组件挂载写入，避免手动 innerHTML 时 ref 尚未挂载（白屏）。
        if (active) setState({ loading: false, err: null, svg });
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        if (active) {
          errRef.current?.(msg);
          setState({ loading: false, err: msg, svg: null });
        }
      }
    })();
    return () => {
      active = false;
    };
  }, [src, refMark]);

  if (state.loading) return <div style={PLACEHOLDER_STYLE}>渲染流程图中…</div>;
  if (state.err)
    return (
      <div style={PLACEHOLDER_STYLE}>
        流程图渲染失败：{state.err}（可下载源码查看）
      </div>
    );

  // 缩放/平移/双击交互统一交给可复用的 PanZoomStage
  return (
    <PanZoomStage height={height}>
      <div dangerouslySetInnerHTML={{ __html: state.svg || "" }} />
    </PanZoomStage>
  );
}
