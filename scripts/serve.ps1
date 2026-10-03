#Requires -Version 5.1
<#
ChronoSino 服务启动脚本（Windows）

  后端 API :8000   （scripts\serve.py，日志 backend\logs\serve.log）
  前端界面 :5173   （vite dev，代理 /api → :8000）

用法：
  .\scripts\serve.ps1                 # 启动后端，并等待就绪
  .\scripts\serve.ps1 -Frontend       # 后端 + 前端一起起
  .\scripts\serve.ps1 -Minimized      # 窗口最小化（后台跑）
  .\scripts\serve.ps1 -Status         # 只看状态，不启动
  .\scripts\serve.ps1 -Stop           # 停止（按端口定位进程）
  .\scripts\serve.ps1 -Restart -Frontend

说明：两个窗口都靠 Start-Process 另开窗口启动，关掉本脚本所在的
PowerShell 窗口不会把它们带走；但**关掉它们自己的窗口等于停服务**。
#>
param(
    [switch]$Frontend,      # 同时启动前端
    [switch]$Minimized,     # 新窗口最小化
    [switch]$Stop,          # 停止服务
    [switch]$Restart,       # 先停再起
    [switch]$Status,        # 只看状态
    [switch]$NoWait,        # 启动后不等待就绪
    [int]$Port = 8000,
    [int]$WebPort = 5173,
    [int]$TimeoutSec = 240  # 后端首次加载图库约 1~2 分钟
)

$ErrorActionPreference = 'Stop'
$Root      = Split-Path -Parent $PSScriptRoot
$Backend   = Join-Path $Root 'backend'
$WebDir    = Join-Path $Root 'frontend'
$Py        = Join-Path $Root '.venv/Scripts/python.exe'
$ApiHealth = "http://127.0.0.1:$Port/api/health"
$WebUrl    = "http://localhost:$WebPort"

function Get-Listener($p) {
    Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
}

function Stop-OnPort($p, $name) {
    $c = Get-Listener $p
    if (-not $c) { Write-Host "  $name :$p 未运行" -ForegroundColor DarkGray; return }
    foreach ($conn in $c) {
        $procId = $conn.OwningProcess
        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Host "  已停止 $name (pid=$procId, :$p)" -ForegroundColor Yellow
        } catch {
            Write-Host "  停止 $name 失败 (pid=$procId): $_" -ForegroundColor Red
        }
    }
}

function Wait-Ready($url, $sec, $name) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    Write-Host "  等待 $name 就绪" -NoNewline
    while ($sw.Elapsed.TotalSeconds -lt $sec) {
        try {
            $r = Invoke-WebRequest -Uri $url -TimeoutSec 5 -UseBasicParsing
            if ($r.StatusCode -eq 200) {
                Write-Host "  OK ($([int]$sw.Elapsed.TotalSeconds)s)" -ForegroundColor Green
                return $true
            }
        } catch { }
        Write-Host "." -NoNewline
        Start-Sleep -Seconds 3
    }
    Write-Host "  超时" -ForegroundColor Red
    return $false
}

function Show-Status {
    $api = Get-Listener $Port
    $web = Get-Listener $WebPort
    if ($api) { Write-Host "  后端 :$Port  运行中 (pid=$($api[0].OwningProcess))" -ForegroundColor Green }
    else      { Write-Host "  后端 :$Port  未运行" -ForegroundColor DarkGray }
    if ($web) { Write-Host "  前端 :$WebPort  运行中 (pid=$($web[0].OwningProcess))" -ForegroundColor Green }
    else      { Write-Host "  前端 :$WebPort  未运行" -ForegroundColor DarkGray }
    if ($api) {
        try {
            $h = (Invoke-WebRequest -Uri $ApiHealth -TimeoutSec 5 -UseBasicParsing).Content
            Write-Host "  health: $h" -ForegroundColor DarkGray
        } catch { Write-Host "  health: 请求失败 $_" -ForegroundColor Red }
    }
}

# ---------------------------------------------------------------- 停止 / 状态
if ($Status) { Show-Status; exit 0 }

if ($Stop -or $Restart) {
    Write-Host "停止服务：" -ForegroundColor Cyan
    Stop-OnPort $WebPort '前端'
    Stop-OnPort $Port   '后端'
    Start-Sleep -Seconds 2
    if ($Stop -and -not $Restart) { exit 0 }
}

# ---------------------------------------------------------------- 启动
if (-not (Test-Path $Py)) { throw "找不到 Python：$Py（请确认 .venv 存在）" }

$style = if ($Minimized) { 'Minimized' } else { 'Normal' }

$apiUp = Get-Listener $Port
if ($apiUp) {
    Write-Host "  后端 :$Port 已在运行 (pid=$($apiUp[0].OwningProcess))，跳过启动" -ForegroundColor DarkGray
} else {
    Write-Host "启动后端 :$Port ..." -ForegroundColor Cyan
    Start-Process -FilePath $Py -ArgumentList '-u', 'scripts\serve.py' `
        -WorkingDirectory $Backend -WindowStyle $style
}

if ($Frontend) {
    $webUp = Get-Listener $WebPort
    if ($webUp) {
        Write-Host "  前端 :$WebPort 已在运行 (pid=$($webUp[0].OwningProcess))，跳过启动" -ForegroundColor DarkGray
    } else {
        Write-Host "启动前端 :$WebPort ..." -ForegroundColor Cyan
        Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', 'npm run dev' `
            -WorkingDirectory $WebDir -WindowStyle $style
    }
}

if ($NoWait) { Show-Status; exit 0 }

$ok = Wait-Ready $ApiHealth $TimeoutSec '后端'
if ($Frontend) { $null = Wait-Ready $WebUrl 60 '前端' }

Write-Host ""
if ($ok) {
    Write-Host "就绪：" -ForegroundColor Green
    Write-Host "  API   $ApiHealth"
    if ($Frontend) { Write-Host "  界面  $WebUrl" }
    Write-Host ""
    Write-Host "常用命令：" -ForegroundColor DarkGray
    Write-Host "  看状态   .\scripts\serve.ps1 -Status" -ForegroundColor DarkGray
    Write-Host "  停服务   .\scripts\serve.ps1 -Stop" -ForegroundColor DarkGray
    Write-Host "  后端日志 Get-Content backend\logs\serve.log -Tail 20 -Wait" -ForegroundColor DarkGray
    Write-Host "  提交构建 curl -X POST http://127.0.0.1:$Port/api/ingest/<book_id> -H 'Content-Type: application/json' -d '{\"use_llm\":true}'" -ForegroundColor DarkGray
    exit 0
} else {
    Write-Host "后端未就绪，看日志：Get-Content backend\logs\serve.log -Tail 40" -ForegroundColor Red
    exit 1
}
