"""Ronda 4 — A.1: split de rutas legacy con stops en >1 región.

Política: una ruta_id debe pertenecer a EXACTAMENTE 1 región. Las rutas
legacy del seed sintético violaron esta regla (hay R-... con 8 regiones).
Esta migración recorre todas las rutas inválidas y reasigna ruta_id en
cada stop según su región:

  R-20260512-093 (8 regiones)
      → R-20260512-093-RM    (stops RM)
      → R-20260512-093-VPO   (stops Valparaíso)
      → R-20260512-093-BIO   (stops Biobío)
      → ...

Códigos de región: dict REGION_CODE abajo. Si una región no está en el
mapa, se usa los primeros 3 caracteres en mayúscula.

NO se borran rows: solo se actualiza ruta_id. La operación es reversible
si se guarda un backup previo (no incluido acá — el caller debe respaldar).

Idempotente: si una ruta YA tiene sufijo de región (ej. termina en -RM),
no se vuelve a tocar.
"""
from __future__ import annotations

import re

from loguru import logger

from db import backend as db_backend, get_conn


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

# Detecta sufijos ya aplicados — para idempotencia
_SUFFIX_RE = re.compile(r"-(RM|VPO|BIO|ARA|COQ|MAU|OHI|ANT|ATA|TAR|LLA|LRI|AYS|MAG|NUB|ARI)$", re.IGNORECASE)


def _region_code(region: str) -> str:
    if not region:
        return "UNK"
    code = REGION_CODE.get(region.strip())
    if code:
        return code
    # Fallback: primeros 3 caracteres en mayúscula, sin acentos
    import unicodedata
    s = unicodedata.normalize("NFKD", region).encode("ascii", "ignore").decode("ascii")
    return (s[:3] or "UNK").upper()


def main(quiet: bool = False) -> None:
    if db_backend() != "sqlserver":
        if not quiet:
            logger.info("[split-routes] backend no-mssql, skip")
        return
    with get_conn() as cn:
        cur = cn.cursor()
        # Detectar rutas multi-region (excluyendo las que ya tienen sufijo)
        cur.execute(
            """SELECT ruta_id, COUNT(DISTINCT region) AS n_regs, COUNT(*) AS n_stops
               FROM fpoc.simpli_visits
               WHERE ruta_id IS NOT NULL AND ruta_id <> ''
                 AND region IS NOT NULL AND region <> ''
               GROUP BY ruta_id
               HAVING COUNT(DISTINCT region) > 1"""
        )
        invalid = [(str(r.ruta_id), int(r.n_regs), int(r.n_stops)) for r in cur.fetchall()]
        # Filtrar las ya sufijadas (idempotencia)
        invalid = [(rid, regs, stops) for rid, regs, stops in invalid
                   if not _SUFFIX_RE.search(rid)]

        if not invalid:
            if not quiet:
                logger.info("[split-routes] nada que hacer — 0 rutas multi-region")
            return

        if not quiet:
            logger.info(f"[split-routes] {len(invalid)} rutas a reparar")

        total_updated = 0
        for rid, n_regs, n_stops in invalid:
            cur.execute(
                "SELECT DISTINCT region FROM fpoc.simpli_visits WHERE ruta_id = ?",
                rid,
            )
            regions = [str(r.region) for r in cur.fetchall() if r.region]
            for region in regions:
                code = _region_code(region)
                new_rid = f"{rid}-{code}"
                cur.execute(
                    "UPDATE fpoc.simpli_visits SET ruta_id = ? "
                    "WHERE ruta_id = ? AND region = ?",
                    new_rid, rid, region,
                )
                n = cur.rowcount or 0
                total_updated += n
                if not quiet:
                    logger.info(f"  {rid} → {new_rid} ({region}): {n} stops")
        cn.commit()
        if not quiet:
            logger.info(
                f"[split-routes] reparadas {len(invalid)} rutas legacy, "
                f"{total_updated} stops reasignados a ruta_id con sufijo de región"
            )


if __name__ == "__main__":
    main()
