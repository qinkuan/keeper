#!/usr/bin/env bash
# 启动 keeper web 控制台（前台运行：日志直接输出到控制台，Ctrl+C 停止）
#
# 用法：
#   ./start_web.sh                # 默认端口 5273
#   PORT=5274 ./start_web.sh      # 自定义前端端口
#   KEEPER_PORT=9090 ./start_web.sh  # 后端 keeper 不在 8080 时，指定接口代理目标
#
# 前置：先用 ../start_keeper.sh 启动后端（默认 :8080），否则界面顶部会显示“服务未启动”。
set -euo pipefail
WEB_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$WEB_DIR"

PORT="${PORT:-5273}"
KEEPER_PORT="${KEEPER_PORT:-8080}"

if ! command -v npm >/dev/null 2>&1; then
  echo "[keeper-web] 未找到 npm，请先安装 Node.js"
  exit 1
fi

# 端口占用检查（避免与已运行的控制台或其它服务冲突）
if command -v lsof >/dev/null 2>&1 && lsof -ti "tcp:${PORT}" >/dev/null 2>&1; then
  echo "[keeper-web] 端口 ${PORT} 已被占用（PID $(lsof -ti "tcp:${PORT}" | head -n1)），请先停止或换端口"
  exit 1
fi

# 首次运行自动安装依赖
if [ ! -d "node_modules" ]; then
  echo "[keeper-web] 首次运行，安装依赖 ..."
  npm install
fi

echo "[keeper-web] 启动控制台：http://localhost:${PORT}"
echo "[keeper-web] 接口代理：/api -> http://localhost:${KEEPER_PORT}（keeper 后端）"
echo "[keeper-web] 日志直接输出到控制台，按 Ctrl+C 停止"
echo ""

cleanup() {
  echo ""
  echo "[keeper-web] 已停止"
}
trap cleanup EXIT
# Ctrl+C：正常退出，由 EXIT trap 收尾
trap 'exit 0' INT TERM

KEEPER_PORT="$KEEPER_PORT" npm run dev -- --port "$PORT" --host
