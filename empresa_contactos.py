"""Empresas transportistas + contactos destinatarios de WhatsApp.

Separa los conceptos de "user con login" y "destinatario de notificación":
- `fpoc_users` → cuentas con email+password que entran al panel.
- `fpoc_empresa_contactos` → personas (jefe / coordinador / dispatcher / driver)
  que reciben WhatsApp cuando hay alertas; no tienen acceso al panel.

Endpoints (prefijo /api/empresa-contactos para evitar pisar /api/empresas que ya
existe en auth.py — el router de listado de empresas con summary se monta como
GET /api/empresa-contactos/empresas).

  GET    /api/empresa-contactos/empresas
  GET    /api/empresa-contactos/empresas/{empresa_id}/contactos
  POST   /api/empresa-contactos/empresas/{empresa_id}/contactos
  PUT    /api/empresa-contactos/empresas/{empresa_id}/contactos/{contact_id}
  DELETE /api/empresa-contactos/empresas/{empresa_id}/contactos/{contact_id}
  POST   /api/empresa-contactos/empresas/{empresa_id}/contactos/{contact_id}/opt-in
  GET    /api/empresa-contactos/empresas/{empresa_id}/contactos/csv-template
  POST   /api/empresa-contactos/empresas/{empresa_id}/contactos/bulk-csv
  POST   /api/empresa-contactos/empresas/{empresa_id}/test-broadcast
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from loguru import logger
from pydantic import BaseModel, Field

from auth import CurrentUser, current_user
from db import get_conn
from schemas import (
    BulkCSVResult,
    ContactoCreate,
    ContactoOut,
    ContactoUpdate,
    EmpresaSummary,
    TestBroadcastResult,
    TestBroadcastRow,
)


router = APIRouter(prefix="/api/empresa-contactos", tags=["empresa-contactos"])


ALLOWED_ROLES = {"jefe", "coordinador", "dispatcher", "otro"}
# 'driver' fue removido: los drivers viven en fpoc.drivers con su propio phone.
# Los contactos de empresa son SOLO no-drivers. Filas legacy con rol='driver'
# fueron migradas a 'otro' en la migración 012.
ALLOWED_REGIONS = {"RM", "regiones", "all"}
ALLOWED_SEVERITIES = {"low", "medium", "high", "critical"}
PHONE_RE = re.compile(r"^\+\d{8,15}$")


CSV_HEADERS = ["nombre", "rol", "phone_e164", "email", "severities", "motivos", "region"]
CSV_TEMPLATE_BODY = (
    ",".join(CSV_HEADERS)
    + "\n"
    + "Jorge Cordero,jefe,+56939568904,jorge@valuedata.cl,critical;high;medium,,all\n"
)


# ============================================================================
# Helpers
# ============================================================================
def _require_admin_or_ops(user: CurrentUser) -> None:
    if user.role not in ("falabella_admin", "falabella_ops"):
        raise HTTPException(403, "requiere rol falabella_admin o falabella_ops")


def _validate_phone(phone: str) -> str:
    p = (phone or "").strip()
    if not PHONE_RE.match(p):
        raise HTTPException(400, f"phone_e164 inválido: {phone!r} (esperado: +<8-15 dígitos>)")
    return p


def _validate_rol(rol: str) -> str:
    if rol not in ALLOWED_ROLES:
        raise HTTPException(400, f"rol inválido: {rol!r} (allowed: {sorted(ALLOWED_ROLES)})")
    return rol


def _validate_region(region: str) -> str:
    if region not in ALLOWED_REGIONS:
        raise HTTPException(400, f"region_filter inválido: {region!r} (allowed: {sorted(ALLOWED_REGIONS)})")
    return region


def _normalize_severities(values: Optional[list[str]]) -> Optional[list[str]]:
    if values is None:
        return None
    cleaned = [v.strip().lower() for v in values if v and v.strip()]
    if not cleaned:
        return None
    bad = [v for v in cleaned if v not in ALLOWED_SEVERITIES]
    if bad:
        raise HTTPException(400, f"severities inválidas: {bad}")
    return cleaned


def _normalize_motivos(values: Optional[list[str]]) -> Optional[list[str]]:
    if values is None:
        return None
    cleaned = [v.strip() for v in values if v and v.strip()]
    if not cleaned:
        return None
    return cleaned


def _row_to_contacto(r) -> ContactoOut:
    severities = json.loads(r.severities_in) if getattr(r, "severities_in", None) else None
    motivos = json.loads(r.motivos_in) if getattr(r, "motivos_in", None) else None
    opt_in = r.opted_in_at
    if hasattr(opt_in, "isoformat"):
        opt_in = opt_in.isoformat()
    created_at = r.created_at
    if hasattr(created_at, "isoformat"):
        created_at = created_at.isoformat()
    updated_at = r.updated_at
    if hasattr(updated_at, "isoformat"):
        updated_at = updated_at.isoformat()
    return ContactoOut(
        contact_id=int(r.contact_id),
        empresa_id=int(r.empresa_id),
        nombre=r.nombre,
        rol=r.rol,
        phone_e164=r.phone_e164,
        email=r.email,
        severities_in=severities,
        motivos_in=motivos,
        region_filter=r.region_filter,
        opted_in_at=opt_in,
        active=bool(r.active),
        notes=r.notes,
        created_by_user_id=int(r.created_by_user_id) if r.created_by_user_id is not None else None,
        created_at=created_at,
        updated_at=updated_at,
    )


def _ensure_empresa_exists(cn, empresa_id: int) -> str:
    cur = cn.cursor()
    cur.execute(
        "SELECT empresa_id, nombre FROM fpoc_empresas_transporte WHERE empresa_id = ?",
        empresa_id,
    )
    r = cur.fetchone()
    if not r:
        raise HTTPException(404, f"empresa {empresa_id} no existe")
    return r.nombre


def _scope_empresa(user: CurrentUser, empresa_id: int) -> None:
    """transport_manager solo puede ver/editar contactos de su empresa."""
    if user.role == "transport_manager" and user.empresa_id != empresa_id:
        raise HTTPException(403, "fuera de scope: no es tu empresa")


# ============================================================================
# Listados
# ============================================================================
@router.get("/empresas", response_model=list[EmpresaSummary])
def list_empresas_with_summary(user: CurrentUser = Depends(current_user)) -> list[EmpresaSummary]:
    """Empresas con summary de contactos y última alerta."""
    out: list[EmpresaSummary] = []
    with get_conn() as cn:
        cur = cn.cursor()
        if user.is_falabella:
            cur.execute(
                "SELECT empresa_id, nombre, activo, central_phone FROM fpoc_empresas_transporte ORDER BY empresa_id"
            )
        else:
            cur.execute(
                "SELECT empresa_id, nombre, activo, central_phone FROM fpoc_empresas_transporte WHERE empresa_id = ?",
                user.empresa_id,
            )
        empresas = cur.fetchall()

        for e in empresas:
            eid = int(e.empresa_id)
            cur.execute(
                """
                SELECT
                  SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) AS total,
                  SUM(CASE WHEN active=1 AND opted_in_at IS NOT NULL THEN 1 ELSE 0 END) AS optin
                FROM fpoc_empresa_contactos WHERE empresa_id = ?
                """,
                eid,
            )
            r = cur.fetchone()
            total = int(r.total or 0) if r else 0
            optin = int(r.optin or 0) if r else 0

            # Última alerta enviada (notifications_log para esta empresa: via
            # contact_id o via user de la empresa)
            cur.execute(
                """
                SELECT MAX(l.created_at) AS last_at
                FROM fpoc_notifications_log l
                LEFT JOIN fpoc_empresa_contactos c ON c.contact_id = l.contact_id
                LEFT JOIN fpoc_users u ON u.user_id = l.user_id
                WHERE (c.empresa_id = ? OR u.empresa_id = ?)
                """,
                eid, eid,
            )
            last_r = cur.fetchone()
            last_at = last_r.last_at if last_r else None
            if hasattr(last_at, "isoformat"):
                last_at = last_at.isoformat()

            out.append(EmpresaSummary(
                empresa_id=eid,
                nombre=e.nombre,
                activo=bool(e.activo),
                central_phone=getattr(e, "central_phone", None),
                contactos_count=total,
                opted_in_count=optin,
                last_alert_at=last_at,
            ))
    return out


@router.get("/empresas/{empresa_id}/contactos", response_model=list[ContactoOut])
def list_contactos(empresa_id: int, user: CurrentUser = Depends(current_user)) -> list[ContactoOut]:
    _scope_empresa(user, empresa_id)
    with get_conn() as cn:
        _ensure_empresa_exists(cn, empresa_id)
        cur = cn.cursor()
        cur.execute(
            """
            SELECT contact_id, empresa_id, nombre, rol, phone_e164, email,
                   severities_in, motivos_in, region_filter, opted_in_at,
                   active, notes, created_by_user_id, created_at, updated_at
            FROM fpoc_empresa_contactos
            WHERE empresa_id = ? AND active = 1
            ORDER BY rol, nombre
            """,
            empresa_id,
        )
        rows = cur.fetchall()
    return [_row_to_contacto(r) for r in rows]


# ============================================================================
# CRUD
# ============================================================================
@router.post("/empresas/{empresa_id}/contactos", response_model=ContactoOut)
def create_contacto(
    empresa_id: int,
    req: ContactoCreate,
    user: CurrentUser = Depends(current_user),
) -> ContactoOut:
    _require_admin_or_ops(user)
    rol = _validate_rol(req.rol)
    phone = _validate_phone(req.phone_e164)
    region = _validate_region(req.region_filter or "all")
    severities = _normalize_severities(req.severities_in)
    motivos = _normalize_motivos(req.motivos_in)

    with get_conn() as cn:
        _ensure_empresa_exists(cn, empresa_id)
        cur = cn.cursor()
        # Duplicados: mismo phone+empresa activo
        cur.execute(
            """
            SELECT contact_id FROM fpoc_empresa_contactos
            WHERE empresa_id = ? AND phone_e164 = ? AND active = 1
            """,
            empresa_id, phone,
        )
        if cur.fetchone() is not None:
            raise HTTPException(409, f"ya existe un contacto activo con ese phone en empresa {empresa_id}")

        cur.execute(
            """
            INSERT INTO fpoc_empresa_contactos
              (empresa_id, nombre, rol, phone_e164, email,
               severities_in, motivos_in, region_filter,
               opted_in_at, active, notes, created_by_user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, ?)
            """,
            empresa_id, req.nombre.strip(), rol, phone, (req.email or None),
            json.dumps(severities) if severities else None,
            json.dumps(motivos) if motivos else None,
            region, (req.notes or None),
            user.user_id,
        )
        cn.commit()
        cur.execute(
            """
            SELECT contact_id, empresa_id, nombre, rol, phone_e164, email,
                   severities_in, motivos_in, region_filter, opted_in_at,
                   active, notes, created_by_user_id, created_at, updated_at
            FROM fpoc_empresa_contactos
            WHERE contact_id = last_insert_rowid()
            """
        )
        r = cur.fetchone()
    return _row_to_contacto(r)


@router.put("/empresas/{empresa_id}/contactos/{contact_id}", response_model=ContactoOut)
def update_contacto(
    empresa_id: int,
    contact_id: int,
    req: ContactoUpdate,
    user: CurrentUser = Depends(current_user),
) -> ContactoOut:
    _require_admin_or_ops(user)

    sets: list[str] = []
    params: list = []
    if req.nombre is not None:
        sets.append("nombre = ?"); params.append(req.nombre.strip())
    if req.rol is not None:
        sets.append("rol = ?"); params.append(_validate_rol(req.rol))
    if req.phone_e164 is not None:
        sets.append("phone_e164 = ?"); params.append(_validate_phone(req.phone_e164))
    if req.email is not None:
        sets.append("email = ?"); params.append(req.email or None)
    if req.severities_in is not None:
        sev = _normalize_severities(req.severities_in)
        sets.append("severities_in = ?"); params.append(json.dumps(sev) if sev else None)
    if req.motivos_in is not None:
        mot = _normalize_motivos(req.motivos_in)
        sets.append("motivos_in = ?"); params.append(json.dumps(mot) if mot else None)
    if req.region_filter is not None:
        sets.append("region_filter = ?"); params.append(_validate_region(req.region_filter))
    if req.notes is not None:
        sets.append("notes = ?"); params.append(req.notes or None)
    if req.active is not None:
        sets.append("active = ?"); params.append(1 if req.active else 0)

    if not sets:
        raise HTTPException(400, "nada que actualizar")

    sets.append("updated_at = CURRENT_TIMESTAMP")
    params.extend([contact_id, empresa_id])

    with get_conn() as cn:
        _ensure_empresa_exists(cn, empresa_id)
        cur = cn.cursor()
        cur.execute(
            f"UPDATE fpoc_empresa_contactos SET {', '.join(sets)} "
            "WHERE contact_id = ? AND empresa_id = ?",
            *params,
        )
        if cur.rowcount == 0:
            raise HTTPException(404, f"contact_id {contact_id} no encontrado en empresa {empresa_id}")
        cn.commit()
        cur.execute(
            """
            SELECT contact_id, empresa_id, nombre, rol, phone_e164, email,
                   severities_in, motivos_in, region_filter, opted_in_at,
                   active, notes, created_by_user_id, created_at, updated_at
            FROM fpoc_empresa_contactos WHERE contact_id = ?
            """,
            contact_id,
        )
        r = cur.fetchone()
    return _row_to_contacto(r)


@router.delete("/empresas/{empresa_id}/contactos/{contact_id}")
def delete_contacto(
    empresa_id: int,
    contact_id: int,
    user: CurrentUser = Depends(current_user),
) -> dict:
    """Soft delete: active=0. Mantiene la fila para auditoría histórica de logs."""
    _require_admin_or_ops(user)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "UPDATE fpoc_empresa_contactos SET active = 0, updated_at = CURRENT_TIMESTAMP "
            "WHERE contact_id = ? AND empresa_id = ?",
            contact_id, empresa_id,
        )
        if cur.rowcount == 0:
            raise HTTPException(404, f"contact_id {contact_id} no encontrado en empresa {empresa_id}")
        cn.commit()
    return {"deleted": contact_id}


@router.post("/empresas/{empresa_id}/contactos/{contact_id}/opt-in", response_model=ContactoOut)
def mark_opt_in(
    empresa_id: int,
    contact_id: int,
    user: CurrentUser = Depends(current_user),
) -> ContactoOut:
    """Marca el contacto como opt-in (firmó ToS WhatsApp / hizo join al sandbox)."""
    _require_admin_or_ops(user)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "UPDATE fpoc_empresa_contactos SET opted_in_at = CURRENT_TIMESTAMP, "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE contact_id = ? AND empresa_id = ?",
            contact_id, empresa_id,
        )
        if cur.rowcount == 0:
            raise HTTPException(404, f"contact_id {contact_id} no encontrado en empresa {empresa_id}")
        cn.commit()
        cur.execute(
            """
            SELECT contact_id, empresa_id, nombre, rol, phone_e164, email,
                   severities_in, motivos_in, region_filter, opted_in_at,
                   active, notes, created_by_user_id, created_at, updated_at
            FROM fpoc_empresa_contactos WHERE contact_id = ?
            """,
            contact_id,
        )
        r = cur.fetchone()
    return _row_to_contacto(r)


# ============================================================================
# CSV: template + bulk import
# ============================================================================
@router.get("/empresas/{empresa_id}/contactos/csv-template")
def csv_template(empresa_id: int, _: CurrentUser = Depends(current_user)):
    """Descarga el template CSV (mismo para todas las empresas)."""
    return PlainTextResponse(
        CSV_TEMPLATE_BODY,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="contactos_empresa_{empresa_id}_template.csv"'
        },
    )


@router.post("/empresas/{empresa_id}/contactos/bulk-csv", response_model=BulkCSVResult)
async def bulk_csv(
    empresa_id: int,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(current_user),
) -> BulkCSVResult:
    """Importa N contactos desde un CSV. Tolerante a errores fila por fila."""
    _require_admin_or_ops(user)

    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    fields = [(f or "").strip().lower() for f in (reader.fieldnames or [])]
    missing = [h for h in ("nombre", "rol", "phone_e164") if h not in fields]
    if missing:
        raise HTTPException(400, f"CSV mal formado: faltan columnas requeridas {missing}")

    added = 0
    skipped: list[dict] = []
    errors: list[dict] = []

    with get_conn() as cn:
        _ensure_empresa_exists(cn, empresa_id)
        cur = cn.cursor()
        # Phones ya existentes para esta empresa (activos) → para detectar duplicados
        cur.execute(
            "SELECT phone_e164 FROM fpoc_empresa_contactos WHERE empresa_id = ? AND active = 1",
            empresa_id,
        )
        existing_phones = {r.phone_e164 for r in cur.fetchall()}

        # Iteramos manualmente para tener row_number consistente
        for idx, raw_row in enumerate(reader, start=2):  # 1 = header
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw_row.items()}
            try:
                nombre = row.get("nombre", "")
                rol = row.get("rol", "")
                phone = row.get("phone_e164", "")
                if not nombre:
                    raise ValueError("nombre vacío")
                if rol not in ALLOWED_ROLES:
                    raise ValueError(f"rol inválido: {rol!r}")
                if not PHONE_RE.match(phone):
                    raise ValueError(f"phone_e164 inválido: {phone!r}")

                email = row.get("email") or None
                sev_raw = row.get("severities") or ""
                mot_raw = row.get("motivos") or ""
                region = (row.get("region") or "all") or "all"
                if region not in ALLOWED_REGIONS:
                    raise ValueError(f"region inválida: {region!r}")

                severities = [s.strip().lower() for s in sev_raw.split(";") if s.strip()] or None
                if severities is not None:
                    bad = [s for s in severities if s not in ALLOWED_SEVERITIES]
                    if bad:
                        raise ValueError(f"severities inválidas: {bad}")
                motivos = [m.strip() for m in mot_raw.split(";") if m.strip()] or None

                if phone in existing_phones:
                    skipped.append({"row": idx, "reason": f"duplicado phone={phone}"})
                    continue

                cur.execute(
                    """
                    INSERT INTO fpoc_empresa_contactos
                      (empresa_id, nombre, rol, phone_e164, email,
                       severities_in, motivos_in, region_filter,
                       opted_in_at, active, notes, created_by_user_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, ?)
                    """,
                    empresa_id, nombre, rol, phone, email,
                    json.dumps(severities) if severities else None,
                    json.dumps(motivos) if motivos else None,
                    region, "Importado vía CSV",
                    user.user_id,
                )
                existing_phones.add(phone)
                added += 1
            except ValueError as e:
                errors.append({"row": idx, "reason": str(e)})
            except Exception as e:  # noqa: BLE001
                errors.append({"row": idx, "reason": f"error inesperado: {e}"})
        cn.commit()

    return BulkCSVResult(added=added, skipped=skipped, errors=errors)


# ============================================================================
# Test broadcast
# ============================================================================
@router.post("/empresas/{empresa_id}/test-broadcast", response_model=TestBroadcastResult)
def test_broadcast(
    empresa_id: int,
    user: CurrentUser = Depends(current_user),
) -> TestBroadcastResult:
    """Envía mensaje de prueba a todos los contactos `active=1 AND opted_in_at IS NOT NULL`."""
    _require_admin_or_ops(user)

    with get_conn() as cn:
        empresa_nombre = _ensure_empresa_exists(cn, empresa_id)
        cur = cn.cursor()
        cur.execute(
            """
            SELECT contact_id, phone_e164, nombre
            FROM fpoc_empresa_contactos
            WHERE empresa_id = ? AND active = 1 AND opted_in_at IS NOT NULL
            """,
            empresa_id,
        )
        targets = [
            {"contact_id": int(r.contact_id), "phone": r.phone_e164, "nombre": r.nombre}
            for r in cur.fetchall()
        ]

    body = (
        f"🔔 Mensaje de prueba ValueData × Falabella · {empresa_nombre} · "
        "si recibís esto, las alertas funcionan."
    )

    rows: list[TestBroadcastRow] = []
    if not targets:
        return TestBroadcastResult(empresa_id=empresa_id, body=body, results=rows, sent=0, failed=0)

    # Reusamos la pipeline de notifications.send_whatsapp pero como esta función
    # no soporta contact_id en el log directamente, hacemos el dispatch nosotros
    # para poder loguear `contact_id` en `fpoc_notifications_log`.
    try:
        from notifications import TwilioConfig, _send_one, _twilio_client  # type: ignore
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[test-broadcast] no pude importar notifications: {e}")
        raise HTTPException(500, "infra de notificaciones no disponible")

    cfg = TwilioConfig.from_env()
    client = None
    if cfg.enabled and not cfg.dry_run:
        try:
            client, _ = _twilio_client()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[test-broadcast] Twilio init falló, cayendo a dry-run: {e}")
            cfg.dry_run = True

    sent = 0
    failed = 0
    with get_conn() as cn:
        for t in targets:
            if not cfg.enabled:
                status, sid, err = "disabled", None, None
            elif cfg.dry_run:
                status, sid, err = "dry_run", None, None
            else:
                status, sid, err = _send_one(client, cfg, t["phone"], body=body, content_sid=None, content_variables=None)

            # Log con contact_id (no user_id)
            cur = cn.cursor()
            cur.execute(
                """
                INSERT INTO fpoc_notifications_log
                  (user_id, contact_id, to_number, channel, subject, body,
                   tracking_id, twilio_sid, status, error_msg, triggered_by,
                   content_sid, content_variables)
                VALUES (NULL, ?, ?, 'whatsapp', ?, ?, NULL, ?, ?, ?, 'test_broadcast', NULL, NULL)
                """,
                t["contact_id"], t["phone"],
                f"Test broadcast {empresa_nombre}",
                body, sid, status, err,
            )
            cn.commit()

            if status == "sent":
                sent += 1
            elif status == "error":
                failed += 1

            rows.append(TestBroadcastRow(
                contact_id=t["contact_id"],
                nombre=t["nombre"],
                phone=t["phone"],
                status=status,
                twilio_sid=sid,
                error=err,
            ))

    return TestBroadcastResult(
        empresa_id=empresa_id, body=body, results=rows,
        sent=sent, failed=failed,
    )
