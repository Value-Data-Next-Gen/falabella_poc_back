"""Migracion 027: piloto controlable.

Agrega columnas necesarias para el control del piloto MVP Fase 3:

- `fpoc.planificacion_imports.sim_clock_offset_min` INT NOT NULL DEFAULT 0
    Offset manual aplicado al sim_clock del dia. 0 => modo automatico (now UTC).
    Cualquier otro valor => modo manual (now UTC + offset_min).
- `fpoc.simpli_visits.latitude` FLOAT NULL
- `fpoc.simpli_visits.longitude` FLOAT NULL
    Coordenadas de la visita. Necesarias para el endpoint
    `/api/operacion/driver-positions` y su interpolacion lineal entre stops.

Backfill best-effort de latitude/longitude para filas historicas: si tenemos
mapping `comuna -> centroid` y la fila tiene `comuna IS NOT NULL`, escribimos
el centroide. El piloto crea filas nuevas con coords reales.

Idempotente: chequea `sys.columns` antes de cada ALTER.

Uso manual:
    python -m fpoc_loader.migrate_sim_clock

Registrada en `fpoc_loader.migrations.MIGRATIONS` como `027_sim_clock`.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv

for _p in (BACKEND / ".env", BACKEND.parent / ".env"):
    if _p.exists():
        load_dotenv(_p)
        break

from loguru import logger  # noqa: E402

from core.db import get_conn  # noqa: E402


# Centroides aproximados por comuna (Santiago + Vina/Valpo/Concepcion).
# Usados para backfill best-effort de filas historicas que no traen lat/lon
# pero si traen comuna.
COMUNA_CENTROIDS: dict[str, tuple[float, float]] = {
    "Las Condes":     (-33.4150, -70.5800),
    "Providencia":    (-33.4250, -70.6100),
    "Vitacura":       (-33.3990, -70.6010),
    "Nunoa":          (-33.4570, -70.5950),
    "Nunoa ":         (-33.4570, -70.5950),
    "Macul":          (-33.4900, -70.6020),
    "San Joaquin":    (-33.4960, -70.6260),
    "Santiago":       (-33.4450, -70.6500),
    "Recoleta":       (-33.4200, -70.6390),
    "Independencia":  (-33.4120, -70.6620),
    "La Florida":     (-33.5220, -70.5990),
    "Maipu":          (-33.5110, -70.7580),
    "Puente Alto":    (-33.6110, -70.5780),
    "Quilicura":      (-33.3640, -70.7290),
    "Valparaiso":     (-33.0458, -71.6197),
    "Vina del Mar":   (-33.0250, -71.5520),
    "Concepcion":     (-36.8270, -73.0500),
    "Talcahuano":     (-36.7240, -73.1170),
}


def _log(msg: str, quiet: bool) -> None:
    if not quiet:
        print(msg)


def _add_planimports_sim_clock(cur, cn, quiet: bool) -> None:
    cur.execute(
        """
        IF NOT EXISTS (
            SELECT 1 FROM sys.columns
            WHERE Name = N'sim_clock_offset_min'
              AND Object_ID = Object_ID(N'fpoc.planificacion_imports')
        )
        BEGIN
            ALTER TABLE fpoc.planificacion_imports
            ADD sim_clock_offset_min INT NOT NULL
                CONSTRAINT DF_planimports_sim_clock_offset DEFAULT 0 WITH VALUES
        END
        """
    )
    cn.commit()
    _log("[ok]   fpoc.planificacion_imports.sim_clock_offset_min asegurada", quiet)


def _add_simpli_latlon(cur, cn, quiet: bool) -> None:
    for col in ("latitude", "longitude"):
        cur.execute(
            f"""
            IF NOT EXISTS (
                SELECT 1 FROM sys.columns
                WHERE Name = N'{col}'
                  AND Object_ID = Object_ID(N'fpoc.simpli_visits')
            )
            BEGIN
                ALTER TABLE fpoc.simpli_visits ADD {col} FLOAT NULL
            END
            """
        )
        cn.commit()
        _log(f"[ok]   fpoc.simpli_visits.{col} asegurada", quiet)


def _backfill_latlon_from_comuna(cur, cn, quiet: bool) -> None:
    """Best-effort: para filas con lat/lon NULL pero comuna conocida, escribe
    el centroide. No es exacto, pero alcanza para ubicar el driver en el mapa
    cuando se procesan visitas historicas."""
    updated_total = 0
    for comuna, (lat, lon) in COMUNA_CENTROIDS.items():
        try:
            cur.execute(
                """
                UPDATE fpoc.simpli_visits
                   SET latitude = ?, longitude = ?
                 WHERE comuna = ?
                   AND (latitude IS NULL OR longitude IS NULL)
                """,
                lat, lon, comuna,
            )
            cn.commit()
            n = int(cur.rowcount or 0)
            updated_total += max(0, n)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[migrate-sim-clock] backfill {comuna}: {e}")
    _log(f"[ok]   backfill lat/lon por comuna: {updated_total} filas", quiet)


def main(quiet: bool = False) -> None:
    """Aplica las columnas de sim_clock + lat/lon. Idempotente."""
    _log("[migrate-sim-clock] backend=sqlserver", quiet)
    with get_conn() as cn:
        cur = cn.cursor()
        try:
            _add_planimports_sim_clock(cur, cn, quiet)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[migrate-sim-clock] planificacion_imports: {e}")
        try:
            _add_simpli_latlon(cur, cn, quiet)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[migrate-sim-clock] simpli_visits lat/lon: {e}")
        try:
            _backfill_latlon_from_comuna(cur, cn, quiet)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[migrate-sim-clock] backfill: {e}")


if __name__ == "__main__":
    main()
