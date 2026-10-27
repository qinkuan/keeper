import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

/**
 * 后端端口：优先环境变量 KEEPER_PORT，其次读内核写下的
 * ~/.keeper/runtime/kernel.json，最后才退回 8080。
 *
 * 为什么不再写死 8080：keeper 启动时自己挑一个空闲端口（用户机器上 8080
 * 很可能已被别的程序占着），实际端口记在 kernel.json 里。写死代理目标会
 * 指到别人的服务上，或者连不上——界面上表现为"服务未启动"，但后端明明活着。
 */
function resolveKeeperPort(): string {
  const env = process.env.KEEPER_PORT;
  if (env) return env;
  try {
    const info = JSON.parse(
      readFileSync(join(homedir(), ".keeper", "runtime", "kernel.json"), "utf-8"),
    );
    if (info && info.port) return String(info.port);
  } catch {
    // 文件不存在 / 读不到：后端还没起，走下面的兜底
  }
  return "8080";
}

// 前端开发服务器把 /api 反向代理到 keeper 服务，无需跨域配置。
const keeperPort = resolveKeeperPort();

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5273,
    proxy: {
      "/api": {
        target: `http://localhost:${keeperPort}`,
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
