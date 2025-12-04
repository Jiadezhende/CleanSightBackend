#!/usr/bin/env pwsh
<#
.SYNOPSIS
    快速启动集成测试的便捷脚本

.DESCRIPTION
    自动检查前置条件并启动完整的集成测试

.EXAMPLE
    .\run_integration_test.ps1
    .\run_integration_test.ps1 -Duration 60
#>

param(
    [int]$Duration = 30,
    [int]$TaskId = 0,
    [string]$ClientId = "integration_test_client"
)

Write-Host "=" -NoNewline -ForegroundColor Cyan
Write-Host ("=" * 69) -ForegroundColor Cyan
Write-Host "🚀 CleanSightBackend 集成测试启动器" -ForegroundColor Green
Write-Host "=" -NoNewline -ForegroundColor Cyan
Write-Host ("=" * 69) -ForegroundColor Cyan

# 检查虚拟环境
if (-not (Test-Path ".venv\Scripts\Activate.ps1")) {
    Write-Host "❌ 虚拟环境不存在" -ForegroundColor Red
    Write-Host "   请先创建虚拟环境: python -m venv .venv" -ForegroundColor Yellow
    exit 1
}

# 激活虚拟环境
Write-Host "⏳ 激活虚拟环境..." -ForegroundColor Yellow
& .\.venv\Scripts\Activate.ps1

# 检查 MediaMTX
Write-Host "⏳ 检查 MediaMTX..." -ForegroundColor Yellow
$mediamtxRunning = netstat -ano | Select-String ":1935" | Select-String "LISTENING"
if (-not $mediamtxRunning) {
    Write-Host "⚠️  MediaMTX 未运行" -ForegroundColor Yellow
    Write-Host "   请在另一个终端启动: cd mediamtx_v1.15.4; .\mediamtx.exe" -ForegroundColor Yellow
    $continue = Read-Host "   是否继续？(y/n)"
    if ($continue -ne "y") {
        exit 1
    }
}

# 检查后端 API
Write-Host "⏳ 检查后端 API..." -ForegroundColor Yellow
try {
    $response = Invoke-WebRequest -Uri "http://localhost:8000/ai/status" -TimeoutSec 2 -ErrorAction Stop
    if ($response.StatusCode -eq 200) {
        Write-Host "✅ 后端 API 运行正常" -ForegroundColor Green
    }
} catch {
    Write-Host "⚠️  后端 API 未运行" -ForegroundColor Yellow
    Write-Host "   请在另一个终端启动: uvicorn app.main:app --reload" -ForegroundColor Yellow
    $continue = Read-Host "   是否继续？(y/n)"
    if ($continue -ne "y") {
        exit 1
    }
}

# 运行集成测试
Write-Host "`n🚀 启动集成测试..." -ForegroundColor Green
Write-Host "   任务 ID: $TaskId" -ForegroundColor Cyan
Write-Host "   客户端 ID: $ClientId" -ForegroundColor Cyan
Write-Host "   测试时长: $Duration 秒`n" -ForegroundColor Cyan

python integration_tests/test_full_pipeline.py --task_id $TaskId --client_id $ClientId --duration $Duration

$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "`n🎉 测试成功完成！" -ForegroundColor Green
} else {
    Write-Host "`n❌ 测试失败 (退出码: $exitCode)" -ForegroundColor Red
}

exit $exitCode
