"""Demo data fix: the sim clock drifted, so delivered visitas have completada_at
far from eta_estimada -> punctuality reads ~3%. Reset completada_at to
eta + a realistic delta so reports/dashboard look sensible.
Run: python -m scripts.fix_punctuality_demo
"""
from __future__ import annotations

import asyncio
import random
from datetime import timedelta

from sqlalchemy import select

from app.db.models.visita import Visita
from app.db.session import get_sessionmaker


async def main() -> None:
    rng = random.Random(7)
    sm = get_sessionmaker()
    async with sm() as db:
        rows = (await db.execute(
            select(Visita).where(
                Visita.estado.in_(("entregado", "no_entregado", "cancelado")),
                Visita.eta_estimada.isnot(None),
                Visita.completada_at.isnot(None),
            )
        )).scalars().all()
        fixed = 0
        for v in rows:
            # ~80% on-time: delta in [-12, +12], ~20% late: [+13, +45]
            delta = rng.randint(-12, 12) if rng.random() < 0.8 else rng.randint(13, 45)
            new_comp = v.eta_estimada + timedelta(minutes=delta)
            if v.completada_at != new_comp:
                v.completada_at = new_comp
                v.llegada_at = new_comp - timedelta(minutes=rng.randint(1, 6))
                fixed += 1
        await db.commit()
        print(f"adjusted completada_at on {fixed} delivered visitas")


if __name__ == "__main__":
    asyncio.run(main())
