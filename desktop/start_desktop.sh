#!/usr/bin/env bash
# 启动 keeper 桌面版：Electron 窗口 + Python 内核（sidecar）。
#
# 链路：Electron 主进程占一个空闲端口 → spawn Python 内核 → 等 /health 就绪 →
# 窗口导航到 http://127.0.0.1:<动态端口>（页面与接口同源，都由内核提供）。
#
# 默认「前台运行」：日志直接输出到控制台，Ctrl+C 停止（Electron 的 before-quit
# 会给内核发 SIGTERM，正常路径下内核会被收干净；被强杀时请用 ./stop_desktop.sh 兜底）。
#
# 用法：
#   ./start_desktop.sh                # 前台运行，Ctrl+C 停止
#   SKIP_BUILD=1 ./start_desktop.sh   # 跳过前端产物检查与自动构建
set -euo pipefail
DESKTOP_DIR="$(cd "$(dirname "$0")" && pwd)"
KEEPER_DIR="$(cd "$DESKTOP_DIR/.." && pwd)"
WEB_DIR="$KEEPER_DIR/web"
PIDFILE="$DESKTOP_DIR/desktop.pid"

cd "$DESKTOP_DIR"

# 1) 前置检查：Node（Electron 主进程跑在 Node 上）、npm（装 Electron 用）
if ! command -v node >/dev/null 2>&1; then
  echo "[keeper-desktop] 未找到 node，请先安装 Node.js"
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "[keeper-desktop] 未找到 npm，请先安装 Node.js"
  exit 1
fi

# 2) 首次运行自动安装 Electron（postinstall 只是提示，不下载内核）
if [ ! -d "$DESKTOP_DIR/node_modules" ]; then
  echo "[keeper-desktop] 首次运行，安装依赖 ..."
  npm install
fi

ELECTRON_BIN="$DESKTOP_DIR/node_modules/.bin/electron"
if [ ! -x "$ELECTRON_BIN" ]; then
  echo "[keeper-desktop] 未找到 Electron（$ELECTRON_BIN）"
  echo "[keeper-desktop] 请手动安装：cd $DESKTOP_DIR && npm install electron"
  exit 1
fi

# 3) 页面是内核托管的静态产物 web/dist。没有 dist 时内核只提供 API，
#    窗口会是空白/404——这是桌面版最常见的"起不来"，所以在这里拦住。
if [ ! -f "$WEB_DIR/dist/index.html" ]; then
  if [ "${SKIP_BUILD:-0}" = "1" ]; then
    echo "[keeper-desktop] 警告：未找到 $WEB_DIR/dist/index.html，页面可能空白（SKIP_BUILD=1 已跳过构建）"
  else
    echo "[keeper-desktop] 未找到前端产物，自动构建 $WEB_DIR/dist ..."
    ( cd "$WEB_DIR" && ./build_web.sh )
  fi
fi

# 4) 已在运行则提示，避免两个实例抢同一个 ~/.keeper 数据库
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null || echo 0)" 2>/dev/null; then
  echo "[keeper-desktop] 已在运行 (PID $(cat "$PIDFILE"))，请先停止再启动：./stop_desktop.sh"
  exit 1
fi

echo "[keeper-desktop] 启动桌面版 ..."
echo "[keeper-desktop] 内核就绪后窗口会自动打开（日志里的「内核就绪：http://127.0.0.1:<端口>」）"
echo "[keeper-desktop] 按 Ctrl+C 停止；若窗口已关但进程残留，用 ./stop_desktop.sh"
echo ""

cleanup() {
  rm -f "$PIDFILE"
  echo ""
  echo "[keeper-desktop] 已退出"
}
trap cleanup EXIT
trap 'exit 0' INT TERM

# exec：让 shell 进程直接变成 Electron，这样 $$ 就是 Electron 的 PID，
# 停止脚本拿到的是真正的窗口进程（否则拿到的是 npm 那种中转进程）。
echo $$ > "$PIDFILE"
exec "$ELECTRON_BIN" .
