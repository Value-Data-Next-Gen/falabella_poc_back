"""Backfill de datos históricos a fpoc.simpli_visits (para demo de comparativas).

Toma la última fecha cargada como template y genera N días hacia atrás con:
  - planned_date desplazada
  - checkout_cl / current_eta_cl desplazados por el mismo delta
  - sla_hour_checkout_eta con jitter ±15%
  - status: flip 1-3% completed <-> failed para variar KPIs por día
  - id ofuscado con offset de fecha (evita PK collision)
  - Misma empresa/driver/ruta (mantiene consistencia por empresa)

Tiene efecto en fpoc.simpli_visits. fpoc.geo_suborders NO se replica: los
endpoints por localidad/motivo se mantienen agregados globales (no por fecha).

Uso:
    python fpoc_loader/seed_history.py              # 30 días
    python fpoc_loader/seed_history.py 60           # N días
"""
from __future__ import annotations

import os
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pyodbc
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent


def get_conn() -> pyodbc.Connection:
    for p in (HERE.parent / ".env", HERE.parent.parent / ".env"):
        if p.exists():
            load_dotenv(p)
            break
    conn_str = (
        f"DRIVER={{{os.environ['DB_DRIVER']}}};"
        f"SERVER={os.environ['DB_SERVER'].replace('tcp:', '')};"
        f"DATABASE={os.environ['DB_NAME']};"
        f"UID={os.environ['DB_USER']};"
        f"PWD={os.environ['DB_PASSWORD']};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )
    return pyodbc.connect(conn_str, autocommit=False)


SIMPLI_COLS = [
    "planned_date", "id", "title", "order", "address",
    "checkout_cl", "current_eta_cl", "status",
    "checkout_comment", "checkout_observation", "reference", "country",
    "sla_hour_checkout_eta", "bin_start", "bin_end", "bin_label", "bin_index",
    "ct", "Patente_falsa", "Empresa_falsa", "Drivername",
    "Fechainicioruta", "Fechainicioruta_hora_cl",
    "fechas_futuras_bq", "finicio_currenteta_bq",
    "current_eta_cl_fechainicioruta", "current_eta_cl_fechainicioruta_dates",
    "ruta_eta_futuro", "ruta_fecha_inicio_mayor_eta",
    "ruta_primer_punto_lejano", "ruta_fecha_inicio_distinta_fecha_eta",
    "am_pm", "ruta_anomala",
]


def bin_of(sla_h: float) -> tuple[float, float, str, int]:
    """Replica el binning 0.5h visto en el Excel original."""
    bin_start = (sla_h // 0.5) * 0.5
    bin_end = bin_start + 0.5
    label = f"[{bin_start}, {bin_end}]"
    # bin_index original del Excel: parece ser un índice discreto
    # Uso un shift para que sea monotónico razonable
    bin_index = int(40 + bin_start * 2)
    return bin_start, bin_end, label, bin_index


def generate_day(cn: pyodbc.Connection, template_date: date, target_date: date, rng: random.Random) -> int:
    """Replica las filas de template_date con variaciones hacia target_date."""
    delta = target_date - template_date
    date_offset_seconds = int(delta.total_seconds())
    # Offset para id único por día (separación grande para evitar colisiones)
    id_offset = abs(delta.days) * 10_000_000

    cur = cn.cursor()
    cur.execute(
        "SELECT " + ", ".join(f"[{c}]" for c in SIMPLI_COLS) +
        " FROM fpoc.simpli_visits WHERE planned_date = ?",
        template_date,
    )
    rows = cur.fetchall()
    if not rows:
        return 0

    # Borrar target si existe (idempotencia)
    cur.execute("DELETE FROM fpoc.simpli_visits WHERE planned_date = ?", target_date)

    new_rows: list[tuple] = []
    for r in rows:
        d = dict(zip(SIMPLI_COLS, r))

        # id único por día
        d["id"] = int(d["id"]) + id_offset

        # planned_date
        d["planned_date"] = target_date

        # checkout_cl y current_eta_cl: mismo timestamp shifted
        if d["checkout_cl"] is not None:
            d["checkout_cl"] = d["checkout_cl"] + delta
        if d["current_eta_cl"] is not None:
            d["current_eta_cl"] = d["current_eta_cl"] + delta

        # sla jitter ±15%
        sla_base = float(d["sla_hour_checkout_eta"])
        jitter = rng.uniform(-0.15, 0.15) * max(abs(sla_base), 1.0)
        new_sla = sla_base + jitter
        d["sla_hour_checkout_eta"] = round(new_sla, 4)
        bs, be, label, idx = bin_of(new_sla)
        d["bin_start"] = bs
        d["bin_end"] = be
        d["bin_label"] = label
        d["bin_index"] = idx

        # Pequeño drift en status (~2% flips)
        if rng.random() < 0.02:
            d["status"] = "failed" if d["status"] == "completed" else "completed"

        # Ruta anómala: drift moderado (~±3%)
        if rng.random() < 0.03:
            d["ruta_anomala"] = 1 - int(d["ruta_anomala"])
            d["ruta_eta_futuro"] = int(d["ruta_anomala"])
            d["ruta_fecha_inicio_distinta_fecha_eta"] = int(d["ruta_anomala"])

        new_rows.append(tuple(d[c] for c in SIMPLI_COLS))

    cur.fast_executemany = True
    placeholders = ", ".join(["?"] * len(SIMPLI_COLS))
    cols_sql = ", ".join(f"[{c}]" for c in SIMPLI_COLS)
    cur.executemany(
        f"INSERT INTO fpoc.simpli_visits ({cols_sql}) VALUES ({placeholders})",
        new_rows,
    )
    cn.commit()
    return len(new_rows)


def main(argv: list[str]) -> int:
    n_days = int(argv[1]) if len(argv) > 1 else 30
    rng = random.Random(42)

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("SELECT MAX(planned_date), COUNT(*) FROM fpoc.simpli_visits")
        max_date, total = cur.fetchone()
        if not max_date:
            print("No hay datos en fpoc.simpli_visits. Carga el xlsx primero.")
            return 1
        template = max_date
        print(f"[template] fecha={template} filas={total}")

        total_inserted = 0
        for offset in range(1, n_days + 1):
            target = template - timedelta(days=offset)
            n = generate_day(cn, template, target, rng)
            total_inserted += n
            print(f"[+{offset:3d}d] {target}: insert {n}")

        cur.execute("SELECT MIN(planned_date), MAX(planned_date), COUNT(*) FROM fpoc.simpli_visits")
        mn, mx, tot = cur.fetchone()
        print(f"[done] rango {mn} → {mx} · total {tot} filas")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
