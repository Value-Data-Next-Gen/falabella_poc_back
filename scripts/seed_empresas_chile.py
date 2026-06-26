"""Seed/upgrade empresas with realistic Chilean company data.

Chilean RUT format:
  - 1–2 digits + "." + 3 digits + "." + 3 digits + "-" + check_digit
  - check_digit ∈ {0..9, K} via Mod-11
  - Example: 76.123.456-7

Chilean phone format:
  - Mobile (CL): +569 + 8 digits  → "+56932942337"
  - Landline Santiago: +562 + 8 digits → "+56226234567"
  - Toll-free "800": +56600 + 6 digits → "+56600370025"

This script UPDATES the existing empresas in `td.empresas` with realistic
data. Idempotent: re-running overwrites the same fields with same values.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from loguru import logger
from sqlalchemy import select

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.db.models.empresa import Empresa  # noqa: E402
from app.db.session import dispose_engine, get_sessionmaker  # noqa: E402


# ----------------------------------------------------------------------------
# Chilean RUT — Mod-11 check digit calculator
# ----------------------------------------------------------------------------

def _calc_check_digit(rut_number: int) -> str:
    """Mod-11 algorithm used by Servicio de Impuestos Internos (SII)."""
    multiplier = 2
    total = 0
    while rut_number > 0:
        total += (rut_number % 10) * multiplier
        rut_number //= 10
        multiplier = 2 if multiplier == 7 else multiplier + 1
    remainder = 11 - (total % 11)
    if remainder == 11:
        return "0"
    if remainder == 10:
        return "K"
    return str(remainder)


def format_rut(number: int) -> str:
    """Format `76123456` → `76.123.456-7`."""
    s = str(number)
    # group from right in chunks of 3
    parts = []
    while s:
        parts.append(s[-3:])
        s = s[:-3]
    body = ".".join(reversed(parts))
    return f"{body}-{_calc_check_digit(number)}"


# ----------------------------------------------------------------------------
# Realistic company profiles (Falabella transportistas mock — NOT real RUTs)
# ----------------------------------------------------------------------------

PROFILES = [
    {
        "match_id": 1,
        "nombre": "Falabella Transporte SpA",
        "razon_social": "Transportes Falabella Servicios Logísticos SpA",
        "rut_number": 76823145,
        "central_phone": "+56226234500",
        "supervisor_phone_e164": "+56932942337",
    },
    {
        "match_id": 2,
        "nombre": "Logística Pacífico",
        "razon_social": "Logística Pacífico Limitada",
        "rut_number": 78456912,
        "central_phone": "+56224447777",
        "supervisor_phone_e164": "+56987651234",
    },
    {
        "match_id": 3,
        "nombre": "Andinos Express",
        "razon_social": "Andinos Express Cargo S.A.",
        "rut_number": 96328547,
        "central_phone": "+56229988555",
        "supervisor_phone_e164": "+56955554433",
    },
]


# ----------------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------------

async def seed() -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        updated = 0
        for prof in PROFILES:
            result = await session.execute(
                select(Empresa).where(Empresa.empresa_id == prof["match_id"])
            )
            empresa = result.scalar_one_or_none()
            if empresa is None:
                logger.warning(f"  skip — empresa_id={prof['match_id']} not found")
                continue

            new_rut = format_rut(prof["rut_number"])
            empresa.nombre = prof["nombre"]
            empresa.razon_social = prof["razon_social"]
            empresa.rut = new_rut
            empresa.central_phone = prof["central_phone"]
            empresa.supervisor_phone_e164 = prof["supervisor_phone_e164"]
            updated += 1
            logger.info(
                f"  ✓ id={empresa.empresa_id} {empresa.nombre:30s} "
                f"rut={new_rut} central={empresa.central_phone} sup={empresa.supervisor_phone_e164}"
            )

        await session.commit()
        logger.info(f"DONE: {updated} empresas updated")

    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(seed())
