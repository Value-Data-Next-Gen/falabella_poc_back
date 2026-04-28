"""Mantenedor de maestros: CRUD admin sobre empresas, users, drivers, vehicles, clients.

Todos los endpoints requieren rol `falabella_admin`. Tras cada mutación de
drivers/vehicles/clients se llama `STATE.reload_maestros()` para que el cache
in-memory quede consistente con la DB.

Endpoints (prefijo /api/admin):
  Empresas:  GET, POST, PUT/{id}, DELETE/{id}     (/empresas)
  Users:     GET, POST, PUT/{id}, DELETE/{id}, POST/{id}/reset-password (/users)
  Drivers:   GET, POST, PUT/{id}, DELETE/{id}     (/drivers)
  Vehicles:  GET, POST, PUT/{id}, DELETE/{id}     (/vehicles)
  Clients:   GET (paginado), POST, PUT/{id}, DELETE/{id} (/clients)
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from passlib.hash import bcrypt
from pydantic import BaseModel, EmailStr, Field

from auth import CurrentUser, require_admin
from db import get_conn

router = APIRouter(prefix="/api/admin", tags=["admin-maestros"])


# ============================================================================
# Empresas
# ============================================================================
class EmpresaIn(BaseModel):
    empresa_id: int = Field(ge=1)
    nombre: str = Field(min_length=1, max_length=100)
    activo: bool = True


class EmpresaUpdate(BaseModel):
    nombre: Optional[str] = Field(default=None, min_length=1, max_length=100)
    activo: Optional[bool] = None


class EmpresaOut(BaseModel):
    empresa_id: int
    nombre: str
    activo: bool
    created_at: Optional[str] = None


def _empresa_row(r) -> EmpresaOut:
    created = r.created_at
    return EmpresaOut(
        empresa_id=int(r.empresa_id),
        nombre=r.nombre,
        activo=bool(r.activo),
        created_at=created.isoformat() if hasattr(created, "isoformat") else (created or None),
    )


@router.get("/empresas", response_model=list[EmpresaOut])
def list_empresas(_: CurrentUser = Depends(require_admin)) -> list[EmpresaOut]:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "SELECT empresa_id, nombre, activo, created_at FROM fpoc.empresas_transporte ORDER BY empresa_id"
        )
        return [_empresa_row(r) for r in cur.fetchall()]


@router.post("/empresas", response_model=EmpresaOut)
def create_empresa(req: EmpresaIn, _: CurrentUser = Depends(require_admin)) -> EmpresaOut:
    with get_conn() as cn:
        cur = cn.cursor()
        try:
            cur.execute(
                "INSERT INTO fpoc.empresas_transporte (empresa_id, nombre, activo) VALUES (?, ?, ?)",
                req.empresa_id, req.nombre, 1 if req.activo else 0,
            )
            cn.commit()
        except Exception as e:  # noqa: BLE001
            cn.rollback()
            raise HTTPException(409, f"empresa duplicada o inválida: {e}")
        cur.execute(
            "SELECT empresa_id, nombre, activo, created_at FROM fpoc.empresas_transporte WHERE empresa_id = ?",
            req.empresa_id,
        )
        return _empresa_row(cur.fetchone())


@router.put("/empresas/{empresa_id}", response_model=EmpresaOut)
def update_empresa(empresa_id: int, req: EmpresaUpdate,
                    _: CurrentUser = Depends(require_admin)) -> EmpresaOut:
    sets, params = [], []
    if req.nombre is not None:
        sets.append("nombre = ?"); params.append(req.nombre)
    if req.activo is not None:
        sets.append("activo = ?"); params.append(1 if req.activo else 0)
    if not sets:
        raise HTTPException(400, "nada que actualizar")
    params.append(empresa_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"UPDATE fpoc.empresas_transporte SET {', '.join(sets)} WHERE empresa_id = ?",
            *params,
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "empresa no encontrada")
        cn.commit()
        cur.execute(
            "SELECT empresa_id, nombre, activo, created_at FROM fpoc.empresas_transporte WHERE empresa_id = ?",
            empresa_id,
        )
        return _empresa_row(cur.fetchone())


@router.delete("/empresas/{empresa_id}")
def delete_empresa(empresa_id: int, _: CurrentUser = Depends(require_admin)) -> dict:
    with get_conn() as cn:
        cur = cn.cursor()
        # Validar que no tenga usuarios asociados
        cur.execute("SELECT COUNT(*) FROM fpoc.users WHERE empresa_id = ?", empresa_id)
        n_users = int(cur.fetchone()[0])
        if n_users > 0:
            raise HTTPException(409, f"empresa tiene {n_users} usuarios; desactivar en vez de eliminar")
        cur.execute("DELETE FROM fpoc.empresas_transporte WHERE empresa_id = ?", empresa_id)
        if cur.rowcount == 0:
            raise HTTPException(404, "empresa no encontrada")
        cn.commit()
    return {"deleted": empresa_id}


# ============================================================================
# Users
# ============================================================================
ALLOWED_ROLES = {"falabella_admin", "falabella_ops", "transport_manager"}


class UserIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=4, max_length=128)
    display_name: str = Field(min_length=1, max_length=200)
    role: str = Field(pattern="^(falabella_admin|falabella_ops|transport_manager)$")
    empresa_id: Optional[int] = None
    activo: bool = True
    phone_e164: Optional[str] = Field(default=None, max_length=20)
    notify_whatsapp: bool = False


class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    role: Optional[str] = Field(default=None, pattern="^(falabella_admin|falabella_ops|transport_manager)$")
    empresa_id: Optional[int] = None
    activo: Optional[bool] = None
    phone_e164: Optional[str] = Field(default=None, max_length=20)
    notify_whatsapp: Optional[bool] = None


class PasswordReset(BaseModel):
    new_password: str = Field(min_length=4, max_length=128)


class UserOut(BaseModel):
    user_id: int
    email: str
    display_name: str
    role: str
    empresa_id: Optional[int] = None
    empresa_nombre: Optional[str] = None
    activo: bool
    phone_e164: Optional[str] = None
    notify_whatsapp: bool
    created_at: Optional[str] = None
    last_login: Optional[str] = None


def _user_row(r) -> UserOut:
    def _iso(v):
        if v is None: return None
        return v.isoformat() if hasattr(v, "isoformat") else str(v)
    return UserOut(
        user_id=int(r.user_id), email=r.email, display_name=r.display_name,
        role=r.role,
        empresa_id=int(r.empresa_id) if r.empresa_id is not None else None,
        empresa_nombre=r.empresa_nombre,
        activo=bool(r.activo),
        phone_e164=r.phone_e164,
        notify_whatsapp=bool(r.notify_whatsapp),
        created_at=_iso(r.created_at),
        last_login=_iso(r.last_login),
    )


_USER_SELECT = """
    SELECT u.user_id, u.email, u.display_name, u.role, u.empresa_id,
           u.activo, u.phone_e164, u.notify_whatsapp,
           u.created_at, u.last_login,
           e.nombre AS empresa_nombre
    FROM fpoc.users u
    LEFT JOIN fpoc.empresas_transporte e ON u.empresa_id = e.empresa_id
