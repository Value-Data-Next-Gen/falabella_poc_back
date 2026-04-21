#!/bin/bash
# Azure App Service (Linux, Python 3.11) — startup command
#
# Nota: el backend entrena un modelo XGBoost en el arranque (~30-40s).
# Configura en Azure App Service > Configuration > General settings:
#   - Startup Command: bash startup.sh
#   - Always On: true (evita cold starts que matan el modelo)
#
# Si el warm-up probe de App Service se queja por timeout, aumentar:
#   WEBSITES_CONTAINER_START_TIME_LIMIT=600   (en Application settings)

set -euo pipefail

# Oryx ya instala requirements.txt en el build; ejecutamos por si
# el deployment se hizo zip-deploy sin build.
if [ -f requirements.txt ]; then
  python -m pip install --upgrade pip
  pip install --no-cache-dir -r requirements.txt
fi

# Puerto: Azure inyecta $PORT; por defecto 8000.
PORT="${PORT:-8000}"

# 1 worker porque el modelo + SHAP vive en memoria del proceso.
# Si escalas, hay que mover STATE a Redis/DB primero.
exec gunicorn main:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --workers 1 \
  --bind "0.0.0.0:${PORT}" \
  --timeout 600 \
  --access-logfile - \
  --error-logfile -
