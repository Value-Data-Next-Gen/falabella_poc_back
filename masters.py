"""Maestros estilo SimpliRoute: drivers, clientes, vehiculos extendidos.

Estos catalogos no cambian dia a dia (es informacion de referencia). Se generan
una vez al startup, indexados por id, y se exponen como endpoints.

En produccion estos endpoints serian un proxy hacia /v1/drivers/, /v1/clients/,
/v1/vehicles/ de SimpliRoute real.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd
from faker import Faker

from pipeline import (
    DEPOT,
    N_VEHICLES,
    PATTERNS,
    SEED,
    is_problem_comuna_coords,
)


VEHICLE_TYPES = [
    ("Furgon 3.5t", 30),
    ("Camion 8t", 60),
    ("Camioneta", 15),
    ("Moto", 5),
]


def gen_drivers(seed: int = SEED) -> list[dict]:
    """Un driver por vehiculo (relacion 1:1 para el demo)."""
    rng = np.random.default_rng(seed + 11)
    fake = Faker("es_CL")
    Faker.seed(seed + 11)
    drivers: list[dict] = []
    for vidx in range(N_VEHICLES):
        is_problem = (vidx + 1) in PATTERNS["problem_drivers"]
        # Drivers problematicos: fail rate inflado, rating bajo
        if is_problem:
            rating = float(round(rng.uniform(3.2, 3.9), 2))
            fail_rate = float(round(rng.uniform(0.18, 0.28), 3))
        else:
            rating = float(round(rng.uniform(4.4, 4.9), 2))
            fail_rate = float(round(rng.uniform(0.05, 0.12), 3))
        drivers.append({
            "driver_id": f"DRV-{vidx + 1:03d}",
            "name": fake.name(),
            "phone": fake.phone_number(),
            "license": "A-3 Profesional",
            "vehicle_id": vidx + 1,
            "vehicle_name": f"FAL-{1000 + vidx}",
            "rating": rating,
            "deliveries_30d": int(rng.integers(220, 320)),
            "fail_rate_30d": fail_rate,
            "active": True,
            "joined_at": (date.today() - timedelta(days=int(rng.integers(60, 1500)))).isoformat(),
        })
    return drivers


def gen_vehicles_extended(drivers: list[dict], seed: int = SEED) -> list[dict]:
    """Vehiculos con metadata completa + driver asignado."""
    rng = np.random.default_rng(seed + 22)
    by_vid = {d["vehicle_id"]: d for d in drivers}
    out: list[dict] = []
    for vidx in range(N_VEHICLES):
        vt, cap = VEHICLE_TYPES[vidx % len(VEHICLE_TYPES)]
        plate = (
            "".join(rng.choice(list("ABCDEFGHJKLMNPRSTUVWXYZ"), size=4))
            + "-"
            + "".join(rng.choice(list("0123456789"), size=2))
        )
        d = by_vid[vidx + 1]
        out.append({
            "vehicle_id": vidx + 1,
            "name": f"FAL-{1000 + vidx}",
            "type": vt,
            "plate": plate,
            "capacity_m3": int(cap),
            "driver_id": d["driver_id"],
            "driver_name": d["name"],
            "depot_lat": float(DEPOT[0]),
            "depot_lon": float(DEPOT[1]),
            "active": True,
            "year": int(rng.integers(2018, 2025)),
            "is_problem_hidden": (vidx + 1) in PATTERNS["problem_drivers"],
        })
    return out


def build_client_master(customers: list[dict],
                         historical_df: pd.DataFrame | None = None) -> list[dict]:
    """Catalogo de empresas/clientes con metricas agregadas del historico.

    Si se pasa historical_df, calcula n_visits_30d, fail_rate_30d, etc.
    """
    out: list[dict] = []
    if historical_df is not None:
        agg = historical_df.groupby("customer_id").agg(
            n_visits=("tracking_id", "count"),
            n_failed=("failed", "sum"),
            last_seen=("planned_date", "max"),
            first_seen=("planned_date", "min"),
        ).to_dict("index")
    else:
        agg = {}

    for c in customers:
        cid = c["customer_id"]
        a = agg.get(cid, {})
        n_visits = int(a.get("n_visits", 0))
        n_failed = int(a.get("n_failed", 0))
        fail_rate = float(n_failed / n_visits) if n_visits > 0 else 0.0
        out.append({
            "customer_id": cid,
            "title": c["title"],
            "address": c["address"],
            "latitude": c["latitude"],
            "longitude": c["longitude"],
            "comuna_id": _comuna_str(c["latitude"], c["longitude"]),
            "is_problem_zone": is_problem_comuna_coords(c["latitude"], c["longitude"]),
            "is_recurrent_failer": bool(c.get("_is_recurrent", False)),
            "n_visits_60d": n_visits,
            "n_failed_60d": n_failed,
            "fail_rate_60d": round(fail_rate, 3),
            "first_seen": str(a.get("first_seen", "")),
            "last_seen": str(a.get("last_seen", "")),
        })
    out.sort(key=lambda x: x["fail_rate_60d"], reverse=True)
    return out


def _comuna_str(lat: float, lon: float) -> str:
    from pipeline import COMUNA_GRID
    glat = round(round(lat / COMUNA_GRID) * COMUNA_GRID, 3)
    glon = round(round(lon / COMUNA_GRID) * COMUNA_GRID, 3)
    return f"{glat:.3f}_{glon:.3f}"
