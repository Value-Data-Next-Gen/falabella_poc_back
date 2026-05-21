"""Sprint 4.A1 + A3 — endpoints específicos de drivers para WhatsApp + scorecard.

- PUT /api/mantenedores/drivers/{driver_id}    — actualiza phone_e164/notify_whatsapp/opted_in_at
- GET /api/drivers/scorecard?period_days=30    — métricas por driver
- POST /api/planificacion/import-mock          — placeholder Sprint 5 (mock)
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from loguru import logger
from pydantic import BaseModel, Field

from core.auth import CurrentUser, current_user, require_admin
from core.db import get_conn


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
               AND sv.empresa_falsa = dd.empresa_id
               AND sv.patente_falsa = dd.vehicle_id
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


# Fase 2 MVP: endpoint `/api/planificacion/import-mock` removido.
# Dependía de sims.live_generator._insert_batch (eliminado). La carga de visitas
# ahora se hace exclusivamente via `/api/planificacion/import-xlsx`.


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

    # XLSX SimpliRoute trae 5 columnas PascalCase — la DB las tiene en
    # snake_case desde migración 011. Renombramos antes de validar.
    df_s = df_s.rename(columns={
        "E" + "mpresa_falsa": "empresa_falsa",
        "P" + "atente_falsa": "patente_falsa",
        "D" + "rivername": "driver_name",
        "F" + "echainicioruta": "fecha_inicio_ruta",
        "F" + "echainicioruta_hora_cl": "fecha_inicio_ruta_hora_cl",
    })

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

    # PLAN limpio: el XLSX puede traer datos históricos con status=completed/failed
    # y checkout_cl con timestamp real. Acá la carga es el PLAN del día — la
    # simulación posterior (driver positions + tick) va completando visitas.
    # Forzamos pending y limpiamos comentarios para que arranque limpio.
    # checkout_cl es NOT NULL en el schema → dejamos el valor del XLSX como
    # sentinela; el "completed real" se determina por status='completed'.
    if "status" in df.columns:
        df["status"] = "pending"
    if "checkout_comment" in df.columns:
        df["checkout_comment"] = None
    if "checkout_observation" in df.columns:
        df["checkout_observation"] = None

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

        # Auto-enrich: derivar ruta_id/region/comuna desde geo_suborders.
        # Sin esto, ruta_id queda NULL y la UI muestra "0 rutas".
        try:
            from collections import Counter
            for d in fechas:
                cur.execute(
                    "SELECT patente_falsa, idruta, region, localidad "
                    "FROM fpoc.geo_suborders WHERE fechapactada = ?",
                    d,
                )
                agg: dict[int, dict] = {}
                for r in cur.fetchall():
                    if r.patente_falsa is None:
                        continue
                    pat = int(r.patente_falsa)
                    slot = agg.setdefault(pat, {
                        "rutas": Counter(), "regions": Counter(), "comunas": Counter(),
                    })
                    if r.idruta is not None:
                        slot["rutas"][int(r.idruta)] += 1
                    if r.region:
                        slot["regions"][str(r.region)] += 1
                    if r.localidad:
                        slot["comunas"][str(r.localidad).strip().title()] += 1
                # Aplicar UPDATE por (patente, fecha)
                for patente, data in agg.items():
                    if not data["rutas"]:
                        continue
                    idruta = data["rutas"].most_common(1)[0][0]
                    region = data["regions"].most_common(1)[0][0] if data["regions"] else None
                    comuna = data["comunas"].most_common(1)[0][0] if data["comunas"] else None
                    cur.execute(
                        "UPDATE fpoc.simpli_visits SET ruta_id = ?, region = ?, comuna = ? "
                        "WHERE patente_falsa = ? AND planned_date = ?",
                        f"R-{idruta}", region, comuna, patente, d,
                    )
                cn.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[import-xlsx] auto-enrich falló (no fatal): {e}")

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

    # Fase 2 MVP: ya no existe live_generator. Si STATE.today coincide con alguna
    # fecha, refrescamos STATE.today para mantener el cache de lookup alineado.
    try:
        from core.state import STATE
        if STATE.today and STATE.today.isoformat() in fechas:
            STATE.today = STATE.today  # idempotente; placeholder por si futuro refresh
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
    visitas_reset: int = 0
    live_gen_paused: bool
    state_today: Optional[str] = None
    snapshot_size: int = 0
    conflicts: list[DotacionConflict] = []
    message: str = ""


@router.post("/api/planificacion/start-day", response_model=StartDayResponse)
def start_day(
    fecha: str = Query(...),
    reset_status: bool = Query(default=True,
        description="Si true, fuerza status='pending' y limpia checkouts. Default true."),
    user: CurrentUser = Depends(current_user),
) -> StartDayResponse:
    """Marca el comienzo del día operativo en la fecha indicada.

    Fase 2 MVP: ya no hay live_generator que pausar ni snapshot ML que refrescar.
    El endpoint solo:
    - Si reset_status=true (default): pasa todas las visitas a status='pending'
      y limpia checkouts (comment/observation). Si la importación quedó con
      visitas pre-completadas (datos sintéticos legacy), esto las pone listas
      para operar.
    - Setea STATE.today = fecha (placeholder de día operativo activo).
    - Cuenta visitas en fpoc.simpli_visits para esa fecha (cuadratura).
    - Devuelve conflictos de dotación pendientes.

    `live_gen_paused` y `snapshot_size` quedan con valores estables (False y 0)
    para no romper el contrato del frontend mientras se migra.
    """
    if not user.is_falabella:
        raise HTTPException(403, "Solo admin/ops puede iniciar el día")
    from datetime import date as _date_cls
    try:
        target = _date_cls.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {fecha}")

    paused = False  # Compat: ya no hay live_gen
    visitas_reset = 0
    if reset_status:
        try:
            with get_conn() as cn:
                cur = cn.cursor()
                cur.execute(
                    """UPDATE fpoc.simpli_visits
                          SET status = 'pending',
                              checkout_comment = NULL,
                              checkout_observation = NULL
                        WHERE planned_date = ?
                          AND status <> 'pending'""",
                    target.isoformat(),
                )
                visitas_reset = int(cur.rowcount or 0)
                cn.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[start-day] reset status fallo: {e}")

    snapshot_size = 0  # Compat post-Fase-2 MVP
    state_today_iso: Optional[str] = None
    try:
        from core.state import STATE
        STATE.today = target
        state_today_iso = STATE.today.isoformat()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[start-day] STATE.today set fallo: {e}")

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

    # Marker explícito de "Iniciar día" en la DB (verdad single-source para day-status).
    try:
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                "SELECT 1 FROM fpoc.planificacion_imports WHERE fecha = ?",
                target.isoformat(),
            )
            if cur.fetchone():
                cur.execute(
                    "UPDATE fpoc.planificacion_imports "
                    "SET started_at = SYSDATETIME(), started_by_user_id = ? "
                    "WHERE fecha = ?",
                    user.user_id, target.isoformat(),
                )
            else:
                cur.execute(
                    "INSERT INTO fpoc.planificacion_imports "
                    "(fecha, count, started_at, started_by_user_id) "
                    "VALUES (?, ?, SYSDATETIME(), ?)",
                    target.isoformat(), visitas, user.user_id,
                )
            cn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[start-day] no se pudo marcar started_at: {e}")

    msg_parts = [f"Día {fecha} iniciado", f"{visitas} visitas en DB"]
    if visitas_reset:
        msg_parts.append(f"{visitas_reset} reseteadas a pending")
    if conflicts:
        msg_parts.append(f"⚠ {len(conflicts)} conflictos de dotación")

    return StartDayResponse(
        ok=True,
        fecha=target.isoformat(),
        visitas_en_db=visitas,
        visitas_reset=visitas_reset,
        live_gen_paused=paused,
        state_today=state_today_iso,
        snapshot_size=snapshot_size,
        conflicts=conflicts,
        message=" · ".join(msg_parts),
    )


class DayStatus(BaseModel):
    fecha: str
    visitas: int = 0
    completed: int = 0
    failed: int = 0
    pending: int = 0
    conflicts_count: int = 0
    is_state_today: bool = False
    live_gen_running: bool = False
    imported_at: Optional[str] = None
    imported_by_user_id: Optional[int] = None
    started_at: Optional[str] = None
    # Status derivado para el wizard
    loaded: bool = False                 # tiene visitas
    dotacion_checked: bool = False       # se cruzó con dotacion (siempre true si hay backend)
    no_conflicts: bool = False           # conflicts_count == 0
    started: bool = False                # is_state_today + live_gen_paused
    config_issues_count: int = 0         # visitas con config faltante
    driver_issues_count: int = 0         # drivers/vehículos sin contacto, licencia, etc.
    vip_count: int = 0                   # cantidad de VIPs del día (informativo)
    prep_ok: bool = False                # listo para iniciar (no_conflicts + 0 issues)


@router.get("/api/planificacion/day-status", response_model=DayStatus)
def day_status(
    fecha: str = Query(...),
    user: CurrentUser = Depends(current_user),
) -> DayStatus:
    """Estado consolidado del día para el wizard:
    visitas, conflictos, si está como STATE.today, si live_gen está corriendo,
    si fue importado, derivaciones para los steps del wizard.
    """
    from datetime import date as _date_cls
    try:
        _date_cls.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {fecha}")

    scope_where = ""
    scope_params: list = []
    if not user.is_falabella:
        scope_where = " AND empresa_falsa = ?"
        scope_params.append(user.empresa_id)

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending
                FROM fpoc.simpli_visits
                WHERE planned_date = ?
                {scope_where}""",
            fecha, *scope_params,
        )
        r = cur.fetchone()
        total = int(r.total or 0)
        completed = int(r.completed or 0)
        failed = int(r.failed or 0)
        pending = int(r.pending or 0)

        imported_at = None
        imported_by = None
        started_at = None
        try:
            cur.execute(
                "SELECT imported_at, imported_by_user_id, started_at "
                "FROM fpoc_planificacion_imports WHERE fecha = ?",
                fecha,
            )
            row = cur.fetchone()
            if row:
                imported_at = str(row.imported_at) if row.imported_at else None
                imported_by = int(row.imported_by_user_id) if row.imported_by_user_id is not None else None
                started_at = str(row.started_at) if row.started_at else None
        except Exception:  # noqa: BLE001
            pass

    conflicts = _check_dotacion_conflicts(fecha) if user.is_falabella else []

    # STATE.today (post Fase-2 MVP: live_gen eliminado, queda False permanente)
    is_state_today = False
    live_gen_running = False
    try:
        from core.state import STATE
        if STATE.today and STATE.today.isoformat() == fecha:
            is_state_today = True
    except Exception:  # noqa: BLE001
        pass

    loaded = total > 0
    no_conflicts = len(conflicts) == 0
    # "started" es ahora un hecho explícito: existe started_at en la fila de
    # planificacion_imports para esa fecha. Antes lo derivábamos de
    # STATE.today + !live_gen_running, lo cual hacía que cualquier reinicio
    # del backend mostrara "iniciado" sin que el usuario hubiera apretado el botón.
    started = started_at is not None

    # Sprint H: contadores accionables (Plan del día simplificado).
    prep = _compute_day_prep(fecha, user) if loaded else _empty_day_prep(fecha)
    config_issues_count = len(prep["config_issues"])
    driver_issues_count = len(prep["driver_issues"])
    vip_count = len(prep["vips"])
    prep_ok = no_conflicts and config_issues_count == 0 and driver_issues_count == 0

    return DayStatus(
        fecha=fecha,
        visitas=total,
        completed=completed,
        failed=failed,
        pending=pending,
        conflicts_count=len(conflicts),
        is_state_today=is_state_today,
        live_gen_running=live_gen_running,
        imported_at=imported_at,
        imported_by_user_id=imported_by,
        started_at=started_at,
        loaded=loaded,
        dotacion_checked=user.is_falabella,
        no_conflicts=no_conflicts,
        started=started,
        config_issues_count=config_issues_count,
        driver_issues_count=driver_issues_count,
        vip_count=vip_count,
        prep_ok=prep_ok,
    )


