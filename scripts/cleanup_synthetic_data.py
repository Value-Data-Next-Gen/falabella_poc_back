"""Borra data sintética de simpli_visits y derivados, dejando solo la data
cargada vía XLSX (que tiene registro en fpoc.planificacion_imports).

Política:
  - "Real" = planned_date está en fpoc.planificacion_imports.
  - Todo el resto (seed sintético + live_gen viejo) se borra.

Borra en este orden:
  1. fpoc.simpli_visits con planned_date no en planificacion_imports
  2. fpoc.geo_suborders huérfanos (sin matching planned_date)
  3. fpoc.dotacion_diaria con fecha no en planificacion_imports
  4. STATE.snapshot_df y reinicio del simulador queda al próximo reload.

USO:
  cd backend && python scripts/cleanup_synthetic_data.py             # DRY RUN
  cd backend && python scripts/cleanup_synthetic_data.py --apply     # ejecuta

DELETE en batches de 5000 con commit por batch para Azure SQL.
"""
from __future__ import annotations

import argparse
import os
import sys

# Permitir correr desde backend/scripts/
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from db import get_conn  # noqa: E402

BATCH = 5000


def main(apply: bool) -> int:
    mode = "APLY" if apply else "DRY RUN"
    print(f"=== Cleanup data sintética [{mode}] ===\n")

    with get_conn() as cn:
        cur = cn.cursor()

        # 1) Audit
        cur.execute("SELECT fecha FROM fpoc.planificacion_imports ORDER BY fecha")
        real_dates = [str(r.fecha) for r in cur.fetchall()]
        print(f"Fechas reales (XLSX cargado): {len(real_dates)}")
        for d in real_dates:
            print(f"  {d}")
        if not real_dates:
            print("\n⚠ No hay fechas reales registradas. Abortando para no borrar todo.")
            return 1

        cur.execute("SELECT COUNT(*) AS n FROM fpoc.simpli_visits")
        total_before = int(cur.fetchone().n)

        marks = ",".join(["?"] * len(real_dates))

        cur.execute(
            f"SELECT COUNT(*) AS n FROM fpoc.simpli_visits "
            f"WHERE planned_date NOT IN ({marks})",
            *real_dates,
        )
        n_syn = int(cur.fetchone().n)

        cur.execute(
            f"SELECT COUNT(*) AS n FROM fpoc.geo_suborders "
            f"WHERE fechapactada NOT IN ({marks})",
            *real_dates,
        )
        n_geo = int(cur.fetchone().n)

        cur.execute(
            f"SELECT COUNT(*) AS n FROM fpoc.dotacion_diaria "
            f"WHERE fecha NOT IN ({marks})",
            *real_dates,
        )
        n_dot = int(cur.fetchone().n)

        print(f"\nA borrar:")
        print(f"  simpli_visits:    {n_syn:>10,} / {total_before:,}")
        print(f"  geo_suborders:    {n_geo:>10,}")
        print(f"  dotacion_diaria:  {n_dot:>10,}")

        if not apply:
            print(f"\n[DRY RUN] no se modifica nada. Correr con --apply para ejecutar.")
            return 0

        # 2) DELETE en batches con commit por batch
        for tabla, count, col in [
            ("fpoc.simpli_visits",   n_syn, "planned_date"),
            ("fpoc.geo_suborders",   n_geo, "fechapactada"),
            ("fpoc.dotacion_diaria", n_dot, "fecha"),
        ]:
            if count == 0:
                continue
            print(f"\nBorrando {tabla} ({count:,} rows)…")
            deleted_total = 0
            while True:
                cur.execute(
                    f"DELETE TOP ({BATCH}) FROM {tabla} "
                    f"WHERE {col} NOT IN ({marks})",
                    *real_dates,
                )
                n = cur.rowcount or 0
                cn.commit()
                deleted_total += n
                if n == 0:
                    break
                print(f"  · batch {deleted_total:,} / {count:,}")
            print(f"  ✓ {deleted_total:,} eliminadas")

        # 3) Marcar migración 017 como aplicada (split rutas multi-region ya no
        # necesario porque borramos las rutas legacy)
        try:
            cur.execute(
                "INSERT INTO fpoc.schema_migrations (migration_id, applied_at) "
                "SELECT '017_split_multi_region_routes', SYSDATETIME() "
                "WHERE NOT EXISTS (SELECT 1 FROM fpoc.schema_migrations "
                "                  WHERE migration_id = '017_split_multi_region_routes')"
            )
            cn.commit()
            print("\n✓ Migración 017 marcada como aplicada (ya no aplica tras la limpieza)")
        except Exception as e:  # noqa: BLE001
            print(f"\n⚠ No pude marcar 017: {e}")

        # 4) Resumen final
        cur.execute("SELECT COUNT(*) AS n FROM fpoc.simpli_visits")
        total_after = int(cur.fetchone().n)
        print(f"\n=== Resumen ===")
        print(f"simpli_visits: {total_before:,} → {total_after:,}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Ejecutar el DELETE (default: dry run)")
    args = ap.parse_args()
    sys.exit(main(apply=args.apply))
