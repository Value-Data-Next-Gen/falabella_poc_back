"""Ronda 4 — A.1: split de rutas legacy con stops en >1 región (en batches).

Política: una ruta_id pertenece a EXACTAMENTE 1 región. Las rutas legacy
del seed sintético violaron esto (R-... con 8 regiones). Esta migración
reasigna ruta_id en cada stop según su región:

  R-20260512-093 (8 regiones)
      → R-20260512-093-RM    (stops RM)
      → R-20260512-093-VPO   (stops Valparaíso)
      → ...

Estrategia: por cada (ruta_id_vieja, region), seleccionar los `id`s de
las visitas y actualizar en batches de 500 para evitar locks largos y
permitir progreso visible.

Idempotente: si una ruta_id YA termina en sufijo conocido, se skipea.
NO se borran rows.
"""
from __future__ import annotations

import re
import unicodedata

from loguru import logger

from core.db import backend as db_backend, get_conn


REGION_CODE = {
    "RM":           "RM",
    "Metropolitana": "RM",
    "Valparaíso":   "VPO",
    "Valparaiso":   "VPO",
    "Biobío":       "BIO",
    "Biobio":       "BIO",
    "Bío-Bío":      "BIO",
    "Bio-Bio":      "BIO",
    "Araucanía":    "ARA",
    "Araucania":    "ARA",
    "Coquimbo":     "COQ",
    "Maule":        "MAU",
    "O'Higgins":    "OHI",
    "OHiggins":     "OHI",
    "Antofagasta":  "ANT",
    "Atacama":      "ATA",
    "Tarapacá":     "TAR",
    "Tarapaca":     "TAR",
    "Los Lagos":    "LLA",
    "Los Ríos":     "LRI",
    "Los Rios":     "LRI",
    "Aysén":        "AYS",
    "Aysen":        "AYS",
    "Magallanes":   "MAG",
    "Ñuble":        "NUB",
    "Nuble":        "NUB",
    "Arica y Parinacota": "ARI",
}

_SUFFIX_RE = re.compile(r"-(RM|VPO|BIO|ARA|COQ|MAU|OHI|ANT|ATA|TAR|LLA|LRI|AYS|MAG|NUB|ARI)$", re.IGNORECASE)
BATCH_SIZE = 500


def _region_code(region: str) -> str:
    if not region:
        return "UNK"
    code = REGION_CODE.get(region.strip())
    if code:
        return code
    s = unicodedata.normalize("NFKD", region).encode("ascii", "ignore").decode("ascii")
    return (s[:3] or "UNK").upper()


def _update_by_ids_chunked(cur, ids: list[int], new_rid: str) -> int:
    """UPDATE en chunks de BATCH_SIZE. Devuelve filas actualizadas total."""
    total = 0
    for i in range(0, len(ids), BATCH_SIZE):
        chunk = ids[i:i + BATCH_SIZE]
        marks = ",".join(["?"] * len(chunk))
        cur.execute(
            f"UPDATE fpoc.simpli_visits SET ruta_id = ? WHERE id IN ({marks})",
            new_rid, *chunk,
        )
        total += cur.rowcount or 0
    return total


def main(quiet: bool = False) -> None:
    if db_backend() != "sqlserver":
        if not quiet:
            logger.info("[split-routes] backend no-mssql, skip")
        return
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """SELECT ruta_id, COUNT(DISTINCT region) AS n_regs, COUNT(*) AS n_stops
               FROM fpoc.simpli_visits
               WHERE ruta_id IS NOT NULL AND ruta_id <> ''
                 AND region IS NOT NULL AND region <> ''
               GROUP BY ruta_id
               HAVING COUNT(DISTINCT region) > 1"""
        )
        invalid = [(str(r.ruta_id), int(r.n_regs), int(r.n_stops)) for r in cur.fetchall()]
        invalid = [(rid, regs, stops) for rid, regs, stops in invalid
                   if not _SUFFIX_RE.search(rid)]

        if not invalid:
            if not quiet:
                logger.info("[split-routes] nada que hacer — 0 rutas multi-region sin sufijo")
            return

        if not quiet:
            logger.info(f"[split-routes] {len(invalid)} rutas a reparar (batches de {BATCH_SIZE})")

        total_updated = 0
        for idx, (rid, n_regs, n_stops) in enumerate(invalid, 1):
            cur.execute(
                "SELECT DISTINCT region FROM fpoc.simpli_visits WHERE ruta_id = ?",
                rid,
            )
            regions = [str(r.region) for r in cur.fetchall() if r.region]
            for region in regions:
                code = _region_code(region)
                new_rid = f"{rid}-{code}"
                # SELECT ids primero (rápido con índice)
                cur.execute(
                    "SELECT id FROM fpoc.simpli_visits WHERE ruta_id = ? AND region = ?",
                    rid, region,
                )
                ids = [int(r.id) for r in cur.fetchall()]
                if not ids:
                    continue
                n = _update_by_ids_chunked(cur, ids, new_rid)
                cn.commit()  # commit por (ruta, region) — progreso visible
                total_updated += n
                if not quiet:
                    logger.info(f"  [{idx}/{len(invalid)}] {rid} → {new_rid} ({region}): {n} stops en {len(ids)//BATCH_SIZE+1} batch(es)")

        if not quiet:
            logger.info(
                f"[split-routes] reparadas {len(invalid)} rutas legacy, "
                f"{total_updated} stops reasignados"
            )


if __name__ == "__main__":
    main()
