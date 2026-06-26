"""Seed `td.drivers` with realistic Chilean driver data.

Distributes ~5 drivers per empresa with:
  - Common Chilean names (first + last + last)
  - Phone format +569XXXXXXXX (Chilean mobile)
  - License format: 8 digits (CL driver license without check digit)
  - Some opted_in (random ~50%), all with `activation_token` generated
  - Random vehicle assignment from the empresa's pool

Idempotent: if driver_id already exists, skip.
"""
from __future__ import annotations

import asyncio
import random
import secrets
import sys
from pathlib import Path

from loguru import logger
from sqlalchemy import select

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.db.models.driver import Driver  # noqa: E402
from app.db.models.empresa import Empresa  # noqa: E402
from app.db.models.vehicle import Vehicle  # noqa: E402
from app.db.session import dispose_engine, get_sessionmaker  # noqa: E402


FIRST_NAMES = [
    "Juan", "Carlos", "Pedro", "Francisco", "Sebastián", "Roberto", "Mauricio",
    "Andrés", "Gonzalo", "Felipe", "Cristián", "Marcelo", "Jorge", "Esteban",
    "Pablo", "Diego", "Manuel", "Ricardo", "Ignacio", "Luis",
    # Mujeres (~30%)
    "María", "Andrea", "Camila", "Pamela", "Carolina", "Daniela",
]

LAST_NAMES = [
    "González", "Muñoz", "Rojas", "Díaz", "Pérez", "Soto", "Contreras", "Silva",
    "Martínez", "Sepúlveda", "Reyes", "Espinoza", "Castro", "Tapia", "Morales",
    "Hernández", "Vargas", "Gutiérrez", "Carrasco", "Núñez", "Fuentes", "Araya",
    "Sandoval", "Cortés", "Bravo", "Alarcón", "Jara", "Vega",
]


def gen_phone(rng: random.Random) -> str:
    """Chilean mobile: +569 + 8 digits."""
    n = rng.randint(50_000_000, 99_999_999)  # avoid leading 0
    return f"+569{n}"


def gen_license() -> str:
    """8-digit Chilean driver license number."""
    return f"{random.randint(10_000_000, 25_999_999)}"


def gen_name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)} {rng.choice(LAST_NAMES)}"


async def seed() -> None:
    sm = get_sessionmaker()
    rng = random.Random(7)  # deterministic

    async with sm() as session:
        empresas = (await session.execute(select(Empresa).order_by(Empresa.empresa_id))).scalars().all()
        if not empresas:
            logger.error("No empresas — run seed_empresas_chile.py first.")
            return

        created = 0
        skipped = 0
        for emp in empresas:
            # Pool of vehicles for this empresa
            veh_result = await session.execute(
                select(Vehicle).where(Vehicle.empresa_id == emp.empresa_id, Vehicle.activo == True)  # noqa: E712
            )
            vehicles = veh_result.scalars().all()

            n_drivers = rng.randint(4, 6)
            logger.info(f"Seeding {n_drivers} drivers for {emp.nombre}")

            for i in range(1, n_drivers + 1):
                driver_id = f"DRV-{emp.empresa_id:02d}{i:03d}"
                # Skip if exists
                exists = await session.execute(select(Driver).where(Driver.driver_id == driver_id))
                if exists.scalar_one_or_none():
                    skipped += 1
                    continue

                # Random vehicle (might be None some times)
                veh = rng.choice(vehicles) if vehicles and rng.random() > 0.2 else None

                # 60% opted-in, 40% pending activation
                will_opt_in = rng.random() > 0.4
                phone = gen_phone(rng)

                from datetime import datetime, timezone
                driver = Driver(
                    driver_id=driver_id,
                    empresa_id=emp.empresa_id,
                    vehicle_id=veh.vehicle_id if veh else None,
                    nombre=gen_name(rng),
                    license=gen_license(),
                    phone_e164=phone,
                    notify_whatsapp=will_opt_in,
                    opted_in_at=datetime.now(timezone.utc) if will_opt_in else None,
                    activation_token=secrets.token_urlsafe(16),
                    activation_used_at=datetime.now(timezone.utc) if will_opt_in else None,
                )
                session.add(driver)
                created += 1
                label = "opted-in" if will_opt_in else "pending"
                veh_label = veh.nombre if veh else "(no vehicle)"
                logger.info(f"  + {driver_id:12s} {driver.nombre:35s} {phone} {veh_label:12s} {label}")

        await session.commit()
        logger.info(f"DONE: created={created} skipped={skipped}")

    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(seed())
