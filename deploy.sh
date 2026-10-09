 #!/usr/bin/env bash
# 打包 keeper 服务端（宝塔部署用）：只保留代码 + 启动脚本，过滤掉所有本机产物。
#
# 包内结构必须保持 start_keeper.sh 依赖的相对位置（别摊平）：
#   <解压目录>/keeper/**            含 start_keeper.sh / stop_keeper.sh / config.yaml / requirements.txt
#
# 前端不在这个包里 —— dist 由 keeper/web/deploy.sh 单独打一个包。
#
# 用法：
#   ./deploy.sh              # 正常打包（含 config.yaml）
#   ./deploy.sh --no-config  # 不带 config.yaml（部署时自己写）
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="$DIR/dist"
WITH_CONFIG=1
[ "${1:-}" = "--no-config" ] && WITH_CONFIG=0

for c in zip rsync; do
  command -v "$c" >/dev/null 2>&1 || { echo "[deploy] 需要 $c 命令"; exit 1; }
done

PKG="keeper-server-$(date +%Y%m%d-%H%M%S)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
D="$STAGE/$PKG"
mkdir -p "$D/keeper"

# 排除的都是**本机产物 / 构建中间物 / 与服务端无关的东西**：
#   data/ .workplace/            运行时（SQLite 库、索引工作副本）
#   *.log *.pid *.bak            运行时
#   __pycache__/ *.pyc *.pyo     Python 字节码
#   evals/.runs/ evals/baselines 评估产物
#   web/                         前端整个目录（dist 单独打包，src/node_modules 用不到）
#   dist/                        本脚本与 web/deploy.sh 的产物目录（zip 会滚雪球）
#   .venv/                       依赖（start_keeper.sh 到目标机上自己装）
#   temp.json  test_*.py         无代码引用的临时文件 / 本地回归脚本
rsync -a \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' \
  --exclude='data' --exclude='.workplace' --exclude='.venv' \
  --exclude='dist' \
  --exclude='*.log' --exclude='*.pid' --exclude='*.bak' \
  --exclude='.DS_Store' --exclude='temp.json' --exclude='test_*.py' \
  --exclude='evals/.runs' --exclude='evals/baselines' \
  --exclude='web' \
  "$DIR/" "$D/keeper/"

# requirements.txt 已随代码 rsync 进 keeper/，start_keeper.sh 就是从这里读的
[ "$WITH_CONFIG" = "1" ] || rm -f "$D/keeper/config.yaml"

# 预留 web/dist —— 前端单独成包（keeper/web/deploy.sh），解压到这里。
# 目录必须先存在：keeper 靠 `web_dist.is_dir()` 决定要不要把前端挂成站点根
# （main.py:151），目录不存在就只提供 API，页面 404。
mkdir -p "$D/keeper/web/dist"
cat > "$D/keeper/web/dist/前端放这里.txt" <<'TXT'
把 keeper-web-*.zip 解压到这个目录，最终结构要是：
    keeper/web/dist/index.html
    keeper/web/dist/assets/

keeper 会把 web/dist 作为站点根（见 main.py），前端与 API 同域，不需要配跨域。
TXT

# 关键文件齐全才打包（少一个就别浪费时间）
MISSING=""
for f in keeper/requirements.txt keeper/__init__.py keeper/main.py keeper/start_keeper.sh; do
  [ -e "$D/$f" ] || MISSING="$MISSING $f"
done
if [ -n "$MISSING" ]; then
  echo "[deploy] 缺少关键文件：$MISSING"; exit 1
fi

mkdir -p "$OUT"
( cd "$D" && zip -qr "$OUT/$PKG.zip" . )
# 打**绝对**路径：只打 dist/xxx.zip 的话，在哪个目录执行它就指哪
echo "[deploy] 完成：${OUT}/${PKG}.zip（$(du -h "$OUT/$PKG.zip" | cut -f1)）"
[ "$WITH_CONFIG" = "1" ] && echo "[deploy] 注意：包内 config.yaml 含 platform.token 与 llm.api_key"

cat <<'TXT'

[deploy] 宝塔部署步骤（方案 A：keeper 自己托管前端，与 API 同域、无需跨域）
  1. 上传 keeper-server-*.zip 到服务器，在宝塔里解压到 /opt/keeper
  2. 上传 keeper-web-*.zip，解压到 /opt/keeper/keeper/web/dist/
     （最终要有 /opt/keeper/keeper/web/dist/index.html）
  3. 如需改配置：编辑 /opt/keeper/keeper/config.yaml（端口、平台 token、模型 key）
  4. 宝塔「进程守护管理器」添加守护进程：
       启动命令：/opt/keeper/keeper/start_keeper.sh
       不要加 --daemon（管理器自己保活，再叠 nohup 容易起两个实例抢端口）
  5. 访问 http://服务器IP:8080 ；nginx 反代到 8080 即可
TXT
exit 0
