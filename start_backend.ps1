#!/usr/bin/env pwsh
<#
.SYNOPSIS
    CleanSight Backend 启动脚本

.DESCRIPTION
    启动 CleanSight Backend 服务，支持本地和外部访问配置

.EXAMPLE
    # 本地开发模式（默认）
    .\start_backend.ps1

    # 生产模式（允许外部访问）
    .\start_backend.ps1 -Host 0.0.0.0 -Port 8000

    # 自定义配置
    .\start_backend.ps1 -Host 192.168.1.100 -Port 9000
#>

param(
    [string]$Host = "127.0.0.1",
    [int]$Port = 8000,
    [switch]$Reload,
    [switch]$Production
)

Write-Host "🚀 CleanSight Backend 启动器" -ForegroundColor Green
Write-Host "=" * 50 -ForegroundColor Cyan

# 检查虚拟环境
if (-not (Test-Path ".venv\Scripts\Activate.ps1")) {
    Write-Host "❌ 虚拟环境不存在" -ForegroundColor Red
    Write-Host "   请先创建虚拟环境: python -m venv .venv" -ForegroundColor Yellow
    exit 1
}

# 激活虚拟环境
Write-Host "⏳ 激活虚拟环境..." -ForegroundColor Yellow
& .\.venv\Scripts\Activate.ps1

# 检查环境变量
Write-Host "⏳ 检查环境配置..." -ForegroundColor Yellow
if (-not (Test-Path ".env")) {
    Write-Host "⚠️  .env 文件不存在，将使用默认配置" -ForegroundColor Yellow
}

# 设置服务器配置
$env:CLEANSIGHT_SERVER_HOST = $Host
$env:CLEANSIGHT_SERVER_PORT = $Port

# 显示配置信息
Write-Host ""
Write-Host "📋 服务器配置:" -ForegroundColor Yellow
Write-Host "   绑定地址: $Host" -ForegroundColor Cyan
Write-Host "   端口: $Port" -ForegroundColor Cyan
Write-Host "   重载模式: $($Reload.ToString())" -ForegroundColor Cyan

if ($Host -eq "0.0.0.0") {
    Write-Host ""
    Write-Host "🌐 外部访问配置:" -ForegroundColor Green
    Write-Host "   本地访问: http://localhost:$Port" -ForegroundColor Cyan
    Write-Host "   网络访问: http://<服务器IP>:$Port" -ForegroundColor Cyan
    Write-Host "   API文档: http://<服务器IP>:$Port/docs" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "⚠️  安全提醒:" -ForegroundColor Yellow
    Write-Host "   - 确保防火墙只开放必要端口" -ForegroundColor Yellow
    Write-Host "   - 考虑使用HTTPS和认证" -ForegroundColor Yellow
    Write-Host "   - 生产环境建议使用反向代理" -ForegroundColor Yellow
} else {
    Write-Host ""
    Write-Host "🏠 本地访问配置:" -ForegroundColor Green
    Write-Host "   访问地址: http://localhost:$Port" -ForegroundColor Cyan
    Write-Host "   API文档: http://localhost:$Port/docs" -ForegroundColor Cyan
}

Write-Host ""
Write-Host "🚀 启动服务..." -ForegroundColor Green

# 构建uvicorn命令
$uvicornCmd = "uvicorn app.main:app"
if ($Reload) {
    $uvicornCmd += " --reload"
}
$uvicornCmd += " --host $Host --port $Port"

Write-Host "   命令: $uvicornCmd" -ForegroundColor Gray
Write-Host ""

# 启动服务
try {
    Invoke-Expression $uvicornCmd
} catch {
    Write-Host "❌ 启动失败: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}