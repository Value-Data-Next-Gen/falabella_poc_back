"""Swappable geocoding service (CR-019, extended in CR-020).

Backends:
  - `nominatim` (default): OpenStreetMap Nominatim public API. Rate-limited to
    1 req/s per Nominatim's usage policy. Retries 3 times on 429/5xx then falls
    back to comuna centroid.
  - `centroide_comuna`: pure-Python lookup against a hardcoded table of
    ~50 Santiago Metropolitan Region comunas. No network. Used as fallback
    even when `nominatim` is selected.

Public API (CR-019):
    coords = await geocode("Av. Apoquindo 4501", "Las Condes", "Region Metropolitana")
    # → (lat, lon) or None

Public API (CR-020 — survives uvicorn restarts):
    report = await geocode_pending_clientes(empresa_ids=None, max_batch=50)
    # → {"procesados": N, "ok": N, "fallback": N, "failed": N}

    asyncio.create_task(geocode_pending_clientes_loop())
    # → started once from `app/main.py` lifespan; sleeps GEOCODING_INTERVAL_SECONDS
    #   between batches. Hardened so a Nominatim failure cannot kill the loop.

Configured via `settings.geocoding_backend` (env GEOCODING_BACKEND).
"""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Final

import httpx
from loguru import logger
from sqlalchemy import or_, select, update

from app.core.config import settings

# ── Nominatim policy ────────────────────────────────────────────────────────
_NOMINATIM_URL: Final = "https://nominatim.openstreetmap.org/search"
_USER_AGENT: Final = "TorreDeControl/2.0 (g.rojaschacon@gmail.com)"
_MAX_RETRIES: Final = 3
_RATE_LIMIT_SECONDS: Final = 1.0

# Lock to enforce 1 req/s across concurrent callers.
_nominatim_lock = asyncio.Lock()
_last_request_at: float = 0.0


# ── Comuna centroids fallback (Region Metropolitana, Chile) ─────────────────
# Public lat/lon for each comuna's approximate centroid. Sourced from
# OpenStreetMap / Wikipedia administrative boundaries.
_COMUNA_CENTROIDS: Final[dict[str, tuple[float, float]]] = {
    # Santiago oriente
    "las condes": (-33.4172, -70.5476),
    "vitacura": (-33.3953, -70.5800),
    "lo barnechea": (-33.3500, -70.5167),
    "providencia": (-33.4304, -70.6092),
    "nunoa": (-33.4569, -70.5972),
    "ñunoa": (-33.4569, -70.5972),
    "nuñoa": (-33.4569, -70.5972),
    "ñuñoa": (-33.4569, -70.5972),
    "la reina": (-33.4486, -70.5403),
    "penalolen": (-33.4828, -70.5414),
    "peñalolen": (-33.4828, -70.5414),
    "peñalolén": (-33.4828, -70.5414),
    "macul": (-33.4925, -70.5975),
    # Santiago centro / norte
    "santiago": (-33.4489, -70.6693),
    "santiago centro": (-33.4489, -70.6693),
    "recoleta": (-33.4022, -70.6394),
    "independencia": (-33.4181, -70.6628),
    "conchali": (-33.3833, -70.6750),
    "conchalí": (-33.3833, -70.6750),
    "huechuraba": (-33.3672, -70.6400),
    "quilicura": (-33.3633, -70.7286),
    "renca": (-33.4078, -70.7297),
    # Santiago sur
    "san miguel": (-33.4969, -70.6517),
    "san joaquin": (-33.4953, -70.6275),
    "san joaquín": (-33.4953, -70.6275),
    "la cisterna": (-33.5333, -70.6644),
    "la granja": (-33.5403, -70.6286),
    "san ramon": (-33.5417, -70.6444),
    "san ramón": (-33.5417, -70.6444),
    "el bosque": (-33.5644, -70.6750),
    "la pintana": (-33.5853, -70.6400),
    "pedro aguirre cerda": (-33.4847, -70.6700),
    # Santiago poniente
    "estacion central": (-33.4544, -70.6889),
    "estación central": (-33.4544, -70.6889),
    "quinta normal": (-33.4283, -70.6889),
    "cerro navia": (-33.4214, -70.7350),
    "lo prado": (-33.4444, -70.7236),
    "pudahuel": (-33.4458, -70.7517),
    # Santiago sur-oriente
    "la florida": (-33.5394, -70.6028),
    "puente alto": (-33.6111, -70.5750),
    "san bernardo": (-33.6000, -70.7000),
    # Periferia
    "maipu": (-33.5110, -70.7580),
    "maipú": (-33.5110, -70.7580),
    "buin": (-33.7333, -70.7417),
    "pirque": (-33.6500, -70.5667),
    "san jose de maipo": (-33.6500, -70.3500),
    "san josé de maipo": (-33.6500, -70.3500),
    "padre hurtado": (-33.5717, -70.8200),
    "penaflor": (-33.6086, -70.8786),
    "peñaflor": (-33.6086, -70.8786),
    "talagante": (-33.6647, -70.9286),
    "el monte": (-33.6817, -70.9839),
    "isla de maipo": (-33.7531, -70.9000),
    "melipilla": (-33.6878, -71.2150),
    "maria pinto": (-33.5333, -71.1000),
    "maría pinto": (-33.5333, -71.1000),
    "curacavi": (-33.4000, -71.1500),
    "curacaví": (-33.4000, -71.1500),
    "lampa": (-33.2833, -70.8833),
    "til til": (-33.0833, -70.9333),
    "tiltil": (-33.0833, -70.9333),
    "colina": (-33.2000, -70.6833),
}


