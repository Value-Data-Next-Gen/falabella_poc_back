"""Comentarios del transportista + configuración de motivos alertables.

Helpers de región para filtrar destinatarios WhatsApp por geografía:
  _visit_region(lat, lon) -> 'RM' | 'regiones'


El catálogo de motivos viene del notebook `auditoria_llm_directo.ipynb` (celda
`REGLAS_OPERACIONALES`). El classifier LLM vive en `motivo_classifier.py` y
expone `POST /api/motivos/classify`.

Endpoints:
  GET  /api/motivos                              -> catálogo fijo
  GET  /api/motivos/alert-config                 -> config (scope empresa, fallback global)
  PUT  /api/motivos/alert-config/{motivo}        -> admin/ops actualiza alertable+severity
  POST /api/visits/{tracking_id}/comment         -> transportista reporta motivo+texto
  GET  /api/visits/{tracking_id}/comments        -> historial de la visita
  GET  /api/comments/recent?limit=50             -> últimos comentarios (scope empresa)
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, Field

from core.auth import CurrentUser, current_user
from core.db import get_conn
from core.events import EVENTS
from core.state import STATE


router = APIRouter(tags=["comments"])


# Catálogo alineado con el del cliente (Excel "Motivo no entrega HD", 11 motivos)
# + 3 motivos internos extra: NO DESPACHA A LOCALIDAD, RIESGO FRAUDE, DETENCION URGENTE.
MOTIVOS_CATALOGO: list[str] = [
    "SIN MORADORES",
    "NO CONOCEN A CLIENTE",
    "PROBLEMA DE DIRECCIÓN/ SIN INFORMACIÓN",
    "NO DESPACHA A LOCALIDAD",
    "FUERA DE COBERTURA/ FRECUENCIA",
    "PROD NO ENTREGADO POR TIEMPO",
    "PRODUCTO NO CARGADO",
    "CLIENTE RECHAZA",
    "SINIESTRO EN CALLE",
    "PRODUCTO CON PROBLEMAS",
    "NO CUMPLE CONDICIONES RETIRO",
    "PRODUCTO ROBADO",
    "RIESGO FRAUDE",
    "DETENCION URGENTE",
]

# Defaults: mapping motivo -> (alertable, severity)
DEFAULT_ALERT_CONFIG: dict[str, tuple[bool, str]] = {
    "SINIESTRO EN CALLE": (True, "critical"),
    "PRODUCTO ROBADO": (True, "critical"),
    "PRODUCTO NO CARGADO": (True, "high"),
    "PROBLEMA DE DIRECCIÓN/ SIN INFORMACIÓN": (True, "medium"),
    "RIESGO FRAUDE": (True, "critical"),
    "DETENCION URGENTE": (True, "high"),
}

# Descripciones default por motivo. Texto del catálogo del cliente (Excel
# "Motivo no entrega HD") + reglas internas de desambiguación "NO usar si..."
# que ayudan al LLM a no confundir motivos similares.
# Los 3 motivos extra internos (NO DESPACHA, RIESGO FRAUDE, DETENCION URGENTE)
# mantienen su descripción interna porque no están en el catálogo del cliente.
DEFAULT_DESCRIPTIONS: dict[str, str] = {
    "SIN MORADORES": (
        "Al llegar a la dirección de entrega, el conductor no encuentra a ninguna persona en el lugar "
        "que pueda recibir el paquete. Esto puede suceder cuando el cliente está ausente, si la dirección "
        "corresponde a un inmueble deshabitado, o cliente reagenda o cliente pide cambiar fecha de entrega.\n"
        "NO usar si: el problema es la dirección -> PROBLEMA DE DIRECCIÓN/ SIN INFORMACIÓN.\n"
        "NO usar si: el cliente atendió pero rechazó -> CLIENTE RECHAZA.\n"
        "NO usar si: vecinos atienden pero no conocen al destinatario -> NO CONOCEN A CLIENTE."
    ),
    "NO CONOCEN A CLIENTE": (
        "Las personas en la dirección indicada afirman no conocer al destinatario o que no corresponde "
        "a su dirección.\n"
        "NO usar si: realmente no había nadie -> SIN MORADORES.\n"
        "NO usar si: la dirección es incorrecta o incompleta -> PROBLEMA DE DIRECCIÓN/ SIN INFORMACIÓN."
    ),
    "PROBLEMA DE DIRECCIÓN/ SIN INFORMACIÓN": (
        "La dirección proporcionada es incorrecta, está incompleta o es imposible de localizar. "
        "Puede que falte información crucial como el número de casa o apartamento, o que la dirección "
        "no corresponda a una ubicación válida.\n"
        "NO usar si: la zona no es atendida -> NO DESPACHA A LOCALIDAD o FUERA DE COBERTURA/ FRECUENCIA.\n"
        "NO usar si: la dirección está bien pero no había nadie -> SIN MORADORES."
    ),
    "NO DESPACHA A LOCALIDAD": (
        "La dirección del cliente está fuera de la zona geográfica que la empresa atiende. "
        "Aun cuando el cliente anula al darse cuenta, la causa raíz sigue siendo no-despacho."
    ),
    "FUERA DE COBERTURA/ FRECUENCIA": (
        "La dirección de entrega está en un área fuera del alcance de la cobertura del servicio, "
        "o en una zona que no se visita con la frecuencia necesaria para cumplir con la entrega."
    ),
    "PROD NO ENTREGADO POR TIEMPO": (
        "El paquete no se pudo entregar dentro del tiempo límite estipulado por el cliente o la empresa. "
        "Esto puede suceder por tráfico, demoras en rutas previas o cualquier otro contratiempo.\n"
        "NO usar si: hubo siniestro -> SINIESTRO EN CALLE.\n"
        "NO usar si: el producto nunca subió al camión -> PRODUCTO NO CARGADO."
    ),
    "PRODUCTO NO CARGADO": (
        "El paquete destinado para la entrega no fue cargado en el vehículo desde el origen, "
        "por lo que no puede ser entregado."
    ),
    "CLIENTE RECHAZA": (
        "El cliente rechaza recibir el paquete. Esto puede deberse a que ya no lo necesita, cambió "
        "de opinión, recibió el pedido incorrecto, o cualquier otra razón personal.\n"
        "ATENCIÓN: si el cliente anula porque la dirección estaba mal, la causa raíz NO es rechazo "
        "-> es PROBLEMA DE DIRECCIÓN/ SIN INFORMACIÓN o NO DESPACHA A LOCALIDAD."
    ),
    "SINIESTRO EN CALLE": (
        "Un incidente en la vía pública, como un accidente, manifestación, cierre de calles o "
        "condiciones climáticas adversas, impide que el conductor llegue al destino de entrega."
    ),
    "PRODUCTO CON PROBLEMAS": (
        "El producto presenta defectos, daños o cualquier problema que impide su entrega en "
        "condiciones adecuadas."
    ),
    "NO CUMPLE CONDICIONES RETIRO": (
        "Las condiciones en el lugar de entrega o las condiciones del producto no son adecuadas "
        "para realizar la entrega o el retiro. Esto puede incluir problemas como falta de espacio, "
        "restricciones de acceso, o situaciones donde el producto no puede ser retirado porque está en uso."
    ),
    "PRODUCTO ROBADO": (
        "El paquete fue robado durante el proceso de entrega, ya sea del vehículo de transporte "
        "o en otro momento del trayecto."
    ),
    "RIESGO FRAUDE": (
        "Pedido sospechoso de fraude (datos del cliente, RUT clonado, dirección de phishing, "
        "intento de estafa, comportamiento inusual). NO entregar. Reportar inmediatamente.\n"
        "NO usar si: el cliente solo rechaza por arrepentimiento -> CLIENTE RECHAZA.\n"
        "NO usar si: hay siniestro físico -> SINIESTRO EN CALLE."
    ),
    "DETENCION URGENTE": (
        "Detención inmediata de la entrega ordenada por Falabella o transporte (alerta judicial, "
        "cancelación administrativa post-pickup, retención por aduana, etc.). NO entregar y "
        "devolver al CD.\n"
        "NO usar si: el cliente rechaza personalmente -> CLIENTE RECHAZA."
    ),
}

ALLOWED_SEVERITY = {"low", "medium", "high", "critical"}


# =============================================================================
# Helper geográfico: clasifica una visita como RM o regiones según lat/lon.
# Bounding box aproximado para Santiago / Región Metropolitana basado en DEPOT.
# =============================================================================
_RM_LAT_MIN, _RM_LAT_MAX = -33.7, -33.2
_RM_LON_MIN, _RM_LON_MAX = -70.9, -70.4


def _visit_region(
    lat: Optional[float],
    lon: Optional[float],
    region_col: Optional[str] = None,
) -> str:
    """Devuelve 'RM' o 'regiones'.

    Sprint 6: si `region_col` viene del dataset (columna `fpoc_simpli_visits.region`),
    preferimos ese valor sobre el cálculo lat/lon. Devolvemos 'RM' si region_col == 'RM',
    'regiones' si tiene cualquier otro valor no-vacío, o caemos al bbox lat/lon.
    """
    if region_col is not None and isinstance(region_col, str) and region_col.strip():
        rc = region_col.strip()
        return "RM" if rc.upper() == "RM" else "regiones"
    if lat is None or lon is None:
        return "regiones"
    try:
        latf = float(lat); lonf = float(lon)
    except (TypeError, ValueError):
        return "regiones"
    if latf == 0 and lonf == 0:
        return "regiones"
    if _RM_LAT_MIN <= latf <= _RM_LAT_MAX and _RM_LON_MIN <= lonf <= _RM_LON_MAX:
        return "RM"
    return "regiones"


# =============================================================================
# Schemas
# =============================================================================
class MotivoOut(BaseModel):
    motivo: str
    default_alertable: bool
    default_severity: str


class MotivoAlertConfig(BaseModel):
    motivo: str
    empresa_id: Optional[int] = None
    alertable: bool
    severity: str
    description: str                                # texto resuelto (custom o default)
    description_is_custom: bool = False             # true si proviene de la DB (no del default)
    default_description: str                        # default catálogo (para mostrar "restaurar")
    is_default: bool = False                        # toda la fila viene de default catálogo
    updated_at: Optional[str] = None
    updated_by: Optional[int] = None


class MotivoAlertConfigUpdate(BaseModel):
    alertable: bool
    severity: str = Field(pattern="^(low|medium|high|critical)$")
    empresa_id: Optional[int] = None  # admin puede setear por empresa o global
    # null = restaurar al default del catálogo; "" = vaciar (no recomendado)
    description: Optional[str] = Field(default=None, max_length=4000)
    reset_description: bool = False  # true -> NULL en DB (vuelve al default)


class CommentCreate(BaseModel):
    motivo: str = Field(min_length=1, max_length=80)
    comentario: str = Field(min_length=1, max_length=2000)


class VisitComment(BaseModel):
    comment_id: int
    tracking_id: str
    vehicle_id: Optional[int] = None
    empresa_id: Optional[int] = None
    motivo: str
    comentario: str
    created_by: Optional[int] = None
    created_by_name: Optional[str] = None
    created_at: str
    alertable: bool = False
    severity: Optional[str] = None


# =============================================================================
# Helpers
# =============================================================================
def _resolve_alert_config(motivo: str, empresa_id: Optional[int]) -> tuple[bool, str]:
    """Devuelve (alertable, severity) para un motivo+empresa.
    Prioridad: row específico empresa > row global (empresa_id NULL) > default catálogo."""
    with get_conn() as cn:
        cur = cn.cursor()
        if empresa_id is not None:
            cur.execute(
                "SELECT alertable, severity FROM fpoc.motivo_alert_config "
                "WHERE motivo = ? AND empresa_id = ?",
                motivo, empresa_id,
            )
            r = cur.fetchone()
            if r is not None:
                return bool(r.alertable), str(r.severity)
        cur.execute(
            "SELECT alertable, severity FROM fpoc.motivo_alert_config "
            "WHERE motivo = ? AND empresa_id IS NULL",
            motivo,
        )
        r = cur.fetchone()
        if r is not None:
            return bool(r.alertable), str(r.severity)
    da, ds = DEFAULT_ALERT_CONFIG.get(motivo, (False, "medium"))
    return da, ds


def _default_description(motivo: str) -> str:
    return DEFAULT_DESCRIPTIONS.get(motivo, "")


def _resolve_description(motivo: str, empresa_id: Optional[int]) -> tuple[str, bool]:
    """Devuelve (description, is_custom). Prioridad: empresa > global > default catálogo."""
    with get_conn() as cn:
        cur = cn.cursor()
        if empresa_id is not None:
            cur.execute(
                "SELECT description FROM fpoc.motivo_alert_config "
                "WHERE motivo = ? AND empresa_id = ?",
                motivo, empresa_id,
            )
            r = cur.fetchone()
            if r is not None and r.description:
                return str(r.description), True
        cur.execute(
            "SELECT description FROM fpoc.motivo_alert_config "
            "WHERE motivo = ? AND empresa_id IS NULL",
            motivo,
        )
        r = cur.fetchone()
        if r is not None and r.description:
            return str(r.description), True
    return _default_description(motivo), False


def _visit_meta(tracking_id: str) -> Optional[dict]:
    """Devuelve toda la info disponible de la visita: vehículo, driver, ruta, ETA, etc.
    Reúne datos del snapshot + maestros (drivers / vehicles_ext) + empresa.

    Si el tracking_id no está en el snapshot ML (sintético) cae a buscar en
    fpoc.simpli_visits (visitas reales del XLSX SimpliRoute)."""
    row_data: Optional[dict] = None
    if STATE.snapshot_df is not None:
        df = STATE.snapshot_df
        matching = df[df["tracking_id"] == tracking_id]
        if not matching.empty:
            row_data = matching.iloc[0].to_dict()

    if row_data is None:
        # Fallback DB: buscar por id (BIGINT del XLSX SimpliRoute)
        try:
            with get_conn() as cn:
                cur = cn.cursor()
                cur.execute(
                    "SELECT id, title, reference, ct, ruta_id, driver_name, "
                    "empresa_falsa, patente_falsa, planned_date, current_eta_cl, "
                    "address, comuna, region "
                    "FROM fpoc.simpli_visits WHERE id = ?",
                    tracking_id,
                )
                r = cur.fetchone()
            if r is None:
                return None
            # Mapeo a la shape esperada (con campos mínimos; algunos quedan None)
            row_data = {
                "tracking_id": str(r.id),
                "vehicle_id": int(r.patente_falsa) if r.patente_falsa is not None else None,
                "title": r.title or "",
                "reference": r.reference,
                "customer_id": None,
                "ct": r.ct,
                "ruta_id": r.ruta_id,
                "address": r.address,
                "comuna": r.comuna,
                "region": r.region,
                "planned_date": str(r.planned_date) if r.planned_date else None,
                "current_eta_cl": str(r.current_eta_cl) if r.current_eta_cl else None,
            }
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[_visit_meta] fallback DB falló: {e}")
            return None

    row = row_data  # nombre estable
    if row.get("vehicle_id") is None:
        return None
    vehicle_id = int(row["vehicle_id"])

    # Driver / vehículo extendido
    driver = None
    vehicle_ext = None
    for d in STATE.drivers:
        if int(d["vehicle_id"]) == vehicle_id:
            driver = d
            break
    for v in STATE.vehicles_ext:
        if int(v["vehicle_id"]) == vehicle_id:
            vehicle_ext = v
            break

    # Empresa
    empresa_id = STATE.vehicle_empresa_map.get(vehicle_id)
    empresa_nombre = None
    if empresa_id is not None:
        for e in STATE.empresas:
            if int(e["empresa_id"]) == empresa_id:
                empresa_nombre = e["nombre"]
                break

    # Campos enriquecidos opcionales (Sprint 3): suborden, CT, folios.
    # En el snapshot sintético sólo `reference` está disponible; lo exponemos como
    # folio principal. `suborden` y `ct` se intentan resolver desde fpoc_simpli_visits
    # / fpoc_geo_suborders matcheando por reference si existe en datos reales.
    reference = str(row.get("reference", "")) if row.get("reference") is not None else ""
    customer_id = str(row.get("customer_id", "")) if row.get("customer_id") is not None else ""

    suborden: Optional[str] = None
    ct: Optional[str] = None
    folios: list[str] = [reference] if reference else []
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            # Buscar CT por reference en fpoc_simpli_visits (datos reales)
            if reference:
                cur.execute(
                    "SELECT ct FROM fpoc.simpli_visits WHERE CAST(reference AS TEXT) = ? LIMIT 1",
                    reference.replace("FAL-", ""),
                )
                r = cur.fetchone()
                if r and r.ct:
                    ct = str(r.ct)
            # Buscar suborden por idruta (no hay match real con sintético, queda None)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[_visit_meta] enrichment skipped: {e}")

    return {
        "vehicle_id": vehicle_id,
        "vehicle_name": str(row.get("vehicle_name") or row.get("title") or ""),
        "title": str(row.get("title") or ""),
        "address": str(row.get("address", "") or ""),
        "order": int(row.get("order", 0)),
        "window_start": str(row.get("window_start", "")),
        "window_end": str(row.get("window_end", "")),
        "estimated_time_arrival": str(row.get("estimated_time_arrival", "")),
        "slack_min": float(row.get("slack_min", 0.0)),
        "p_fallo": float(row.get("p_fallo", 0.0)),
        "alert_slack": str(row.get("alert_slack", "")),
        "latitude": float(row.get("latitude", 0.0)),
        "longitude": float(row.get("longitude", 0.0)),
        # Maestros
        "plate": (vehicle_ext or {}).get("plate"),
        "vehicle_type": (vehicle_ext or {}).get("type"),
        "vehicle_year": (vehicle_ext or {}).get("year"),
        "capacity_m3": (vehicle_ext or {}).get("capacity_m3"),
        "driver_id": (driver or {}).get("driver_id"),
        "driver_name": (driver or {}).get("name") or (vehicle_ext or {}).get("driver_name"),
        "driver_phone": (driver or {}).get("phone"),
        "driver_rating": (driver or {}).get("rating"),
        # Empresa
        "empresa_id": empresa_id,
        "empresa_nombre": empresa_nombre,
        # Sprint 3: enriquecimiento operativo
        "reference": reference or None,
        "customer_id": customer_id or None,
        "suborden": suborden,
        "ct": ct,
        "folios": folios,
    }


# =============================================================================
# Catálogo de motivos
# =============================================================================
@router.get("/api/motivos", response_model=list[MotivoOut])
def list_motivos(_: CurrentUser = Depends(current_user)) -> list[MotivoOut]:
    out: list[MotivoOut] = []
    for m in MOTIVOS_CATALOGO:
        da, ds = DEFAULT_ALERT_CONFIG.get(m, (False, "medium"))
        out.append(MotivoOut(motivo=m, default_alertable=da, default_severity=ds))
    return out


# =============================================================================
# Configuración de alertas por motivo
# =============================================================================
@router.get("/api/motivos/alert-config", response_model=list[MotivoAlertConfig])
def get_alert_config(
    empresa_id: Optional[int] = Query(default=None),
    user: CurrentUser = Depends(current_user),
) -> list[MotivoAlertConfig]:
    """Devuelve la config efectiva por motivo. Si el user no es falabella,
    se fuerza empresa_id = user.empresa_id."""
    if not user.is_falabella:
        empresa_id = user.empresa_id

    out: list[MotivoAlertConfig] = []
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """
            SELECT motivo, empresa_id, alertable, severity, description, updated_at, updated_by
            FROM fpoc.motivo_alert_config
            WHERE empresa_id IS NULL OR empresa_id = ?
            """,
            empresa_id,
        )
        rows = cur.fetchall()

    by_motivo_empresa: dict[str, dict] = {}
    by_motivo_global: dict[str, dict] = {}
    for r in rows:
        d = {
            "motivo": r.motivo,
            "empresa_id": int(r.empresa_id) if r.empresa_id is not None else None,
            "alertable": bool(r.alertable),
            "severity": str(r.severity),
            "description_db": (str(r.description) if r.description else None),
            "updated_at": r.updated_at.isoformat() if hasattr(r.updated_at, "isoformat") else (r.updated_at or None),
            "updated_by": int(r.updated_by) if r.updated_by is not None else None,
        }
        if r.empresa_id is None:
            by_motivo_global[r.motivo] = d
        else:
            by_motivo_empresa[r.motivo] = d

    for motivo in MOTIVOS_CATALOGO:
        default_desc = _default_description(motivo)
        if empresa_id is not None and motivo in by_motivo_empresa:
            d = by_motivo_empresa[motivo]
            desc_db = d.pop("description_db")
            out.append(MotivoAlertConfig(
                is_default=False,
                description=desc_db or default_desc,
                description_is_custom=bool(desc_db),
                default_description=default_desc,
                **d,
            ))
        elif motivo in by_motivo_global:
            d = by_motivo_global[motivo]
            desc_db = d.pop("description_db")
            out.append(MotivoAlertConfig(
                is_default=False,
                description=desc_db or default_desc,
                description_is_custom=bool(desc_db),
                default_description=default_desc,
                **d,
            ))
        else:
            da, ds = DEFAULT_ALERT_CONFIG.get(motivo, (False, "medium"))
            out.append(MotivoAlertConfig(
                motivo=motivo, empresa_id=empresa_id,
                alertable=da, severity=ds,
                description=default_desc,
                description_is_custom=False,
                default_description=default_desc,
                is_default=True,
            ))
    return out


@router.put("/api/motivos/alert-config/{motivo}", response_model=MotivoAlertConfig)
def update_alert_config(
    motivo: str,
    req: MotivoAlertConfigUpdate,
    user: CurrentUser = Depends(current_user),
) -> MotivoAlertConfig:
    if motivo not in MOTIVOS_CATALOGO:
        raise HTTPException(404, f"motivo desconocido: {motivo!r}")
    if req.severity not in ALLOWED_SEVERITY:
        raise HTTPException(400, f"severity inválida: {req.severity!r}")

    # Solo falabella_admin/ops pueden tocar la config.
    if not user.is_falabella:
        raise HTTPException(403, "solo falabella_admin/ops pueden configurar alertas")

    target_empresa = req.empresa_id  # None = global

    # Resolvemos qué descripción guardar.
    # - reset_description=True -> NULL (vuelve al default catálogo)
    # - description provista (no vacía) -> guarda ese texto
    # - description omitida (None y reset_description=False) -> conserva la actual de DB
    with get_conn() as cn:
        cur = cn.cursor()
        # Leemos descripción previa para preservarla si no se provee otra
        if target_empresa is None:
            cur.execute(
                "SELECT description FROM fpoc.motivo_alert_config WHERE motivo = ? AND empresa_id IS NULL",
                motivo,
            )
        else:
            cur.execute(
                "SELECT description FROM fpoc.motivo_alert_config WHERE motivo = ? AND empresa_id = ?",
                motivo, target_empresa,
            )
        prev = cur.fetchone()
        prev_desc = (prev.description if prev and prev.description else None)

        if req.reset_description:
            new_desc = None
        elif req.description is not None and req.description.strip() != "":
            new_desc = req.description.strip()
        else:
            new_desc = prev_desc

        # UPSERT
        if target_empresa is None:
            cur.execute(
                "DELETE FROM fpoc.motivo_alert_config WHERE motivo = ? AND empresa_id IS NULL",
                motivo,
            )
        else:
            cur.execute(
                "DELETE FROM fpoc.motivo_alert_config WHERE motivo = ? AND empresa_id = ?",
                motivo, target_empresa,
            )
        cur.execute(
            """
            INSERT INTO fpoc.motivo_alert_config
              (motivo, empresa_id, alertable, severity, description, updated_by)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            motivo, target_empresa,
            1 if req.alertable else 0, req.severity, new_desc, user.user_id,
        )
        cn.commit()

        if target_empresa is None:
            cur.execute(
                "SELECT motivo, empresa_id, alertable, severity, description, updated_at, updated_by "
                "FROM fpoc.motivo_alert_config WHERE motivo = ? AND empresa_id IS NULL",
                motivo,
            )
        else:
            cur.execute(
                "SELECT motivo, empresa_id, alertable, severity, description, updated_at, updated_by "
                "FROM fpoc.motivo_alert_config WHERE motivo = ? AND empresa_id = ?",
                motivo, target_empresa,
            )
        r = cur.fetchone()

    default_desc = _default_description(motivo)
    desc_db = str(r.description) if r.description else None
    return MotivoAlertConfig(
        motivo=r.motivo,
        empresa_id=int(r.empresa_id) if r.empresa_id is not None else None,
        alertable=bool(r.alertable),
        severity=str(r.severity),
        description=desc_db or default_desc,
        description_is_custom=bool(desc_db),
        default_description=default_desc,
        is_default=False,
        updated_at=r.updated_at.isoformat() if hasattr(r.updated_at, "isoformat") else (r.updated_at or None),
        updated_by=int(r.updated_by) if r.updated_by is not None else None,
    )


