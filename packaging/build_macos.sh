#!/usr/bin/env bash
# 一键打包 macOS 可安装应用（.app + .dmg）。
#
# 三步：
#   1) 前端产物  web/dist              —— 内核把它挂成站点根（页面与 API 同源）
#   2) 内核      packaging/dist/kernel —— PyInstaller 冻成自包含可执行文件（自带 Python）
#   3) 桌面壳    desktop/release/*.dmg —— electron-builder 打包 + 造 dmg
#
# 用法：
#   ./build_macos.sh                # 全流程
#   ./build_macos.sh --skip-web     # 复用现有 web/dist
#   ./build_macos.sh --skip-kernel  # 复用现有 packaging/dist/kernel
#
# 产物：
#   desktop/release/Keeper-<版本>-arm64.dmg   <- 拿去分发（拖进「应用程序」即可）
#   desktop/release/mac-arm64/Keeper.app     <- 未压缩的 app（可直接 open 调试）
#
# 签名：没有开发者证书时产物**未签名**，本机可正常打开；给别人用需要
# 右键打开，或 xattr -dr com.apple.quarantine Keeper.app。
# 有证书就设 CSC_LINK（base64 的 p12）+ CSC_KEY_PASSWORD，electron-builder 会自动
# 签名并公证（见 packaging/README.md）。
set -euo pipefail

PKG_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$PKG_DIR/.." && pwd)"
WEB_DIR="$ROOT/web"
DESKTOP_DIR="$ROOT/desktop"
DIST_DIR="$PKG_DIR/dist"

SKIP_WEB=0
SKIP_KERNEL=0
for arg in "$@"; do  case "$arg" in
    --skip-web) SKIP_WEB=1 ;;
    --skip-kernel) SKIP_KERNEL=1 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg"; exit 1 ;;
  esac
done

# 清空一个目录（保留目录本身）。
# 不用 rm -rf：一次删几千个文件会被某些环境的「删除保护」拦下（IDE 的安全删除
# 守卫就是这种），find -delete 逐层删更稳。
clean_dir() {
  [ -d "$1" ] || return 0
  find "$1" -mindepth 1 -delete 2>/dev/null || rm -rf "$1"
}

echo "==> [1/3] 前端产物 web/dist"
if [ "$SKIP_WEB" = "1" ] && [ -f "$WEB_DIR/dist/index.html" ]; then
  echo "    跳过（复用已有产物）"
else
  ( cd "$WEB_DIR" && npm install --silent --include=dev && npm run build )
fi
[ -f "$WEB_DIR/dist/index.html" ] || { echo "前端产物缺失：$WEB_DIR/dist/index.html"; exit 1; }
echo "==> [2/3] 冻结 Python 内核（PyInstaller）"
if [ "$SKIP_KERNEL" = "1" ] && [ -x "$DIST_DIR/kernel/keeper-kernel" ]; then
  echo "    跳过（复用已有内核）"
else
  # 解释器：优先用仓库的 .venv（依赖已经装好了），否则用系统 python3
  if [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
  else
    PY="$(command -v python3)"
  fi
  echo "    解释器：$PY"

  # PyInstaller 是**构建期**依赖，不是运行期依赖，所以不进 requirements.txt。
  if ! "$PY" -m PyInstaller --version >/dev/null 2>&1; then
    echo "    安装 PyInstaller ..."
    "$PY" -m pip install -q "pyinstaller>=6.6"
  fi

  # 可选运行时：litert-lm（本地向量检索的 embedding 引擎）。
  # 它的 wheel 是平台相关的（Intel Mac 没有）→ 装不上就跳过，只少了语义检索。
  "$PY" "$PKG_DIR/install_optional_deps.py" || true

  mkdir -p "$DIST_DIR"
  clean_dir "$PKG_DIR/build"
  clean_dir "$DIST_DIR/kernel"
  "$PY" -m PyInstaller \
    --noconfirm --clean \
    --distpath "$DIST_DIR" \
    --workpath "$PKG_DIR/build" \
    "$PKG_DIR/keeper-kernel.spec"
fi
[ -x "$DIST_DIR/kernel/keeper-kernel" ] || { echo "内核产物缺失：$DIST_DIR/kernel/keeper-kernel"; exit 1; }

# 额外的运行时目录（Windows 上放真 node 与 busybox；mac 上是空的）
mkdir -p "$DIST_DIR/runtime"

# 配置模板：首次运行时复制到 ~/.keeper/config.yaml（用户数据留在 home，不进包）
cp "$ROOT/config.yaml" "$DIST_DIR/config.default.yaml"

echo "==> [3/3] electron-builder 出 .app / .dmg"
cd "$DESKTOP_DIR"
if [ ! -d node_modules/electron-builder ]; then
  npm install --silent --include=dev
fi
# 没有签名证书时别去找签名身份（否则 electron-builder 会去问 keychain）
export CSC_IDENTITY_AUTO_DISCOVERY=false
npx electron-builder --mac --arm64 --publish never

echo
echo "==> 完成"
ls -lh "$DESKTOP_DIR/release"/*.dmg 2>/dev/null || true
echo "    app：$DESKTOP_DIR/release/mac-arm64/Keeper.app"
echo "    试跑：open \"$DESKTOP_DIR/release/mac-arm64/Keeper.app\""