def _normalize(s: str) -> str:
    """Lowercase + strip. Accents preserved (table has both variants)."""
    return s.strip().lower()


def centroide_comuna(comuna: str | None) -> tuple[float, float] | None:
    """Look up lat/lon for a comuna centroid. Returns None if unknown."""
    if not comuna:
        return None
    return _COMUNA_CENTROIDS.get(_normalize(comuna))


async def _geocode_nominatim(
    direccion: str,
    comuna: str,
    region: str,
) -> tuple[float, float] | None:
    """Single Nominatim lookup with 1 req/s gate + 3-retry backoff on 429/5xx."""
    global _last_request_at

    query = f"{direccion}, {comuna}, {region}, Chile"
    params = {"q": query, "format": "json", "limit": 1}
    headers = {"User-Agent": _USER_AGENT}

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            # Hold the lock across the gate AND the request so concurrent
            # callers can't fire in parallel — only one Nominatim request is in
            # flight at a time, started >=1s after the previous one finished.
            async with _nominatim_lock:
                loop = asyncio.get_event_loop()
                delta = loop.time() - _last_request_at
                if delta < _RATE_LIMIT_SECONDS:
                    await asyncio.sleep(_RATE_LIMIT_SECONDS - delta)
                async with httpx.AsyncClient(timeout=10.0) as client:
                    r = await client.get(_NOMINATIM_URL, params=params, headers=headers)
                _last_request_at = loop.time()
            if r.status_code == 200:
                data = r.json()
                if not data:
                    return None
                lat = float(data[0]["lat"])
                lon = float(data[0]["lon"])
                return lat, lon
            if r.status_code in (429, 500, 502, 503, 504):
                backoff = 2 ** attempt
                logger.warning(
                    f"Nominatim {r.status_code} for '{query}'; retry {attempt}/{_MAX_RETRIES} after {backoff}s"
                )
                await asyncio.sleep(backoff)
                continue
            logger.warning(f"Nominatim {r.status_code} for '{query}': {r.text[:200]}")
            return None
        except (TimeoutError, httpx.RequestError) as e:
            logger.warning(f"Nominatim network error for '{query}': {e}; retry {attempt}/{_MAX_RETRIES}")
            await asyncio.sleep(2 ** attempt)

    logger.warning(f"Nominatim exhausted retries for '{query}'")
    return None


