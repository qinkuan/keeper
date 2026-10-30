<#
.SYNOPSIS
  Windows 冒烟测试：在 CI 的 Windows runner 上验「打包产物在没有 Python/Node 的机器上能干活」。

.DESCRIPTION
  逐项检查，每项打印 PASS / FAIL，最后一项失败就退出码非 0（CI 因此不出包）：

    1. 冻结内核能自己起来（目标机没有 python3 也能起），/health 通
    2. 内置 python3（内核 exe 的副本，靠文件名切解释器模式）能跑脚本
    3. 随包的真 node 能跑（Windows 上带；没有就跳过）
    4. bash 插件能执行一条命令（Windows 上用内置 busybox sh / Git Bash / cmd）
    5. 插件绑定用 junction 建得起来（Windows 上没有开发者模式也能建目录联接）
    6. 打包好的桌面 app 能起来，并把内核拉起来
    7. SQLite FTS5 可用（关键词检索的地基）
    8. jieba 分词可用（中文检索的前提）
    9. 检索端点 /search/query 可用（关键词模式）
    10. litert 原生库可加载（向量检索运行时；没装就跳过）
    11. PDF 页面渲染可用（pypdfium2；没装就跳过）


  注意：这些检查跑在 **Windows Server 2022** 上，那台机器本身可能装了
  Python/Node —— 所以每一步都用**绝对路径**指向内置的运行时，不靠 PATH 猜。
#>
param(
  [string]$AppDir = "$PSScriptRoot\..\..\desktop\release\win-unpacked",
  [string]$DistDir = "$PSScriptRoot\..\dist"
)

$ErrorActionPreference = "Continue"
$script:Failures = 0

function Step($name, $block) {
  Write-Host ""
  Write-Host "--- $name" -ForegroundColor Yellow
  try {
    & $block
    Write-Host "PASS: $name" -ForegroundColor Green
  } catch {
    Write-Host "FAIL: $name —— $($_.Exception.Message)" -ForegroundColor Red
    $script:Failures++
  }
}

# 找到打包产物：优先 unpacked 目录，其次从 nsis 安装包里拿不到（那就用 dist）
$KernelExe = Join-Path $DistDir "kernel\keeper-kernel.exe"
$AppExe = $null
if (Test-Path $AppDir) {
  $AppExe = Get-ChildItem -Path $AppDir -Filter "*.exe" -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.DirectoryName -notlike "*Resources*" } | Select-Object -First 1
}

# 内核端口（后面几项要用）
$Port = 18731

# 内置的 python3.exe：内核 exe 的**同目录副本**，靠文件名切解释器模式
# （见 packaging/launcher.py）。后面几步都要用它跑脚本。
$PyExe = Join-Path (Split-Path $KernelExe) "python3.exe"
if ((Test-Path $KernelExe) -and -not (Test-Path $PyExe)) {
  Copy-Item $KernelExe $PyExe -Force
}

Write-Host "kernel: $KernelExe"
Write-Host "app   : $(if ($AppExe) { $AppExe.FullName } else { '(未找到)' })"

