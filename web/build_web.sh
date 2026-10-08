#!/usr/bin/env bash
# 构建 keeper 前端生产产物（输出到 web/dist）。
# keeper 启动时会自动把 dist/ 作为站点根托管，实现“一个进程 = API + 页面”的单体站点。
#
# 用法：
#   ./build_web.sh
set -euo pipefail
WEB_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$WEB_DIR"

if ! command -v npm >/dev/null 2>&1; then
  echo "[keeper-web] 未找到 npm，请先安装 Node.js"
  exit 1
fi

echo "[keeper-web] 安装依赖 ..."
npm install

echo "[keeper-web] 构建生产产物到 dist/ ..."
npm run build

echo "[keeper-web] 完成。keeper 启动后将自动托管 dist/ 作为站点根。"