"""


@router.get("/users", response_model=list[UserOut])
def list_users(_: CurrentUser = Depends(require_admin)) -> list[UserOut]:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(_USER_SELECT + " ORDER BY u.user_id")
        return [_user_row(r) for r in cur.fetchall()]


@router.post("/users", response_model=UserOut)
def create_user(req: UserIn, _: CurrentUser = Depends(require_admin)) -> UserOut:
    if req.role == "transport_manager" and req.empresa_id is None:
        raise HTTPException(400, "transport_manager requiere empresa_id")
    with get_conn() as cn:
        cur = cn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO fpoc.users
                  (email, password_hash, display_name, role, empresa_id, activo,
                   phone_e164, notify_whatsapp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING user_id
                """,
                req.email.lower(), bcrypt.hash(req.password), req.display_name,
                req.role, req.empresa_id, 1 if req.activo else 0,
                req.phone_e164, 1 if req.notify_whatsapp else 0,
            )
            new_id = int(cur.fetchone()[0])
            cn.commit()
        except Exception as e:  # noqa: BLE001
            cn.rollback()
            raise HTTPException(409, f"email duplicado o datos inválidos: {e}")
        cur.execute(_USER_SELECT + " WHERE u.user_id = ?", new_id)
        return _user_row(cur.fetchone())


