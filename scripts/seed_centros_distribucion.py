"""Crea tabla fpoc.centros_distribucion + seed inicial.

Idempotente: si la tabla ya existe la usa; si los CDs ya están seedeados
no los duplica (UPSERT por region+nombre).

Uso (desde la raíz del repo):
    python backend/scripts/seed_centros_distribucion.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from core.db import get_conn  # noqa: E402


# Lista canónica de CDs por región chilena. Lat/lon = centro urbano del CD
# operacional principal de cada región. Para el POC son 7 CDs cubriendo desde
# Coquimbo hasta Araucanía + RM.
SEED_CDS = [
    # (region, nombre, ciudad, lat, lon)
    ("RM",          "CD OMNICANAL LOF2", "Santiago",   -33.4489, -70.6693),
    ("Valparaíso",  "CD VALPARAÍSO",      "Valparaíso", -33.0472, -71.6127),
    ("O'Higgins",   "CD RANCAGUA",        "Rancagua",   -34.1708, -70.7444),
    ("Maule",       "CD TALCA",           "Talca",      -35.4264, -71.6553),
    ("Biobío",      "CD CONCEPCIÓN",      "Concepción", -36.8201, -73.0444),
    ("Araucanía",   "CD TEMUCO",          "Temuco",     -38.7359, -72.5904),
    ("Coquimbo",    "CD LA SERENA",       "La Serena",  -29.9027, -71.2519),
]


DDL_SQLSERVER = """
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES
               WHERE TABLE_SCHEMA = 'fpoc' AND TABLE_NAME = 'centros_distribucion')
BEGIN
    CREATE TABLE fpoc.centros_distribucion (
        cd_id INT IDENTITY(1,1) PRIMARY KEY,
        region NVARCHAR(50) NOT NULL,
        nombre NVARCHAR(100) NOT NULL,
        ciudad NVARCHAR(100) NULL,
        lat FLOAT NOT NULL,
        lon FLOAT NOT NULL,
        activo BIT NOT NULL DEFAULT 1,
        created_at DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
        CONSTRAINT UQ_cd_region_nombre UNIQUE (region, nombre)
    )
END
"""

# SQLite equivalent (el rewriter maneja fpoc.X → fpoc_X)
DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS fpoc_centros_distribucion (
    cd_id INTEGER PRIMARY KEY AUTOINCREMENT,
    region TEXT NOT NULL,
    nombre TEXT NOT NULL,
    ciudad TEXT,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    activo INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (region, nombre)
)
"""


def main() -> None:
    import os
    backend = os.environ.get("DB_BACKEND", "sqlite").lower()
    print(f"[seed-cds] DB_BACKEND={backend}")

    with get_conn() as cn:
        cur = cn.cursor()
        # DDL
        if backend == "sqlserver":
            # Ejecutamos por bloque IF/BEGIN, así pyodbc lo manda como una sola batch
            cur.execute(DDL_SQLSERVER)
        else:
            cur.execute(DDL_SQLITE)
        cn.commit()
        print("[seed-cds] DDL OK")

        # Seed (upsert ligero: insertar si no existe)
        inserted = 0
        for region, nombre, ciudad, lat, lon in SEED_CDS:
            try:
                cur.execute(
                    "SELECT cd_id FROM fpoc.centros_distribucion "
                    "WHERE region = ? AND nombre = ?",
                    region, nombre,
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        "INSERT INTO fpoc.centros_distribucion "
                        "(region, nombre, ciudad, lat, lon, activo) "
                        "VALUES (?, ?, ?, ?, ?, 1)",
                        region, nombre, ciudad, lat, lon,
                    )
                    inserted += 1
            except Exception as e:  # noqa: BLE001
                print(f"[seed-cds] insert {region}/{nombre} falló: {e}")
        cn.commit()
        print(f"[seed-cds] {inserted} CDs insertados (de {len(SEED_CDS)})")

        # Verificar
        cur.execute(
            "SELECT cd_id, region, nombre, ciudad, lat, lon FROM fpoc.centros_distribucion "
            "WHERE activo = 1 ORDER BY region"
        )
        for r in cur.fetchall():
            print(f"  CD {r.cd_id}: {r.region:12s} {r.nombre:25s} {r.ciudad:15s} "
                  f"({r.lat:.4f}, {r.lon:.4f})")


if __name__ == "__main__":
    main()
