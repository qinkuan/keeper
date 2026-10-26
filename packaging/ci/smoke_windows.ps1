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

  注意：这些检查跑在 **Windows Server 2022** 上，那台机器本身可能装了
  Python/Node —— 所以每一步都用**绝对路径**指向内置的运行时，不靠 PATH 猜。
#>
param(
  [string]$AppDir = "$PSScriptRoot\..\..\desktop\release\mac-win-unpacked",
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
  $port = 18731
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

Step "4. bash 插件能执行命令" {
  $plugin = Join-Path $PSScriptRoot "..\..\plugins\bash\bin\bash_server"
  if (-not (Test-Path $plugin)) { throw "找不到 bash 插件：$plugin" }
  $ws = Join-Path $TmpHome "ws"; New-Item -ItemType Directory -Force -Path $ws | Out-Null
  $env:PATH = (Split-Path $KernelExe) + ";" + $env:PATH
  $payload = '{"tool":"run","args":{"command":"echo SHELL-OK","timeout":30},"context":{"workspace":"' + $ws + '","read_only":false,"agent_space":"' + $ws + '"}}'
  # Windows 没有 shebang：插件的 bin 是文本脚本，得用解释器启动（内核里也是
  # 这么干的，见 tools/subprocess_tool.py 的 build_argv）
  $py = Join-Path (Split-Path $KernelExe) "python3.exe"
  if (-not (Test-Path $py)) { Copy-Item $KernelExe $py -Force }
  $out = $payload | & $py $plugin 2>$null | Select-Object -Last 1 | Out-String
  if ($out -notmatch "SHELL-OK") { throw "bash 插件没跑通：$out" }
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
