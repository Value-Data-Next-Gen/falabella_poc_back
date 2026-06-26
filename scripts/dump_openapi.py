"""Export the OpenAPI spec to backend/openapi.json WITHOUT starting the server.

Pattern from FastAPI Discussion #14712: import the `app` instance (the lifespan
context manager is NOT entered when you just call `app.openapi()`), so no DB
connection or external side effects are triggered.

Run from `backend/` directory:
    uv run python scripts/dump_openapi.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make `backend/` importable regardless of the cwd from which the script runs.
HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent
sys.path.insert(0, str(BACKEND_ROOT))

# Import after sys.path adjustment.
from app.main import app  # noqa: E402

OUT = BACKEND_ROOT / "openapi.json"


def main() -> None:
    spec = app.openapi()
    OUT.write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
    n_paths = len(spec.get("paths", {}))
    n_schemas = len(spec.get("components", {}).get("schemas", {}))
    print(f"Wrote {OUT} ({n_paths} paths, {n_schemas} schemas)")


if __name__ == "__main__":
    main()
