

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
ENV_FILE=".env"
[ ! -f "$ENV_FILE" ] && [ -f "../.env" ] && ENV_FILE="../.env"

DOTENV_DB_HOST=""
DOTENV_DB_PORT=""
DOTENV_APP_HOST=""
DOTENV_APP_PORT=""
if [ -f "$ENV_FILE" ]; then
    DOTENV_DB_HOST="$(grep -E '^POSTGRES_HOST=' "$ENV_FILE" | cut -d= -f2- | tr -d '"'\''')"
    DOTENV_DB_PORT="$(grep -E '^POSTGRES_PORT=' "$ENV_FILE" | cut -d= -f2- | tr -d '"'\''')"
    DOTENV_APP_HOST="$(grep -E '^(BACKEND_HOST|API_HOST)=' "$ENV_FILE" | head -n 1 | cut -d= -f2- | tr -d '"'\''')"
    DOTENV_APP_PORT="$(grep -E '^(BACKEND_PORT|API_PORT)=' "$ENV_FILE" | head -n 1 | cut -d= -f2- | tr -d '"'\''')"
fi
DB_HOST="${POSTGRES_HOST:-${DOTENV_DB_HOST:-localhost}}"
DB_PORT="${POSTGRES_PORT:-${DOTENV_DB_PORT:-5432}}"
APP_HOST="${BACKEND_HOST:-${API_HOST:-${DOTENV_APP_HOST:-0.0.0.0}}}"
APP_PORT="${BACKEND_PORT:-${API_PORT:-${DOTENV_APP_PORT:-8001}}}"

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

echo "Starting FastAPI application on $APP_HOST:$APP_PORT..."
$UV_CMD run uvicorn app.main:app --host "$APP_HOST" --port "$APP_PORT" --reload
