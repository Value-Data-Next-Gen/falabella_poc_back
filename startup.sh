#!/bin/bash
# Azure App Service (Linux, Python 3.11) — startup command
#
# Nota: el backend entrena un modelo XGBoost en el arranque (~30-40s).
# Configura en Azure App Service > Configuration > General settings:
#   - Startup Command: bash startup.sh
#   - Always On: true (evita cold starts que tiran el modelo en memoria)
#   - WEBSITES_CONTAINER_START_TIME_LIMIT=600 (evita timeout del warm-up probe)

set -euo pipefail

# Oryx ya instala requirements.txt en el build; fallback por si llegó zip-deploy sin build.
if [ -f requirements.txt ]; then
  python -m pip install --upgrade pip
  pip install --no-cache-dir -r requirements.txt
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
