"""Comentarios del transportista + configuración de motivos alertables.

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

import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, Field

from auth import CurrentUser, current_user
from db import get_conn
from events import EVENTS
from state import STATE


router = APIRouter(tags=["comments"])


# Catálogo extraído literal del notebook (celda 8, REGLAS_OPERACIONALES)
MOTIVOS_CATALOGO: list[str] = [
    "SIN MORADORES",
    "PROBLEMA DE DIRECCION/ SIN INFORMACION",
    "NO DESPACHA A LOCALIDAD",
    "FUERA DE COBERTURA/ FRECUENCIA",
    "PROD N ENTREGADO X TIEMPO",
    "PRODUCTO NO CARGADO",
    "CLIENTE RECHAZA ENVIO",
    "SINIESTRO EN CALLE",
    "PRODUCTO CON PROBLEMAS",
    "NO CUMPLE CONDICIONES RETIRO",
    "PRODUCTO ROBADO",
]

# Defaults: mapping motivo -> (alertable, severity)
DEFAULT_ALERT_CONFIG: dict[str, tuple[bool, str]] = {
    "SINIESTRO EN CALLE": (True, "critical"),
    "PRODUCTO ROBADO": (True, "critical"),
    "PRODUCTO NO CARGADO": (True, "high"),
    "PROBLEMA DE DIRECCION/ SIN INFORMACION": (True, "medium"),
}

# Descripciones default por motivo. Texto literal del notebook
# auditoria_llm_directo.ipynb (REGLAS_OPERACIONALES). Se usan en el system prompt
# del LLM cuando no hay override en fpoc.motivo_alert_config.description.
DEFAULT_DESCRIPTIONS: dict[str, str] = {
    "SIN MORADORES": (
        "Nadie disponible en domicilio. Cliente no atiende, no responde llamado/timbre.\n"
        "NO usar si: el problema es la dirección -> PROBLEMA DE DIRECCION.\n"
        "NO usar si: el cliente atendió pero rechazó -> CLIENTE RECHAZA ENVIO."
    ),
    "PROBLEMA DE DIRECCION/ SIN INFORMACION": (
        "Dirección errónea, mal escrita, inexistente, sin numeración, no ubicable, mal geolocalizada, sin información de contacto.\n"
        "NO usar si: la zona no es atendida -> NO DESPACHA o FUERA DE COBERTURA.\n"
        "NO usar si: la dirección está bien pero no había nadie -> SIN MORADORES."
    ),
    "NO DESPACHA A LOCALIDAD": (
        "La dirección del cliente está fuera de la zona geográfica que la empresa atiende. "
        "Aun cuando el cliente anula al darse cuenta, la causa raíz sigue siendo no-despacho."
    ),
    "FUERA DE COBERTURA/ FRECUENCIA": (
        "La ruta no llega a esa zona en el día/horario actual. Frecuencia insuficiente, fuera de ruta del día."
    ),
    "PROD N ENTREGADO X TIEMPO": (
        "No se alcanzó a entregar dentro del tiempo. Atraso, fin de turno. CONDUCTOR NO GESTIONA cae acá cuando es por gestión de tiempo.\n"
        "NO usar si: hubo siniestro -> SINIESTRO EN CALLE.\n"
        "NO usar si: el producto nunca subió al camión -> PRODUCTO NO CARGADO."
    ),
    "PRODUCTO NO CARGADO": (
        "El producto no fue cargado al vehículo en origen. Quedó en bodega, no se cargó por capacidad, fue olvidado."
    ),
    "CLIENTE RECHAZA ENVIO": (
        "El cliente está disponible y rechaza recibir: anula, no quiere, devuelve, cancela.\n"
        "ATENCION: si el cliente anula porque la dirección estaba mal, la causa raíz NO es rechazo - es PROBLEMA DE DIRECCION o NO DESPACHA."
    ),
    "SINIESTRO EN CALLE": (
        "Eventos en ruta: asalto, robo, encerrona, accidente, choque, panne, intervención de carabineros."
    ),
    "PRODUCTO CON PROBLEMAS": (
        "Producto roto, dañado, embalaje en mal estado, faltante, incompleto."
    ),
    "NO CUMPLE CONDICIONES RETIRO": (
        "El destinatario no cumple los requisitos: falta documentación (RUT, autorización), no acredita identidad."
    ),
    "PRODUCTO ROBADO": (
        "El producto fue robado/sustraído de la carga."
    ),
}

ALLOWED_SEVERITY = {"low", "medium", "high", "critical"}


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
    Reúne datos del snapshot + maestros (drivers / vehicles_ext) + empresa."""
    if STATE.snapshot_df is None:
        return None
    df = STATE.snapshot_df
    matching = df[df["tracking_id"] == tracking_id]
    if matching.empty:
        return None
    row = matching.iloc[0]
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

    return {
        "vehicle_id": vehicle_id,
        "vehicle_name": str(row["vehicle_name"]),
        "title": str(row["title"]),
        "address": str(row.get("address", "")),
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

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """
            INSERT INTO fpoc.visit_comments
              (tracking_id, vehicle_id, empresa_id, motivo, comentario, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            tracking_id, vehicle_id, empresa_id,
            motivo, comentario, user_id,
        )
        cn.commit()
        cur.execute(
            """
            SELECT c.comment_id, c.tracking_id, c.vehicle_id, c.empresa_id,
                   c.motivo, c.comentario, c.created_by, c.created_at,
                   u.display_name AS created_by_name
            FROM fpoc.visit_comments c
            LEFT JOIN fpoc.users u ON u.user_id = c.created_by
            WHERE c.comment_id = last_insert_rowid()
            """
        )
        r = cur.fetchone()

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
                from notifications import send_whatsapp
                with get_conn() as cn:
                    cur = cn.cursor()
                    cur.execute(
                        """
                        SELECT user_id, phone_e164 FROM fpoc.users
                        WHERE activo = 1 AND notify_whatsapp = 1
                          AND phone_e164 IS NOT NULL AND length(phone_e164) > 0
                          AND (role IN ('falabella_admin','falabella_ops') OR empresa_id = ?)
                        """,
                        empresa_id,
                    )
                    targets = [(int(u.user_id), u.phone_e164) for u in cur.fetchall()]
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
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[comments] auto-notify falló: {e}")

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

