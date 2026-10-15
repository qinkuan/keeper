import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 前端开发服务器把 /api 反向代理到 keeper 服务（默认 :8080），无需跨域配置。
// keeper 端口可用 KEEPER_PORT 覆盖（需与后端实际端口一致）。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5273,
    proxy: {
      "/api": {
        target: process.env.KEEPER_PORT
          ? `http://localhost:${process.env.KEEPER_PORT}`
          : "http://localhost:8080",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
  build: {
    // 主 chunk 曾经 5MB 一个整包：改一行代码就让整个包失效，缓存全废。
    // 按「变更频率」切分——几乎不动的三方大块各自成块，业务代码单独一块。
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (!id.includes("node_modules")) return;
          // React 全家桶：最稳定，单独缓存
          if (/node_modules\/(react|react-dom|scheduler)\//.test(id)) {
            return "vendor-react";
          }
          // antd 与其 CSS-in-JS / 图标：体积大、升级才变
          if (
            /node_modules\/(antd|@ant-design|rc-|@rc-component)/.test(id)
          ) {
            return "vendor-antd";
          }
          /*
           * monaco 同样**不**手动分块。
           * 试过给它单独成块（vendor-monaco），结果 Vite 把它算进了入口的依赖
           * 图，index.html 里照样出现 3.9MB 的 modulepreload——按需加载白做。
           * 不分块时它跟着 lazy 的 CodeView / ArtifactCard 走，首屏不再碰它。
           */
          /*
           * 其余一律**不指定**（返回 undefined，交给 Rollup 默认行为）。
           *
           * 踩过的坑：曾经给未匹配的 node_modules 兜底返回 "vendor"，结果 shiki
           * 那上百个按语言动态 import 的语法包全被吸进同一个 vendor 块——一个
           * 10MB 的块被首屏路径引用，比拆包前还糟。动态 import 的边界必须保留，
           * 手动分块只应该用在「确定是首屏必需」的大块上。
           */
          return;
        },
      },
    },
    // monaco 有 3.9MB，但它已被 CodeView 的 lazy import 隔离，只在打开编码
    // 工作台时才下载；阈值调高是为了让「真正该报警的块」还能报出来。
    chunkSizeWarningLimit: 4000,
  },
});
