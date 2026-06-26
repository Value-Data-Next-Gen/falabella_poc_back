"""Seed empresa_contactos with realistic Chilean data.

Per empresa: 1 jefe + 1 coordinador + 1 dispatcher (3 contactos).
Names: Chilean. Phones: +569 mobile or +5622 landline.
"""
from __future__ import annotations

import asyncio
import random
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from sqlalchemy import select

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.db.models.empresa import Empresa  # noqa: E402
from app.db.models.empresa_contacto import EmpresaContacto  # noqa: E402
from app.db.session import dispose_engine, get_sessionmaker  # noqa: E402


FIRST_NAMES = [
    "Patricia", "Carolina", "Andrea", "Pamela", "Verónica", "Daniela",
    "Marcelo", "Roberto", "Juan Pablo", "Sebastián", "Felipe",
    "Cristián", "Macarena", "Constanza", "Camila",
]
LAST_NAMES = [
    "Pérez", "González", "Soto", "Muñoz", "Rojas", "Vargas", "Tapia",
    "Silva", "Castro", "Espinoza", "Núñez", "Contreras",
]

PROFILES = [
    ("jefe", ["+5694{n}", "+5697{n}"]),         # jefe → mobile
    ("coordinador", ["+5694{n}", "+5696{n}"]),  # coord → mobile
    ("dispatcher", ["+5622{n}", "+5622{n}"]),   # dispatcher → landline Santiago
]


def gen_phone(rng: random.Random, prefix: str) -> str:
    digits = "".join(str(rng.randint(0, 9)) for _ in range(7))
    return prefix.format(n=digits)


def gen_name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)} {rng.choice(LAST_NAMES)}"


async def seed() -> None:
    sm = get_sessionmaker()
    rng = random.Random(13)

    async with sm() as session:
        empresas = (await session.execute(select(Empresa).order_by(Empresa.empresa_id))).scalars().all()
        if not empresas:
            logger.error("No empresas — run seed_empresas_chile.py first.")
            return

        created = 0
        skipped = 0
        for emp in empresas:
            logger.info(f"Seeding contactos for {emp.nombre} (id={emp.empresa_id})")

            for rol, phone_options in PROFILES:
                # Idempotent: check if a contact with this rol already exists in this empresa
                dup = await session.execute(
                    select(EmpresaContacto).where(
                        EmpresaContacto.empresa_id == emp.empresa_id,
                        EmpresaContacto.rol == rol,
                        EmpresaContacto.activo == True,  # noqa: E712
                    )
                )
                if dup.scalar_one_or_none():
                    skipped += 1
                    logger.info(f"  - skip (already has {rol})")
                    continue

                phone_template = rng.choice(phone_options)
                phone = gen_phone(rng, phone_template)
                nombre = gen_name(rng)
                will_opt_in = rng.random() > 0.3  # 70% opted in

                contacto = EmpresaContacto(
                    empresa_id=emp.empresa_id,
                    nombre=nombre,
                    rol=rol,
                    phone_e164=phone,
                    email=f"{nombre.split()[0].lower()}.{nombre.split()[-1].lower()}@td-mock.cl",
                    opted_in_at=datetime.now(timezone.utc) if will_opt_in else None,
                    activation_token=secrets.token_urlsafe(16),
                    activation_used_at=datetime.now(timezone.utc) if will_opt_in else None,
                    notes=f"Contacto {rol} mock generado para demo.",
                )
                session.add(contacto)
                created += 1
                label = "opted-in" if will_opt_in else "pending"
                logger.info(f"  + {rol:13s} {nombre:40s} {phone} {label}")

        await session.commit()
        logger.info(f"DONE: created={created} skipped={skipped}")

    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(seed())