# =============================================================================
# Comentarios del transportista
# =============================================================================
def _build_alert_whatsapp_body(
    severity: str,
    motivo: str,
    comentario: str,
    user_display_name: str,
    tracking_id: str,
    meta: dict,
) -> str:
    """Arma el body del WhatsApp con la mayor cantidad de contexto operativo
    posible (vehículo, patente, conductor, ETA, slack, ubicación)."""
    sev_emoji = {
        "critical": "🚨",
        "high": "⚠️",
        "medium": "🔔",
        "low": "ℹ️",
    }.get(severity, "🔔")

    lines: list[str] = []
    lines.append(f"{sev_emoji} *ALERTA {severity.upper()}* — Falabella ValueData")
    lines.append(f"*Motivo:* {motivo}")
    lines.append("")

    # Vehículo + driver
    veh = meta.get("vehicle_name") or "—"
    plate = meta.get("plate")
    vtype = meta.get("vehicle_type")
    vehicle_line = f"*Vehículo:* {veh}"
    if plate:
        vehicle_line += f"  ·  *Patente:* {plate}"
    if vtype:
        cap = meta.get("capacity_m3")
        cap_str = f" {cap}m³" if cap else ""
        vehicle_line += f"  ({vtype}{cap_str})"
    lines.append(vehicle_line)

    driver_name = meta.get("driver_name")
    if driver_name:
        driver_line = f"*Conductor:* {driver_name}"
        rating = meta.get("driver_rating")
        if rating is not None:
            driver_line += f" (★ {rating})"
        phone = meta.get("driver_phone")
        if phone:
            driver_line += f"  ·  📞 {phone}"
        lines.append(driver_line)

    # Empresa
    empresa = meta.get("empresa_nombre")
    if empresa:
        lines.append(f"*Empresa:* {empresa}")

    # Sprint 3: identificadores operativos (suborden / CT / folios). Solo se
    # incluyen si están disponibles — no inventamos campos.
    sub_ct_folio_parts: list[str] = []
    suborden = meta.get("suborden")
    if suborden:
        sub_ct_folio_parts.append(f"*Suborden:* {suborden}")
    ct = meta.get("ct")
    if ct:
        sub_ct_folio_parts.append(f"*CT:* {ct}")
    folios = meta.get("folios") or []
    if folios:
        # Limitamos a 3 folios para no inflar el body
        shown = ", ".join(folios[:3])
        suffix = f" (+{len(folios) - 3} más)" if len(folios) > 3 else ""
        sub_ct_folio_parts.append(f"*Folios:* {shown}{suffix}")
    if sub_ct_folio_parts:
        lines.append("  ·  ".join(sub_ct_folio_parts))

    lines.append("")

    # Cliente / visita
    title = meta.get("title")
    if title:
        lines.append(f"*Cliente:* {title}")
    addr = meta.get("address")
    if addr:
        lines.append(f"*Dirección:* {addr}")
    order = meta.get("order")
    if order:
        lines.append(f"*Parada:* #{order} en ruta")

    # Ventana / ETA / slack / riesgo
    we = (meta.get("window_end") or "")[:5]
    eta = (meta.get("estimated_time_arrival") or "")[:5]
    if we or eta:
        lines.append(f"*Ventana entrega:* hasta {we or '—'}  ·  *ETA:* {eta or '—'}")
    slack = meta.get("slack_min")
    if slack is not None:
        slack_str = f"{slack:+.0f} min"
        if slack < 0:
            slack_str += " (ya pasó deadline)"
        elif slack < 20:
            slack_str += " (ajustado)"
        lines.append(f"*Slack:* {slack_str}")
    p = meta.get("p_fallo")
    if p is not None and p > 0:
        lines.append(f"*Riesgo de fallo (modelo):* {p*100:.0f}%")

    # Geo
    lat = meta.get("latitude")
    lon = meta.get("longitude")
    if lat and lon:
        lines.append(f"*Ubicación:* https://maps.google.com/?q={lat:.5f},{lon:.5f}")

    lines.append("")
    lines.append(f"*Comentario:* {comentario[:400]}")
    lines.append(f"*Reportado por:* {user_display_name}")
    lines.append(f"*Tracking:* {tracking_id}")

    return "\n".join(lines)