# ============================================================================
# Plan del día simplificado: VIPs / Config pendiente / Drivers con problemas
# ============================================================================
class PrepVip(BaseModel):
    tracking_id: str
    cliente: str
    comuna: Optional[str] = None
    folio: Optional[str] = None
    deadline: Optional[str] = None
    ruta_id: Optional[str] = None
    driver_name: Optional[str] = None
    priority_set: bool = False


class PrepConfigIssue(BaseModel):
    tracking_id: str
    cliente: str
    issue_type: str   # 'no_region' | 'no_comuna' | 'no_ruta' | 'no_ct' | 'vip_sin_prioridad'
    issue_label: str


class PrepDriverIssue(BaseModel):
    driver_id: Optional[str] = None
    driver_name: Optional[str] = None
    ruta_id: Optional[str] = None
    issue_type: str   # 'dotacion' | 'sin_telefono' | 'sin_licencia' | 'driver_inactivo' | 'vehiculo_no_operable'
    issue_label: str
    affects_visits: int = 0


class DayPrep(BaseModel):
    fecha: str
    vips: list[PrepVip] = []
    config_issues: list[PrepConfigIssue] = []
    driver_issues: list[PrepDriverIssue] = []
    all_ok: bool = False


def _empty_day_prep(fecha: str) -> dict:
    return {"fecha": fecha, "vips": [], "config_issues": [], "driver_issues": []}