# 隔离的 HOME：绝不碰真实用户目录
$TmpHome = Join-Path $env:TEMP ("keeper-smoke-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force -Path (Join-Path $TmpHome ".keeper") | Out-Null
Copy-Item (Join-Path $DistDir "config.default.yaml") (Join-Path $TmpHome ".keeper\config.yaml") -Force
$env:HOME = $TmpHome
$env:USERPROFILE = $TmpHome

Step "1. 冻结内核能自己起来（/health）" {
  if (-not (Test-Path $KernelExe)) { throw "内核不存在：$KernelExe" }
  $port = $Port
  $log = Join-Path $TmpHome "kernel.out"
  $proc = Start-Process -FilePath $KernelExe `
      -ArgumentList @("--config", (Join-Path $TmpHome ".keeper\config.yaml"), "$port") `
      -RedirectStandardOutput $log -RedirectStandardError (Join-Path $TmpHome "kernel.err") `
      -PassThru -NoNewWindow
  $script:KernelProc = $proc
  $ok = $false
  for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Seconds 1
    try {
      $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -UseBasicParsing -TimeoutSec 3
      if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch {}
  }
  if (-not $ok) { throw "内核 40 秒内没就绪（看 $log）" }
}

Step "2. 内置 python3 能跑脚本（内核 exe 副本，靠文件名切解释器模式）" {
  $copy = Join-Path (Split-Path $KernelExe) "python3.exe"
  Copy-Item $KernelExe $copy -Force
  $script = Join-Path $TmpHome "t.py"
  Set-Content -Path $script -Value "import sys; print('PY-OK', sys.version.split()[0])" -Encoding utf8
  $out = & $copy $script 2>&1 | Out-String
  if ($out -notmatch "PY-OK") { throw "内置 python3 没跑起来：$out" }
  Write-Host $out.Trim()
}

Step "3. 随包的真 node 能跑（没有就跳过）" {
  $node = Join-Path $DistDir "runtime\node\node.exe"
  if (-not (Test-Path $node)) { Write-Host "SKIP: 没带 node（app 里用 Electron 自带的）"; return }
  $out = & $node -e "console.log('NODE-OK', process.version)" 2>&1 | Out-String
  if ($out -notmatch "NODE-OK") { throw "内置 node 没跑起来：$out" }
  Write-Host $out.Trim()
}

Step "4. bash 插件能执行命令（Windows 上用内置 POSIX shell）" {
  # 插件 bin 的**文件名会变**（bash_server → bash_server.py，2026-10 那次 CI 就是
  # 因为这里写死了旧名字而红了一项）。所以要跑哪个文件，从插件清单里读出来，
  # 而不是把文件名钉死在这儿；读不到再退回 glob 找。
  $pluginDir = Join-Path $PSScriptRoot "..\..\plugins\bash"
  $plugin = $null
  $manifest = Join-Path $pluginDir "keeper-plugin.json"
  if (Test-Path $manifest) {
    try {
      $m = Get-Content $manifest -Raw | ConvertFrom-Json
      $cmd = [string]$m.bin[0].command
      if ($cmd) {
        $rel = ($cmd -replace '\$\{PLUGIN_ROOT\}', '') -replace '^[/\\]+', ''
        $candidate = Join-Path $pluginDir $rel
        if (Test-Path $candidate) { $plugin = $candidate }
      }
    } catch {
      Write-Host "    （插件清单读不出来，退回 glob 找 bin）"
    }
  }
  if (-not $plugin) {
    $hit = Get-ChildItem -Path (Join-Path $pluginDir "bin") -Filter "bash_server*" `
            -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($hit) { $plugin = $hit.FullName }
  }
  if (-not $plugin) { throw "找不到 bash 插件的 bin（$pluginDir\bin\bash_server*）" }
  $ws = Join-Path $TmpHome "ws"; New-Item -ItemType Directory -Force -Path $ws | Out-Null
  $env:PATH = (Split-Path $KernelExe) + ";" + $env:PATH
  # 让插件用**随包的内置 POSIX shell**（busybox 的 sh.exe）—— 这才是这一步要验的
  # 东西：agent 写的命令在 Windows 上得是 POSIX 语法，不能退到 cmd.exe。
  # 内核平时就是通过 KEEPER_SHELL / KEEPER_SHELL_FLAG 注入给插件的
  # （见 framework/shell.py 与 plugins/bash/bin/bash_server.py）。
  $sh = Join-Path $DistDir "runtime\sh\sh.exe"
  $posix = $false
  if (Test-Path $sh) {
    $env:KEEPER_SHELL = $sh
    $env:KEEPER_SHELL_FLAG = "-c"
    $posix = $true
  } else {
    Write-Host "    （没装上内置 busybox sh，退回系统 shell；只要求命令能跑通）"
  }
  # payload 必须用 ConvertTo-Json 生成：**不能手拼字符串**。Windows 路径里全是
  # 反斜杠（``C:\Users\...\ws``），在 JSON 字符串里 ``\U`` 是非法转义 → 插件会报
  # "Invalid \escape"。mac 上路径是 ``/tmp/...`` 没这问题，只有 Windows 才炸。
  # uname 是 POSIX 专属命令：cmd.exe 没有它 → 用来确认真的落在 POSIX shell 上。
  $payload = @{
    tool    = "run"
    args    = @{ command = "echo SHELL-OK; uname -s"; timeout = 30 }
    context = @{ workspace = $ws; read_only = $false; agent_space = $ws }
  } | ConvertTo-Json -Depth 5 -Compress
  # Windows 没有 shebang：插件的 bin 是文本脚本，得用解释器启动（内核里也是
  # 这么干的，见 tools/subprocess_tool.py 的 build_argv）
  $py = Join-Path (Split-Path $KernelExe) "python3.exe"
  if (-not (Test-Path $py)) { Copy-Item $KernelExe $py -Force }
  $out = $payload | & $py $plugin 2>$null | Select-Object -Last 1 | Out-String
  if ($out -notmatch "SHELL-OK") { throw "bash 插件没跑通：$out" }
  if ($posix -and $out -notmatch "Linux|Windows|MINGW|MSYS|CYGWIN|Darwin") {
    throw "用的不是 POSIX shell（uname 没输出，cmd.exe 会把整句当 echo）：$out"
  }
  Write-Host $out.Trim()
}

