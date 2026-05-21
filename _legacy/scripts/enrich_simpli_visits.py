"""Enriquece simpli_visits con ruta_id, region y comuna desde geo_suborders.

El XLSX simpli no trae ruta_id directamente. La hoja Geo sí tiene:
  - idruta (BIGINT)
  - patente_falsa
  - region, localidad (comuna)
  - fechapactada

Estrategia: por cada (patente, fechapactada) en geo, agrupamos el idruta
mayoritario + region + localidad. Después actualizamos simpli_visits
matcheando por (patente_falsa, planned_date).

USO:
  python scripts/enrich_simpli_visits.py             # DRY RUN
  python scripts/enrich_simpli_visits.py --apply     # ejecuta
  python scripts/enrich_simpli_visits.py --apply --fecha 2026-04-19   # solo 1 día
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from typing import Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from db import get_conn  # noqa: E402

BATCH = 500


def _normalize_localidad(s: Optional[str]) -> Optional[str]:
    """Pasa 'PENALOLEN' / 'PENALOLEN' / 'Penalolen' --> 'Peñalolén' formato título."""
    if not s:
        return None
    s = s.strip().title()
    return s or None


def main(apply: bool, fecha: Optional[str]) -> int:
    mode = "APLY" if apply else "DRY RUN"
    print(f"=== Enrich simpli_visits [{mode}] {fecha or '(todas las fechas)'} ===\n")

    with get_conn() as cn:
        cur = cn.cursor()

        # 1) Construir mapping (patente, fecha) -> (idruta_mayoritario, region, comuna)
        params: list = []
        where_geo = ""
        where_simpli = ""
        if fecha:
            where_geo = "WHERE fechapactada = ?"
            where_simpli = "WHERE planned_date = ?"
            params = [fecha]

        cur.execute(
            f"""SELECT patente_falsa, fechapactada AS fecha, idruta, region, localidad
                FROM fpoc.geo_suborders {where_geo}""",
            *params,
        )
        # mapping key: (patente_int, fecha_str)
        # value: { rutas: Counter[idruta], regions: Counter, comunas: Counter }
        agg: dict[tuple, dict] = {}
        for r in cur.fetchall():
            if r.patente_falsa is None or r.fecha is None:
                continue
            key = (int(r.patente_falsa), str(r.fecha))
            slot = agg.setdefault(key, {"rutas": Counter(), "regions": Counter(), "comunas": Counter()})
            if r.idruta is not None:
                slot["rutas"][int(r.idruta)] += 1
            if r.region:
                slot["regions"][str(r.region)] += 1
            if r.localidad:
                slot["comunas"][_normalize_localidad(r.localidad)] += 1

        print(f"Pares (patente, fecha) con datos geo: {len(agg)}")

        # 2) Sample de lo que se resolvió
        for key in list(agg.keys())[:3]:
            d = agg[key]
            top_ruta = d["rutas"].most_common(1)
            top_region = d["regions"].most_common(1)
            top_comuna = d["comunas"].most_common(1)
            print(f"  patente={key[0]} fecha={key[1]} --> "
                  f"idruta={top_ruta[0][0] if top_ruta else None} "
                  f"region={top_region[0][0] if top_region else None} "
                  f"comuna={top_comuna[0][0] if top_comuna else None}")

        # 3) Audit cuántas visitas se afectarían
        cur.execute(
            f"SELECT COUNT(*) AS n FROM fpoc.simpli_visits {where_simpli} "
            f"{'AND' if where_simpli else 'WHERE'} ruta_id IS NULL",
            *params,
        )
        n_pending = int(cur.fetchone().n)
        print(f"\nVisitas con ruta_id NULL a enriquecer: {n_pending}")

        if not apply:
            print(f"\n[DRY RUN] no se modifica nada.")
            return 0

        # 4) Para cada par, UPDATE las visitas simpli del mismo (patente, fecha)
        updated = 0
        for (patente, f), d in agg.items():
            top_ruta = d["rutas"].most_common(1)
            top_region = d["regions"].most_common(1)
            top_comuna = d["comunas"].most_common(1)
            if not top_ruta:
                continue  # sin idruta no podemos derivar ruta_id
            new_rid = f"R-{top_ruta[0][0]}"
            region = top_region[0][0] if top_region else None
            comuna = top_comuna[0][0] if top_comuna else None

            cur.execute(
                "UPDATE fpoc.simpli_visits SET ruta_id = ?, region = ?, comuna = ? "
                "WHERE patente_falsa = ? AND planned_date = ? AND ruta_id IS NULL",
                new_rid, region, comuna, patente, f,
            )
            n = cur.rowcount or 0
            updated += n
            cn.commit()

        print(f"\n[OK] Visitas actualizadas: {updated} / {n_pending}")

        # 5) Verificación
        cur.execute(
            f"SELECT COUNT(DISTINCT ruta_id) AS nr, COUNT(*) AS nv FROM fpoc.simpli_visits "
            f"{where_simpli}",
            *params,
        )
        v = cur.fetchone()
        print(f"  --> {int(v.nr)} rutas distintas en {int(v.nv)} visitas")

    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--fecha", default=None, help="YYYY-MM-DD (default: todas)")
    args = ap.parse_args()
    sys.exit(main(apply=args.apply, fecha=args.fecha))
