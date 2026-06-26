"""Seed vehicles with realistic Chilean license plates and characteristics.

Chilean plate format (2007+):
  - 4 letters + 2 digits, e.g. "JKLR-12"
  - Letters skip vowels and Q (to avoid ambiguity) → alphabet [BCDFGHJKLMNPRSTVWXYZ]
  - Visualized "BCDF·12" with hyphen.

This script:
  1. Looks up all empresas in td.empresas.
  2. Creates 3–6 vehicles per empresa with diverse types:
     - Furgón pequeño (8 m3, 2018-2024)
     - Furgón mediano (15 m3)
     - Camión 3/4 (25 m3)
     - Camión liviano (35 m3)
  3. Depot lat/lon in Santiago RM area (~-33.4 / -70.6).

Idempotent: if a plate already exists, skip (UNIQUE constraint).
"""
from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path

from loguru import logger
from sqlalchemy import select

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.db.models.empresa import Empresa  # noqa: E402
from app.db.models.vehicle import Vehicle  # noqa: E402
from app.db.session import dispose_engine, get_sessionmaker  # noqa: E402


# ----------------------------------------------------------------------------
# Chilean plate generator
# ----------------------------------------------------------------------------

# Letters used in Chilean plates (skip A,E,I,O,U,Q).
_CHILE_LETTERS = "BCDFGHJKLMNPRSTVWXYZ"


def gen_plate(rng: random.Random) -> str:
    """Generate a realistic Chilean plate, e.g. 'JKLR-12'."""
    letters = "".join(rng.choice(_CHILE_LETTERS) for _ in range(4))
    digits = f"{rng.randint(10, 99)}"
    return f"{letters}-{digits}"


# ----------------------------------------------------------------------------
# Vehicle profiles
# ----------------------------------------------------------------------------

VEHICLE_PROFILES = [
    # (tipo, capacity_m3, nombre_prefix, year_range)
    ("Furgón Pequeño",       8, "FUR",  (2019, 2024)),  # ej Renault Kangoo, Fiat Fiorino
    ("Furgón Mediano",      15, "FUR",  (2018, 2024)),  # ej Mercedes Sprinter, Fiat Ducato
    ("Furgón Grande",       20, "FUR",  (2018, 2024)),  # ej Mercedes Sprinter L3
    ("Camión 3/4",          25, "CAM",  (2017, 2024)),  # ej JAC, JMC, Hyundai HD45
    ("Camión Liviano",      35, "CAM",  (2017, 2024)),  # ej Hino Dutro, Isuzu NQR
    ("Camión Mediano",      50, "CAM",  (2016, 2023)),  # ej Mercedes Atego
]

# Santiago RM lat/lon range (rough bounding box).
SANTIAGO_LAT_RANGE = (-33.55, -33.35)
SANTIAGO_LON_RANGE = (-70.80, -70.55)


def gen_depot(rng: random.Random) -> tuple[float, float]:
    lat = round(rng.uniform(*SANTIAGO_LAT_RANGE), 6)
    lon = round(rng.uniform(*SANTIAGO_LON_RANGE), 6)
    return lat, lon


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

async def seed() -> None:
    sm = get_sessionmaker()
    rng = random.Random(42)  # deterministic — re-runs produce same data

    async with sm() as session:
        empresas = (await session.execute(select(Empresa).order_by(Empresa.empresa_id))).scalars().all()
        if not empresas:
            logger.error("No empresas in td.empresas — run earlier seeds first.")
            return

        created = 0
        skipped = 0
        for empresa in empresas:
            n_vehicles = rng.randint(3, 6)
            logger.info(f"Seeding {n_vehicles} vehicles for empresa {empresa.empresa_id} ({empresa.nombre})")

            for i in range(n_vehicles):
                profile = rng.choice(VEHICLE_PROFILES)
                tipo, capacity, prefix, (yr_min, yr_max) = profile
                year = rng.randint(yr_min, yr_max)
                plate = gen_plate(rng)

                # Check uniqueness before insert.
                exists = await session.execute(
                    select(Vehicle).where(Vehicle.plate == plate)
                )
                if exists.scalar_one_or_none():
                    skipped += 1
                    continue

                lat, lon = gen_depot(rng)
                nombre = f"{prefix}-{empresa.empresa_id:02d}{i + 1:02d}"

                vehicle = Vehicle(
                    empresa_id=empresa.empresa_id,
                    nombre=nombre,
                    plate=plate,
                    tipo=tipo,
                    capacity_m3=capacity,
                    year=year,
                    depot_lat=lat,
                    depot_lon=lon,
                )
                session.add(vehicle)
                created += 1
                logger.info(f"  + {nombre:12s} {plate:10s} {tipo:18s} {capacity:3d}m³  {year}  ({lat},{lon})")

        await session.commit()
        logger.info(f"DONE: created={created} skipped_dup_plate={skipped}")

    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(seed())
