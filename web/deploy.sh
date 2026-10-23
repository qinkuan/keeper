#!/usr/bin/env bash
# 构建并打包 keeper 前端产物（宝塔部署用）：只出 dist 一个包。
#
# 用法：
#   ./deploy.sh              # 先 npm run build，再打 dist
#   ./deploy.sh --no-build   # 直接打现成的 dist（已构建过）
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
# 包输出到 keeper/dist/（与 keeper/deploy.sh 的服务端包同目录，方便一起取）
KEEPER_DIR="$(cd "$DIR/.." && pwd)"
OUT="$KEEPER_DIR/dist"
BUILD=1
[ "${1:-}" = "--no-build" ] && BUILD=0

command -v zip >/dev/null 2>&1 || { echo "[deploy] 需要 zip 命令"; exit 1; }

if [ "$BUILD" = "1" ]; then
  echo "[deploy] 构建前端 ..."
  ( cd "$DIR" && npm run build )
fi

[ -d "$DIR/dist" ] || { echo "[deploy] dist 不存在，先构建"; exit 1; }
[ -f "$DIR/dist/index.html" ] || { echo "[deploy] dist/index.html 不像构建产物"; exit 1; }

PKG="keeper-web-$(date +%Y%m%d-%H%M%S)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/$PKG"
rsync -a --exclude='.DS_Store' "$DIR/dist/" "$STAGE/$PKG/"

mkdir -p "$OUT"
( cd "$STAGE/$PKG" && zip -qr "$OUT/$PKG.zip" . )
N="$(find "$DIR/dist" -type f | wc -l | tr -d ' ')"
# 打**绝对**路径：原来只打 dist/xxx.zip，脚本内部会 cd（构建时），
# 那个 dist 到底指哪取决于你在哪个目录执行，很容易找不到包
echo "[deploy] 完成：${OUT}/${PKG}.zip（$(du -h "$OUT/${PKG}.zip" | cut -f1)，${N} 个文件）"
exit 0
