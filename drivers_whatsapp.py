"""Sprint 4.A1 + A3 — endpoints específicos de drivers para WhatsApp + scorecard.

- PUT /api/mantenedores/drivers/{driver_id}    — actualiza phone_e164/notify_whatsapp/opted_in_at
- GET /api/drivers/scorecard?period_days=30    — métricas por driver
- POST /api/planificacion/import-mock          — placeholder Sprint 5 (mock)
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import CurrentUser, current_user, require_admin
from db import get_conn


router = APIRouter(tags=["drivers-whatsapp"])


# =============================================================================
# Schemas
# =============================================================================
class DriverWhatsAppUpdate(BaseModel):
    phone_e164: Optional[str] = Field(default=None, max_length=20)
    notify_whatsapp: Optional[bool] = None
    opted_in_at: Optional[str] = None  # ISO timestamp; None = no toca; '' = limpiar
    set_opted_in_now: Optional[bool] = False  # shortcut: setea opted_in_at = NOW
    clear_opted_in: Optional[bool] = False    # shortcut: setea opted_in_at = NULL


class DriverWhatsAppOut(BaseModel):
    driver_id: str
    name: str
    phone: Optional[str] = None
    phone_e164: Optional[str] = None
    notify_whatsapp: bool
    opted_in_at: Optional[str] = None


def _row_to_out(r) -> DriverWhatsAppOut:
    optin = r.opted_in_at
    return DriverWhatsAppOut(
        driver_id=r.driver_id,
        name=r.name,
        phone=r.phone,
        phone_e164=r.phone_e164,
        notify_whatsapp=bool(r.notify_whatsapp),
        opted_in_at=optin.isoformat() if hasattr(optin, "isoformat") else (optin or None),
    )


@router.put("/api/mantenedores/drivers/{driver_id}", response_model=DriverWhatsAppOut)
def update_driver_whatsapp(
    driver_id: str,
    req: DriverWhatsAppUpdate,
    _: CurrentUser = Depends(require_admin),
) -> DriverWhatsAppOut:
    """Actualiza campos de WhatsApp/opt-in de un driver. Solo admin.

    Recordatorio sandbox Twilio: incluso seteando notify_whatsapp=1 y opted_in_at,
    los envíos siguen restringidos al sandbox a menos que ENABLE_AUTO_NOTIFY=true
    en el .env. Este endpoint no toca esa env var ni envía broadcast de prueba.
    """
    sets: list[str] = []
    params: list = []

    if req.phone_e164 is not None:
        # '' explícito = limpiar
        v = req.phone_e164.strip() if req.phone_e164 else None
        sets.append("phone_e164 = ?")
        params.append(v if v else None)

    if req.notify_whatsapp is not None:
        sets.append("notify_whatsapp = ?")
        params.append(1 if req.notify_whatsapp else 0)

    if req.set_opted_in_now:
        sets.append("opted_in_at = CURRENT_TIMESTAMP")
    elif req.clear_opted_in:
        sets.append("opted_in_at = NULL")
    elif req.opted_in_at is not None:
        if req.opted_in_at == "":
            sets.append("opted_in_at = NULL")
        else:
            sets.append("opted_in_at = ?")
            params.append(req.opted_in_at)

    if not sets:
        raise HTTPException(400, "nada que actualizar")

    sets.append("updated_at = CURRENT_TIMESTAMP")
    params.append(driver_id)

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"UPDATE fpoc_drivers SET {', '.join(sets)} WHERE driver_id = ?",
            *params,
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "driver no encontrado")
        cn.commit()
        cur.execute(
            "SELECT driver_id, name, phone, phone_e164, notify_whatsapp, opted_in_at "
            "FROM fpoc_drivers WHERE driver_id = ?",
            driver_id,
        )
        r = cur.fetchone()
    return _row_to_out(r)


# =============================================================================
# Driver scorecard (A3)
# =============================================================================
class DriverScorecardRow(BaseModel):
    driver_id: str
    driver_name: str
    vehicle_id: Optional[int] = None
    vehicle_name: Optional[str] = None
    empresa_id: Optional[int] = None
    empresa_nombre: Optional[str] = None
    deliveries_30d: int
    fail_rate_30d: float
    comments_total: int
    corrections_pending: int
    corrections_accepted: int
    corrections_rejected: int
    corrections_acceptance_rate: float
    rating: float
    alerts_critical_30d: int
    alerts_medium_30d: int
    rank_fail_rate: int
    rank_acceptance: int


@router.get("/api/drivers/scorecard", response_model=list[DriverScorecardRow])
def get_driver_scorecard(
    period_days: int = 30,
    empresa_id: Optional[int] = None,
    region: str = "all",
    user: CurrentUser = Depends(current_user),
) -> list[DriverScorecardRow]:
    if not user.is_falabella:
        raise HTTPException(403, "solo falabella_admin/ops")
    if period_days < 1 or period_days > 365:
        raise HTTPException(400, "period_days fuera de rango")
    if region not in ("all", "RM", "regiones"):
        raise HTTPException(400, "region debe ser all|RM|regiones")

    # Filtro region: usa columna `region` propia de cada tabla de log
    # (poblada al insertar). Evita join con fpoc_simpli_visits porque los
    # tracking_ids del simulador (TRK*) no se mapean a los ids del Excel.
    if region == "RM":
        region_filter = "AND region = 'RM'"
    elif region == "regiones":
        region_filter = "AND region IS NOT NULL AND region != 'RM'"
    else:
        region_filter = ""

    where_empresa = ""
    params_empresa: list = []
    if empresa_id is not None:
        where_empresa = "AND v.vehicle_id IN (SELECT vehicle_id FROM fpoc_vehicles WHERE driver_id = d.driver_id)"
        # Note: vehicles also point to empresas via state map; we filter via driver-vehicle
        # For simplicity we filter at the python level using driver-empresa map below.

    rows: list[DriverScorecardRow] = []
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """
            SELECT d.driver_id, d.name AS driver_name,
                   d.vehicle_id, d.vehicle_name,
                   d.rating, d.deliveries_30d, d.fail_rate_30d,
                   v.vehicle_id AS v_id
            FROM fpoc_drivers d
            LEFT JOIN fpoc_vehicles v ON v.driver_id = d.driver_id
            WHERE d.active = 1
            ORDER BY d.driver_id
            """
        )
        drivers = cur.fetchall()

        # Empresa per vehicle (via state map o tabla; usamos el state si está disponible)
        from state import STATE
        veh_to_empresa = STATE.vehicle_empresa_map if hasattr(STATE, "vehicle_empresa_map") else {}

        # Empresas
        cur.execute("SELECT empresa_id, nombre FROM fpoc_empresas_transporte")
        empresas_map = {int(r.empresa_id): r.nombre for r in cur.fetchall()}

        for d in drivers:
            vid = int(d.vehicle_id) if d.vehicle_id is not None else None
            emp_id = veh_to_empresa.get(vid) if vid is not None else None
            if empresa_id is not None and emp_id != empresa_id:
                continue

            # Comments totals (last N days)
            cur.execute(
                f"""
                SELECT COUNT(*) AS n
                FROM fpoc_visit_comments
                WHERE vehicle_id = ?
                  AND created_at >= datetime('now', '-{int(period_days)} days')
                  {region_filter}
                """,
                vid if vid is not None else -1,
            )
            n_comments = int(cur.fetchone().n)

            # Corrections by status
            cur.execute(
                f"""
                SELECT status, COUNT(*) AS n
                FROM fpoc_motivo_corrections
                WHERE driver_id = ?
                  AND created_at >= datetime('now', '-{int(period_days)} days')
                  {region_filter}
                GROUP BY status
                """,
                d.driver_id,
            )
            corr_counts = {r.status: int(r.n) for r in cur.fetchall()}
            n_pending = corr_counts.get("pending", 0)
            n_accepted = corr_counts.get("accepted", 0)
            n_rejected = corr_counts.get("rejected", 0)
            decided = n_accepted + n_rejected
            acceptance_rate = (n_accepted / decided) if decided else 0.0

            # Alerts (notifications log con tracking de comments del driver)
            # Heurística: alertas por severity en body inline del log
            cur.execute(
                f"""
                SELECT body
                FROM fpoc_notifications_log
                WHERE driver_id = ?
                  AND triggered_by IN ('comment_alert','motivo_correction')
                  AND created_at >= datetime('now', '-{int(period_days)} days')
                  {region_filter}
                """,
                d.driver_id,
            )
            log_rows = cur.fetchall()
            alerts_critical = sum(1 for r in log_rows if "CRITICAL" in (r.body or "").upper())
            alerts_medium = sum(1 for r in log_rows if "MEDIUM" in (r.body or "").upper())

            rows.append(DriverScorecardRow(
                driver_id=d.driver_id,
                driver_name=d.driver_name,
                vehicle_id=vid,
                vehicle_name=d.vehicle_name,
                empresa_id=emp_id,
                empresa_nombre=empresas_map.get(emp_id) if emp_id else None,
                deliveries_30d=int(d.deliveries_30d or 0),
                fail_rate_30d=float(d.fail_rate_30d or 0.0),
                comments_total=n_comments,
                corrections_pending=n_pending,
                corrections_accepted=n_accepted,
                corrections_rejected=n_rejected,
                corrections_acceptance_rate=round(acceptance_rate, 3),
                rating=float(d.rating or 0.0),
                alerts_critical_30d=alerts_critical,
                alerts_medium_30d=alerts_medium,
                rank_fail_rate=0,  # poblamos abajo
                rank_acceptance=0,
            ))

    # Ranking (compute después)
    by_fail = sorted(rows, key=lambda r: r.fail_rate_30d, reverse=True)
    for i, r in enumerate(by_fail, start=1):
        r.rank_fail_rate = i
    by_acc = sorted(rows, key=lambda r: r.corrections_acceptance_rate, reverse=True)
    for i, r in enumerate(by_acc, start=1):
        r.rank_acceptance = i

    return rows


# =============================================================================
# Mock SimpliRoute import (Sprint 5 placeholder)
# =============================================================================
class ImportMockResponse(BaseModel):
    ok: bool
    count: int
    fecha: str


@router.post("/api/planificacion/import-mock", response_model=ImportMockResponse)
def import_mock(_: CurrentUser = Depends(current_user)) -> ImportMockResponse:
    """Mock para POC: simula la importación desde SimpliRoute. No persiste nada."""
    import random
    return ImportMockResponse(
        ok=True,
        count=random.randint(180, 320),
        fecha=datetime.utcnow().date().isoformat(),
    )