@router.put("/users/{user_id}", response_model=UserOut)
def update_user(user_id: int, req: UserUpdate,
                 _: CurrentUser = Depends(require_admin)) -> UserOut:
    sets, params = [], []
    for field, col in [
        ("email", "email"), ("display_name", "display_name"),
        ("role", "role"), ("empresa_id", "empresa_id"),
        ("phone_e164", "phone_e164"),
    ]:
        v = getattr(req, field)
        if v is not None:
            sets.append(f"{col} = ?")
            params.append(v.lower() if field == "email" else v)
    if req.activo is not None:
        sets.append("activo = ?"); params.append(1 if req.activo else 0)
    if req.notify_whatsapp is not None:
        sets.append("notify_whatsapp = ?"); params.append(1 if req.notify_whatsapp else 0)
    if not sets:
        raise HTTPException(400, "nada que actualizar")
    params.append(user_id)
    with get_conn() as cn:
        cur = cn.cursor()
        try:
            cur.execute(f"UPDATE fpoc.users SET {', '.join(sets)} WHERE user_id = ?", *params)
        except Exception as e:  # noqa: BLE001
            cn.rollback()
            raise HTTPException(409, f"datos inválidos: {e}")
        if cur.rowcount == 0:
            raise HTTPException(404, "user no encontrado")
        cn.commit()
        cur.execute(_USER_SELECT + " WHERE u.user_id = ?", user_id)
        return _user_row(cur.fetchone())


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: int, req: PasswordReset,
                    _: CurrentUser = Depends(require_admin)) -> dict:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            "UPDATE fpoc.users SET password_hash = ? WHERE user_id = ?",
            bcrypt.hash(req.new_password), user_id,
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "user no encontrado")
        cn.commit()
    return {"reset": user_id}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, admin: CurrentUser = Depends(require_admin)) -> dict:
    if user_id == admin.user_id:
        raise HTTPException(400, "no puedes eliminarte a ti mismo")
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("DELETE FROM fpoc.users WHERE user_id = ?", user_id)
        if cur.rowcount == 0:
            raise HTTPException(404, "user no encontrado")
        cn.commit()
    return {"deleted": user_id}


# ============================================================================
# Drivers
# ============================================================================
class DriverIn(BaseModel):
    driver_id: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=200)
    phone: Optional[str] = Field(default=None, max_length=50)
    license: Optional[str] = Field(default="A-3 Profesional", max_length=50)
    vehicle_id: int = Field(ge=1)
    vehicle_name: str = Field(min_length=1, max_length=50)
    rating: float = Field(default=4.5, ge=0.0, le=5.0)
    deliveries_30d: int = Field(default=0, ge=0)
    fail_rate_30d: float = Field(default=0.10, ge=0.0, le=1.0)
    joined_at: Optional[date] = None
    active: bool = True


class DriverUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    phone: Optional[str] = Field(default=None, max_length=50)
    license: Optional[str] = Field(default=None, max_length=50)
    vehicle_id: Optional[int] = Field(default=None, ge=1)
    vehicle_name: Optional[str] = Field(default=None, min_length=1, max_length=50)
    rating: Optional[float] = Field(default=None, ge=0.0, le=5.0)
    deliveries_30d: Optional[int] = Field(default=None, ge=0)
    fail_rate_30d: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    joined_at: Optional[date] = None
    active: Optional[bool] = None


