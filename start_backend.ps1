# CleanSight Backend 启动脚本（Windows 开发机）
# 用法: .\start_backend.ps1 [dev|test|prod]
#
# 一条命令拉起整套环境：RTSP 网关（含 MediaMTX）+ 后端。
# 端口属于基础设施参数，基准值写在本脚本里，按环境自动分配：
#   dev / prod → 基准端口（与 Linux 一致）
#   test       → 整体 +100（与同机 prod 隔离）
# .env* 只放业务参数（DB / 告警 URL / 密钥 / 网关 IP 白名单），不放端口。

param(
    [string]$env = "dev"  # 默认开发环境
)

Write-Host "Starting CleanSight Backend..." -ForegroundColor Cyan
Write-Host ""

# 激活虚拟环境
if (Test-Path ".\.venv\Scripts\Activate.ps1") {
    & ".\.venv\Scripts\Activate.ps1"
} else {
    Write-Host "Error: Virtual environment not found at .\.venv" -ForegroundColor Red
    Write-Host "Please create it first: python -m venv .venv" -ForegroundColor Yellow
    exit 1
}

# 根据参数设置环境
switch ($env.ToLower()) {
    "dev" {
        $env:CLEANSIGHT_ENV = 'dev'
        Write-Host "Environment: Development (.env.dev)" -ForegroundColor Green
    }
    "test" {
        $env:CLEANSIGHT_ENV = 'test'
        Write-Host "Environment: Test (.env.test)" -ForegroundColor Yellow
    }
    "prod" {
        $env:CLEANSIGHT_ENV = 'prod'
        Write-Host "Environment: Production (.env)" -ForegroundColor Red
    }
    default {
        Write-Host "Error: Invalid environment '$env'" -ForegroundColor Red
        Write-Host "Usage: .\start_backend.ps1 [dev|test|prod]" -ForegroundColor Yellow
        exit 1
    }
}

# ===== 端口分配（基准 + 环境偏移）=====
# 基准端口（dev/prod 直接用）；test 整体 +100 以与同机 prod 隔离。
$BaseBackend  = 8000   # 后端 HTTP/WS
$BaseProxy    = 8004   # 网关对外 RTSP（客户端连这个）
$BaseInternal = 18004  # MediaMTX RTSP（内部，网关回源）
$BaseRtp      = 8002   # MediaMTX RTP（UDP，内部）
$BaseRtcp     = 8003   # MediaMTX RTCP（UDP，内部）

$Offset = if ($env.ToLower() -eq "test") { 100 } else { 0 }

$BackendPort  = $BaseBackend  + $Offset
$ProxyPort    = $BaseProxy    + $Offset
$InternalPort = $BaseInternal + $Offset
$RtpPort      = $BaseRtp      + $Offset
$RtcpPort     = $BaseRtcp     + $Offset

# 导出给三方进程（.env* 不含端口，端口完全由这里注入）：
#   后端：识别本机 MediaMTX 并回源改写（app/services/stream/service.py:_rewrite_rtsp_url）
$env:CLEANSIGHT_MEDIAMTX_PROXY_PORT    = "$ProxyPort"
$env:CLEANSIGHT_MEDIAMTX_INTERNAL_PORT = "$InternalPort"
#   网关：对外监听端口 + 回源目标端口（mediamtx_gateway/main.py 认 GATEWAY_*）
$env:GATEWAY_LISTEN_PORT = "$ProxyPort"
$env:GATEWAY_TARGET_PORT = "$InternalPort"
#   MediaMTX：覆盖 mediamtx.yml 中对应监听地址（MediaMTX 原生认 MTX_*）
$env:MTX_RTSPADDRESS = "127.0.0.1:$InternalPort"
$env:MTX_RTPADDRESS  = "127.0.0.1:$RtpPort"
$env:MTX_RTCPADDRESS = "127.0.0.1:$RtcpPort"

Write-Host ""
Write-Host "Ports ($env):"
Write-Host "  backend HTTP/WS       : $BackendPort"
Write-Host "  gateway RTSP (extern) : $ProxyPort"
Write-Host "  MediaMTX RTSP (intern): $InternalPort"
Write-Host "  MediaMTX RTP/RTCP     : $RtpPort / $RtcpPort"
Write-Host ""

# 确保日志目录存在（log-config 在应用代码前加载，需提前创建）
if (-not (Test-Path "logs")) { New-Item -ItemType Directory -Path "logs" | Out-Null }

# 启动前端口占用自检：只报告，不代为杀进程。
# 常见成因是上次被强杀 / Ctrl+C 后遗留的孤儿 mediamtx.exe，但占用者也可能是无关服务
# 或人为在调的另一个实例，杀不杀由人决定。这里 fail fast，避免后面 bind 失败的迷惑报错。
$conflicts = @()
foreach ($p in @(
    @{ Label = "gateway RTSP (extern)"; Port = $ProxyPort },
    @{ Label = "MediaMTX RTSP (intern)"; Port = $InternalPort }
)) {
    $owners = (Get-NetTCPConnection -LocalPort $p.Port -State Listen -ErrorAction SilentlyContinue).OwningProcess
    foreach ($procId in ($owners | Select-Object -Unique)) {
        if (-not $procId) { continue }
        $desc = "PID=$procId"
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($proc) {
            # StartTime 对高权限进程会抛访问拒绝，取不到就算了
            try { $desc += ", started $($proc.StartTime.ToString('yyyy-MM-dd HH:mm:ss'))" } catch {}
            $desc = "$($proc.ProcessName) ($desc)"
        }
        $conflicts += "  $($p.Port)  $($p.Label)  <- $desc"
    }
}

if ($conflicts.Count -gt 0) {
    Write-Host "Port already in use, refusing to start:" -ForegroundColor Red
    $conflicts | ForEach-Object { Write-Host $_ -ForegroundColor Red }
    Write-Host ""
    Write-Host "If it is a leftover mediamtx.exe / gateway python from a previous run, clean it up manually:" -ForegroundColor Yellow
    Write-Host "  Get-Process -Id <PID> | Format-List Name, Path, StartTime" -ForegroundColor Yellow
    Write-Host "  taskkill /T /F /PID <PID>" -ForegroundColor Yellow
    exit 1
}

# 后台启动 RTSP 网关（其自身拉起并守护 MediaMTX，MediaMTX 继承上面设置的 MTX_*）
$gw = Start-Process -FilePath "python" -ArgumentList "-m", "mediamtx_gateway.main" -NoNewWindow -PassThru

try {
    # reload 仅用于开发：prod/test 不挂文件监听
    $reload = if ($env.ToLower() -eq "dev") { "--reload" } else { "" }

    # 前台启动后端
    $uvArgs = @("app.main:app", "--host", "0.0.0.0", "--port", "$BackendPort", "--log-config", "logging_config.json")
    if ($reload) { $uvArgs += $reload }
    & uvicorn @uvArgs
}
finally {
    # 后端退出时一并清理网关及其子进程（MediaMTX）。
    # 必须用 taskkill /T：Stop-Process -Force 只杀网关 python，会把 mediamtx.exe 留成孤儿占住 18004。
    if ($gw) { taskkill /T /F /PID $gw.Id 2>$null | Out-Null }
}
