"""Demo seed: a CERRADO día for empresa 1 with visitas spread across several
Chilean regions, with realistic ETA/completion times so the by-region and
punctuality reports are meaningful. Idempotent via a folio marker prefix.
Run: python -m scripts.seed_regions_demo
"""
from __future__ import annotations

import asyncio
import random
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, select

from app.db.models.dia_operativo import DiaOperativo
from app.db.models.visita import Visita
from app.db.session import get_sessionmaker

EMPRESA_ID = 1
FECHA = date(2026, 6, 16)
MARKER = "DEMO-REG-"
REGIONS = [
    ("Región Metropolitana", 14),
    ("Valparaíso", 10),
    ("Biobío", 9),
    ("Maule", 7),
    ("La Araucanía", 6),
]


async def main() -> None:
    rng = random.Random(42)
    sm = get_sessionmaker()
    async with sm() as db:
        # idempotent: drop any prior demo visitas + día for this fecha/empresa
        prior = (await db.execute(
            select(DiaOperativo).where(DiaOperativo.empresa_id == EMPRESA_ID, DiaOperativo.fecha == FECHA)
        )).scalars().all()
        for d in prior:
            await db.execute(delete(Visita).where(Visita.dia_id == d.dia_id, Visita.folio_cliente.like(f"{MARKER}%")))
        dia = prior[0] if prior else None
        if dia is None:
            dia = DiaOperativo(empresa_id=EMPRESA_ID, fecha=FECHA, estado="CERRADO",
                               cerrado_at=datetime(2026, 6, 16, 20, 0, tzinfo=UTC))
            db.add(dia); await db.flush()
        elif dia.estado != "CERRADO":
            dia.estado = "CERRADO"

        n = 0
        for region, count in REGIONS:
            for i in range(count):
                n += 1
                hour = 9 + (n % 8)
                eta = datetime(2026, 6, 16, hour, rng.randint(0, 59), tzinfo=UTC)
                delivered = rng.random() < 0.9
                comp = eta + timedelta(minutes=rng.randint(-5, 25)) if delivered else None
                db.add(Visita(
                    dia_id=dia.dia_id, empresa_id=EMPRESA_ID, orden=n,
                    cliente_nombre=f"Cliente {region[:3]}-{i+1}", direccion=f"Calle {n}",
                    region=region, estado="entregado" if delivered else "no_entregado",
                    motivo=None if delivered else rng.choice(["SIN MORADORES", "DIRECCION ERRADA", "CLIENTE RECHAZA"]),
                    folio_cliente=f"{MARKER}{n:04d}", eta_estimada=eta, completada_at=comp,
                    es_vip=1 if rng.random() < 0.1 else 0,
                ))
        await db.commit()
        print(f"seeded dia_id={dia.dia_id} fecha={FECHA} visitas={n} across {len(REGIONS)} regions")


if __name__ == "__main__":
    asyncio.run(main())
