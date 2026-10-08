#!/usr/bin/env bash
# 停止 agent 服务（含其拉起的 MCP stdio 子进程，避免孤儿占用端口）
# 脚本位于 niubiplatform/keeper/，PID 文件同目录。
set -u
KEEPER_DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$KEEPER_DIR/keeper.pid"

stopped=0

# 1) 按 PID 文件精确停止
if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE")"
  if kill -0 "$PID" 2>/dev/null; then
    echo "[keeper] 停止 keeper 进程 (PID $PID) ..."
    kill -TERM "$PID" 2>/dev/null || true
    stopped=1
  fi
  rm -f "$PIDFILE"
fi

# 2) 兜底：按进程名清理（处理脚本未记录 PID 的情况）
if pkill -f "keeper.main" 2>/dev/null; then
  echo "[keeper] 已按进程名清理 keeper"
  stopped=1
fi

# 3) 清理可能孤儿化的 MCP 二进制（stdio 子进程在父进程被强杀时可能残留，占 9749）
if pkill -f "codebase-memory-mcp" 2>/dev/null; then
  echo "[keeper] 已停止 codebase-memory-mcp 子进程"
  stopped=1
fi

if [ "$stopped" -eq 0 ]; then
  echo "[keeper] 未找到运行中的 keeper 进程"
else
  echo "[keeper] 完成。"
fi