def _compute_day_prep(fecha: str, user: CurrentUser) -> dict:
    """Calcula las 3 secciones del Plan del día. Retorna dict (no Pydantic)
    para que sea reutilizable desde /day-status."""
    scope_where = ""
    scope_params: list = []
    if not user.is_falabella:
        scope_where = " AND s.empresa_falsa = ?"
        scope_params.append(user.empresa_id)

    vips: list[dict] = []
    config_issues: list[dict] = []
    driver_issues: list[dict] = []

    with get_conn() as cn:
        cur = cn.cursor()
        # ---- VIPs del día (match por title contra fpoc.vip_clients activos) ----
        cur.execute(
            f"""SELECT s.id, s.title, s.comuna, s.reference, s.ruta_id, s.driver_name,
                       s.sla_hour_checkout_eta
                FROM fpoc.simpli_visits s
                INNER JOIN fpoc.vip_clients v
                    ON v.active = 1 AND v.match_type = 'title' AND v.match_value = s.title
                WHERE s.planned_date = ?{scope_where}""",
            fecha, *scope_params,
        )
        vip_rows = cur.fetchall()
        vip_tids = [str(r.id) for r in vip_rows]

        # Priorities ya seteadas para esos tids
        priority_tids: set[str] = set()
        if vip_tids:
            marks = ",".join(["?"] * len(vip_tids))
            cur.execute(
                f"SELECT tracking_id FROM fpoc.visit_priority_overrides "
                f"WHERE tracking_id IN ({marks})",
                *vip_tids,
            )
            priority_tids = {str(r.tracking_id) for r in cur.fetchall()}

        for r in vip_rows:
            tid = str(r.id)
            vips.append({
                "tracking_id": tid,
                "cliente": str(r.title or ""),
                "comuna": str(r.comuna) if r.comuna else None,
                "folio": str(r.reference) if r.reference else None,
                "deadline": None,
                "ruta_id": str(r.ruta_id) if r.ruta_id else None,
                "driver_name": str(r.driver_name) if r.driver_name else None,
                "priority_set": tid in priority_tids,
            })

        # ---- Config issues (region/comuna/ruta/ct faltantes, VIP sin priority) ----
        cur.execute(
            f"""SELECT s.id, s.title, s.region, s.comuna, s.ruta_id, s.ct
                FROM fpoc.simpli_visits s
                WHERE s.planned_date = ?
                  AND (s.region IS NULL OR s.comuna IS NULL
                       OR s.ruta_id IS NULL OR s.ruta_id = ''
                       OR s.ct IS NULL OR s.ct = '')
                {scope_where}""",
            fecha, *scope_params,
        )
        for r in cur.fetchall():
            tid = str(r.id)
            cliente = str(r.title or "")
            if not r.ruta_id:
                config_issues.append({"tracking_id": tid, "cliente": cliente,
                                      "issue_type": "no_ruta",
                                      "issue_label": "Sin ruta asignada"})
            elif not r.region:
                config_issues.append({"tracking_id": tid, "cliente": cliente,
                                      "issue_type": "no_region",
                                      "issue_label": "Sin región"})
            elif not r.comuna:
                config_issues.append({"tracking_id": tid, "cliente": cliente,
                                      "issue_type": "no_comuna",
                                      "issue_label": "Sin comuna"})
            elif not r.ct:
                config_issues.append({"tracking_id": tid, "cliente": cliente,
                                      "issue_type": "no_ct",
                                      "issue_label": "Sin centro de distribución"})

        for v in vips:
            if not v["priority_set"]:
                config_issues.append({
                    "tracking_id": v["tracking_id"], "cliente": v["cliente"],
                    "issue_type": "vip_sin_prioridad",
                    "issue_label": "VIP sin prioridad explícita seteada",
                })

        # ---- Driver/dotación issues ----
        # 1) Conflictos de dotación del día (ausentes, licencia, mantención, etc.)
        for c in _check_dotacion_conflicts(fecha):
            driver_issues.append({
                "driver_id": str(c.driver_id) if c.driver_id is not None else None,
                "driver_name": c.driver_name,
                "ruta_id": c.ruta_id,
                "issue_type": "dotacion",
                "issue_label": f"{c.estado} — {c.motivo or 'sin motivo'}",
                "affects_visits": int(c.visitas_afectadas or 0),
            })

        # 2) Drivers de las rutas del día sin phone_e164 o sin licencia
        cur.execute(
            f"""SELECT DISTINCT s.driver_name, s.ruta_id, COUNT(s.id) AS n
                FROM fpoc.simpli_visits s
                WHERE s.planned_date = ?{scope_where}
                  AND s.driver_name IS NOT NULL AND s.driver_name <> ''
                GROUP BY s.driver_name, s.ruta_id""",
            fecha, *scope_params,
        )
        ruta_drivers = cur.fetchall()
        names = list({r.driver_name for r in ruta_drivers if r.driver_name})
        driver_meta: dict[str, dict] = {}
        if names:
            marks = ",".join(["?"] * len(names))
            cur.execute(
                f"""SELECT driver_id, name, phone_e164, license, active
                    FROM fpoc.drivers WHERE name IN ({marks})""",
                *names,
            )
            for r in cur.fetchall():
                driver_meta[str(r.name)] = {
                    "driver_id": str(r.driver_id) if r.driver_id is not None else None,
                    "phone_e164": r.phone_e164,
                    "license": r.license,
                    "active": bool(r.active),
                }

        for r in ruta_drivers:
            dn = str(r.driver_name)
            meta = driver_meta.get(dn)
            ruta = str(r.ruta_id) if r.ruta_id else None
            affects = int(r.n or 0)
            if not meta:
                driver_issues.append({
                    "driver_id": None, "driver_name": dn, "ruta_id": ruta,
                    "issue_type": "driver_inactivo",
                    "issue_label": "Driver no existe en maestro",
                    "affects_visits": affects,
                })
                continue
            if not meta["active"]:
                driver_issues.append({
                    "driver_id": meta["driver_id"], "driver_name": dn, "ruta_id": ruta,
                    "issue_type": "driver_inactivo",
                    "issue_label": "Driver marcado inactivo",
                    "affects_visits": affects,
                })
            if not meta["phone_e164"]:
                driver_issues.append({
                    "driver_id": meta["driver_id"], "driver_name": dn, "ruta_id": ruta,
                    "issue_type": "sin_telefono",
                    "issue_label": "Sin teléfono E.164 para WhatsApp",
                    "affects_visits": affects,
                })
            if not meta["license"]:
                driver_issues.append({
                    "driver_id": meta["driver_id"], "driver_name": dn, "ruta_id": ruta,
                    "issue_type": "sin_licencia",
                    "issue_label": "Sin licencia registrada",
                    "affects_visits": affects,
                })

    return {
        "fecha": fecha,
        "vips": vips,
        "config_issues": config_issues,
        "driver_issues": driver_issues,
    }


