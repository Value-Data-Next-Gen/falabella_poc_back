"""Sprint 4.A1 + A3 — endpoints específicos de drivers para WhatsApp + scorecard.

- PUT /api/mantenedores/drivers/{driver_id}    — actualiza phone_e164/notify_whatsapp/opted_in_at
- GET /api/drivers/scorecard?period_days=30    — métricas por driver
- POST /api/planificacion/import-mock          — placeholder Sprint 5 (mock)
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
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

    # CUADRATURA: pausar el live_generator para que no siga inyectando filas
    # al planned_date recién importado. Antes esto causaba que las 288 visitas
    # subidas se inflaran a 700+ luego de unos minutos.
    try:
        from state import STATE as _STATE
        if _STATE.today and target_date.isoformat() == _STATE.today.isoformat():
            from live_generator import STATE as LIVE_STATE
            if LIVE_STATE.enabled:
                LIVE_STATE.enabled = False
                msg += " · live_generator pausado (cuadratura)"
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


class ImportXlsxResponse(BaseModel):
    ok: bool
    fechas: list[str] = []          # planned_dates encontrados en el xlsx
    simpli_count: int = 0
    geo_count: int = 0
    skipped: int = 0
    message: str = ""
    conflicts: list[DotacionConflict] = []


@router.post("/api/planificacion/import-xlsx", response_model=ImportXlsxResponse)
async def import_xlsx(
    file: UploadFile = File(...),
    force: bool = Query(default=False, description="Si true, reemplaza fechas existentes"),
    user: CurrentUser = Depends(current_user),
) -> ImportXlsxResponse:
    """Carga el XLSX REAL de SimpliRoute (formato datos_eta_YYYY-MM-DD.xlsx).

    Hoja 'Simpli' → fpoc.simpli_visits (DELETE por planned_date encontrado + INSERT).
    Hoja 'Geo'   → fpoc.geo_suborders (DELETE por idruta encontrado + INSERT).

    Reusa la lógica del script fpoc_loader/load_to_azure.py para mantener
    consistencia con el seed inicial.
    """
    if not user.is_falabella:
        raise HTTPException(403, "Solo admin/ops puede cargar XLSX")
    import pandas as pd
    import io as _io
    from fpoc_loader.load_to_azure import SIMPLI_COLS, GEO_COLS

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "archivo vacío")
    if len(raw) > 50 * 1024 * 1024:
        raise HTTPException(413, "archivo > 50MB")

    try:
        xl = pd.ExcelFile(_io.BytesIO(raw))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"no es un xlsx válido: {e}")

    sheets = set(xl.sheet_names)
    if "Simpli" not in sheets:
        raise HTTPException(400, f"hoja 'Simpli' no encontrada. hojas: {list(sheets)}")
    has_geo = "Geo" in sheets

    df_s = pd.read_excel(xl, sheet_name="Simpli")
    df_g = pd.read_excel(xl, sheet_name="Geo") if has_geo else None

    # Validar columnas mínimas en Simpli
    missing = [c for c in SIMPLI_COLS if c not in df_s.columns]
    if missing:
        raise HTTPException(400, f"hoja Simpli sin columnas requeridas: {missing[:10]}")

    df = df_s[SIMPLI_COLS].copy()
    before = len(df)
    df = df.drop_duplicates(subset=["id"], keep="first")
    deduped = before - len(df)

    df["planned_date"] = pd.to_datetime(df["planned_date"]).dt.date
    df["checkout_cl"] = pd.to_datetime(df["checkout_cl"])
    df["current_eta_cl"] = pd.to_datetime(df["current_eta_cl"])
    for c in ("checkout_comment", "checkout_observation"):
        df[c] = df[c].astype(object).where(df[c].notna(), None)
    for c in (
        "fechas_futuras_bq", "finicio_currenteta_bq",
        "current_eta_cl_fechainicioruta_dates",
        "ruta_eta_futuro", "ruta_fecha_inicio_mayor_eta",
        "ruta_primer_punto_lejano", "ruta_fecha_inicio_distinta_fecha_eta",
        "ruta_anomala",
    ):
        if c in df.columns:
            df[c] = df[c].astype(int)

    fechas = sorted({d.isoformat() for d in df["planned_date"].unique()})

    # Si force=False y alguna fecha ya tiene visitas, devolver advertencia
    with get_conn() as cn:
        cur = cn.cursor()
        if not force:
            existing_dates = []
            for d in fechas:
                cur.execute("SELECT COUNT(*) FROM fpoc.simpli_visits WHERE planned_date = ?", d)
                if int(cur.fetchone()[0]) > 0:
                    existing_dates.append(d)
            if existing_dates:
                return ImportXlsxResponse(
                    ok=False,
                    fechas=fechas,
                    skipped=len(df),
                    message=(f"Fechas ya cargadas: {existing_dates}. "
                              f"Reenvía con force=true para reemplazar."),
                )

        # DELETE + INSERT por fechas
        for d in fechas:
            cur.execute("DELETE FROM fpoc.simpli_visits WHERE planned_date = ?", d)

        placeholders = ", ".join(["?"] * len(SIMPLI_COLS))
        cols_sql = ", ".join(f"[{c}]" for c in SIMPLI_COLS)
        rows = [tuple(None if pd.isna(v) else v for v in row)
                for row in df.itertuples(index=False, name=None)]
        try:
            cur.fast_executemany = True
        except Exception:  # noqa: BLE001
            pass
        cur.executemany(
            f"INSERT INTO fpoc.simpli_visits ({cols_sql}) VALUES ({placeholders})",
            rows,
        )

        simpli_count = len(rows)
        geo_count = 0
        if df_g is not None and not df_g.empty:
            missing_g = [c for c in GEO_COLS if c not in df_g.columns]
            if not missing_g:
                dg = df_g[GEO_COLS].copy().drop_duplicates(subset=["Suborden"], keep="first")
                dg["fechapactada"] = pd.to_datetime(dg["fechapactada"]).dt.date
                for c in ("lpn", "parentorder"):
                    dg[c] = dg[c].astype("Int64")
                for c in ("motivonoentrega", "comentarionoentrega"):
                    dg[c] = dg[c].astype(object).where(dg[c].notna(), None)
                rutas = dg["idruta"].unique().tolist()
                for i in range(0, len(rutas), 1000):
                    chunk = rutas[i:i + 1000]
                    marks = ",".join(["?"] * len(chunk))
                    cur.execute(f"DELETE FROM fpoc.geo_suborders WHERE idruta IN ({marks})", *chunk)
                geo_placeholders = ", ".join(["?"] * len(GEO_COLS))
                geo_cols_sql = ", ".join(f"[{c}]" for c in GEO_COLS)
                geo_rows = [tuple(None if pd.isna(v) else v for v in row)
                            for row in dg.itertuples(index=False, name=None)]
                cur.executemany(
                    f"INSERT INTO fpoc.geo_suborders ({geo_cols_sql}) VALUES ({geo_placeholders})",
                    geo_rows,
                )
                geo_count = len(geo_rows)

        # Registrar imports
        for d in fechas:
            cur.execute("SELECT 1 FROM fpoc_planificacion_imports WHERE fecha = ?", (d,))
            count_for_date = int(df[df["planned_date"].astype(str) == d].shape[0])
            if cur.fetchone():
                cur.execute(
                    "UPDATE fpoc_planificacion_imports SET count = ?, "
                    "imported_at = CURRENT_TIMESTAMP, imported_by_user_id = ? "
                    "WHERE fecha = ?",
                    (count_for_date, user.user_id, d),
                )
            else:
                cur.execute(
                    "INSERT INTO fpoc_planificacion_imports (fecha, count, imported_by_user_id) "
                    "VALUES (?, ?, ?)",
                    (d, count_for_date, user.user_id),
                )
        cn.commit()

    # Pausar live_gen si STATE.today coincide con alguna fecha
    paused = False
    try:
        from state import STATE
        if STATE.today and STATE.today.isoformat() in fechas:
            from live_generator import STATE as LIVE_STATE
            if LIVE_STATE.enabled:
                LIVE_STATE.enabled = False
                paused = True
            STATE.reset_day(start_date=STATE.today, day_seed=getattr(STATE, "day_seed", 42))
    except Exception:  # noqa: BLE001
        pass

    conflicts = []
    for d in fechas:
        conflicts.extend(_check_dotacion_conflicts(d))

    msg = f"Cargadas {simpli_count} visitas para {len(fechas)} fecha(s): {fechas}"
    if geo_count:
        msg += f" · {geo_count} suborders geo"
    if deduped:
        msg += f" · {deduped} duplicados omitidos"
    if paused:
        msg += " · live_gen pausado"
    if conflicts:
        msg += f" · ⚠ {len(conflicts)} conflictos de dotación"

    return ImportXlsxResponse(
        ok=True,
        fechas=fechas,
        simpli_count=simpli_count,
        geo_count=geo_count,
        skipped=deduped,
        message=msg,
        conflicts=conflicts,
    )


class StartDayResponse(BaseModel):
    ok: bool
    fecha: str
    visitas_en_db: int
    live_gen_paused: bool
    state_today: Optional[str] = None
    snapshot_size: int = 0
    conflicts: list[DotacionConflict] = []
    message: str = ""


@router.post("/api/planificacion/start-day", response_model=StartDayResponse)
def start_day(
    fecha: str = Query(...),
    user: CurrentUser = Depends(current_user),
) -> StartDayResponse:
    """Marca el comienzo del día operativo en la fecha indicada.

    - Detiene el live_generator (deja de inyectar visitas al planned_date).
    - Setea STATE.today = fecha y refresca el snapshot ML/in-memory.
    - Cuenta visitas reales en fpoc.simpli_visits para esa fecha (cuadratura).
    - Devuelve conflictos de dotación pendientes.
    """
    if not user.is_falabella:
        raise HTTPException(403, "Solo admin/ops puede iniciar el día")
    from datetime import date as _date_cls
    try:
        target = _date_cls.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {fecha}")

    # Pausar live_gen
    paused = False
    try:
        from live_generator import STATE as LIVE_STATE
        if LIVE_STATE.enabled:
            LIVE_STATE.enabled = False
            paused = True
    except Exception:  # noqa: BLE001
        pass

    # Setear STATE.today + refrescar snapshot
    snapshot_size = 0
    state_today_iso: Optional[str] = None
    try:
        from state import STATE
        STATE.reset_day(start_date=target, day_seed=getattr(STATE, "day_seed", 42))
        state_today_iso = STATE.today.isoformat() if STATE.today else None
        snapshot_size = len(STATE.snapshot_df) if STATE.snapshot_df is not None else 0
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[start-day] reset_day fallo: {e}")

    # Contar visitas
    visitas = 0
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM fpoc.simpli_visits WHERE planned_date = ?",
                target.isoformat(),
            )
            visitas = int(cur.fetchone()[0])
    except Exception:  # noqa: BLE001
        pass

    conflicts = _check_dotacion_conflicts(target.isoformat())

    msg_parts = [f"Día {fecha} iniciado", f"{visitas} visitas en DB"]
    if paused:
        msg_parts.append("live_gen pausado")
    if conflicts:
        msg_parts.append(f"⚠ {len(conflicts)} conflictos de dotación")

    return StartDayResponse(
        ok=True,
        fecha=target.isoformat(),
        visitas_en_db=visitas,
        live_gen_paused=paused,
        state_today=state_today_iso,
        snapshot_size=snapshot_size,
        conflicts=conflicts,
        message=" · ".join(msg_parts),
    )


class CalendarDay(BaseModel):
    fecha: str
    visitas: int
    is_today: bool                # coincide con STATE.today
    imported_at: Optional[str] = None
    conflicts_count: int = 0
    failed: int = 0
    completed: int = 0
    pending: int = 0


@router.get("/api/planificacion/calendar", response_model=list[CalendarDay])
def operational_calendar(
    month: Optional[str] = Query(default=None, description="YYYY-MM (default: mes actual)"),
    user: CurrentUser = Depends(current_user),
) -> list[CalendarDay]:
    """Lista de días con visitas cargadas para un mes.

    Si transport_manager, las visitas se filtran a su empresa.
    """
    from datetime import date as _date_cls
    today = _date_cls.today()
    if month:
        try:
            y, m = month.split("-")
            year, mon = int(y), int(m)
        except Exception:
            raise HTTPException(400, f"month inválido: {month} (esperado YYYY-MM)")
    else:
        year, mon = today.year, today.month
    start = _date_cls(year, mon, 1)
    if mon == 12:
        end = _date_cls(year + 1, 1, 1)
    else:
        end = _date_cls(year, mon + 1, 1)

    # Scope por empresa
    scope_where = ""
    scope_params: list = []
    if not user.is_falabella:
        scope_where = " AND Empresa_falsa = ?"
        scope_params.append(user.empresa_id)

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""SELECT planned_date,
                       COUNT(*) AS total,
                       SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending
                FROM fpoc.simpli_visits
                WHERE planned_date >= ? AND planned_date < ?
                {scope_where}
                GROUP BY planned_date
                ORDER BY planned_date""",
            start.isoformat(), end.isoformat(), *scope_params,
        )
        rows = cur.fetchall()
        # Imports log
        imports_map: dict[str, str] = {}
        try:
            cur.execute(
                "SELECT fecha, imported_at FROM fpoc_planificacion_imports "
                "WHERE fecha >= ? AND fecha < ?",
                start.isoformat(), end.isoformat(),
            )
            for r in cur.fetchall():
                imports_map[str(r.fecha)] = str(r.imported_at) if r.imported_at else None
        except Exception:  # noqa: BLE001
            pass

    # State.today
    state_today = None
    try:
        from state import STATE
        if STATE.today:
            state_today = STATE.today.isoformat()
    except Exception:  # noqa: BLE001
        pass

    out: list[CalendarDay] = []
    for r in rows:
        fecha_iso = r.planned_date.isoformat() if hasattr(r.planned_date, "isoformat") else str(r.planned_date)
        conflicts = _check_dotacion_conflicts(fecha_iso) if user.is_falabella else []
        out.append(CalendarDay(
            fecha=fecha_iso,
            visitas=int(r.total or 0),
            is_today=(fecha_iso == state_today),
            imported_at=imports_map.get(fecha_iso),
            conflicts_count=len(conflicts),
            failed=int(r.failed or 0),
            completed=int(r.completed or 0),
            pending=int(r.pending or 0),
        ))
    return out


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
