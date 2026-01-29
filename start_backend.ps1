# CleanSight Backend 启动脚本（Windows）
# 用法: .\start_backend.ps1 [dev|test|prod]

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

Write-Host ""

# 启动服务
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --log-config logging_config.json
