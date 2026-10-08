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
});
