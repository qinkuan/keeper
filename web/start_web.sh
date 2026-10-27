#!/usr/bin/env bash
# 启动 keeper web 控制台（前台运行：日志直接输出到控制台，Ctrl+C 停止）
#
# 用法：
#   ./start_web.sh                # 默认端口 5273
#   PORT=5274 ./start_web.sh      # 自定义前端端口
#   KEEPER_PORT=9090 ./start_web.sh  # 后端端口不是自动探测到的那个时，手动指定
#
# 前置：先用 ../start_keeper.sh 启动后端，否则界面顶部会显示“服务未启动”。
#
# 后端端口**不写死**：keeper 现在启动时自己挑一个空闲端口，并把实际端口写进
# ~/.keeper/runtime/kernel.json —— 这里从那个文件读，读不到才退回 8080。
set -euo pipefail
WEB_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$WEB_DIR"

PORT="${PORT:-5273}"

# 后端地址：从内核写下的 kernel.json 里读实际端口。
#
# 为什么不再硬编码 8080：keeper 的端口是**动态**的（用户机器上 8080 可能已被
# 别的程序占着，启动时会自动换一个）。写死的代理目标会指到别人的服务上——
# 或者干脆连不上，界面上表现为"服务未启动"，但后端明明活着。
KERNEL_INFO="${KEEPER_HOME:-$HOME}/.keeper/runtime/kernel.json"
KEEPER_PORT="${KEEPER_PORT:-}"
if [ -z "$KEEPER_PORT" ]; then
  if [ -f "$KERNEL_INFO" ]; then
    KEEPER_PORT="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("port",""))' "$KERNEL_INFO" 2>/dev/null || true)"
  fi
  KEEPER_PORT="${KEEPER_PORT:-8080}"
fi

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
if [ ! -f "$KERNEL_INFO" ] && [ "${KEEPER_PORT}" = "8080" ]; then
  echo "[keeper-web] 提示：没找到 ~/.keeper/runtime/kernel.json，说明后端可能没起；"
  echo "           起后端用 ../start_keeper.sh（端口是自动分配的）"
fi
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