async def geocode(
    direccion: str,
    comuna: str,
    region: str = "Region Metropolitana",
) -> tuple[float, float] | None:
    """Geocode address using the configured backend; fall back to comuna centroid.

    Returns:
        (lat, lon) tuple, or None if both backends fail.
    """
    if not direccion or not direccion.strip():
        return centroide_comuna(comuna)

    backend = (settings.geocoding_backend or "nominatim").lower()

    if backend == "nominatim":
        coords = await _geocode_nominatim(direccion, comuna, region)
        if coords is not None:
            return coords
        # Fallback chain.
        return centroide_comuna(comuna)

    # Default + explicit "centroide_comuna".
    return centroide_comuna(comuna)


# ── CR-020: lifespan-owned background loop + admin trigger ─────────────────

# Max times we'll re-hit Nominatim for the SAME cliente before giving up. Each
# attempt = one `_geocode_nominatim` call (which itself retries 3x on 429/5xx).
# When attempts >= GEOCODING_MAX_ATTEMPTS we mark `geocoding_status='failed'`.
GEOCODING_MAX_ATTEMPTS: Final = 3


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"Invalid int for {name}={raw!r}; using default {default}")
        return default


async def geocode_pending_clientes(
    empresa_ids: list[int] | None = None,
    max_batch: int = 50,
) -> dict[str, int]:
    """Walk clientes that still need geocoding and upgrade them.

    Selection:
      * `geocoding_status IN ('pending', 'centroide_fallback')` — centroide rows
        are still eligible because we want the fine-grained Nominatim hit.
      * Optional `empresa_ids` filter (admin endpoint passes a single id).
      * `geocoding_attempts < GEOCODING_MAX_ATTEMPTS` to avoid infinite retries.

    For each cliente:
      1. Increment `geocoding_attempts`.
      2. Call Nominatim (1 req/s globally enforced inside `_geocode_nominatim`).
      3. On success → set lat/lon, `geocoding_status='nominatim_ok'`,
         `geocoded_at = now`, then UPDATE all visitas of this cliente whose
         lat/lon still match the old centroide (so the simulator sees the new
         coords). On failure → set `geocoding_status='centroide_fallback'`
         (keep existing centroide if any) and bump attempts; after MAX_ATTEMPTS
         set `'failed'`.

    Errors are swallowed per-cliente so one bad row doesn't kill the batch.

    Returns counts: `{procesados, ok, fallback, failed}`.
    """
    # Lazy import to avoid circular reference (`app.db.session` imports settings
    # from `app.core.config`, which is fine, but keeping it inside the function
    # also means importing geocoding from a migration script is cheap).
    from app.db.models.cliente import Cliente
    from app.db.models.visita import Visita
    from app.db.session import get_sessionmaker

    report = {"procesados": 0, "ok": 0, "fallback": 0, "failed": 0}

    sm = get_sessionmaker()
    async with sm() as session:
        stmt = (
            select(Cliente)
            .where(
                or_(
                    Cliente.geocoding_status == "pending",
                    Cliente.geocoding_status == "centroide_fallback",
                ),
                Cliente.geocoding_attempts < GEOCODING_MAX_ATTEMPTS,
                Cliente.direccion_default.is_not(None),
            )
            .limit(max_batch)
        )
        if empresa_ids:
            stmt = stmt.where(Cliente.empresa_id.in_(empresa_ids))

        clientes = (await session.execute(stmt)).scalars().all()
        if not clientes:
            return report

        for c in clientes:
            report["procesados"] += 1
            old_lat = c.lat_default
            old_lon = c.lon_default
            c.geocoding_attempts = (c.geocoding_attempts or 0) + 1

            try:
                coords = await _geocode_nominatim(
                    c.direccion_default or "",
                    c.comuna_default or "",
                    c.region_default or "Region Metropolitana",
                )
            except Exception as e:
                logger.warning(
                    f"[geocode] unexpected error cliente_id={c.cliente_id}: {e}"
                )
                coords = None

            if coords is not None:
                new_lat, new_lon = coords
                c.lat_default = new_lat
                c.lon_default = new_lon
                c.geocoding_status = "nominatim_ok"
                c.geocoded_at = datetime.now(UTC)
                report["ok"] += 1

                # Cascade lat/lon to visitas of this cliente that still hold the
                # centroide (or no coords). We DO NOT overwrite visitas that
                # already have a non-matching lat/lon (admin may have edited).
                try:
                    if old_lat is not None and old_lon is not None:
                        # Upgrade only visitas frozen at the old centroide.
                        await session.execute(
                            update(Visita)
                            .where(
                                Visita.cliente_id == c.cliente_id,
                                Visita.lat == old_lat,
                                Visita.lon == old_lon,
                            )
                            .values(lat=new_lat, lon=new_lon)
                        )
                    else:
                        # No previous coords on the cliente → only fill in
                        # visitas that had no coords either.
                        await session.execute(
                            update(Visita)
                            .where(
                                Visita.cliente_id == c.cliente_id,
                                Visita.lat.is_(None),
                            )
                            .values(lat=new_lat, lon=new_lon)
                        )
                except Exception as e:
                    logger.warning(
                        f"[geocode] visitas cascade failed cliente_id={c.cliente_id}: {e}"
                    )
            else:
                # Nominatim couldn't resolve. If we already had a centroide,
                # keep it; otherwise try one now so the row at least has coords.
                if c.lat_default is None and c.comuna_default:
                    cen = centroide_comuna(c.comuna_default)
                    if cen is not None:
                        c.lat_default, c.lon_default = cen

                if c.geocoding_attempts >= GEOCODING_MAX_ATTEMPTS:
                    c.geocoding_status = "failed"
                    report["failed"] += 1
                else:
                    # Stay eligible for the next pass.
                    c.geocoding_status = (
                        "centroide_fallback" if c.lat_default is not None else "pending"
                    )
                    report["fallback"] += 1

            # Commit per cliente. With 1 req/s rate-limit anyway, the overhead
            # of an extra round-trip is irrelevant and we lose less work on
            # restart. Errors here are caught so the batch keeps going.
            try:
                await session.commit()
            except Exception as e:
                logger.warning(
                    f"[geocode] commit failed cliente_id={c.cliente_id}: {e}"
                )
                await session.rollback()

    logger.info(
        f"[geocode] batch done — procesados={report['procesados']} "
        f"ok={report['ok']} fallback={report['fallback']} failed={report['failed']}"
    )
    return report


async def geocode_pending_clientes_loop() -> None:
    """Long-lived task: walk pending clientes forever, sleeping between batches.

    Started from `app/main.py` lifespan after `app.state.db_ready = True`. The
    outer try/except guarantees the loop never dies — any unexpected error
    sleeps GEOCODING_INTERVAL_SECONDS and continues. `asyncio.CancelledError` is
    re-raised so uvicorn can shut down cleanly.
    """
    interval = _env_int("GEOCODING_INTERVAL_SECONDS", 10)
    batch_size = _env_int("GEOCODING_BATCH_SIZE", 50)
    logger.info(
        f"[geocode] background loop started "
        f"(interval={interval}s, batch={batch_size}, max_attempts={GEOCODING_MAX_ATTEMPTS})"
    )

    while True:
        try:
            await geocode_pending_clientes(empresa_ids=None, max_batch=batch_size)
        except asyncio.CancelledError:
            logger.info("[geocode] background loop cancelled — exiting")
            raise
        except Exception as e:
            logger.error(f"[geocode] batch raised; loop continues: {e}")

        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("[geocode] background loop cancelled during sleep — exiting")
            raise