def _resolve_alert_targets(
    *,
    empresa_id: Optional[int],
    severity: str,
    motivo: str,
    visit_region: str,
) -> tuple[list[tuple[Optional[int], str]], dict[str, int]]:
    """Resuelve la lista de destinatarios para una alerta, combinando:
      A) `fpoc_users` con `notify_whatsapp=1` (admin/ops + manager de la empresa)
      B) `fpoc_empresa_contactos` activos con opt-in, filtrados por
         severidad/motivo/región.

    Devuelve (targets, contact_ids_by_phone). targets es la lista lista para
    `send_whatsapp`; contact_ids_by_phone permite re-asociar el log al
    contact_id después.

    Dedup por phone_e164: si el mismo número aparece como user y como contacto,
    se queda con el user (porque ese sí tiene user_id).
    """
    targets_users: list[tuple[Optional[int], str]] = []
    targets_contacts: list[tuple[Optional[int], str]] = []
    targets_drivers: list[tuple[Optional[int], str]] = []
    contact_ids_by_phone: dict[str, int] = {}

    with get_conn() as cn:
        cur = cn.cursor()
        # A) Users
        cur.execute(
            """
            SELECT user_id, phone_e164 FROM fpoc.users
            WHERE activo = 1 AND notify_whatsapp = 1
              AND phone_e164 IS NOT NULL AND phone_e164 <> ''
              AND (role IN ('falabella_admin','falabella_ops') OR empresa_id = ?)
            """,
            empresa_id,
        )
        for u in cur.fetchall():
            targets_users.append((int(u.user_id), u.phone_e164))

        # C) Drivers de la empresa con opt-in WhatsApp (Sprint 4.A1).
        # Match por empresa via mapping vehicle_empresa (state) — vamos por SQL
        # buscando drivers cuya empresa coincide (heurística: drivers cuyo
        # vehicle_id está en el set de vehículos de la empresa).
        cur.execute(
            """
            SELECT DISTINCT d.driver_id, d.phone_e164
            FROM fpoc_drivers d
            WHERE d.active = 1
              AND d.notify_whatsapp = 1
              AND d.opted_in_at IS NOT NULL
              AND d.phone_e164 IS NOT NULL AND d.phone_e164 <> ''
            """
        )
        all_drivers = cur.fetchall()
        # Filtramos a la empresa via STATE.vehicle_empresa_map (best-effort).
        try:
            from core.state import STATE
            vmap = getattr(STATE, "vehicle_empresa_map", {}) or {}
            # Nota: drivers tienen vehicle_id 1:1 en `fpoc_drivers.vehicle_id`,
            # pero la fuente de verdad es STATE; recuperamos empresa de cada driver.
            cur.execute(
                "SELECT driver_id, vehicle_id FROM fpoc_drivers WHERE active = 1"
            )
            driver_to_vehicle = {r.driver_id: int(r.vehicle_id) for r in cur.fetchall()}
        except Exception:  # noqa: BLE001
            vmap = {}
            driver_to_vehicle = {}

        for d in all_drivers:
            vid = driver_to_vehicle.get(d.driver_id)
            if empresa_id is not None and vid is not None:
                d_emp = vmap.get(vid)
                if d_emp != empresa_id:
                    continue
            targets_drivers.append((None, d.phone_e164))

        # B) Contactos de la empresa (con opt-in). Filtros JSON los aplicamos en Python.
        cur.execute(
            """
            SELECT contact_id, phone_e164, severities_in, motivos_in, region_filter
            FROM fpoc_empresa_contactos
            WHERE active = 1
              AND opted_in_at IS NOT NULL
              AND empresa_id = ?
            """,
            empresa_id,
        )
        for c in cur.fetchall():
            phone = c.phone_e164
            # Severity filter
            sev_raw = c.severities_in
            if sev_raw:
                try:
                    sev_list = json.loads(sev_raw)
                except Exception:  # noqa: BLE001
                    sev_list = None
                if sev_list and severity not in sev_list:
                    continue
            # Motivo filter
            mot_raw = c.motivos_in
            if mot_raw:
                try:
                    mot_list = json.loads(mot_raw)
                except Exception:  # noqa: BLE001
                    mot_list = None
                if mot_list and motivo not in mot_list:
                    continue
            # Region filter
            region = (c.region_filter or "all").lower()
            if region != "all" and region != visit_region:
                continue
            targets_contacts.append((None, phone))
            contact_ids_by_phone[phone] = int(c.contact_id)

    # Dedup: phones que ya aparecen en users no se incluyen como contactos.
    # Prioridad users > contactos > drivers (Sprint 4.A1).
    user_phones = {p for _, p in targets_users}
    targets = list(targets_users)
    contact_phones: set[str] = set()
    for uid, phone in targets_contacts:
        if phone in user_phones:
            continue
        targets.append((uid, phone))
        contact_phones.add(phone)
    for uid, phone in targets_drivers:
        if phone in user_phones or phone in contact_phones:
            continue
        targets.append((uid, phone))

    return targets, contact_ids_by_phone


