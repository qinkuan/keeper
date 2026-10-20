import PanZoomStage from "./PanZoomStage";

interface ImageViewerProps {
  src: string;
  alt?: string;
  height?: number;
}

/**
 * 图片内联预览：复用 PanZoomStage，支持滚轮/捏合缩放、拖拽平移、双击放大/复位。
 * 图片按容器 fit 默认整张可见；加载完成由 ResizeObserver 自动触发量测。
 */
export default function ImageViewer({ src, alt, height = 360 }: ImageViewerProps) {
  return (
    <PanZoomStage height={height}>
      <img
        src={src}
        alt={alt}
        draggable={false}
        style={{
          display: "block",
          width: "auto",
          height: "auto",
          maxWidth: "none",
          maxHeight: "none",
          // 透明/浅色 PNG 在白底上看不清，给个浅灰底
          background:
            "repeating-conic-gradient(#f3f3f3 0% 25%, #fff 0% 50%) 50% / 16px 16px",
        }}
      />
    </PanZoomStage>
  );
}
