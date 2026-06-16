#!/bin/bash
set -e

echo "Starting server setup..."

# 1. Create .env from .env.example if it doesn't exist
if [ ! -f ".env" ]; then
    echo "Creating .env from .env.example..."
    cp .env.example .env
else
    echo ".env already exists."
fi

# 2. Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment 'venv'..."
    python -m venv venv
else
    echo "Virtual environment already exists."
fi

# 3. Activate the virtual environment based on OS
echo "Activating virtual environment..."
if [ -d "venv/Scripts" ]; then
    # Windows (e.g. Git Bash)
    source venv/Scripts/activate
elif [ -d "venv/bin" ]; then
    # Mac/Linux
    source venv/bin/activate
else
    echo "Error: Could not find virtual environment activation script."
    exit 1
fi

# 4. Install dependencies
echo "Installing dependencies from requirements.txt..."
pip install -r requirements.txt

echo ""
echo "Setup is complete!"
echo "Run 'bash init.sh' to start the server."