Step "5. 插件绑定（junction）在没有开发者模式时也建得起来" {
  $code = @"
import os, sys
from pathlib import Path
import tempfile
d = Path(tempfile.mkdtemp())
real = d / 'real'; real.mkdir()
link = d / 'link'
from keeper.framework.linkutil import make_dir_link, is_link
make_dir_link(real, link, relative=False)
assert is_link(link), '建出来的东西不是链接'
assert Path(os.path.realpath(link)) == Path(os.path.realpath(real)), '链接指向不对'
print('LINK-OK', link)
"@
  $script = Join-Path $TmpHome "link_test.py"
  Set-Content -Path $script -Value $code -Encoding utf8
  $out = & $KernelExe --py $script 2>&1 | Out-String
  if ($out -notmatch "LINK-OK") { throw "junction 建不起来：$out" }
  Write-Host $out.Trim()
}

Step "6. 打包好的桌面 app 能起来并拉起内核" {
  if (-not $AppExe) { throw "找不到打包好的 app（$AppDir）" }
  $outFile = Join-Path $TmpHome "app.out"
  $proc = Start-Process -FilePath $AppExe.FullName `
      -ArgumentList @("--user-data-dir", (Join-Path $TmpHome "udata")) `
      -RedirectStandardOutput $outFile -RedirectStandardError (Join-Path $TmpHome "app.err") `
      -PassThru
  $script:AppProc = $proc
  Start-Sleep -Seconds 25
  $kernel = Get-Process -Name "keeper-kernel" -ErrorAction SilentlyContinue
  if (-not $kernel) { throw "app 起来了但没拉起内核（看 $outFile）" }
  Write-Host "内核进程：$($kernel.Id -join ',')"
}

# ── 检索能力（关键词路）：SQLite FTS5 + jieba + 检索端点 ───────────────
Step "7. SQLite FTS5 可用（关键词检索的地基）" {
  if (-not (Test-Path $PyExe)) { throw "没有内置 python3.exe：$PyExe" }
  $script = Join-Path $TmpHome "fts5_test.py"
  Set-Content -Path $script -Encoding utf8 -Value @'
import sqlite3
c = sqlite3.connect(":memory:")
c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
print("FTS5-OK")
'@
  $out = & $PyExe $script 2>&1 | Out-String
  if ($out -notmatch "FTS5-OK") { throw "FTS5 不可用（Windows 的 Python 没编 FTS5？）：$out" }
  Write-Host $out.Trim()
}

Step "8. jieba 分词可用（中文检索的前提）" {
  if (-not (Test-Path $PyExe)) { throw "没有内置 python3.exe：$PyExe" }
  $script = Join-Path $TmpHome "jieba_test.py"
  Set-Content -Path $script -Encoding utf8 -Value @'
import jieba
print("JIEBA-OK", "/".join(jieba.cut("向量检索测试")))
'@
  $out = & $PyExe $script 2>&1 | Out-String
  if ($out -notmatch "JIEBA-OK") { throw "jieba 不可用（词典没打进包？）：$out" }
  Write-Host $out.Trim()
}

Step "9. 检索端点 /search/query 可用（关键词模式，不需要模型）" {
  $body = @{ query = "向量检索"; top_k = 5; mode = "keyword" } | ConvertTo-Json
  $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/search/query" -Method POST `
      -Body $body -ContentType "application/json" -UseBasicParsing -TimeoutSec 15
  if ($r.StatusCode -ne 200) { throw "检索端点返回 $($r.StatusCode)：$($r.Content)" }
  Write-Host "检索端点正常：$($r.Content)"
}

