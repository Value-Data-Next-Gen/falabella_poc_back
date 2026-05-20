"""Cierra días EN_CURSO huérfanos (R7).

El simulador legacy y los auto-rollover viejos podían dejar varios días
marcados como EN_CURSO al mismo tiempo. Eso desalinea state.today (singleton
in-memory) con la fecha del DiaActivoPicker.

Este script:
  1. Lista todos los días con state='EN_CURSO'.
  2. Si pasás --keep YYYY-MM-DD, lo mantiene EN_CURSO y cierra los demás.
  3. Si no pasás --keep, conserva el más reciente (max(started_at)) y
     cierra el resto.
  4. Idempotente.

Uso:
  python -m scripts.cleanup_stale_en_curso              # dry-run, lista
  python -m scripts.cleanup_stale_en_curso --apply      # cierra (keep el más reciente)
  python -m scripts.cleanup_stale_en_curso --apply --keep 2026-05-12
"""
from __future__ import annotations

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_conn  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Ejecuta UPDATE (sin esto, dry-run)")
    parser.add_argument("--keep", type=str, default=None, help="Fecha YYYY-MM-DD a mantener EN_CURSO (default: la más reciente)")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"== cleanup_stale_en_curso · mode={mode} ==\n")

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT fecha, started_at, started_by_user_id "
            "FROM fpoc.planificacion_imports "
            "WHERE state = 'EN_CURSO' "
            "ORDER BY started_at DESC, fecha DESC"
        )
        rows = cur.fetchall()

        if not rows:
            print("No hay días en EN_CURSO. Nada que limpiar.")
            return 0

        items = [
            {
                "fecha": str(r.fecha if hasattr(r, "fecha") else r[0]),
                "started_at": str(r.started_at) if r.started_at else None,
                "started_by": int(r.started_by_user_id) if r.started_by_user_id is not None else None,
            }
            for r in rows
        ]
        print(f"Días en EN_CURSO: {len(items)}")
        for it in items:
            print(f"  - {it['fecha']}  started_at={it['started_at']}  user={it['started_by']}")
        print()

        keep = args.keep or items[0]["fecha"]
        to_close = [it["fecha"] for it in items if it["fecha"] != keep]

        if not to_close:
            print(f"Solo hay un día EN_CURSO ({keep}). Nada que limpiar.")
            return 0

        print(f"Mantener: {keep}")
        print(f"Cerrar:   {to_close}")

        if not args.apply:
            print("\n(dry-run — corre con --apply para ejecutar)")
            return 0

        marks = ",".join(["?"] * len(to_close))
        cur.execute(
            f"UPDATE fpoc.planificacion_imports "
            f"SET state = 'CERRADO', closed_at = SYSDATETIME() "
            f"WHERE fecha IN ({marks})",
            *to_close,
        )
        cn.commit()
        print(f"\n[APPLY] {cur.rowcount} día(s) cerrado(s).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
