#!/usr/bin/env bash
# 停止 keeper web 控制台（按端口清理 vite 进程）
#
# 用法：
#   ./stop_web.sh                # 停止默认端口 5273
#   PORT=5274 ./stop_web.sh      # 停止指定端口
set -u

PORT="${PORT:-5273}"
stopped=0

# 1) 按端口精确停止
if command -v lsof >/dev/null 2>&1; then
  PIDS="$(lsof -ti "tcp:${PORT}" 2>/dev/null || true)"
  if [ -n "$PIDS" ]; then
    echo "[keeper-web] 停止端口 ${PORT} 上的进程：${PIDS}"
    # shellcheck disable=SC2086
    kill -TERM $PIDS 2>/dev/null || true
    stopped=1
  fi
fi

# 2) 兜底：按 vite 命令行清理（端口未监听但进程仍在时）
if pkill -f "vite.*--port ${PORT}" 2>/dev/null; then
  echo "[keeper-web] 已按进程名清理 vite"
  stopped=1
fi

if [ "$stopped" -eq 0 ]; then
  echo "[keeper-web] 未找到运行中的控制台"
else
  echo "[keeper-web] 完成。"
fi
