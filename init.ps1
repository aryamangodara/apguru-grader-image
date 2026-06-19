Write-Host "Starting FastAPI server..." -ForegroundColor Cyan

# Activate virtual environment if it exists
if (Test-Path "venv\Scripts\Activate.ps1") {
    . .\venv\Scripts\Activate.ps1
}

# Start the application
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
