"""Seed a live EN_CURSO día so the Centro de Control board is populated:
pending + overdue (atrasada) + VIP + blocked-client stops, and 2 open alerts.
Run: python -m scripts.seed_command_center_demo
"""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, select

from app.db.models.alert import Alert
from app.db.models.cliente import Cliente
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.driver import Driver
from app.db.models.ruta import Ruta
from app.db.models.sim_clock import SimClock
from app.db.models.vehicle import Vehicle
from app.db.models.visita import Visita
from app.db.session import get_sessionmaker

EMP = 1
FECHA = date(2026, 6, 18)
MARK = "CCDEMO-"


async def main() -> None:
    sm = get_sessionmaker()
    async with sm() as db:
        now = await db.scalar(select(SimClock.sim_now).where(SimClock.id == 1)) or datetime.now(UTC)
        # clean prior demo
        prev = (await db.execute(select(DiaOperativo).where(DiaOperativo.empresa_id == EMP, DiaOperativo.fecha == FECHA))).scalars().all()
        for d in prev:
            await db.execute(delete(Alert).where(Alert.dia_id == d.dia_id))
            await db.execute(delete(Visita).where(Visita.dia_id == d.dia_id))
            await db.execute(delete(Ruta).where(Ruta.dia_id == d.dia_id))
            await db.delete(d)
        await db.flush()

        drv = await db.scalar(select(Driver).where(Driver.empresa_id == EMP).limit(1))
        veh = await db.scalar(select(Vehicle).where(Vehicle.empresa_id == EMP).limit(1))
        blocked = await db.scalar(select(Cliente).where(Cliente.retener == True))  # noqa: E712

        dia = DiaOperativo(empresa_id=EMP, fecha=FECHA, estado="EN_CURSO", iniciado_at=now)
        db.add(dia); await db.flush()
        ruta = Ruta(dia_id=dia.dia_id, driver_id=drv.driver_id if drv else None,
                    vehicle_id=veh.vehicle_id if veh else None, orden=1)
        db.add(ruta); await db.flush()

        def v(orden, estado, **kw):
            return Visita(dia_id=dia.dia_id, empresa_id=EMP, ruta_id=ruta.ruta_id, orden=orden,
                          cliente_nombre=f"Cliente {orden}", direccion=f"Calle {orden}", estado=estado, **kw)

        rows = [
            v(1, "entregado", eta_estimada=now - timedelta(hours=2), completada_at=now - timedelta(hours=2)),
            v(2, "entregado", eta_estimada=now - timedelta(hours=1), completada_at=now - timedelta(minutes=50)),
            v(3, "pendiente", eta_estimada=now - timedelta(minutes=40)),   # atrasada
            v(4, "pendiente", eta_estimada=now - timedelta(minutes=20)),   # atrasada
            v(5, "pendiente", eta_estimada=now + timedelta(minutes=30)),   # on time
            v(6, "pendiente", es_vip=1, eta_estimada=now + timedelta(minutes=15)),  # VIP
        ]
        if blocked:
            rows.append(v(7, "pendiente", cliente_id=blocked.cliente_id, cliente_nombre=blocked.nombre,
                          eta_estimada=now + timedelta(minutes=45)))
        db.add_all(rows)
        db.add_all([
            Alert(tipo="eta_breach", severity="critica", empresa_id=EMP, dia_id=dia.dia_id,
                  descripcion=f"{MARK}Visita #3 atrasada 40 min", estado="abierta", created_at=now - timedelta(minutes=12)),
            Alert(tipo="manual", severity="alta", empresa_id=EMP, dia_id=dia.dia_id,
                  descripcion=f"{MARK}Conductor reporta corte de calle", estado="abierta", created_at=now - timedelta(minutes=5)),
        ])
        await db.commit()
        print(f"seeded EN_CURSO dia_id={dia.dia_id} ({len(rows)} visitas, 2 alertas)")


if __name__ == "__main__":
    asyncio.run(main())
