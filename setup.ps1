Write-Host "Starting server setup..." -ForegroundColor Cyan

# 1. Create .env from .env.example if it doesn't exist
if (-Not (Test-Path ".env")) {
    Write-Host "Creating .env from .env.example..."
    Copy-Item ".env.example" ".env"
} else {
    Write-Host ".env already exists."
}

# 2. Create virtual environment if it doesn't exist
if (-Not (Test-Path "venv")) {
    Write-Host "Creating virtual environment 'venv'..."
    python -m venv venv
} else {
    Write-Host "Virtual environment already exists."
}

# 3. Activate the virtual environment
Write-Host "Activating virtual environment..."
if (Test-Path "venv\Scripts\Activate.ps1") {
    . .\venv\Scripts\Activate.ps1
} else {
    Write-Host "Error: Could not find virtual environment activation script." -ForegroundColor Red
    exit 1
}

# 4. Install dependencies
Write-Host "Installing dependencies from requirements.txt..."
python -m pip install -r requirements.txt

Write-Host ""
Write-Host "Setup is complete!" -ForegroundColor Green
Write-Host "Run '.\init.ps1' to start the server." -ForegroundColor Yellow
