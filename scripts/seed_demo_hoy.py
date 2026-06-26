"""Seed a small, narratable EN_CURSO dia for today's client demo (2026-06-26).

Run: python -m scripts.seed_demo_hoy

3 rutas (3 conductores) x 5 visitas c/u = 15 visitas, mostrando:
- Mezcla de entregadas / pendientes / atrasadas (eta_breach) / VIP
- Cliente RETENIDO (no entregar) en la ruta 1
- Una no_entregado con motivo + comentario para clasificacion IA
- 3 alertas vivas: 1 eta_breach critica, 1 manual alta, 1 cliente_retenido

Idempotente: borra cualquier dia previo de la misma fecha/empresa.
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
FECHA = date(2026, 6, 26)
MARK = "DEMO0626-"


async def main() -> None:
    sm = get_sessionmaker()
    async with sm() as db:
        now = await db.scalar(select(SimClock.sim_now).where(SimClock.id == 1)) or datetime.now(UTC)

        # 1) Wipe previous dia for the same fecha+empresa (idempotent re-run).
        prev = (await db.execute(
            select(DiaOperativo).where(
                DiaOperativo.empresa_id == EMP, DiaOperativo.fecha == FECHA
            )
        )).scalars().all()
        for d in prev:
            await db.execute(delete(Alert).where(Alert.dia_id == d.dia_id))
            await db.execute(delete(Visita).where(Visita.dia_id == d.dia_id))
            await db.execute(delete(Ruta).where(Ruta.dia_id == d.dia_id))
            await db.delete(d)
        await db.flush()

        # 2) Pick 3 active drivers + 3 vehicles. Prefer DRV-01001 (opted_in)
        #    in slot 1 so we can demo bot/whatsapp tools live.
        # MSSQL lacks NULLS LAST; emulate via a CASE: opted_in first, then driver_id.
        from sqlalchemy import case
        opted_first = case((Driver.opted_in_at.is_(None), 1), else_=0)
        drivers = (await db.execute(
            select(Driver).where(Driver.empresa_id == EMP, Driver.activo == True)  # noqa: E712
            .order_by(opted_first, Driver.driver_id.asc())
            .limit(3)
        )).scalars().all()
        vehicles = (await db.execute(
            select(Vehicle).where(Vehicle.empresa_id == EMP, Vehicle.activo == True)  # noqa: E712
            .limit(3)
        )).scalars().all()
        if len(drivers) < 3 or len(vehicles) < 3:
            raise SystemExit("Necesito al menos 3 drivers + 3 vehiculos activos en empresa 1")

        blocked = await db.scalar(
            select(Cliente).where(Cliente.retener == True)  # noqa: E712
        )

        # 3) Dia EN_CURSO.
        dia = DiaOperativo(
            empresa_id=EMP, fecha=FECHA, estado="EN_CURSO",
            iniciado_at=now - timedelta(hours=3),
        )
        db.add(dia)
        await db.flush()

        # ---- Ruta 1: muy saludable (4/5 entregadas, 0 atrasos). ----
        r1 = Ruta(dia_id=dia.dia_id, driver_id=drivers[0].driver_id,
                  vehicle_id=vehicles[0].vehicle_id, orden=1, folio=f"{MARK}R1")
        # ---- Ruta 2: con atrasos + 1 no_entregado motivo. ----
        r2 = Ruta(dia_id=dia.dia_id, driver_id=drivers[1].driver_id,
                  vehicle_id=vehicles[1].vehicle_id, orden=2, folio=f"{MARK}R2")
        # ---- Ruta 3: con VIP y cliente retenido. ----
        r3 = Ruta(dia_id=dia.dia_id, driver_id=drivers[2].driver_id,
                  vehicle_id=vehicles[2].vehicle_id, orden=3, folio=f"{MARK}R3")
        db.add_all([r1, r2, r3])
        await db.flush()

        # Visit helper.
        def v(ruta: Ruta, orden: int, estado: str, **kw) -> Visita:
            return Visita(
                dia_id=dia.dia_id, empresa_id=EMP, ruta_id=ruta.ruta_id,
                orden=orden, estado=estado, **kw,
            )

        # Comunas representativas de Stgo para que el mapa luzca.
        rows: list[Visita] = [
            # RUTA 1 — saludable
            v(r1, 1, "entregado", cliente_nombre="Maria Gonzalez", direccion="Av. Providencia 1234",
              comuna="Providencia", lat=-33.4256, lon=-70.6135,
              eta_estimada=now - timedelta(hours=2, minutes=30),
              completada_at=now - timedelta(hours=2, minutes=20),
              folio_cliente=f"{MARK}F1001"),
            v(r1, 2, "entregado", cliente_nombre="Juan Perez", direccion="Av. Apoquindo 4500",
              comuna="Las Condes", lat=-33.4090, lon=-70.5650,
              eta_estimada=now - timedelta(hours=1, minutes=45),
              completada_at=now - timedelta(hours=1, minutes=40),
              folio_cliente=f"{MARK}F1002"),
            v(r1, 3, "entregado", cliente_nombre="Carla Soto", direccion="El Bosque Norte 0123",
              comuna="Las Condes", lat=-33.4140, lon=-70.5790,
              eta_estimada=now - timedelta(hours=1),
              completada_at=now - timedelta(minutes=55),
              folio_cliente=f"{MARK}F1003"),
            v(r1, 4, "entregado", cliente_nombre="Pedro Ramirez", direccion="Vitacura 3500",
              comuna="Vitacura", lat=-33.3950, lon=-70.5910,
              eta_estimada=now - timedelta(minutes=20),
              completada_at=now - timedelta(minutes=15),
              folio_cliente=f"{MARK}F1004"),
            v(r1, 5, "pendiente", cliente_nombre="Sofia Munoz", direccion="Alonso de Cordova 2580",
              comuna="Vitacura", lat=-33.3970, lon=-70.5860,
              eta_estimada=now + timedelta(minutes=25),
              folio_cliente=f"{MARK}F1005"),

            # RUTA 2 — con 2 atrasos + 1 no_entregado para clasificar IA
            v(r2, 1, "entregado", cliente_nombre="Luis Rojas", direccion="Av. Vicuna Mackenna 3300",
              comuna="Macul", lat=-33.4870, lon=-70.6090,
              eta_estimada=now - timedelta(hours=2),
              completada_at=now - timedelta(hours=1, minutes=50),
              folio_cliente=f"{MARK}F2001"),
            v(r2, 2, "no_entregado", cliente_nombre="Ana Torres", direccion="Departamental 1450",
              comuna="San Miguel", lat=-33.5040, lon=-70.6580,
              eta_estimada=now - timedelta(hours=1, minutes=15),
              completada_at=now - timedelta(hours=1, minutes=5),
              motivo="Cliente ausente",
              motivo_comentario="Toque timbre 3 veces, nadie atendio, vecino dice que cliente en trabajo",
              folio_cliente=f"{MARK}F2002"),
            v(r2, 3, "entregado", cliente_nombre="Carlos Vargas", direccion="Gran Avenida 5670",
              comuna="San Miguel", lat=-33.5090, lon=-70.6510,
              eta_estimada=now - timedelta(minutes=50),
              completada_at=now - timedelta(minutes=42),
              folio_cliente=f"{MARK}F2003"),
            v(r2, 4, "pendiente", cliente_nombre="Patricia Flores", direccion="Lo Espejo 1230",
              comuna="Lo Espejo", lat=-33.5320, lon=-70.6920,
              eta_estimada=now - timedelta(minutes=35),     # ATRASADA
              folio_cliente=f"{MARK}F2004"),
            v(r2, 5, "pendiente", cliente_nombre="Roberto Castro", direccion="El Bosque 870",
              comuna="El Bosque", lat=-33.5680, lon=-70.6730,
              eta_estimada=now - timedelta(minutes=15),     # ATRASADA
              folio_cliente=f"{MARK}F2005"),

            # RUTA 3 — VIP + retenido + buena progresion
            v(r3, 1, "entregado", cliente_nombre="Empresa XYZ (VIP)",
              direccion="Av. Kennedy 5757", comuna="Las Condes",
              lat=-33.4040, lon=-70.5530,
              es_vip=1,
              eta_estimada=now - timedelta(hours=1, minutes=30),
              completada_at=now - timedelta(hours=1, minutes=22),
              folio_cliente=f"{MARK}F3001"),
            v(r3, 2, "entregado", cliente_nombre="Daniela Fuentes", direccion="Manquehue Sur 950",
              comuna="Las Condes", lat=-33.4180, lon=-70.5700,
              eta_estimada=now - timedelta(minutes=55),
              completada_at=now - timedelta(minutes=48),
              folio_cliente=f"{MARK}F3002"),
            v(r3, 3, "pendiente", cliente_nombre="Marcelo Lopez (VIP)",
              direccion="Cerro Colorado 5240", comuna="Las Condes",
              lat=-33.4090, lon=-70.5580,
              es_vip=1,
              eta_estimada=now + timedelta(minutes=10),
              folio_cliente=f"{MARK}F3003"),
            v(r3, 4, "pendiente", cliente_nombre="Veronica Aguilar", direccion="Tobalaba 1500",
              comuna="Providencia", lat=-33.4200, lon=-70.6020,
              eta_estimada=now + timedelta(minutes=40),
              folio_cliente=f"{MARK}F3004"),
        ]
        # Visita 5 de ruta 3: cliente RETENIDO si lo tenemos en BD.
        if blocked:
            rows.append(v(r3, 5, "pendiente",
                          cliente_id=blocked.cliente_id,
                          cliente_nombre=blocked.nombre,
                          direccion="Direccion bloqueada (RETENIDO)",
                          comuna="Nunoa", lat=-33.4560, lon=-70.5950,
                          eta_estimada=now + timedelta(minutes=70),
                          folio_cliente=f"{MARK}F3005"))
        else:
            rows.append(v(r3, 5, "pendiente",
                          cliente_nombre="Sin cliente retenido (seed)",
                          direccion="Irarrazaval 2200", comuna="Nunoa",
                          lat=-33.4560, lon=-70.5950,
                          eta_estimada=now + timedelta(minutes=70),
                          folio_cliente=f"{MARK}F3005"))
        db.add_all(rows)

        # 4) Alertas vivas para el Centro de Control.
        db.add_all([
            Alert(tipo="eta_breach", severity="critica", empresa_id=EMP, dia_id=dia.dia_id,
                  descripcion=f"{MARK}Visita Patricia Flores atrasada 35 min (Ruta 2)",
                  estado="abierta", created_at=now - timedelta(minutes=8)),
            Alert(tipo="manual", severity="alta", empresa_id=EMP, dia_id=dia.dia_id,
                  descripcion=f"{MARK}Conductor reporta corte de calle Gran Avenida",
                  estado="abierta", created_at=now - timedelta(minutes=15)),
            Alert(tipo="manual", severity="media", empresa_id=EMP, dia_id=dia.dia_id,
                  descripcion=f"{MARK}Cliente marcado NO ENTREGAR (retenido) en ruta 3",
                  estado="abierta", created_at=now - timedelta(minutes=3)),
        ])

        await db.commit()

        # 5) Resumen narrable.
        print("\n" + "=" * 60)
        print(f"  DEMO HOY · empresa={EMP} fecha={FECHA} dia_id={dia.dia_id}")
        print("=" * 60)
        print(f"  Sim now : {now}")
        print(f"  Estado  : EN_CURSO")
        print(f"  Rutas   : 3  |  Visitas: {len(rows)}  |  Alertas: 3")
        print()
        print(f"  R1 (sana)        driver={drivers[0].driver_id} {drivers[0].nombre}")
        print(f"  R2 (con atrasos) driver={drivers[1].driver_id} {drivers[1].nombre}")
        print(f"  R3 (VIP+reten.)  driver={drivers[2].driver_id} {drivers[2].nombre}")
        print()
        print("  Para reproducir: python -m scripts.seed_demo_hoy")
        print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
