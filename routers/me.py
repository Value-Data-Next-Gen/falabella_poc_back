"""Endpoints scopeados al usuario logueado.

Pensado para el rol `driver`: el endpoint `/api/me/orders` devuelve solo las
visitas asignadas a SU vehículo en SU empresa para la fecha del plan vivo.
Los demás endpoints (documents, capacitaciones, profile) son atajos de los
mismos endpoints admin pero pre-filtrados por driver_id del usuario logueado.

Funciona también para transport_manager y falabella_admin (si tienen un
driver_id seteado, lo cual no es lo común). El guard `_require_driver_user`
fuerza que sea rol=driver con driver_id válido.
"""
from __future__ import annotations

from datetime import date as _date_cls
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.auth import CurrentUser, current_user
from core.db import get_conn

router = APIRouter(prefix="/api/me", tags=["me"])


def _require_driver_user(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    if user.role != "driver":
        raise HTTPException(403, "endpoint solo para usuarios con rol driver")
    if not user.driver_id:
        raise HTTPException(403, "tu cuenta no está asociada a un driver")
    return user


# ---------------- Profile ----------------
class DriverProfile(BaseModel):
    user_id: int
    driver_id: str
    name: str
    email: str
    phone: Optional[str] = None
    license: Optional[str] = None
    empresa_id: Optional[int] = None
    empresa_nombre: Optional[str] = None
    vehicle_id: Optional[int] = None
    vehicle_name: Optional[str] = None
    plate: Optional[str] = None
    active: bool


@router.get("/profile", response_model=DriverProfile)
def my_profile(user: CurrentUser = Depends(_require_driver_user)) -> DriverProfile:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """SELECT d.driver_id, d.name, d.phone, d.license, d.empresa_id,
                       e.nombre AS empresa_nombre,
                       d.vehicle_id, d.vehicle_name, v.plate, d.active
                FROM fpoc.drivers d
                LEFT JOIN fpoc.empresas_transporte e ON e.empresa_id = d.empresa_id
                LEFT JOIN fpoc.vehicles v ON v.vehicle_id = d.vehicle_id
                WHERE d.driver_id = ?""",
            user.driver_id,
        )
        r = cur.fetchone()
        if not r:
            raise HTTPException(404, "driver no encontrado")
        return DriverProfile(
            user_id=user.user_id,
            driver_id=str(r.driver_id),
            name=str(r.name),
            email=user.email,
            phone=r.phone,
            license=r.license,
            empresa_id=int(r.empresa_id) if r.empresa_id is not None else None,
            empresa_nombre=r.empresa_nombre,
            vehicle_id=int(r.vehicle_id) if r.vehicle_id is not None else None,
            vehicle_name=r.vehicle_name,
            plate=r.plate,
            active=bool(r.active),
        )


# ---------------- Orders (visitas asignadas) ----------------
class MyOrder(BaseModel):
    tracking_id: str
    order: int
    title: str
    address: str
    comuna: Optional[str] = None
    region: str
    status: str
    current_eta_cl: str
    current_eta_hhmm: str
    sla_hour: float
    ruta_id: Optional[str] = None
    reference: Optional[int] = None


@router.get("/orders", response_model=list[MyOrder])
def my_orders(
    fecha: Optional[str] = Query(default=None, description="YYYY-MM-DD; default = hoy"),
    user: CurrentUser = Depends(_require_driver_user),
) -> list[MyOrder]:
    target = _date_cls.fromisoformat(fecha) if fecha else _date_cls.today()
    with get_conn() as cn:
        cur = cn.cursor()
        # Resolver vehicle_id del driver
        cur.execute(
            "SELECT vehicle_id, empresa_id FROM fpoc.drivers WHERE driver_id = ?",
            user.driver_id,
        )
        d = cur.fetchone()
        if not d:
            raise HTTPException(404, "driver no encontrado")
        vehicle_id = int(d.vehicle_id) if d.vehicle_id is not None else None
        empresa_id = int(d.empresa_id) if d.empresa_id is not None else None
        if vehicle_id is None or empresa_id is None:
            return []

        cur.execute(
            """SELECT id, [order], title, address, comuna, region, status,
                       current_eta_cl, sla_hour_checkout_eta, ruta_id, reference
                FROM fpoc.simpli_visits
                WHERE planned_date = ?
                  AND empresa_falsa = ?
                  AND patente_falsa = ?
                ORDER BY [order]""",
            target.isoformat(), empresa_id, vehicle_id,
        )
        rows = cur.fetchall()

    out: list[MyOrder] = []
    for r in rows:
        eta = str(r.current_eta_cl) if r.current_eta_cl else ""
        hhmm = ""
        if eta:
            try:
                s = eta.replace(" UTC", "").replace("T", " ")
                hhmm = s.split(" ", 1)[1][:5] if " " in s else s[:5]
            except Exception:  # noqa: BLE001
                hhmm = ""
        out.append(MyOrder(
            tracking_id=str(r.id),
            order=int(getattr(r, "order")) if hasattr(r, "order") else int(r["order"]),
            title=str(r.title) if r.title else "",
            address=str(r.address) if r.address else "",
            comuna=r.comuna,
            region=str(r.region) if r.region else "RM",
            status=str(r.status) if r.status else "pending",
            current_eta_cl=eta,
            current_eta_hhmm=hhmm,
            sla_hour=float(r.sla_hour_checkout_eta) if r.sla_hour_checkout_eta is not None else 0.0,
            ruta_id=str(r.ruta_id) if r.ruta_id else None,
            reference=int(r.reference) if r.reference is not None else None,
        ))
    return out