class DriverOut(BaseModel):
    driver_id: str
    name: str
    phone: Optional[str] = None
    license: Optional[str] = None
    vehicle_id: int
    vehicle_name: str
    rating: float
    deliveries_30d: int
    fail_rate_30d: float
    joined_at: Optional[str] = None
    active: bool
    is_problem_hidden: bool = False


def _driver_row(r) -> DriverOut:
    joined = r.joined_at
    return DriverOut(
        driver_id=r.driver_id, name=r.name, phone=r.phone, license=r.license,
        vehicle_id=int(r.vehicle_id), vehicle_name=r.vehicle_name,
        rating=float(r.rating), deliveries_30d=int(r.deliveries_30d),
        fail_rate_30d=float(r.fail_rate_30d),
        joined_at=joined.isoformat() if hasattr(joined, "isoformat") else (joined or None),
        active=bool(r.active),
        is_problem_hidden=bool(r.is_problem_hidden),
    )


def _refresh_state_maestros() -> None:
    """Llamar tras CRUD de drivers/vehicles/clients."""
    try:
        from state import STATE
        STATE.reload_maestros()
    except Exception:  # noqa: BLE001
        pass  # Tolerante: el endpoint igual devolvió OK al cliente.


def _fetch_driver(driver_id: str) -> DriverOut:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """SELECT driver_id, name, phone, license, vehicle_id, vehicle_name,
                       rating, deliveries_30d, fail_rate_30d, joined_at, active,
                       is_problem_hidden
                FROM fpoc.drivers WHERE driver_id = ?""",
            driver_id,
        )
        r = cur.fetchone()
        if not r:
            raise HTTPException(404, "driver no encontrado")
        return _driver_row(r)


def _fetch_vehicle(vehicle_id: int) -> VehicleOut:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """SELECT vehicle_id, name, type, plate, capacity_m3, driver_id, driver_name,
                       depot_lat, depot_lon, year, active, is_problem_hidden
                FROM fpoc.vehicles WHERE vehicle_id = ?""",
            vehicle_id,
        )
        r = cur.fetchone()
        if not r:
            raise HTTPException(404, "vehicle no encontrado")
        return _vehicle_row(r)


@router.get("/drivers", response_model=list[DriverOut])
def list_drivers(_: CurrentUser = Depends(require_admin)) -> list[DriverOut]:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """SELECT driver_id, name, phone, license, vehicle_id, vehicle_name,
                       rating, deliveries_30d, fail_rate_30d, joined_at, active,
                       is_problem_hidden
                FROM fpoc.drivers ORDER BY vehicle_id"""
        )
        return [_driver_row(r) for r in cur.fetchall()]


