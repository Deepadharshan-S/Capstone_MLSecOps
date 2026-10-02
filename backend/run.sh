#!/bin/bash
# Exit immediately if a command exits with a non-zero status
set -e

# Get the directory of the script
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Change directory to the backend directory where alembic.ini and .env are located
cd "$SCRIPT_DIR"

# MLflow prints emoji to stdout ("🏃 View run …"). Some Windows/WSL terminals
# default to a legacy code page and raise UnicodeEncodeError inside that write,
# which surfaces as a 500 from POST /models/upload. Force UTF-8.
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

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

# Wait for Postgres to accept connections instead of dying on the first
# migration attempt (Docker Desktop / `docker compose up -d postgres` often
# needs a few seconds after boot).
echo "Waiting for database..."
DB_HOST="$(grep -E '^POSTGRES_HOST=' .env | cut -d= -f2- | tr -d '"'\''')"
DB_PORT="$(grep -E '^POSTGRES_PORT=' .env | cut -d= -f2- | tr -d '"'\''')"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"

MAX_DB_WAIT_SECONDS=60
waited=0
until (echo > /dev/tcp/"$DB_HOST"/"$DB_PORT") 2>/dev/null; do
    if [ "$waited" -ge "$MAX_DB_WAIT_SECONDS" ]; then
        echo "Error: database not reachable at $DB_HOST:$DB_PORT after ${MAX_DB_WAIT_SECONDS}s." >&2
        echo "Start it with: docker compose up -d postgres" >&2
        exit 1
    fi
    sleep 2
    waited=$((waited + 2))
done
echo "Database is accepting connections at $DB_HOST:$DB_PORT"

echo "Running database migrations..."
$UV_CMD run alembic upgrade head

echo "Seeding database..."
$UV_CMD run python -m app.db.seed_db

echo "Starting FastAPI application..."
$UV_CMD run uvicorn app.main:app --host 127.0.0.1 --port 8001