# ---------------- Documents (read + upload propios) ----------------
class MyDocument(BaseModel):
    doc_id: int
    tipo: str
    filename: str
    file_size: int
    uploaded_at: str
    expires_at: Optional[str] = None
    notes: Optional[str] = None


@router.get("/documents", response_model=list[MyDocument])
def my_documents(user: CurrentUser = Depends(_require_driver_user)) -> list[MyDocument]:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """SELECT doc_id, tipo, filename, file_size, uploaded_at, expires_at, notes
                FROM fpoc.driver_documents
                WHERE driver_id = ?
                ORDER BY uploaded_at DESC""",
            user.driver_id,
        )
        return [
            MyDocument(
                doc_id=int(r.doc_id), tipo=str(r.tipo), filename=str(r.filename),
                file_size=int(r.file_size or 0),
                uploaded_at=r.uploaded_at.isoformat() if hasattr(r.uploaded_at, "isoformat") else str(r.uploaded_at),
                expires_at=r.expires_at.isoformat() if hasattr(r.expires_at, "isoformat") else (str(r.expires_at) if r.expires_at else None),
                notes=r.notes,
            )
            for r in cur.fetchall()
        ]


_VALID_TIPOS = ("licencia", "antecedentes", "contrato", "poliza", "certificacion", "otro")


@router.post("/documents", response_model=MyDocument)
async def upload_my_document(
    tipo: str = Query(...),
    expires_at: Optional[str] = Query(default=None),
    notes: Optional[str] = Query(default=None, max_length=500),
    file: UploadFile = File(...),
    user: CurrentUser = Depends(_require_driver_user),
) -> MyDocument:
    if tipo not in _VALID_TIPOS:
        raise HTTPException(400, f"tipo inválido. Permitidos: {_VALID_TIPOS}")
    data = await file.read()
    if not data:
        raise HTTPException(400, "archivo vacío")
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "archivo > 25MB")

    import uuid as _uuid
    from core.storage import upload as storage_upload
    safe_name = (file.filename or "documento").replace("/", "_").replace("\\", "_")
    blob_path = f"drivers/{user.driver_id}/{_uuid.uuid4().hex}_{safe_name}"
    storage_upload(blob_path, data, content_type=file.content_type)

    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """INSERT INTO fpoc.driver_documents
                (driver_id, tipo, filename, blob_path, file_size, content_type,
                 uploaded_by_user_id, expires_at, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            user.driver_id, tipo, safe_name, blob_path, len(data),
            file.content_type, user.user_id, expires_at, notes,
        )
        cn.commit()
        cur.execute(
            """SELECT TOP 1 doc_id, tipo, filename, file_size, uploaded_at, expires_at, notes
                FROM fpoc.driver_documents
                WHERE driver_id = ? AND blob_path = ?
                ORDER BY doc_id DESC""",
            user.driver_id, blob_path,
        )
        r = cur.fetchone()
    return MyDocument(
        doc_id=int(r.doc_id), tipo=str(r.tipo), filename=str(r.filename),
        file_size=int(r.file_size or 0),
        uploaded_at=r.uploaded_at.isoformat() if hasattr(r.uploaded_at, "isoformat") else str(r.uploaded_at),
        expires_at=r.expires_at.isoformat() if hasattr(r.expires_at, "isoformat") else (str(r.expires_at) if r.expires_at else None),
        notes=r.notes,
    )


@router.get("/documents/{doc_id}/download")
def download_my_document(
    doc_id: int,
    user: CurrentUser = Depends(_require_driver_user),
) -> StreamingResponse:
    import io
    from core.storage import download as storage_download
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT filename, blob_path, content_type FROM fpoc.driver_documents "
            "WHERE driver_id = ? AND doc_id = ?",
            user.driver_id, doc_id,
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "documento no encontrado")
    try:
        data, ct = storage_download(str(row.blob_path))
    except FileNotFoundError:
        raise HTTPException(404, "blob ausente en storage")
    content_type = row.content_type or ct or "application/octet-stream"
    return StreamingResponse(
        io.BytesIO(data),
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{row.filename}"'},
    )


# ---------------- Capacitaciones (read-only) ----------------
class MyCapacitacion(BaseModel):
    cap_id: int
    modulo_codigo: str
    modulo_nombre: str
    fecha_completado: str
    vence_at: Optional[str] = None
    notas: Optional[str] = None


@router.get("/capacitaciones", response_model=list[MyCapacitacion])
def my_capacitaciones(user: CurrentUser = Depends(_require_driver_user)) -> list[MyCapacitacion]:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """SELECT c.cap_id, m.codigo, m.nombre, c.fecha_completado, c.vence_at, c.notas
                FROM fpoc.driver_capacitaciones c
                INNER JOIN fpoc.capacitacion_modulos m ON m.modulo_id = c.modulo_id
                WHERE c.driver_id = ?
                ORDER BY c.fecha_completado DESC""",
            user.driver_id,
        )
        out: list[MyCapacitacion] = []
        for r in cur.fetchall():
            out.append(MyCapacitacion(
                cap_id=int(r.cap_id),
                modulo_codigo=str(r.codigo),
                modulo_nombre=str(r.nombre),
                fecha_completado=r.fecha_completado.isoformat() if hasattr(r.fecha_completado, "isoformat") else str(r.fecha_completado),
                vence_at=r.vence_at.isoformat() if hasattr(r.vence_at, "isoformat") else (str(r.vence_at) if r.vence_at else None),
                notas=r.notas,
            ))
        return out
