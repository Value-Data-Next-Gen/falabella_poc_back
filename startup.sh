#!/bin/bash
# Azure App Service (Linux, Python 3.11) — startup command for Torre de Control v2.
#
# Azure config:
#   - Startup Command: bash startup.sh
#   - Always On: true
#   - WEBSITES_CONTAINER_START_TIME_LIMIT=600
#
# NOTE: no `alembic upgrade` here — the prod `td` schema is already at head.
# Run migrations deliberately/out-of-band, never automatically against prod.
set -euo pipefail

# ----------------------------------------------------------------------------
# Microsoft ODBC Driver 17 for SQL Server (Linux Python image lacks it).
# Installed only if missing; needed by pyodbc/aioodbc → Azure SQL.
# ----------------------------------------------------------------------------
if ! odbcinst -q -d -n "ODBC Driver 17 for SQL Server" >/dev/null 2>&1; then
  echo "[startup] Installing ODBC Driver 17 (first run)..."
  if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
  $SUDO apt-get update -y >/dev/null 2>&1 || true
  $SUDO apt-get install -y curl gnupg apt-transport-https >/dev/null 2>&1 || true
  curl -fsSL https://packages.microsoft.com/keys/microsoft.asc 2>/dev/null \
    | $SUDO gpg --batch --yes --dearmor -o /usr/share/keyrings/microsoft.gpg 2>/dev/null || true
  DEBIAN_VER=$(grep -oP '(?<=VERSION_ID=")[0-9]+' /etc/os-release || echo "12")
  echo "deb [signed-by=/usr/share/keyrings/microsoft.gpg arch=amd64] https://packages.microsoft.com/debian/${DEBIAN_VER}/prod $(grep -oP '(?<=VERSION_CODENAME=)[a-z]+' /etc/os-release || echo bookworm) main" \
    | $SUDO tee /etc/apt/sources.list.d/mssql-release.list >/dev/null
  $SUDO apt-get update -y >/dev/null 2>&1 || true
  ACCEPT_EULA=Y $SUDO apt-get install -y msodbcsql17 unixodbc-dev >/dev/null 2>&1 \
    && echo "[startup] ODBC Driver 17 installed." \
    || echo "[startup] WARN: ODBC install failed; pyodbc may not connect."
else
  echo "[startup] ODBC Driver 17 already present."
fi

# Oryx installs requirements.txt during build; fallback for zip-deploy w/o build.
if [ -f requirements.txt ]; then
  python -m pip install --upgrade pip >/dev/null 2>&1 || true
  pip install --no-cache-dir -r requirements.txt
fi

# Run from the real wwwroot so `app.main` resolves (Oryx staging /tmp dir omits
# sibling packages otherwise).
APP_DIR="/home/site/wwwroot"
if [ -d "$APP_DIR" ] && [ -f "$APP_DIR/app/main.py" ]; then
  cd "$APP_DIR"
  export PYTHONPATH="${APP_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
  echo "[startup] cwd=$APP_DIR PYTHONPATH=$PYTHONPATH"
fi

PORT="${PORT:-8000}"
# 1 worker: APScheduler + sim/lookup state live in-process.
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --workers 1
