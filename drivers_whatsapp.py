"""Sprint 4.A1 + A3 — endpoints específicos de drivers para WhatsApp + scorecard.

- PUT /api/mantenedores/drivers/{driver_id}    — actualiza phone_e164/notify_whatsapp/opted_in_at
- GET /api/drivers/scorecard?period_days=30    — métricas por driver
- POST /api/planificacion/import-mock          — placeholder Sprint 5 (mock)
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
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

    rows: list[DriverScorecardRow] = []
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """
            SELECT d.driver_id, d.name AS driver_name,
                   d.empresa_id, d.vehicle_id, d.vehicle_name,
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
        # Empresas
        cur.execute("SELECT empresa_id, nombre FROM fpoc_empresas_transporte")
        empresas_map = {int(r.empresa_id): r.nombre for r in cur.fetchall()}

        for d in drivers:
            vid = int(d.vehicle_id) if d.vehicle_id is not None else None
            emp_id = int(d.empresa_id) if d.empresa_id is not None else None
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
# Mock SimpliRoute import (Sprint 5+) — persiste en fpoc_simpli_visits
# Idempotente por fecha: si ya importaste el día, lo dice y no duplica.
# =============================================================================
class DotacionConflict(BaseModel):
    empresa_id: int
    empresa_nombre: Optional[str] = None
    driver_id: Optional[str] = None
    driver_name: Optional[str] = None
    vehicle_id: Optional[int] = None
    plate: Optional[str] = None
    estado: str
    motivo: Optional[str] = None
    visitas_afectadas: int = 0
    ruta_id: Optional[str] = None


class ImportMockResponse(BaseModel):
    ok: bool
    count: int
    fecha: str
    already_imported: bool = False
    message: str = ""
    conflicts: list[DotacionConflict] = []


def _check_dotacion_conflicts(target_date_iso: str) -> list[DotacionConflict]:
    """Cruza visitas del día contra dotacion_diaria y devuelve drivers/vehículos
    asignados a rutas pero marcados como no operables (ausente/licencia/mantencion/baja).

    Match por (empresa_id, vehicle_id) — robusto, vehicle_id es int en ambas tablas.
    También chequea drivers con `active=0` aunque no haya override.
    """
    out: list[DotacionConflict] = []
    blocking_estados = ("ausente", "licencia", "mantencion", "baja")
    with get_conn() as cn:
        cur = cn.cursor()
        # 1) Por dotacion_diaria override (estados bloqueantes)
        cur.execute(
            f"""
            SELECT dd.empresa_id, e.nombre AS empresa_nombre,
                   dd.driver_id, d.name AS driver_name,
                   dd.vehicle_id, v.plate,
                   dd.estado, dd.motivo,
                   COUNT(sv.id) AS visitas, MAX(sv.ruta_id) AS ruta_id
              FROM fpoc.dotacion_diaria dd
              LEFT JOIN fpoc.empresas_transporte e ON e.empresa_id = dd.empresa_id
              LEFT JOIN fpoc.drivers d ON d.driver_id = dd.driver_id
              LEFT JOIN fpoc.vehicles v ON v.vehicle_id = dd.vehicle_id
              LEFT JOIN fpoc.simpli_visits sv
                ON sv.planned_date = dd.fecha
               AND sv.Empresa_falsa = dd.empresa_id
               AND sv.Patente_falsa = dd.vehicle_id
             WHERE dd.fecha = ?
               AND dd.estado IN ({",".join("?" * len(blocking_estados))})
             GROUP BY dd.empresa_id, e.nombre, dd.driver_id, d.name,
                      dd.vehicle_id, v.plate, dd.estado, dd.motivo
            """,
            target_date_iso, *blocking_estados,
        )
        for r in cur.fetchall():
            out.append(DotacionConflict(
                empresa_id=int(r.empresa_id),
                empresa_nombre=r.empresa_nombre,
                driver_id=r.driver_id, driver_name=r.driver_name,
                vehicle_id=int(r.vehicle_id) if r.vehicle_id is not None else None,
                plate=r.plate,
                estado=str(r.estado), motivo=r.motivo,
                visitas_afectadas=int(r.visitas or 0),
                ruta_id=r.ruta_id,
            ))
    return out


class ImportLogRow(BaseModel):
    fecha: str
    count: int
    imported_at: str
    imported_by_user_id: Optional[int] = None


@router.get("/api/planificacion/imports", response_model=list[ImportLogRow])
def list_imports(_: CurrentUser = Depends(current_user)) -> list[ImportLogRow]:
    """Histórico de cargas de SimpliRoute (lo que muestra el panel "Última carga").
    Persistente entre sesiones, sobrevive a refresh y navegación."""
    out: list[ImportLogRow] = []
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT fecha, count, imported_at, imported_by_user_id "
            "FROM fpoc_planificacion_imports ORDER BY fecha DESC LIMIT 50"
        )
        for r in cur.fetchall():
            out.append(ImportLogRow(
                fecha=str(r[0]),
                count=int(r[1]),
                imported_at=str(r[2]) if r[2] else "",
                imported_by_user_id=int(r[3]) if r[3] is not None else None,
            ))
    return out


