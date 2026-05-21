"""CRUD admin de clients. Extraído de mantenedores.py en R7-F4.

URLs (todas bajo /api/admin del router padre):
  GET    /api/admin/clients   (paginado, filtros search/only_recurrent/only_problem)
  POST   /api/admin/clients
  PUT    /api/admin/clients/{customer_id}
  DELETE /api/admin/clients/{customer_id}
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.auth import CurrentUser, require_admin
from core.db import get_conn
from routers.mantenedores_shared import refresh_state_maestros


router = APIRouter(tags=["admin-maestros"])


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
    refresh_state_maestros()
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
    refresh_state_maestros()
    return out


@router.delete("/clients/{customer_id}")
def delete_client(customer_id: str, _: CurrentUser = Depends(require_admin)) -> dict:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("DELETE FROM fpoc.clients WHERE customer_id = ?", customer_id)
        if cur.rowcount == 0:
            raise HTTPException(404, "client no encontrado")
        cn.commit()
    refresh_state_maestros()
    return {"deleted": customer_id}
