/**
 * 调试模式开关的 React Query 钩子。
 *
 * 后端读 settings.yaml 顶层 ``debug``（默认 false）；开启后前端显示更多调试用的
 * 功能与诊断信息，关闭时这些界面以最简洁形式呈现。手改 settings.yaml 后立即生效。
 *
 * 用法：
 *   const debug = useDebug();
 *   {debug && <MyDebugPanel />}   // 只在调试模式下显示
 */
import { useQuery } from "@tanstack/react-query";
import { apiGetDebug } from "../api/client";

export function useDebug(): boolean {
  const q = useQuery({
    queryKey: ["app", "debug"],
    queryFn: apiGetDebug,
    // 调试开关很少变：30s 内不重新拉，手改 settings.yaml 后最多等一个周期
    staleTime: 30_000,
  });
  return !!q.data?.debug;
}
