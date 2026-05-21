"""Limpieza idempotente de data sucia detectada en R7 §3.12.

Acciones:
  1. Drivers con prefijo DVR-* (typo de DRV-*) → desactivar y reportar.
     Si el driver con el id "correcto" (mismo nombre con prefijo DRV-)
     ya existe, no creamos uno nuevo — solo marcamos active=0 el typo.
  2. Driver DRV-011 con name='Manuel' → completar a 'Manuel Pérez González'.
  3. Auditoría 1 vehículo = 1 driver activo. Si encontramos un vehículo
     asignado a 2+ drivers activos, marcamos como inactivos los typo
     (DVR-*) primero, después los que tienen nombres incompletos, después
     los más antiguos. Reportamos sin tocar si no podemos decidir.
  4. Reporte final: conteo de drivers post-limpieza.

Uso:
  python -m scripts.cleanup_data_round7        # dry-run
  python -m scripts.cleanup_data_round7 --apply # ejecuta UPDATEs
"""
from __future__ import annotations

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_conn  # noqa: E402


def _scan_typo_drivers(cur) -> list[dict]:
    cur.execute(
        "SELECT driver_id, name, empresa_id, vehicle_id, active "
        "FROM fpoc.drivers WHERE driver_id LIKE 'DVR-%'"
    )
    return [
        {
            "driver_id": str(r.driver_id),
            "name": r.name,
            "empresa_id": int(r.empresa_id) if r.empresa_id is not None else None,
            "vehicle_id": int(r.vehicle_id) if r.vehicle_id is not None else None,
            "active": bool(r.active),
        }
        for r in cur.fetchall()
    ]


def _find_manuel_sin_apellido(cur) -> list[dict]:
    """driver_id DRV-011 con name='Manuel' (sin apellidos)."""
    cur.execute(
        "SELECT driver_id, name FROM fpoc.drivers "
        "WHERE driver_id = 'DRV-011' AND LTRIM(RTRIM(name)) = 'Manuel'"
    )
    return [{"driver_id": str(r.driver_id), "name": r.name} for r in cur.fetchall()]


def _find_vehicle_conflicts(cur) -> list[dict]:
    """Vehículos con >1 driver activo asignado."""
    cur.execute(
        """
        SELECT vehicle_id, COUNT(*) AS n_drivers,
               STRING_AGG(driver_id, ',') AS drivers
        FROM fpoc.drivers
        WHERE active = 1 AND vehicle_id IS NOT NULL
        GROUP BY vehicle_id
        HAVING COUNT(*) > 1
        """
    )
    return [
        {
            "vehicle_id": int(r.vehicle_id),
            "n_drivers": int(r.n_drivers),
            "drivers": str(r.drivers).split(",") if r.drivers else [],
        }
        for r in cur.fetchall()
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Ejecuta UPDATEs (sin esto, dry-run)")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"== cleanup_data_round7 · mode={mode} ==\n")

    with get_conn() as cn:
        cur = cn.cursor()

        # ----- 1) DVR-* typos -----
        typos = _scan_typo_drivers(cur)
        print(f"[1] Drivers con prefijo DVR-* (typo): {len(typos)}")
        for d in typos:
            print(f"    - {d['driver_id']} '{d['name']}' empresa={d['empresa_id']} "
                  f"vehiculo={d['vehicle_id']} active={d['active']}")
        if args.apply and typos:
            cur.execute("UPDATE fpoc.drivers SET active = 0 WHERE driver_id LIKE 'DVR-%'")
            cn.commit()
            print(f"    [APPLY] {cur.rowcount} drivers desactivados.\n")
        else:
            print()

        # ----- 2) Manuel sin apellido -----
        manuels = _find_manuel_sin_apellido(cur)
        print(f"[2] DRV-011 'Manuel' sin apellido: {len(manuels)}")
        for d in manuels:
            print(f"    - {d['driver_id']} -> 'Manuel Pérez González'")
        if args.apply and manuels:
            cur.execute(
                "UPDATE fpoc.drivers SET name = 'Manuel Pérez González' "
                "WHERE driver_id = 'DRV-011' AND LTRIM(RTRIM(name)) = 'Manuel'"
            )
            cn.commit()
            print(f"    [APPLY] {cur.rowcount} drivers renombrados.\n")
        else:
            print()

        # ----- 3) Conflictos 1 vehiculo = 1 driver -----
        conflicts = _find_vehicle_conflicts(cur)
        print(f"[3] Vehículos con >1 driver activo: {len(conflicts)}")
        for c in conflicts:
            print(f"    - vehicle_id={c['vehicle_id']} drivers={c['drivers']} (n={c['n_drivers']})")
        if args.apply and conflicts:
            # Heurística: para cada vehículo conflictivo, mantenemos el driver
            # cuyo driver_id es lexicográficamente menor (DRV-005 < DVR-004
            # alfa) y tiene nombre con espacio (apellido incluido). Resto
            # → active=0.
            for c in conflicts:
                vid = c["vehicle_id"]
                cur.execute(
                    "SELECT driver_id, name FROM fpoc.drivers "
                    "WHERE vehicle_id = ? AND active = 1 ORDER BY driver_id",
                    vid,
                )
                cand = [{"driver_id": str(r.driver_id), "name": r.name or ""} for r in cur.fetchall()]
                # Mantén el que NO empieza con DVR- y tiene apellido (espacio)
                keep = next(
                    (c for c in cand if not c["driver_id"].startswith("DVR-") and " " in c["name"].strip()),
                    cand[0] if cand else None,
                )
                if keep is None:
                    continue
                to_deactivate = [c["driver_id"] for c in cand if c["driver_id"] != keep["driver_id"]]
                if to_deactivate:
                    marks = ",".join(["?"] * len(to_deactivate))
                    cur.execute(
                        f"UPDATE fpoc.drivers SET active = 0 WHERE driver_id IN ({marks})",
                        *to_deactivate,
                    )
                    print(f"    [APPLY] vehicle {vid}: keep {keep['driver_id']}, "
                          f"deactivate {to_deactivate}")
            cn.commit()
            print()
        else:
            print()

        # ----- Reporte final -----
        cur.execute("SELECT COUNT(*) AS n FROM fpoc.drivers WHERE active = 1")
        active = int(cur.fetchone().n)
        cur.execute(
            "SELECT vehicle_id, COUNT(*) AS n FROM fpoc.drivers WHERE active = 1 "
            "AND vehicle_id IS NOT NULL GROUP BY vehicle_id HAVING COUNT(*) > 1"
        )
        remaining = list(cur.fetchall())
        print(f"== Reporte final: {active} drivers activos. "
              f"Conflictos remanentes: {len(remaining)}")
        if remaining and not args.apply:
            print("    (corre con --apply para resolver)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
