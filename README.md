# Falabella POC — Backend (ValueData)

FastAPI + XGBoost + SHAP. Torre de control logística con predicción anticipada de fallas de entrega sobre datos tipo SimpliRoute.

## Correr localmente

```bash
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt
uvicorn main:app --host 127.0.0.1 --port 8090 --reload
```

El arranque entrena el modelo (~30-40s). Endpoints bajo `/api/*`. Proxy del frontend apunta a `127.0.0.1:8090`.

## Azure App Service

- Runtime: Python 3.11 (Linux).
- Startup Command: `bash startup.sh`.
- Application Settings:
  - `DB_SERVER`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_DRIVER`, `DB_SCHEMA` (Azure SQL)
  - `WEBSITES_CONTAINER_START_TIME_LIMIT=600` (evita timeout por el entrenamiento del modelo)
  - `SCM_DO_BUILD_DURING_DEPLOYMENT=1` (Oryx instala `requirements.txt`)
- Always On: **true** — el modelo vive en memoria del proceso; cold starts lo tiran.

## ETL Azure SQL (schema fpoc)

`fpoc_loader/` carga el Excel `datos_eta_YYYY-MM-DD.xlsx` a `fpoc.simpli_visits` y `fpoc.geo_suborders` en Azure SQL. Idempotente por fecha (simpli) / idruta (geo).

```bash
python fpoc_loader/load_to_azure.py datos_eta_2026_04_19.xlsx
```
