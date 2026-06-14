# CleanSight Backend startup script for Windows.
# Usage: .\start_backend.ps1 [dev|test|prod]

param(
    [ValidateSet("dev", "test", "prod")]
    [string]$EnvName = "dev"
)

Write-Host "Starting CleanSight Backend..." -ForegroundColor Cyan
Write-Host ""

if (Test-Path ".\.venv312\Scripts\Activate.ps1") {
    & ".\.venv312\Scripts\Activate.ps1"
} elseif (Test-Path ".\.venv\Scripts\Activate.ps1") {
    & ".\.venv\Scripts\Activate.ps1"
} else {
    Write-Host "Error: virtual environment not found at .\.venv312 or .\.venv" -ForegroundColor Red
    Write-Host "Create it first: python -m venv .venv312" -ForegroundColor Yellow
    exit 1
}

switch ($EnvName) {
    "dev" {
        $env:CLEANSIGHT_ENV = "dev"
        Write-Host "Environment: Development (.env.dev)" -ForegroundColor Green
    }
    "test" {
        $env:CLEANSIGHT_ENV = "test"
        Write-Host "Environment: Test (.env.test)" -ForegroundColor Yellow
    }
    "prod" {
        $env:CLEANSIGHT_ENV = "prod"
        Write-Host "Environment: Production (.env)" -ForegroundColor Red
    }
}

Write-Host ""

if (-not (Test-Path "logs")) {
    New-Item -ItemType Directory -Path "logs" | Out-Null
}

python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --log-config logging_config.json
