#!/bin/bash
# Azure App Service (Linux, Python 3.11) — startup command
#
# Nota: el backend entrena un modelo XGBoost en el arranque (~30-40s).
# Configura en Azure App Service > Configuration > General settings:
#   - Startup Command: bash startup.sh
#   - Always On: true (evita cold starts que tiran el modelo en memoria)
#   - WEBSITES_CONTAINER_START_TIME_LIMIT=600 (evita timeout del warm-up probe)

set -euo pipefail

# ----------------------------------------------------------------------------
# Microsoft ODBC Driver 17 for SQL Server
# Linux App Service Python image NO trae el driver. Lo instalamos solo si falta
# (en warm-restart no re-corre apt). Necesario para pyodbc → Azure SQL.
# ----------------------------------------------------------------------------
if ! odbcinst -q -d -n "ODBC Driver 17 for SQL Server" >/dev/null 2>&1; then
  echo "[startup] Instalando ODBC Driver 17 (primera vez)..."
  if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
  $SUDO apt-get update -y >/dev/null 2>&1 || true
  $SUDO apt-get install -y curl gnupg apt-transport-https >/dev/null 2>&1 || true
  curl -fsSL https://packages.microsoft.com/keys/microsoft.asc 2>/dev/null \
    | $SUDO gpg --batch --yes --dearmor -o /usr/share/keyrings/microsoft.gpg 2>/dev/null || true
  DEBIAN_VER=$(grep -oP '(?<=VERSION_ID=")[0-9]+' /etc/os-release || echo "11")
  echo "deb [signed-by=/usr/share/keyrings/microsoft.gpg arch=amd64] https://packages.microsoft.com/debian/${DEBIAN_VER}/prod $(grep -oP '(?<=VERSION_CODENAME=)[a-z]+' /etc/os-release || echo bullseye) main" \
    | $SUDO tee /etc/apt/sources.list.d/mssql-release.list >/dev/null
  $SUDO apt-get update -y >/dev/null 2>&1 || true
  ACCEPT_EULA=Y $SUDO apt-get install -y msodbcsql17 unixodbc-dev >/dev/null 2>&1 \
    && echo "[startup] ODBC Driver 17 instalado." \
    || echo "[startup] WARN: instalación ODBC falló; pyodbc puede no conectar."
else
  echo "[startup] ODBC Driver 17 ya presente."
fi

# Oryx ya instala requirements.txt en el build; fallback por si llegó zip-deploy sin build.
if [ -f requirements.txt ]; then
  python -m pip install --upgrade pip
  pip install --no-cache-dir -r requirements.txt
fi

# Persistencia SQLite en /home/data (solo si DB_BACKEND=sqlite).
# /home está montado como Azure Files; el resto del filesystem es efímero.
if [ -n "${SQLITE_PATH:-}" ]; then
  mkdir -p "$(dirname "$SQLITE_PATH")"
fi

PORT="${PORT:-8000}"

# 1 worker porque el modelo + SHAP + STATE viven en memoria del proceso.
# Si escalas horizontal, hay que mover STATE a Redis/DB antes.
exec python -m uvicorn main:app \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --workers 1 \
  --timeout-keep-alive 600 \
  --log-level info \
  --access-log
