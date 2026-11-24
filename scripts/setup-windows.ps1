#Requires -Version 5.1
<#
.SYNOPSIS
  clash-radar 一键部署（Windows）

.DESCRIPTION
  自动探测代理客户端的配置目录和控制器端口，生成 .env，然后构建并启动容器。
  支持 FlClash / Clash Verge Rev / Clash Nyanpasu 等基于 mihomo 内核的客户端。

.EXAMPLE
  .\scripts\setup-windows.ps1
  .\scripts\setup-windows.ps1 -Port 9097 -BudgetGb 200
  .\scripts\setup-windows.ps1 -ClashApi http://host.docker.internal:9097 -NoStart
#>
[CmdletBinding()]
param(
    [string]$ClashApi = '',
    [int]$Port = 0,
    [double]$BudgetGb = 0,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "!!  $msg" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- 1. Docker
Step '检查 Docker'
try {
    $null = docker info 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'docker info failed' }
} catch {
    Fail 'Docker 没在运行。先启动 Docker Desktop，再重跑本脚本。'
}

# ---------------------------------------------------------- 2. 客户端配置目录
Step '定位代理客户端配置目录'
$candidates = @()
if ($env:CLASH_CONFIG_DIR) { $candidates += $env:CLASH_CONFIG_DIR }
$candidates += @(
    (Join-Path $env:APPDATA 'com.follow\clash'),                              # FlClash
    (Join-Path $env:APPDATA 'io.github.clash-verge-rev.clash-verge-rev'),     # Clash Verge Rev
    (Join-Path $env:APPDATA 'clash-verge'),
    (Join-Path $env:APPDATA 'Clash Nyanpasu'),
    (Join-Path $env:APPDATA 'clash-nyanpasu'),
    (Join-Path $env:LOCALAPPDATA 'com.follow\clash')
)

$configDir = $null
foreach ($dir in $candidates) {
    if (-not $dir -or -not (Test-Path -LiteralPath $dir)) { continue }
    if (Test-Path -LiteralPath (Join-Path $dir 'shared_preferences.json') -PathType Leaf) { $configDir = $dir; break }
    if (Test-Path -LiteralPath (Join-Path $dir 'config.yaml') -PathType Leaf) { $configDir = $dir; break }
}
if (-not $configDir) {
    Write-Host '    没找到已知的客户端配置目录。' -ForegroundColor Yellow
    Write-Host '    请手动把配置目录写进 .env 的 FLCLASH_DIR，或用 -ClashApi + CLASH_SECRET 指定。' -ForegroundColor Yellow
    $configDir = Join-Path $PWD 'flclash-config'
    if (-not (Test-Path -LiteralPath $configDir)) { New-Item -ItemType Directory -Path $configDir | Out-Null }
} else {
    Write-Host "    $configDir"
}

# ------------------------------------------------------------ 3. 控制器端口
Step '探测控制器端口'
$detectedPort = 0
foreach ($file in @('config.yaml', 'clash-verge.yaml', 'verge.yaml')) {
    $path = Join-Path $configDir $file
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
    $text = Get-Content -LiteralPath $path -Raw -Encoding UTF8
    if ($text -match 'external-controller:\s*"?[\d\.]+:(\d+)') { $detectedPort = [int]$Matches[1]; break }
}

function Test-Port([int]$p) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect('127.0.0.1', $p, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(400)
        if ($ok -and $client.Connected) { $client.Close(); return $true }
        $client.Close(); return $false
    } catch { return $false }
}

if (-not $Port) {
    if ($detectedPort -and (Test-Port $detectedPort)) {
        $Port = $detectedPort
    } else {
        foreach ($p in 9090, 9097, 9091, 9095, 9093) {
            if (Test-Port $p) { $Port = $p; break }
        }
    }
}
if (-not $Port) { $Port = 9090; Write-Host '    没探测到监听端口，默认用 9090' -ForegroundColor Yellow }
else { Write-Host "    $Port" }

if (-not $ClashApi) { $ClashApi = "http://host.docker.internal:$Port" }

# ---------------------------------------------------------------- 4. 写 .env
Step '生成 .env'
if (Test-Path -LiteralPath '.env') {
    $backup = ".env.bak." + (Get-Date -Format 'yyyyMMddHHmmss')
    Copy-Item -LiteralPath '.env' -Destination $backup
    Write-Host "    已备份旧配置到 $backup"
}
if (-not $BudgetGb) { $BudgetGb = 100 }

$envDir = $configDir -replace '\\', '/'
$envLines = @(
    '# 由 scripts/setup-windows.ps1 生成，可手工修改',
    "FLCLASH_DIR=$envDir",
    "CLASH_API=$ClashApi",
    'CLASH_SECRET=',
    'PORT=8787',
    "BUDGET_GB=$BudgetGb",
    'BILLING_MODE=updown',
    'TZ_OFFSET_HOURS=8',
    'DETAIL_RETENTION_DAYS=30'
)
# 必须无 BOM：compose 会把 BOM 当成第一个键的一部分
[System.IO.File]::WriteAllLines((Join-Path $PWD '.env'), $envLines, (New-Object System.Text.UTF8Encoding($false)))
$envLines | ForEach-Object { Write-Host "    $_" }

if ($NoStart) {
    Step '已跳过启动（-NoStart）。手动执行： docker compose up -d --build'
    exit 0
}

# ---------------------------------------------------------------- 5. 启动
Step '构建并启动容器'
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { Fail 'docker compose 失败，看上面的报错。' }

Step '等待健康检查'
$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    try {
        $res = Invoke-RestMethod -Uri 'http://127.0.0.1:8787/api/health' -TimeoutSec 3
        if ($res.ok) { $healthy = $true; break }
    } catch { }
}
if ($healthy) {
    Write-Host ''
    Write-Host '  ✅ 起来了： http://127.0.0.1:8787' -ForegroundColor Green
    Write-Host '     前 1 小时只建立基线，之后才给「预计几天用完」。' -ForegroundColor DarkGray
} else {
    Write-Host '  容器起来了但接口没响应，执行 docker compose logs 看日志。' -ForegroundColor Yellow
}
