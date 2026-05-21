"""Truncate destructivo de tablas de visitas para empezar de cero.

Borra TODAS las visitas y dependencias derivadas. Preserva:
  - fpoc.users, drivers, vehicles, empresas_transporte, empresa_contactos
  - fpoc.app_config, day_config
  - fpoc.schema_migrations
  - fpoc.notifications_log (historial Twilio)
  - fpoc.whatsapp_sessions

Borra:
  - fpoc.motivo_corrections
  - fpoc.visit_comments
  - fpoc.visit_priority_overrides
  - fpoc.client_day_notes
  - fpoc.dotacion_diaria
  - fpoc.vip_clients         (opcional via --keep-vips)
  - fpoc.geo_suborders
  - fpoc.simpli_visits
  - fpoc.planificacion_imports

USO:
  python scripts/truncate_visit_data.py                # DRY RUN
  python scripts/truncate_visit_data.py --apply        # ejecuta
  python scripts/truncate_visit_data.py --apply --keep-vips  # preserva VIPs marcados
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from db import get_conn  # noqa: E402


# Orden importa: hijos primero, padres después.
TARGETS = [
    "fpoc.motivo_corrections",
    "fpoc.visit_comments",
    "fpoc.visit_priority_overrides",
    "fpoc.client_day_notes",
    "fpoc.dotacion_diaria",
    "fpoc.geo_suborders",
    "fpoc.simpli_visits",
    "fpoc.planificacion_imports",
    # vip_clients se controla con flag
]


def main(apply: bool, keep_vips: bool) -> int:
    mode = "APLY" if apply else "DRY RUN"
    print(f"=== Truncate visit data [{mode}] ===\n")

    targets = list(TARGETS)
    if not keep_vips:
        targets.append("fpoc.vip_clients")

    with get_conn() as cn:
        cur = cn.cursor()
        # Audit pre
        for t in targets:
            cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
            n = int(cur.fetchone().n)
            print(f"  {t:<40} {n:>10,} rows")

        if not apply:
            print(f"\n[DRY RUN] no se modifica nada. Correr con --apply para borrar.")
            return 0

        print()
        for t in targets:
            try:
                # Intentamos TRUNCATE primero (más rápido). Si falla por FK,
                # caemos a DELETE.
                try:
                    cur.execute(f"TRUNCATE TABLE {t}")
                    cn.commit()
                    print(f"  [OK] TRUNCATE {t}")
                except Exception as e:
                    if "FOREIGN KEY" in str(e).upper() or "REFERENCE" in str(e).upper():
                        cur.execute(f"DELETE FROM {t}")
                        cn.commit()
                        print(f"  [OK] DELETE   {t}")
                    else:
                        raise
            except Exception as e:  # noqa: BLE001
                print(f"  [ERR] {t}: {str(e)[:150]}")

        # Audit post
        print(f"\n=== Resumen ===")
        for t in targets:
            try:
                cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
                print(f"  {t:<40} {int(cur.fetchone().n):>10,} rows")
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--keep-vips", action="store_true", help="No borrar fpoc.vip_clients")
    args = ap.parse_args()
    sys.exit(main(apply=args.apply, keep_vips=args.keep_vips))
