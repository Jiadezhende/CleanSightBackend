#!/bin/bash
# Environment startup script

echo "Starting environment..."

# Activate virtual environment
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
else
    echo "Error: Virtual environment not found"
    exit 1
fi

# Set production mode, 0 for development, 1 for production
export CLEANSIGHT_PROD=0

if [ "$CLEANSIGHT_PROD" -eq 1 ]; then
    echo "Production mode enabled"
else
    echo "Development mode enabled"
fi

# Start service
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