@router.post("/drivers", response_model=DriverOut)
def create_driver(req: DriverIn, _: CurrentUser = Depends(require_admin)) -> DriverOut:
    with get_conn() as cn:
        cur = cn.cursor()
        try:
            cur.execute(
                """INSERT INTO fpoc.drivers
                    (driver_id, name, phone, license, vehicle_id, vehicle_name,
                     rating, deliveries_30d, fail_rate_30d, joined_at, active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                req.driver_id, req.name, req.phone, req.license,
                req.vehicle_id, req.vehicle_name,
                req.rating, req.deliveries_30d, req.fail_rate_30d,
                req.joined_at.isoformat() if req.joined_at else None,
                1 if req.active else 0,
            )
            cn.commit()
        except Exception as e:  # noqa: BLE001
            cn.rollback()
            raise HTTPException(409, f"driver_id duplicado o datos inválidos: {e}")
    _refresh_state_maestros()
    return _fetch_driver(req.driver_id)


@router.put("/drivers/{driver_id}", response_model=DriverOut)
def update_driver(driver_id: str, req: DriverUpdate,
                   _: CurrentUser = Depends(require_admin)) -> DriverOut:
    sets, params = [], []
    for field in ["name", "phone", "license", "vehicle_id", "vehicle_name",
                   "rating", "deliveries_30d", "fail_rate_30d"]:
        v = getattr(req, field)
        if v is not None:
            sets.append(f"{field} = ?"); params.append(v)
    if req.joined_at is not None:
        sets.append("joined_at = ?"); params.append(req.joined_at.isoformat())
    if req.active is not None:
        sets.append("active = ?"); params.append(1 if req.active else 0)
    sets.append("updated_at = CURRENT_TIMESTAMP")
    if not sets:
        raise HTTPException(400, "nada que actualizar")
    params.append(driver_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(f"UPDATE fpoc.drivers SET {', '.join(sets)} WHERE driver_id = ?", *params)
        if cur.rowcount == 0:
            raise HTTPException(404, "driver no encontrado")
        cn.commit()
    _refresh_state_maestros()
    return _fetch_driver(driver_id)


@router.delete("/drivers/{driver_id}")
def delete_driver(driver_id: str, _: CurrentUser = Depends(require_admin)) -> dict:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("DELETE FROM fpoc.drivers WHERE driver_id = ?", driver_id)
        if cur.rowcount == 0:
            raise HTTPException(404, "driver no encontrado")
        cn.commit()
    _refresh_state_maestros()
    return {"deleted": driver_id}


# ============================================================================
# Vehicles
# ============================================================================
class VehicleIn(BaseModel):
    vehicle_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=50)
    type: str = Field(min_length=1, max_length=50)
    plate: str = Field(min_length=1, max_length=20)
    capacity_m3: int = Field(ge=0)
    driver_id: Optional[str] = Field(default=None, max_length=20)
    driver_name: Optional[str] = Field(default=None, max_length=200)
    depot_lat: float = -33.45
    depot_lon: float = -70.66
    year: Optional[int] = Field(default=None, ge=1990, le=2100)
    active: bool = True


class VehicleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=50)
    type: Optional[str] = Field(default=None, min_length=1, max_length=50)
    plate: Optional[str] = Field(default=None, min_length=1, max_length=20)
    capacity_m3: Optional[int] = Field(default=None, ge=0)
    driver_id: Optional[str] = Field(default=None, max_length=20)
    driver_name: Optional[str] = Field(default=None, max_length=200)
    year: Optional[int] = Field(default=None, ge=1990, le=2100)
    active: Optional[bool] = None


class VehicleOut(BaseModel):
    vehicle_id: int
    name: str
    type: str
    plate: str
    capacity_m3: int
    driver_id: Optional[str] = None
    driver_name: Optional[str] = None
    depot_lat: float
    depot_lon: float
    year: Optional[int] = None
    active: bool
    is_problem_hidden: bool = False


def _vehicle_row(r) -> VehicleOut:
    return VehicleOut(
        vehicle_id=int(r.vehicle_id), name=r.name, type=r.type, plate=r.plate,
        capacity_m3=int(r.capacity_m3),
        driver_id=r.driver_id, driver_name=r.driver_name,
        depot_lat=float(r.depot_lat), depot_lon=float(r.depot_lon),
        year=int(r.year) if r.year is not None else None,
        active=bool(r.active),
        is_problem_hidden=bool(r.is_problem_hidden),
    )


@router.get("/vehicles", response_model=list[VehicleOut])
def list_vehicles(_: CurrentUser = Depends(require_admin)) -> list[VehicleOut]:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """SELECT vehicle_id, name, type, plate, capacity_m3, driver_id, driver_name,
                       depot_lat, depot_lon, year, active, is_problem_hidden
                FROM fpoc.vehicles ORDER BY vehicle_id"""
        )
        return [_vehicle_row(r) for r in cur.fetchall()]


@router.post("/vehicles", response_model=VehicleOut)
def create_vehicle(req: VehicleIn, _: CurrentUser = Depends(require_admin)) -> VehicleOut:
    with get_conn() as cn:
        cur = cn.cursor()
        try:
            cur.execute(
                """INSERT INTO fpoc.vehicles
                    (vehicle_id, name, type, plate, capacity_m3, driver_id, driver_name,
                     depot_lat, depot_lon, year, active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                req.vehicle_id, req.name, req.type, req.plate, req.capacity_m3,
                req.driver_id, req.driver_name, req.depot_lat, req.depot_lon,
                req.year, 1 if req.active else 0,
            )
            cn.commit()
        except Exception as e:  # noqa: BLE001
            cn.rollback()
            raise HTTPException(409, f"vehicle_id duplicado o datos inválidos: {e}")
    _refresh_state_maestros()
    return _fetch_vehicle(req.vehicle_id)


@router.put("/vehicles/{vehicle_id}", response_model=VehicleOut)
def update_vehicle(vehicle_id: int, req: VehicleUpdate,
                    _: CurrentUser = Depends(require_admin)) -> VehicleOut:
    sets, params = [], []
    for field in ["name", "type", "plate", "capacity_m3", "driver_id", "driver_name", "year"]:
        v = getattr(req, field)
        if v is not None:
            sets.append(f"{field} = ?"); params.append(v)
    if req.active is not None:
        sets.append("active = ?"); params.append(1 if req.active else 0)
    sets.append("updated_at = CURRENT_TIMESTAMP")
    if not sets:
        raise HTTPException(400, "nada que actualizar")
    params.append(vehicle_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(f"UPDATE fpoc.vehicles SET {', '.join(sets)} WHERE vehicle_id = ?", *params)
        if cur.rowcount == 0:
            raise HTTPException(404, "vehicle no encontrado")
        cn.commit()
    _refresh_state_maestros()
    return _fetch_vehicle(vehicle_id)


@router.delete("/vehicles/{vehicle_id}")
def delete_vehicle(vehicle_id: int, _: CurrentUser = Depends(require_admin)) -> dict:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("DELETE FROM fpoc.vehicles WHERE vehicle_id = ?", vehicle_id)
        if cur.rowcount == 0:
            raise HTTPException(404, "vehicle no encontrado")
        cn.commit()
    _refresh_state_maestros()
    return {"deleted": vehicle_id}


# ============================================================================
# Clients
# ============================================================================
class ClientIn(BaseModel):
    customer_id: str = Field(min_length=1, max_length=20)
    title: str = Field(min_length=1, max_length=200)
    address: str = Field(min_length=1, max_length=500)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    is_recurrent: bool = False
    in_problem_comuna: bool = False
    notes: Optional[str] = Field(default=None, max_length=500)


class ClientUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    address: Optional[str] = Field(default=None, min_length=1, max_length=500)
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    is_recurrent: Optional[bool] = None
    in_problem_comuna: Optional[bool] = None
    notes: Optional[str] = Field(default=None, max_length=500)


class ClientOut(BaseModel):
    customer_id: str
    title: str
    address: str
    latitude: float
    longitude: float
    is_recurrent: bool
    in_problem_comuna: bool
    notes: Optional[str] = None


class ClientsPage(BaseModel):
    rows: list[ClientOut]
    total: int
    limit: int
    offset: int


def _client_row(r) -> ClientOut:
    return ClientOut(
        customer_id=r.customer_id, title=r.title, address=r.address,
        latitude=float(r.latitude), longitude=float(r.longitude),
        is_recurrent=bool(r.is_recurrent),
        in_problem_comuna=bool(r.in_problem_comuna),
        notes=r.notes,
    )


@router.get("/clients", response_model=ClientsPage)
def list_clients(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    search: Optional[str] = Query(default=None),
    only_recurrent: bool = Query(default=False),
    only_problem: bool = Query(default=False),
    _: CurrentUser = Depends(require_admin),
) -> ClientsPage:
    where, params = ["1=1"], []
    if search:
        where.append("(title LIKE ? OR customer_id LIKE ? OR address LIKE ?)")
        like = f"%{search}%"; params.extend([like, like, like])
    if only_recurrent:
        where.append("is_recurrent = 1")
    if only_problem:
        where.append("in_problem_comuna = 1")
    where_sql = " AND ".join(where)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM fpoc.clients WHERE {where_sql}", *params)
        total = int(cur.fetchone()[0])
        cur.execute(
            f"""SELECT customer_id, title, address, latitude, longitude,
                       is_recurrent, in_problem_comuna, notes
                FROM fpoc.clients WHERE {where_sql}
                ORDER BY title
                LIMIT ? OFFSET ?""",
            *params, limit, offset,
        )
        rows = [_client_row(r) for r in cur.fetchall()]
    return ClientsPage(rows=rows, total=total, limit=limit, offset=offset)


@router.post("/clients", response_model=ClientOut)
def create_client(req: ClientIn, _: CurrentUser = Depends(require_admin)) -> ClientOut:
    with get_conn() as cn:
        cur = cn.cursor()
        try:
            cur.execute(
                """INSERT INTO fpoc.clients
                    (customer_id, title, address, latitude, longitude,
                     is_recurrent, in_problem_comuna, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                req.customer_id, req.title, req.address, req.latitude, req.longitude,
                1 if req.is_recurrent else 0,
                1 if req.in_problem_comuna else 0,
                req.notes,
            )
            cn.commit()
        except Exception as e:  # noqa: BLE001
            cn.rollback()
            raise HTTPException(409, f"customer_id duplicado: {e}")
        cur.execute(
            """SELECT customer_id, title, address, latitude, longitude,
                      is_recurrent, in_problem_comuna, notes
               FROM fpoc.clients WHERE customer_id = ?""",
            req.customer_id,
        )
        out = _client_row(cur.fetchone())
    _refresh_state_maestros()
    return out


@router.put("/clients/{customer_id}", response_model=ClientOut)
def update_client(customer_id: str, req: ClientUpdate,
                   _: CurrentUser = Depends(require_admin)) -> ClientOut:
    sets, params = [], []
    for field in ["title", "address", "latitude", "longitude", "notes"]:
        v = getattr(req, field)
        if v is not None:
            sets.append(f"{field} = ?"); params.append(v)
    if req.is_recurrent is not None:
        sets.append("is_recurrent = ?"); params.append(1 if req.is_recurrent else 0)
    if req.in_problem_comuna is not None:
        sets.append("in_problem_comuna = ?"); params.append(1 if req.in_problem_comuna else 0)
    sets.append("updated_at = CURRENT_TIMESTAMP")
    if not sets:
        raise HTTPException(400, "nada que actualizar")
    params.append(customer_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(f"UPDATE fpoc.clients SET {', '.join(sets)} WHERE customer_id = ?", *params)
        if cur.rowcount == 0:
            raise HTTPException(404, "client no encontrado")
        cn.commit()
        cur.execute(
            """SELECT customer_id, title, address, latitude, longitude,
                      is_recurrent, in_problem_comuna, notes
               FROM fpoc.clients WHERE customer_id = ?""",
            customer_id,
        )
        out = _client_row(cur.fetchone())
    _refresh_state_maestros()
    return out


@router.delete("/clients/{customer_id}")
def delete_client(customer_id: str, _: CurrentUser = Depends(require_admin)) -> dict:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("DELETE FROM fpoc.clients WHERE customer_id = ?", customer_id)
        if cur.rowcount == 0:
            raise HTTPException(404, "client no encontrado")
        cn.commit()
    _refresh_state_maestros()
    return {"deleted": customer_id}
