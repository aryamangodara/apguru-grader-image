#!/bin/bash
set -e

echo "Starting FastAPI server..."

# Activate virtual environment if it exists
if [ -d "venv/Scripts" ]; then
    source venv/Scripts/activate
elif [ -d "venv/bin" ]; then
    source venv/bin/activate
fi

# Start the application
uvicorn app.main:app --host localhost --port 8080 --reload
