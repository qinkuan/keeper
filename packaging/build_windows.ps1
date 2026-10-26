<#
.SYNOPSIS
  在 Windows 上打包 keeper（nsis 安装包）。

.DESCRIPTION
  与 build_macos.sh 同构的三步：
    1) 前端产物  web/dist
    2) 内核      packaging/dist/kernel（PyInstaller 冻成自带 Python 的可执行文件）
    3) 桌面壳    desktop/release/*.exe（electron-builder + nsis）

  Windows 专属的两样额外运行时（放进 packaging/dist/runtime → app 的 Resources/runtime）：
    - 真 node（node.exe + npm/npx）：Electron 自带的 node 不能被 busybox 的 sh 执行，
      而且没有 npm；带一份真的，agent 写 JS、跑 npx 形态的 MCP server 都行。
    - busybox 的 sh.exe（POSIX shell）：Windows 自带的 cmd.exe 语法与 mac/Linux 不同
      （管道、&&、$VAR、glob），agent 写的命令会莫名其妙地挂。busybox 的 ash 是真 POSIX。

  下载不到 busybox 时不报错：shell 自动退回 Git Bash → cmd.exe（见
  keeper/framework/shell.py）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File packaging/build_windows.ps1
  powershell -ExecutionPolicy Bypass -File packaging/build_windows.ps1 -SkipWeb
#>
param(
  [switch]$SkipWeb,
  [switch]$SkipKernel,
  [string]$NodeVersion = "22.13.0"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Dist = Join-Path $PSScriptRoot "dist"
$RuntimeDist = Join-Path $Dist "runtime"

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# ── 1) 前端 ───────────────────────────────────────────────────────────────
Write-Step "[1/3] 前端产物 web/dist"
if ($SkipWeb -and (Test-Path (Join-Path $Root "web\dist\index.html"))) {
  Write-Host "    跳过（复用已有产物）"
} else {
  Push-Location (Join-Path $Root "web")
  try {
    npm install --silent --include=dev
    npm run build
  } finally { Pop-Location }
}
if (-not (Test-Path (Join-Path $Root "web\dist\index.html"))) { throw "前端产物缺失" }

# ── 2) 冻结内核 ───────────────────────────────────────────────────────────
Write-Step "[2/3] 冻结 Python 内核（PyInstaller）"
if ($SkipKernel -and (Test-Path (Join-Path $Dist "kernel\keeper-kernel.exe"))) {
  Write-Host "    跳过（复用已有内核）"
} else {
  $VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
  if (Test-Path $VenvPy) { $Py = $VenvPy } else { $Py = "python" }
  & $Py -m PyInstaller --version *> $null
  if ($LASTEXITCODE -ne 0) {
    Write-Host "    安装 PyInstaller ..."
    & $Py -m pip install -q "pyinstaller>=6.6"
  }
  # 可选运行时：litert-lm（本地向量检索的 embedding 引擎，Windows x64 有 wheel）。
  # 装不上就跳过 —— 只少了语义检索，关键词检索照常可用。
  & $Py (Join-Path $PSScriptRoot "install_optional_deps.py")
  if ($LASTEXITCODE -ne 0) { Write-Warning "可选依赖安装有告警，不影响构建" }
  New-Item -ItemType Directory -Force -Path $Dist | Out-Null
  & $Py -m PyInstaller --noconfirm --clean `
      --distpath $Dist --workpath (Join-Path $PSScriptRoot "build") `
      (Join-Path $PSScriptRoot "keeper-kernel.spec")
  if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }
}
if (-not (Test-Path (Join-Path $Dist "kernel\keeper-kernel.exe"))) { throw "内核产物缺失" }

# ── 2b) 额外运行时：真 node + busybox 的 POSIX shell ─────────────────────
Write-Step "[2b/3] 额外运行时（node / busybox sh）"
New-Item -ItemType Directory -Force -Path $RuntimeDist | Out-Null

# 真 node：Windows 侧的 node.exe 能被任何 shell 执行，还带 npm/npx
$NodeDir = Join-Path $RuntimeDist "node"
if (-not (Test-Path (Join-Path $NodeDir "node.exe"))) {
  $zip = Join-Path $env:TEMP "node-win-x64.zip"
  $url = "https://nodejs.org/dist/v$NodeVersion/node-v$NodeVersion-win-x64.zip"
  try {
    Write-Host "    下载 node v$NodeVersion ..."
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $env:TEMP -Force
    $src = Join-Path $env:TEMP "node-v$NodeVersion-win-x64"
    if (Test-Path $NodeDir) { Remove-Item -Recurse -Force $NodeDir }
    New-Item -ItemType Directory -Force -Path $NodeDir | Out-Null
    # 只拷 node.exe 与 npm/npx（不要整个包，node_modules 有几十 MB）
    Copy-Item (Join-Path $src "node.exe") $NodeDir -Force
    if (Test-Path (Join-Path $src "node_modules\npm")) {
      New-Item -ItemType Directory -Force -Path (Join-Path $NodeDir "node_modules") | Out-Null
      Copy-Item (Join-Path $src "node_modules\npm") (Join-Path $NodeDir "node_modules") -Recurse -Force
      Copy-Item (Join-Path $src "node_modules\npm.cmd") $NodeDir -Force -ErrorAction SilentlyContinue
      Copy-Item (Join-Path $src "node_modules\npx.cmd") $NodeDir -Force -ErrorAction SilentlyContinue
    }
    Write-Host "    node 就绪：$NodeDir"
  } catch {
    Write-Warning "node 下载失败（$($_.Exception.Message)）：agent 将只能用 Electron 自带的 node"
  } finally {
    if (Test-Path $zip) { Remove-Item $zip -Force -ErrorAction SilentlyContinue }
  }
}

# busybox 的 sh.exe：POSIX shell（Windows 没有，cmd 语法又不一样）
$ShDir = Join-Path $RuntimeDist "sh"
if (-not (Test-Path (Join-Path $ShDir "sh.exe"))) {
  New-Item -ItemType Directory -Force -Path $ShDir | Out-Null
  $bb = Join-Path $ShDir "busybox64.exe"
  $urls = @(
    "https://github.com/rmyorston/busybox-w32/releases/download/1_35_0/busybox64.exe",
    "https://github.com/rmyorston/busybox-w32/releases/download/1_34_0/busybox64.exe"
  )
  foreach ($u in $urls) {
    try {
      Write-Host "    下载 busybox-w32 ..."
      Invoke-WebRequest -Uri $u -OutFile $bb -UseBasicParsing
      break
    } catch {
      Write-Host "    $u 不可用：$($_.Exception.Message)"
    }
  }
  if (Test-Path $bb) {
    Copy-Item $bb (Join-Path $ShDir "sh.exe") -Force
    Write-Host "    POSIX shell 就绪：$ShDir\sh.exe"
  } else {
    Write-Warning "busybox 下载失败：shell 将退回 Git Bash / cmd.exe（命令语法会与 mac 不同）"
  }
}

# 配置模板：首次运行复制到 ~/.keeper/config.yaml
Copy-Item (Join-Path $Root "config.yaml") (Join-Path $Dist "config.default.yaml") -Force

# ── 3) electron-builder ──────────────────────────────────────────────────
Write-Step "[3/3] electron-builder 出 .exe 安装包"
Push-Location (Join-Path $Root "desktop")
try {
  if (-not (Test-Path "node_modules\electron-builder")) { npm install --silent --include=dev }
  $env:CSC_IDENTITY_AUTO_DISCOVERY = "false"
  npx electron-builder --win nsis --x64 --publish never
  if ($LASTEXITCODE -ne 0) { throw "electron-builder 失败" }
} finally { Pop-Location }

Write-Host ""
Write-Host "==> 完成" -ForegroundColor Green
Get-ChildItem (Join-Path $Root "desktop\release\*.exe") | ForEach-Object { Write-Host "    $($_.Name)  $([math]::Round($_.Length/1MB))MB" }
