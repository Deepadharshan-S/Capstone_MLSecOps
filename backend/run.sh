#!/bin/bash
# Exit immediately if a command exits with a non-zero status
set -e

# Get the directory of the script
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Change directory to the backend directory where alembic.ini and .env are located
cd "$SCRIPT_DIR"

# Determine the correct command to run uv
if command -v uv &> /dev/null; then
    UV_CMD="uv"
elif [ -f "../.venv/bin/python" ] && ../.venv/bin/python -m uv --version &> /dev/null; then
    UV_CMD="../.venv/bin/python -m uv"
elif python -m uv --version &> /dev/null; then
    UV_CMD="python -m uv"
elif python3 -m uv --version &> /dev/null; then
    UV_CMD="python3 -m uv"
else
    echo "Error: uv is not installed or not found in PATH." >&2
    exit 1
fi

echo "Running database migrations..."
$UV_CMD run alembic upgrade head

echo "Seeding database..."
$UV_CMD run python -m app.db.seed_db

echo "Starting FastAPI application..."
$UV_CMD run uvicorn app.main:app --host 127.0.0.1 --port 8001