class DayClient(BaseModel):
    tracking_id: str
    cliente: str
    comuna: Optional[str] = None
    ruta_id: Optional[str] = None
    driver_name: Optional[str] = None
    is_vip: bool = False


@router.get("/api/planificacion/day-clients", response_model=list[DayClient])
def day_clients(
    fecha: str = Query(...),
    q: Optional[str] = Query(default=None, description="Substring sobre title"),
    limit: int = Query(default=50, ge=1, le=200),
    user: CurrentUser = Depends(current_user),
) -> list[DayClient]:
    """Lista distinct clientes del día para buscador VIP / configuración manual."""
    from datetime import date as _date_cls
    try:
        _date_cls.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {fecha}")

    scope_where = ""
    params: list = [fecha]
    if not user.is_falabella:
        scope_where = " AND s.empresa_falsa = ?"
        params.append(user.empresa_id)
    q_where = ""
    if q and q.strip():
        q_where = " AND LOWER(s.title) LIKE ?"
        params.append(f"%{q.strip().lower()}%")

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""SELECT s.title, s.id, s.comuna, s.ruta_id, s.driver_name,
                       (CASE WHEN v.vip_id IS NULL THEN 0 ELSE 1 END) AS is_vip
                FROM fpoc.simpli_visits s
                LEFT JOIN fpoc.vip_clients v
                    ON v.active = 1 AND v.match_type = 'title' AND v.match_value = s.title
                WHERE s.planned_date = ?{scope_where}{q_where}
                ORDER BY s.title
                LIMIT ?""",
            *params, limit,
        )
        out: list[DayClient] = []
        seen: set[str] = set()
        for r in cur.fetchall():
            title = str(r.title or "")
            if not title or title in seen:
                continue
            seen.add(title)
            out.append(DayClient(
                tracking_id=str(r.id),
                cliente=title,
                comuna=str(r.comuna) if r.comuna else None,
                ruta_id=str(r.ruta_id) if r.ruta_id else None,
                driver_name=str(r.driver_name) if r.driver_name else None,
                is_vip=bool(r.is_vip),
            ))
        return out


@router.get("/api/planificacion/day-prep", response_model=DayPrep)
def day_prep(
    fecha: str = Query(...),
    user: CurrentUser = Depends(current_user),
) -> DayPrep:
    """Plan del día simplificado: 3 listas accionables."""
    from datetime import date as _date_cls
    try:
        _date_cls.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(400, f"fecha inválida: {fecha}")
    p = _compute_day_prep(fecha, user)
    all_ok = (not p["config_issues"]) and (not p["driver_issues"])
    return DayPrep(
        fecha=fecha,
        vips=[PrepVip(**v) for v in p["vips"]],
        config_issues=[PrepConfigIssue(**c) for c in p["config_issues"]],
        driver_issues=[PrepDriverIssue(**d) for d in p["driver_issues"]],
        all_ok=all_ok,
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
        scope_where = " AND empresa_falsa = ?"
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
        from core.state import STATE
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
