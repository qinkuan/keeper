#!/usr/bin/env bash
# 启动 agent 服务（连接配置里声明的 MCP server + 提供 HTTP API）
# 脚本位于 keeper/，依赖（.venv / requirements.txt）都在 keeper 目录里，整包自包含。
#
# 默认「前台运行」：日志直接打印到控制台，按 Ctrl+C 即可优雅停止
# （会走 shutdown()，一并关闭 MCP 会话与 stdio 子进程）。
#
# 用法：
#   ./start_keeper.sh              # 前台运行，默认端口 8080，Ctrl+C 停止
#   PORT=9090 ./start_keeper.sh    # 自定义端口
#   LOG_LEVEL=DEBUG ./start_keeper.sh   # 更详细的日志
#   ./start_keeper.sh --daemon     # 后台守护运行（日志 -> keeper.log）
set -euo pipefail
KEEPER_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$KEEPER_DIR/.." && pwd)"
cd "$ROOT"

PORT="${PORT:-8080}"
# 必须是绝对路径：load_config 对相对路径按 keeper 包目录解析，
# 传 "keeper/config.yaml" 会被拼成 keeper/keeper/config.yaml 而找不到。
CONFIG="$KEEPER_DIR/config.yaml"

# 本进程的家目录。宝塔的进程守护 / systemd / cron 这类环境常常**不设 HOME**，
# 而脚本开了 set -u，直接写 $HOME 会报 "HOME: unbound variable" 起不来。
# 拿不到就从 passwd 查当前 uid 的家目录，再兜底 /root。
KEEPER_HOME="${HOME:-}"
if [ -z "$KEEPER_HOME" ]; then
  KEEPER_HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6 || true)"
fi
[ -n "$KEEPER_HOME" ] || KEEPER_HOME="/root"

# 插件库根目录（与 config.yaml 的 plugins.root 默认值一致；可用 PLUGIN_ROOT 覆盖）。
# 不再硬编码某一个插件的二进制路径——插件是用户往库里放的，脚本不该知道
# 库里将来会有谁。
PLUGIN_ROOT="${PLUGIN_ROOT:-$KEEPER_HOME/.keeper/plugins}"
PIDFILE="$KEEPER_DIR/keeper.pid"
LOGFILE="$KEEPER_DIR/keeper.log"

# 若已运行则提示：守护模式有 PID 文件，前台模式按进程名查找
RUNNING_PID=""
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  RUNNING_PID="$(cat "$PIDFILE")"
else
  RUNNING_PID="$(pgrep -f "keeper.main" | head -n1 || true)"
fi
if [ -n "$RUNNING_PID" ]; then
  echo "[keeper] 已在运行 (PID ${RUNNING_PID})，请先停止再启动"
  exit 1
fi

# 虚拟环境默认建在 keeper 目录里（requirements.txt 也在那儿，整包自包含）。
# 老布局把 venv 放在项目根并与后端共用，那种目录里已经装好依赖了，继续复用，避免重装。
VENV="$KEEPER_DIR/.venv"
if [ ! -d "$VENV" ] && [ -f "$ROOT/.venv/.requirements.stamp" ]; then
  VENV="$ROOT/.venv"
fi
if [ ! -d "$VENV" ]; then
  echo "[keeper] 创建虚拟环境 $VENV ..."
  python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# requirements.txt 随代码放在 keeper 目录；老布局（放在项目根）也照旧兼容。
REQ="$KEEPER_DIR/requirements.txt"
[ -f "$REQ" ] || REQ="$ROOT/requirements.txt"
if [ ! -f "$REQ" ]; then
  echo "[keeper] 找不到 requirements.txt（找过：$KEEPER_DIR 与 $ROOT）"
  exit 1
fi

# 依赖只在**首次**或 **requirements.txt 变了**的时候装。
#
# 为什么不用「每次都装」：这个脚本大多是被进程守护管理器（宝塔 / systemd /
# supervisor）拉起的，崩溃后会被立刻重启。每次启动都 pip install 意味着
# ① 每次重启都要联网，慢；② 离线或 pip 源抽风时 `set -e` 直接退出，管理器
# 又拉起、又 pip —— 变成装不上的死循环。
REQ_HASH="$(python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest()[:16])' "$REQ")"
REQ_STAMP="$VENV/.requirements.stamp"
if [ ! -f "$REQ_STAMP" ] || [ "$(cat "$REQ_STAMP" 2>/dev/null || echo x)" != "$REQ_HASH" ]; then
  echo "[keeper] 安装依赖 ($REQ) ..."
  pip install -q -r "$REQ"
  echo "$REQ_HASH" > "$REQ_STAMP"
else
  echo "[keeper] 依赖未变化，跳过安装"
fi

# 确保插件里的可执行文件有 +x（从 zip / 别的机器拷过来常常会丢）
if [ -d "$PLUGIN_ROOT" ]; then
  find "$PLUGIN_ROOT" -maxdepth 3 -type f -path '*/mcp/*' ! -perm -u+x -exec chmod +x {} + 2>/dev/null || true
  find "$PLUGIN_ROOT" -maxdepth 3 -type f -path '*/bin/*' ! -perm -u+x -exec chmod +x {} + 2>/dev/null || true
fi

# ---- 后台守护模式（可选）----
if [ "${1:-}" = "--daemon" ]; then
  echo "[keeper] 后台启动 (http://localhost:${PORT})，日志 -> ${LOGFILE}"
  nohup "$VIRTUAL_ENV/bin/python" -m keeper.main \
    --config "$CONFIG" "$PORT" >> "$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  echo "[keeper] 已启动 PID $(cat "$PIDFILE")"
  echo "[keeper] 查看日志: tail -f ${LOGFILE}；停止: ./stop_keeper.sh"
  exit 0
fi

# ---- 前台运行（默认）：控制台输出 + Ctrl+C 停止 ----
# 落一份日志文件。宝塔进程守护 / systemd 只会收走 stdout，进程一重启上次的
# 记录就跟着没了——排查「上次那个错为什么发生」全靠人肉复现。文件在 keeper.log，
# 可用 LOG_FILE 换路径、LOG_MAX_BYTES / LOG_BACKUP_COUNT 调大小与保留份数。
# 注意：--daemon 模式下不设它（那条路已经把 stdout 重定向进同一个文件了，
# 再加一个 FileHandler 会让两路输出交错）。
export LOG_FILE="${LOG_FILE:-$KEEPER_DIR/keeper.log}"

echo "[keeper] 前台启动 (http://localhost:${PORT})"
echo "[keeper] 日志直接输出到控制台，按 Ctrl+C 停止（会一并关闭 MCP 连接）"
echo "[keeper] 日志文件: ${LOG_FILE}"
echo ""

cleanup() {
  echo ""
  echo "[keeper] 已停止"
}
trap cleanup EXIT

# Ctrl+C：让脚本收到 INT 后正常退出（python 侧已优雅关闭 MCP 与二进制）
trap 'exit 0' INT TERM

"$VIRTUAL_ENV/bin/python" -m keeper.main --config "$CONFIG" "$PORT"