def _backfill_contact_id_in_log(
    *,
    tracking_id: str,
    contact_ids_by_phone: dict[str, int],
) -> None:
    """Después de send_whatsapp, asocia contact_id a las filas recién creadas.
    `send_whatsapp` no conoce nuestros contact_ids; busca por tracking + phone +
    user_id NULL y completa el campo. Tolerante a fallos (best-effort)."""
    if not contact_ids_by_phone:
        return
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            for phone, cid in contact_ids_by_phone.items():
                cur.execute(
                    """
                    UPDATE fpoc_notifications_log
                    SET contact_id = ?
                    WHERE notification_id IN (
                        SELECT notification_id FROM fpoc_notifications_log
                        WHERE tracking_id = ? AND to_number = ? AND user_id IS NULL AND contact_id IS NULL
                        ORDER BY notification_id DESC LIMIT 1
                    )
                    """,
                    cid, tracking_id, phone,
                )
            cn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[comments] backfill contact_id falló: {e}")


_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def severity_rank(s: str) -> int:
    """Numérico para comparar severities (low=0, critical=3)."""
    return _SEVERITY_RANK.get((s or "low").lower(), 0)


def dispatch_comment_alert(
    *, tracking_id: str, motivo: str, comentario: str,
    user_display_name: str, triggered_by: str = "comment_alert",
    extra_subject: Optional[str] = None,
) -> dict:
    """Emite evento + WhatsApp para un comentario alertable.

    Reutilizable: lo llama _persist_and_dispatch_comment al crear el comentario
    y también accept_correction cuando la severity escala. Devuelve dict con
    {alertable, severity, sent}.
    """
    meta = _visit_meta(tracking_id)
    if meta is None:
        return {"alertable": False, "severity": None, "sent": 0, "error": "tracking not found"}
    vehicle_id = meta["vehicle_id"]
    empresa_id = STATE.vehicle_empresa_map.get(int(vehicle_id))
    alertable, severity = _resolve_alert_config(motivo, empresa_id)
    if not alertable:
        return {"alertable": False, "severity": severity, "sent": 0}

    EVENTS.emit("comment_alert", STATE.sim_clock or datetime.utcnow(), {
        "tracking_id": tracking_id,
        "vehicle_id": vehicle_id,
        "vehicle_name": meta["vehicle_name"],
        "title": meta["title"],
        "motivo": motivo,
        "comentario": (comentario or "")[:200],
        "severity": severity,
        "reason": f"{triggered_by}: {motivo}",
        "reported_by": user_display_name,
    })

    sent = 0
    if os.environ.get("ENABLE_AUTO_NOTIFY", "false").lower() == "true":
        try:
            from routers.notifications import send_whatsapp
            visit_region = _visit_region(meta.get("latitude"), meta.get("longitude"))
            targets, contact_ids_by_phone = _resolve_alert_targets(
                empresa_id=empresa_id, severity=severity,
                motivo=motivo, visit_region=visit_region,
            )
            if targets:
                subj_pref = (extra_subject + " · ") if extra_subject else ""
                body = _build_alert_whatsapp_body(
                    severity=severity, motivo=motivo, comentario=comentario,
                    user_display_name=user_display_name, tracking_id=tracking_id, meta=meta,
                )
                send_whatsapp(
                    body=body, targets=targets,
                    subject=f"{subj_pref}Motivo {motivo} · {meta.get('vehicle_name','')} · {meta.get('plate') or ''}".strip(),
                    tracking_id=tracking_id, triggered_by=triggered_by,
                )
                _backfill_contact_id_in_log(
                    tracking_id=tracking_id, contact_ids_by_phone=contact_ids_by_phone,
                )
                sent = len(targets)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[comments] dispatch_comment_alert falló: {e}")
    return {"alertable": True, "severity": severity, "sent": sent}


