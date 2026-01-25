# Environment startup script
Write-Host "Starting environment..." -ForegroundColor Cyan

# Activate virtual environment
if (Test-Path ".\.venv\Scripts\Activate.ps1") {
    & ".\.venv\Scripts\Activate.ps1"
} else {
    Write-Host "Error: Virtual environment not found" -ForegroundColor Red
    exit 1
}

# Set production mode, 0 for development, 1 for production
$env:CLEANSIGHT_PROD = '0'

if ($env:CLEANSIGHT_PROD -eq '1') {
    Write-Host "Production mode enabled" -ForegroundColor Green
} else {
    Write-Host "Development mode enabled" -ForegroundColor Green
}

# Start service with colored logging
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --log-config logging_config.json
