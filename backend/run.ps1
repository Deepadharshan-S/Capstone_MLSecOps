# PowerShell entry point for Windows (run.sh requires a bash shell).
# Usage:  .\run.ps1
# Mirrors backend/run.sh: wait for DB -> alembic upgrade -> seed -> uvicorn.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# MLflow prints emoji to stdout ("🏃 View run …"). Windows' default ANSI code
# page (cp1252) raises UnicodeEncodeError inside that write, which surfaces as a
# 500 from POST /models/upload. Force UTF-8 for the whole process tree.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# --- locate uv -------------------------------------------------------------
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Error "uv is not installed or not found in PATH. Install it from https://docs.astral.sh/uv/"
    exit 1
}

# --- read DB host/port from .env ------------------------------------------
$dbHost = "localhost"
$dbPort = "5432"
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*POSTGRES_HOST\s*=\s*(.+)$') { $dbHost = $Matches[1].Trim().Trim('"', "'") }
    if ($_ -match '^\s*POSTGRES_PORT\s*=\s*(.+)$') { $dbPort = $Matches[1].Trim().Trim('"', "'") }
}

# --- wait for postgres -----------------------------------------------------
Write-Host "Waiting for database at ${dbHost}:${dbPort}..."
$deadline = (Get-Date).AddSeconds(60)
while ($true) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $client.Connect($dbHost, [int]$dbPort)
        $client.Close()
        break
    } catch {
        if ((Get-Date) -gt $deadline) {
            Write-Error "Database not reachable at ${dbHost}:${dbPort} after 60s. Start it with: docker compose up -d postgres"
            exit 1
        }
        Start-Sleep -Seconds 2
    }
}
Write-Host "Database is accepting connections."

Write-Host "Running database migrations..."
& uv run alembic upgrade head
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Seeding database..."
& uv run python -m app.db.seed_db
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Starting FastAPI application on http://127.0.0.1:8001 ..."
& uv run uvicorn app.main:app --host 127.0.0.1 --port 8001
