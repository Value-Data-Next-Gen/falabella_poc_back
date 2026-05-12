"""Buscador global cross-entidad (Sprint 7).

Endpoint:
  GET /api/search?q={query}

Busca el query (LIKE %q% case-insensitive) en 6 categorías y devuelve
hasta 5 hits por categoría:

  - vips:      fpoc_vip_clients.match_value (+ tier en sublabel)
  - empresas:  fpoc_empresas_transporte.nombre
  - contactos: fpoc_empresa_contactos.nombre / email / phone_e164
  - drivers:   fpoc_drivers.driver_id / name / phone_e164
  - visitas:   fpoc_simpli_visits.id (TRK / numérico) o title
  - motivos:   MOTIVOS_CATALOGO (matcheo en memoria) + alert_config

Scope:
  - falabella_admin / falabella_ops: ven todo
  - transport_manager: scope a su empresa_id (no ve VIPs/empresas/drivers
    de otras empresas; sí ve catálogo motivos global)
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from auth import CurrentUser, current_user
from db import get_conn

router = APIRouter(tags=["search"])

MAX_PER_CATEGORY = 5


# =============================================================================
# Modelos
# =============================================================================
class SearchHit(BaseModel):
    kind: str          # 'vip' | 'empresa' | 'contacto' | 'driver' | 'visita' | 'motivo'
    id: str            # id estable como string (vip_id, empresa_id, contact_id, driver_id, tracking_id, motivo)
    label: str         # texto principal
    sublabel: Optional[str] = None  # contexto (empresa, severity, etc.)
    # Hints para navegación frontend (opcionales)
    empresa_id: Optional[int] = None
    tracking_id: Optional[str] = None


class SearchResults(BaseModel):
    vips: list[SearchHit]
    empresas: list[SearchHit]
    contactos: list[SearchHit]
    drivers: list[SearchHit]
    visitas: list[SearchHit]
    motivos: list[SearchHit]


# =============================================================================
# Helpers SQL
# =============================================================================
def _like(q: str) -> str:
    """Normaliza el query para LIKE: escapa _ y % y agrega comodines."""
    safe = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{safe}%"


# =============================================================================
# Búsquedas por categoría
# =============================================================================
def _search_vips(cn, q: str, user: CurrentUser) -> list[SearchHit]:
    like = _like(q)
    if user.is_falabella:
        sql = (
            "SELECT vip_id, match_type, match_value, empresa_id, tier, active "
            "FROM fpoc_vip_clients "
            "WHERE active = 1 AND match_value LIKE ? ESCAPE '\\' "
            "ORDER BY tier, match_value LIMIT ?"
        )
        params = [like, MAX_PER_CATEGORY]
    else:
        # Transport manager ve VIPs globales (empresa_id NULL) + de su empresa
        sql = (
            "SELECT vip_id, match_type, match_value, empresa_id, tier, active "
            "FROM fpoc_vip_clients "
            "WHERE active = 1 AND match_value LIKE ? ESCAPE '\\' "
            "  AND (empresa_id IS NULL OR empresa_id = ?) "
            "ORDER BY tier, match_value LIMIT ?"
        )
        params = [like, user.empresa_id, MAX_PER_CATEGORY]
    cur = cn.cursor().execute(sql, *params)
    out: list[SearchHit] = []
    for r in cur.fetchall():
        sub = f"{r.match_type} · {r.tier}"
        out.append(SearchHit(
            kind="vip",
            id=str(r.vip_id),
            label=str(r.match_value),
            sublabel=sub,
            empresa_id=int(r.empresa_id) if r.empresa_id is not None else None,
        ))
    return out


def _search_empresas(cn, q: str, user: CurrentUser) -> list[SearchHit]:
    if not user.is_falabella:
        # Transport manager solo "ve" su propia empresa
        cur = cn.cursor().execute(
            "SELECT empresa_id, nombre, activo FROM fpoc_empresas_transporte "
            "WHERE empresa_id = ? AND nombre LIKE ? ESCAPE '\\' LIMIT ?",
            user.empresa_id, _like(q), MAX_PER_CATEGORY,
        )
    else:
        cur = cn.cursor().execute(
            "SELECT empresa_id, nombre, activo FROM fpoc_empresas_transporte "
            "WHERE nombre LIKE ? ESCAPE '\\' "
            "ORDER BY nombre LIMIT ?",
            _like(q), MAX_PER_CATEGORY,
        )
    out: list[SearchHit] = []
    for r in cur.fetchall():
        sub = "Activa" if int(r.activo) == 1 else "Inactiva"
        out.append(SearchHit(
            kind="empresa",
            id=str(r.empresa_id),
            label=str(r.nombre),
            sublabel=sub,
            empresa_id=int(r.empresa_id),
        ))
    return out


def _search_contactos(cn, q: str, user: CurrentUser) -> list[SearchHit]:
    like = _like(q)
    if user.is_falabella:
        sql = (
            "SELECT c.contact_id, c.empresa_id, c.nombre, c.rol, c.phone_e164, c.email, "
            "       e.nombre AS empresa_nombre "
            "FROM fpoc_empresa_contactos c "
            "LEFT JOIN fpoc_empresas_transporte e ON e.empresa_id = c.empresa_id "
            "WHERE c.active = 1 AND ("
            "      c.nombre LIKE ? ESCAPE '\\' "
            "   OR c.email LIKE ? ESCAPE '\\' "
            "   OR c.phone_e164 LIKE ? ESCAPE '\\' "
            ") ORDER BY c.nombre LIMIT ?"
        )
        params = [like, like, like, MAX_PER_CATEGORY]
    else:
        sql = (
            "SELECT c.contact_id, c.empresa_id, c.nombre, c.rol, c.phone_e164, c.email, "
            "       e.nombre AS empresa_nombre "
            "FROM fpoc_empresa_contactos c "
            "LEFT JOIN fpoc_empresas_transporte e ON e.empresa_id = c.empresa_id "
            "WHERE c.active = 1 AND c.empresa_id = ? AND ("
            "      c.nombre LIKE ? ESCAPE '\\' "
            "   OR c.email LIKE ? ESCAPE '\\' "
            "   OR c.phone_e164 LIKE ? ESCAPE '\\' "
            ") ORDER BY c.nombre LIMIT ?"
        )
        params = [user.empresa_id, like, like, like, MAX_PER_CATEGORY]
    cur = cn.cursor().execute(sql, *params)
    out: list[SearchHit] = []
    for r in cur.fetchall():
        emp = r.empresa_nombre or f"Empresa {r.empresa_id}"
        sub_parts = [str(emp), str(r.rol)]
        if r.phone_e164:
            sub_parts.append(str(r.phone_e164))
        out.append(SearchHit(
            kind="contacto",
            id=str(r.contact_id),
            label=str(r.nombre),
            sublabel=" · ".join(sub_parts),
            empresa_id=int(r.empresa_id) if r.empresa_id is not None else None,
        ))
    return out


def _search_drivers(cn, q: str, user: CurrentUser) -> list[SearchHit]:
    """Drivers no tienen empresa_id directo en la tabla; el scope se hace por
    vehicle_id (un driver = un vehicle a través de fpoc_drivers.vehicle_id).
    Para transport_manager filtramos por la lista de vehicle_ids de su empresa,
    cargada via STATE."""
    like = _like(q)
    base = (
        "SELECT driver_id, name, phone, phone_e164, vehicle_id, vehicle_name, active "
        "FROM fpoc_drivers "
        "WHERE active = 1 AND ("
        "      driver_id LIKE ? ESCAPE '\\' "
        "   OR name LIKE ? ESCAPE '\\' "
        "   OR phone LIKE ? ESCAPE '\\' "
        "   OR phone_e164 LIKE ? ESCAPE '\\' "
        ")"
    )
    params: list = [like, like, like, like]

    if not user.is_falabella:
        base += " AND empresa_id = ?"
        params.append(user.empresa_id)

    base += " ORDER BY name LIMIT ?"
    params.append(MAX_PER_CATEGORY)

    cur = cn.cursor().execute(base, *params)
    out: list[SearchHit] = []
    for r in cur.fetchall():
        sub_parts = [str(r.vehicle_name)]
        if r.phone_e164:
            sub_parts.append(str(r.phone_e164))
        elif r.phone:
            sub_parts.append(str(r.phone))
        out.append(SearchHit(
            kind="driver",
            id=str(r.driver_id),
            label=str(r.name),
            sublabel=" · ".join(sub_parts),
        ))
    return out


def _search_visitas(cn, q: str, user: CurrentUser) -> list[SearchHit]:
    """Busca visitas en fpoc_simpli_visits.

    La columna `id` es entero. Si `q` es numérico, hacemos match exacto.
    Si q empieza con TRK seguido de dígitos, intentamos extraer el número.
    Además matcheamos en `title` (cliente)."""
    q_strip = q.strip()
    q_num: Optional[int] = None
    # Caso "TRK1234" → int(1234) si todo el resto es dígito
    if q_strip.upper().startswith("TRK"):
        rest = q_strip[3:]
        if rest.isdigit():
            try:
                q_num = int(rest)
            except ValueError:
                q_num = None
    elif q_strip.isdigit():
        try:
            q_num = int(q_strip)
        except ValueError:
            q_num = None

    like = _like(q)
    where_parts = ["title LIKE ? ESCAPE '\\'"]
    params: list = [like]
    if q_num is not None:
        where_parts.append("id = ?")
        params.append(q_num)

    where_sql = " OR ".join(where_parts)

    if user.is_falabella:
        sql = (
            "SELECT id, title, status, planned_date, empresa_falsa AS empresa_id, "
            "       e.nombre AS empresa_nombre "
            "FROM fpoc_simpli_visits v "
            "LEFT JOIN fpoc_empresas_transporte e ON e.empresa_id = v.empresa_falsa "
            f"WHERE ({where_sql}) "
            "ORDER BY planned_date DESC LIMIT ?"
        )
        params.append(MAX_PER_CATEGORY)
    else:
        sql = (
            "SELECT id, title, status, planned_date, empresa_falsa AS empresa_id, "
            "       e.nombre AS empresa_nombre "
            "FROM fpoc_simpli_visits v "
            "LEFT JOIN fpoc_empresas_transporte e ON e.empresa_id = v.empresa_falsa "
            f"WHERE empresa_falsa = ? AND ({where_sql}) "
            "ORDER BY planned_date DESC LIMIT ?"
        )
        params = [user.empresa_id] + params + [MAX_PER_CATEGORY]

    cur = cn.cursor().execute(sql, *params)
    out: list[SearchHit] = []
    for r in cur.fetchall():
        emp = r.empresa_nombre or f"Empresa {r.empresa_id}"
        sub = f"{emp} · {r.status}"
        out.append(SearchHit(
            kind="visita",
            id=str(r.id),
            label=str(r.title),
            sublabel=sub,
            empresa_id=int(r.empresa_id) if r.empresa_id is not None else None,
            tracking_id=str(r.id),
        ))
    return out


def _search_motivos(cn, q: str, user: CurrentUser) -> list[SearchHit]:
    """Matcheo en memoria sobre MOTIVOS_CATALOGO + lookup alert_config."""
    try:
        from comments import MOTIVOS_CATALOGO
    except Exception:
        return []

    qu = q.strip().upper()
    if not qu:
        return []

    matches = [m for m in MOTIVOS_CATALOGO if qu in m.upper()]
    matches = matches[:MAX_PER_CATEGORY]
    if not matches:
        return []

    # Lookup alert_config global (empresa_id IS NULL) para context
    cfg: dict[str, tuple[bool, str]] = {}
    try:
        cur = cn.cursor().execute(
            "SELECT motivo, alertable, severity FROM fpoc_motivo_alert_config "
            "WHERE empresa_id IS NULL"
        )
        for r in cur.fetchall():
            cfg[str(r.motivo)] = (bool(int(r.alertable)), str(r.severity))
    except Exception:
        cfg = {}

    out: list[SearchHit] = []
    for m in matches:
        alertable, sev = cfg.get(m, (False, "medium"))
        sub = f"{'alertable' if alertable else 'no alerta'} · {sev}"
        out.append(SearchHit(
            kind="motivo",
            id=m,
            label=m,
            sublabel=sub,
        ))
    return out


# =============================================================================
# Endpoint principal
# =============================================================================
@router.get("/api/search", response_model=SearchResults)
def search_global(
    q: str = Query(..., min_length=2, max_length=80),
    user: CurrentUser = Depends(current_user),
) -> SearchResults:
    """Busca q en VIPs, empresas, contactos, drivers, visitas y motivos.
    Devuelve hasta 5 resultados por categoría."""
    q_clean = q.strip()
    if len(q_clean) < 2:
        return SearchResults(
            vips=[], empresas=[], contactos=[], drivers=[], visitas=[], motivos=[],
        )

    with get_conn() as cn:
        return SearchResults(
            vips=_search_vips(cn, q_clean, user),
            empresas=_search_empresas(cn, q_clean, user),
            contactos=_search_contactos(cn, q_clean, user),
            drivers=_search_drivers(cn, q_clean, user),
            visitas=_search_visitas(cn, q_clean, user),
            motivos=_search_motivos(cn, q_clean, user),
        )
