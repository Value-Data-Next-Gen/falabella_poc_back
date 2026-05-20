"""Dumpea backend/openapi.json desde la app FastAPI.

Importa `app` de `backend/main.py`, llama `app.openapi()` y escribe el JSON
formateado. Este archivo se versiona en disco — es el contrato con el frontend
que después corre `npm run gen-types` para generar `src/types/api.ts`.

Uso (desde la raíz del repo):
    python backend/scripts/dump_openapi.py

Nota: levanta el entrenamiento del modelo XGBoost al importar `app`
(~30-40s en cold). Para evitarlo en pipelines de CI se puede setear
SKIP_BOOT_TRAINING=true si el código lo soporta (no garantizado).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Permitir `import main` y `import core.*` desde la raíz de backend/
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from main import app  # noqa: E402

OUT = BACKEND_DIR / "openapi.json"


def main() -> None:
    schema = app.openapi()
    OUT.write_text(json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[dump_openapi] {OUT.relative_to(BACKEND_DIR.parent)} escrito "
          f"({OUT.stat().st_size:,} bytes, {len(schema.get('paths', {}))} paths)")


if __name__ == "__main__":
    main()
