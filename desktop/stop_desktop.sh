#!/usr/bin/env bash
# 停止 keeper 桌面版：关 Electron 窗口，并确保 Python 内核一起退干净。
#
# 为什么要单独兜底内核：内核是 Electron 的子进程。正常关窗时 Electron 的
# before-quit 会 SIGTERM 它；但窗口进程被强杀（kill -9 / 崩溃）时内核会变成
# 孤儿进程，继续占着 ~/.keeper 的数据库，下次启动就撞库。
#
# 用法：
#   ./stop_desktop.sh                # 优雅停止（默认最多等 8 秒）
#   GRACE=15 ./stop_desktop.sh       # 内核关 MCP 会话慢时放宽等待
set -u
DESKTOP_DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$DESKTOP_DIR/desktop.pid"
GRACE="${GRACE:-8}"

# 1) 定位 Electron 进程：优先 PID 文件，兜底按本目录的 Electron 精确匹配。
#    刻意不写 pkill -f "Electron"——那会误杀机器上其它 Electron 应用。
PIDS=""
if [ -f "$PIDFILE" ]; then
  PID="$(tr -d '[:space:]' < "$PIDFILE" 2>/dev/null || true)"
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    PIDS="$PID"
  fi
  rm -f "$PIDFILE"
fi
if [ -z "$PIDS" ]; then
  PIDS="$(pgrep -f "keeper/desktop/node_modules/electron" 2>/dev/null | tr '\n' ' ' || true)"
fi

if [ -z "${PIDS// /}" ]; then
  echo "[keeper-desktop] 未找到运行中的桌面版"
  exit 0
fi

# 2) 杀之前先记下内核 PID：进程一退出就查不到父子关系了。
#    只看直接子进程里命令行含 keeper.main 的那个（Electron 还有一批渲染/工具子进程）。
KERNEL_PIDS=""
for pid in $PIDS; do
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do
    if ps -o command= -p "$child" 2>/dev/null | grep -q "keeper.main"; then
      KERNEL_PIDS="$KERNEL_PIDS $child"
    fi
  done
done

echo "[keeper-desktop] 停止 Electron（PID${PIDS}）..."
# shellcheck disable=SC2086
kill -TERM $PIDS 2>/dev/null || true

# 3) 等优雅退出：这段时间是给 Electron 走 before-quit、内核关 MCP 会话用的
deadline=$((SECONDS + GRACE))
while [ "$SECONDS" -lt "$deadline" ]; do
  alive=0
  for pid in $PIDS; do
    kill -0 "$pid" 2>/dev/null && alive=1
  done
  [ "$alive" -eq 0 ] && break
  sleep 0.5
done

# 4) 仍未退：强杀窗口，并带走刚才记下的内核（它已经没有父进程来收它了）
remaining=""
for pid in $PIDS; do
  kill -0 "$pid" 2>/dev/null && remaining="$remaining $pid"
done
if [ -n "${remaining// /}" ]; then
  echo "[keeper-desktop] ${GRACE}s 内未退出，强制结束（PID${remaining}）"
  # shellcheck disable=SC2086
  kill -KILL $remaining 2>/dev/null || true
fi

for pid in $KERNEL_PIDS; do
  kill -0 "$pid" 2>/dev/null || continue
  echo "[keeper-desktop] 停止残留内核 (PID $pid) ..."
  kill -TERM "$pid" 2>/dev/null || true
  sleep 2
  kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
done

echo "[keeper-desktop] 完成。"