Step "10. litert 原生库可加载（向量检索运行时；没装就跳过）" {
  if (-not (Test-Path $PyExe)) { throw "没有内置 python3.exe：$PyExe" }
  $script = Join-Path $TmpHome "litert_test.py"
  Set-Content -Path $script -Encoding utf8 -Value @'
try:
    import litert_lm._ffi as ffi
except ImportError:
    print("LITERT-SKIP")
else:
    print("LITERT-OK", ffi._get_lib())
'@
  $out = & $PyExe $script 2>&1 | Out-String
  if ($out -match "LITERT-SKIP") {
    Write-Host "SKIP: 没装 litert-lm（这个平台没有 wheel）——只有关键词检索可用"
    return
  }
  if ($out -notmatch "LITERT-OK") { throw "litert 原生库加载失败：$out" }
  Write-Host $out.Trim()
}

Step "11. PDF 页面渲染可用（pypdfium2；没装就跳过）" {
  # 「我的空间」预览 PDF 靠服务端把整页渲染成 PNG（pdfpage/render.py）。
  # pypdfium2 的原生库（Windows: pdfium.dll）是 ctypes 按名加载的，**最容易在
  # 打包时丢**——丢了的表现是「开发态能预览、装出来的版本一律渲染失败」。
  # 这里在**冻结产物**里真渲染一页出来，别等用户发现。
  if (-not (Test-Path $PyExe)) { throw "没有内置 python3.exe：$PyExe" }
  $script = Join-Path $TmpHome "pdfium_test.py"
  Set-Content -Path $script -Encoding utf8 -Value @'
# 用 find_spec 判「装没装」而不是 try-import：装没装 → SKIP；
# 装了却加载不了（PDFium 原生库没进包）→ 必须**报错**，不能被当成 SKIP 蒙过去。
import importlib.util
try:
    spec = importlib.util.find_spec("pypdfium2")
except Exception:
    spec = None
if spec is None:
    print("PDFIUM-SKIP")
else:
    import os, tempfile
    import pypdfium2 as pdfium
    from keeper.pdfpage.render import render_page
    doc = pdfium.PdfDocument.new()
    doc.new_page(200, 300)
    path = os.path.join(tempfile.mkdtemp(), "t.pdf")
    doc.save(path)
    doc.close()
    png = render_page(path, 1, scale=1.0)
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "渲染出来的不是 PNG"
    print("PDFIUM-OK", len(png))
'@
  $out = & $PyExe $script 2>&1 | Out-String
  if ($out -match "PDFIUM-SKIP") {
    Write-Host "SKIP: 没装 pypdfium2（PDF 预览不可用）"
    return
  }
  if ($out -notmatch "PDFIUM-OK") { throw "PDF 页面渲染不可用：$out" }
  Write-Host $out.Trim()
}

# 收尾
foreach ($p in @($script:KernelProc, $script:AppProc)) {
  if ($p -and -not $p.HasExited) { try { Stop-Process -Id $p.Id -Force } catch {} }
}

Write-Host ""
if ($script:Failures -gt 0) {
  Write-Host "==> $script:Failures 项失败" -ForegroundColor Red
  exit 1
}
Write-Host "==> 全部通过" -ForegroundColor Green