def _persist_and_dispatch_comment(
    tracking_id: str,
    motivo: str,
    comentario: str,
    user_id: Optional[int],
    user_display_name: str,
) -> "VisitComment":
    """Inserta el comentario, emite evento si es alertable y dispara WhatsApp.

    Helper compartido entre el endpoint POST /api/visits/{tid}/comment y el
    comment_simulator. user_id puede ser None (simulador sin usuario)."""
    if motivo not in MOTIVOS_CATALOGO:
        raise HTTPException(400, f"motivo inválido: {motivo!r}")

    meta = _visit_meta(tracking_id)
    if meta is None:
        raise HTTPException(404, f"tracking_id {tracking_id} no encontrado")

    vehicle_id = meta["vehicle_id"]
    empresa_id = STATE.vehicle_empresa_map.get(int(vehicle_id))
    alertable, severity = _resolve_alert_config(motivo, empresa_id)
    region = _visit_region(meta.get("latitude"), meta.get("longitude"))

    from core.db import backend as db_backend
    with get_conn() as cn:
        cur = cn.cursor()
        if db_backend() == "sqlserver":
            # OUTPUT INSERTED.comment_id es 100% confiable con pyodbc;
            # SCOPE_IDENTITY() en cur.execute separado a veces devuelve NULL.
            cur.execute(
                """
                INSERT INTO fpoc.visit_comments
                  (tracking_id, vehicle_id, empresa_id, motivo, comentario, created_by, region)
                OUTPUT INSERTED.comment_id
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                tracking_id, vehicle_id, empresa_id,
                motivo, comentario, user_id, region,
            )
            new_id_row = cur.fetchone()
            new_id = int(new_id_row[0]) if new_id_row else None
            cn.commit()
        else:
            cur.execute(
                """
                INSERT INTO fpoc.visit_comments
                  (tracking_id, vehicle_id, empresa_id, motivo, comentario, created_by, region)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                tracking_id, vehicle_id, empresa_id,
                motivo, comentario, user_id, region,
            )
            cur.execute("SELECT last_insert_rowid()")
            new_id_row = cur.fetchone()
            new_id = int(new_id_row[0]) if new_id_row and new_id_row[0] else None
            cn.commit()

        if new_id is None:
            raise HTTPException(500, "no se pudo obtener comment_id tras INSERT")

        cur.execute(
            """
            SELECT c.comment_id, c.tracking_id, c.vehicle_id, c.empresa_id,
                   c.motivo, c.comentario, c.created_by, c.created_at,
                   u.display_name AS created_by_name
            FROM fpoc.visit_comments c
            LEFT JOIN fpoc.users u ON u.user_id = c.created_by
            WHERE c.comment_id = ?
            """,
            new_id,
        )
        r = cur.fetchone()
        if r is None:
            raise HTTPException(500, f"comment {new_id} insertado pero no se pudo leer")

    created_at = r.created_at.isoformat() if hasattr(r.created_at, "isoformat") else str(r.created_at)

    if alertable:
        EVENTS.emit("comment_alert", STATE.sim_clock or datetime.utcnow(), {
            "tracking_id": tracking_id,
            "vehicle_id": vehicle_id,
            "vehicle_name": meta["vehicle_name"],
            "title": meta["title"],
            "motivo": motivo,
            "comentario": comentario[:200],
            "severity": severity,
            "reason": f"Comentario alertable: {motivo}",
            "reported_by": user_display_name,
        })

        if os.environ.get("ENABLE_AUTO_NOTIFY", "false").lower() == "true":
            try:
                from routers.notifications import send_whatsapp
                visit_region = _visit_region(meta.get("latitude"), meta.get("longitude"))
                targets, contact_ids_by_phone = _resolve_alert_targets(
                    empresa_id=empresa_id,
                    severity=severity,
                    motivo=motivo,
                    visit_region=visit_region,
                )
                if targets:
                    body = _build_alert_whatsapp_body(
                        severity=severity,
                        motivo=motivo,
                        comentario=comentario,
                        user_display_name=user_display_name,
                        tracking_id=tracking_id,
                        meta=meta,
                    )
                    send_whatsapp(
                        body=body, targets=targets,
                        subject=f"Motivo {motivo} · {meta.get('vehicle_name','')} · {meta.get('plate') or ''}".strip(),
                        tracking_id=tracking_id,
                        triggered_by="comment_alert",
                    )
                    # Patch posterior: send_whatsapp loguea con user_id=None para
                    # los contactos. Actualizamos las filas más recientes para
                    # poblar contact_id donde aplique.
                    _backfill_contact_id_in_log(
                        tracking_id=tracking_id,
                        contact_ids_by_phone=contact_ids_by_phone,
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[comments] auto-notify falló: {e}")

    # Sprint 4.A2: validación LLM automática del motivo (siempre, aun si no es
    # alertable, mientras el classifier devuelva algo no-fallback distinto).
    try:
        from routers.motivo_corrections import maybe_create_correction_from_comment
        maybe_create_correction_from_comment(
            comment_id=int(r.comment_id),
            tracking_id=tracking_id,
            motivo_reportado=motivo,
            comentario=comentario,
            empresa_id=empresa_id,
            user_display_name=user_display_name,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[comments] motivo correction hook falló: {e}")

    return VisitComment(
        comment_id=int(r.comment_id),
        tracking_id=r.tracking_id,
        vehicle_id=int(r.vehicle_id) if r.vehicle_id is not None else None,
        empresa_id=int(r.empresa_id) if r.empresa_id is not None else None,
        motivo=r.motivo,
        comentario=r.comentario,
        created_by=int(r.created_by) if r.created_by is not None else None,
        created_by_name=r.created_by_name,
        created_at=created_at,
        alertable=alertable,
        severity=severity,
    )


@router.post("/api/visits/{tracking_id}/comment", response_model=VisitComment)
def add_visit_comment(
    tracking_id: str,
    req: CommentCreate,
    user: CurrentUser = Depends(current_user),
) -> VisitComment:
    # Scope: transport_manager solo puede comentar su empresa
    meta = _visit_meta(tracking_id)
    if meta is None:
        raise HTTPException(404, f"tracking_id {tracking_id} no encontrado")
    vehicle_id = meta["vehicle_id"]
    empresa_id = STATE.vehicle_empresa_map.get(int(vehicle_id))
    if not user.is_falabella and empresa_id != user.empresa_id:
        raise HTTPException(403, "vehículo fuera de tu empresa")

    return _persist_and_dispatch_comment(
        tracking_id=tracking_id,
        motivo=req.motivo,
        comentario=req.comentario,
        user_id=user.user_id,
        user_display_name=user.display_name,
    )


@router.get("/api/visits/{tracking_id}/comments", response_model=list[VisitComment])
def list_visit_comments(
    tracking_id: str,
    user: CurrentUser = Depends(current_user),
) -> list[VisitComment]:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """
            SELECT c.comment_id, c.tracking_id, c.vehicle_id, c.empresa_id,
                   c.motivo, c.comentario, c.created_by, c.created_at,
                   u.display_name AS created_by_name
            FROM fpoc.visit_comments c
            LEFT JOIN fpoc.users u ON u.user_id = c.created_by
            WHERE c.tracking_id = ?
            ORDER BY c.created_at DESC
            """,
            tracking_id,
        )
        rows = cur.fetchall()

    out: list[VisitComment] = []
    for r in rows:
        empresa_id = int(r.empresa_id) if r.empresa_id is not None else None
        if not user.is_falabella and empresa_id != user.empresa_id:
            continue
        alertable, severity = _resolve_alert_config(r.motivo, empresa_id)
        created_at = r.created_at.isoformat() if hasattr(r.created_at, "isoformat") else str(r.created_at)
        out.append(VisitComment(
            comment_id=int(r.comment_id),
            tracking_id=r.tracking_id,
            vehicle_id=int(r.vehicle_id) if r.vehicle_id is not None else None,
            empresa_id=empresa_id,
            motivo=r.motivo,
            comentario=r.comentario,
            created_by=int(r.created_by) if r.created_by is not None else None,
            created_by_name=r.created_by_name,
            created_at=created_at,
            alertable=alertable,
            severity=severity,
        ))
    return out


@router.get("/api/comments/recent", response_model=list[VisitComment])
def list_recent_comments(
    limit: int = Query(default=50, ge=1, le=500),
    only_alertable: bool = Query(default=False),
    user: CurrentUser = Depends(current_user),
) -> list[VisitComment]:
    where = ""
    params: list = []
    if not user.is_falabella:
        where = " WHERE c.empresa_id = ?"
        params.append(user.empresa_id)
    params.append(limit)

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""
            SELECT c.comment_id, c.tracking_id, c.vehicle_id, c.empresa_id,
                   c.motivo, c.comentario, c.created_by, c.created_at,
                   u.display_name AS created_by_name
            FROM fpoc.visit_comments c
            LEFT JOIN fpoc.users u ON u.user_id = c.created_by
            {where}
            ORDER BY c.created_at DESC
            LIMIT ?
            """,
            *params,
        )
        rows = cur.fetchall()

    out: list[VisitComment] = []
    for r in rows:
        empresa_id = int(r.empresa_id) if r.empresa_id is not None else None
        alertable, severity = _resolve_alert_config(r.motivo, empresa_id)
        if only_alertable and not alertable:
            continue
        created_at = r.created_at.isoformat() if hasattr(r.created_at, "isoformat") else str(r.created_at)
        out.append(VisitComment(
            comment_id=int(r.comment_id),
            tracking_id=r.tracking_id,
            vehicle_id=int(r.vehicle_id) if r.vehicle_id is not None else None,
            empresa_id=empresa_id,
            motivo=r.motivo,
            comentario=r.comentario,
            created_by=int(r.created_by) if r.created_by is not None else None,
            created_by_name=r.created_by_name,
            created_at=created_at,
            alertable=alertable,
            severity=severity,
        ))
    return out

