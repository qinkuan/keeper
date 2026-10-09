#!/usr/bin/env bash
# 构建并打包 keeper 前端产物（宝塔部署用）：只出 dist 一个包。
#
# 用法：
#   ./deploy.sh              # 先 npm run build，再打 dist
#   ./deploy.sh --no-build   # 直接打现成的 dist（已构建过）
#   VITE_PLATFORM_URL=http://x:9095 ./deploy.sh   # 临时换平台地址
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
# 包输出到 keeper/dist/（与 keeper/deploy.sh 的服务端包同目录，方便一起取）
KEEPER_DIR="$(cd "$DIR/.." && pwd)"
OUT="$KEEPER_DIR/dist"
BUILD=1
[ "${1:-}" = "--no-build" ] && BUILD=0

# 平台（keeperplatform）地址：**编译期常量**（client.ts 的 VITE_PLATFORM_URL），
# 打进产物后改不了，只能重新构建。
# 默认 /platform：与 keeper 同源，由 nginx 反代到 127.0.0.1:9095，
# 这样浏览器不用直连 9095，也就不需要平台侧配 CORS。
# 需要换成绝对地址时用环境变量覆盖：VITE_PLATFORM_URL=http://x:9095 ./deploy.sh
export VITE_PLATFORM_URL="${VITE_PLATFORM_URL:-/platform}"

command -v zip >/dev/null 2>&1 || { echo "[deploy] 需要 zip 命令"; exit 1; }

if [ "$BUILD" = "1" ]; then
  echo "[deploy] 平台地址 VITE_PLATFORM_URL=${VITE_PLATFORM_URL}"
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
case "$VITE_PLATFORM_URL" in
  /*) cat <<'TXT'

[deploy] nginx 需要有这条同源反代（平台在 9095）：
       location ^~ /platform/ { proxy_pass http://127.0.0.1:9095/; }
       少了它，智能体市场 / 装载会 404。
TXT
    ;;
esac
exit 0