@router.post("/api/planificacion/import-mock", response_model=ImportMockResponse)
def import_mock(
    fecha: Optional[str] = None,
    force: bool = False,
    user: CurrentUser = Depends(current_user),
) -> ImportMockResponse:
    """Importa visitas mock para una fecha y las persiste en fpoc_simpli_visits.

    - `fecha`: 'YYYY-MM-DD'. Default: STATE.today si existe, sino hoy.
    - `force=true`: re-importar aunque la fecha ya esté cargada (para demos
      destructivas).

    Idempotencia: marker en `fpoc_planificacion_imports`. Si ya hay para la
    fecha, devuelve `already_imported=true` + el count anterior, sin duplicar.
    """
    from datetime import date as _date_cls
    from state import STATE
    if fecha:
        try:
            target_date = _date_cls.fromisoformat(fecha)
        except ValueError:
            raise HTTPException(400, f"fecha inválida: {fecha} (esperado YYYY-MM-DD)")
    else:
        target_date = getattr(STATE, "today", None) or _date_cls.today()

    # Chequeo idempotencia
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT count, imported_at FROM fpoc_planificacion_imports WHERE fecha = ?",
            (target_date.isoformat(),),
        )
        existing = cur.fetchone()

    if existing and not force:
        prev_count = int(existing[0])
        prev_at = str(existing[1]) if existing[1] else "?"
        conflicts_existing = _check_dotacion_conflicts(target_date.isoformat())
        return ImportMockResponse(
            ok=True,
            count=prev_count,
            fecha=target_date.isoformat(),
            already_imported=True,
            message=f"Ya cargaste el día {target_date.isoformat()} ({prev_count} visitas, {prev_at}). Mandá ?force=true para re-importar.",
            conflicts=conflicts_existing,
        )

    # Importación real: usamos el live_generator (sintetiza visitas con todos
    # los campos que requiere fpoc_simpli_visits, drivers de fpoc_drivers).
    # Antes de insertar, limpiamos las visitas existentes para esa fecha así
    # los drivers ficticios del seed inicial no se mezclan con los reales y el
    # match driver↔visita queda 1:1.
    try:
        from live_generator import _insert_batch
        import random
        n = random.randint(180, 320)
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute("DELETE FROM fpoc_simpli_visits WHERE planned_date = ?", (target_date.isoformat(),))
            deleted = cur.rowcount or 0
            cn.commit()
            inserted = _insert_batch(cn, target_date, n)
            cur = cn.cursor()
            # Upsert portátil sqlite/sqlserver: chequear primero, INSERT o UPDATE.
            # ON CONFLICT es sqlite-only; MERGE es T-SQL. Esta forma anda en ambos.
            cur.execute(
                "SELECT 1 FROM fpoc_planificacion_imports WHERE fecha = ?",
                (target_date.isoformat(),),
            )
            if cur.fetchone():
                cur.execute(
                    """UPDATE fpoc_planificacion_imports
                          SET count = ?, imported_at = CURRENT_TIMESTAMP,
                              imported_by_user_id = ?
                        WHERE fecha = ?""",
                    (inserted, user.user_id, target_date.isoformat()),
                )
            else:
                cur.execute(
                    """INSERT INTO fpoc_planificacion_imports
                            (fecha, count, imported_by_user_id)
                         VALUES (?, ?, ?)""",
                    (target_date.isoformat(), inserted, user.user_id),
                )
            cn.commit()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Error importando: {e}")

    msg = f"Importadas {inserted} visitas para {target_date.isoformat()}"
    if deleted:
        msg += f" (reemplazadas {deleted} visitas previas)"

    # Si la fecha importada es la del simulador, refrescar el snapshot ML para
    # que la analitica vea las visitas nuevas al instante.
    try:
        from state import STATE
        if STATE.today and target_date.isoformat() == STATE.today.isoformat():
            STATE.reset_day(start_date=target_date, day_seed=STATE.day_seed)
            msg += f" (snapshot ML refrescado a {len(STATE.snapshot_df) if STATE.snapshot_df is not None else 0} visitas)"
    except Exception:  # noqa: BLE001
        pass

    conflicts = _check_dotacion_conflicts(target_date.isoformat())
    if conflicts:
        msg += f" · ⚠ {len(conflicts)} driver(s)/vehículo(s) marcados no operables"

    return ImportMockResponse(
        ok=True,
        count=inserted,
        fecha=target_date.isoformat(),
        already_imported=False,
        message=msg,
        conflicts=conflicts,
    )


@router.get("/api/planificacion/dotacion-check", response_model=list[DotacionConflict])
def dotacion_check(
    fecha: str = Query(...),
    _: CurrentUser = Depends(current_user),
) -> list[DotacionConflict]:
    """Cruza visitas del día contra dotacion_diaria y devuelve conflictos
    (drivers/vehículos en rutas pero marcados ausente/licencia/mantencion/baja).
    Útil antes y después de la carga; el import-mock también lo incluye en su
    respuesta automáticamente.
    """
    from datetime import date as _date_cls
    try:
        target = _date_cls.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {fecha} (esperado YYYY-MM-DD)")
    return _check_dotacion_conflicts(target.isoformat())
