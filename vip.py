"""Clientes VIP: CRUD y helpers de matching.

Matcheo por:
  match_type='customer_id'  -> coincide exact con pipeline (C0001…)
  match_type='title'        -> coincide con simpli_visits.title y visits sintéticas
  match_type='reference'    -> coincide con reference (FAL-123456)

empresa_id=NULL → VIP global (aplica a todas las empresas)
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import CurrentUser, current_user, require_admin
from db import get_conn

router = APIRouter(prefix="/api/vip-clients", tags=["vip"])


class VipClient(BaseModel):
    vip_id: int
    match_type: str
    match_value: str
    empresa_id: Optional[int] = None
    tier: str
    notes: Optional[str] = None
    active: bool
    created_by: Optional[int] = None
    created_at: str


class VipClientCreate(BaseModel):
    match_type: str = Field(pattern="^(customer_id|title|reference)$")
    match_value: str = Field(min_length=1, max_length=200)
    empresa_id: Optional[int] = None
    tier: str = Field(default="VIP", max_length=20)
    notes: Optional[str] = Field(default=None, max_length=500)


def _row_to_vip(r) -> VipClient:
    return VipClient(
        vip_id=int(r.vip_id),
        match_type=r.match_type,
        match_value=r.match_value,
        empresa_id=int(r.empresa_id) if r.empresa_id is not None else None,
        tier=r.tier,
        notes=r.notes,
        active=bool(r.active),
        created_by=int(r.created_by) if r.created_by is not None else None,
        created_at=r.created_at.isoformat(),
    )


@router.get("", response_model=list[VipClient])
def list_vip(user: CurrentUser = Depends(current_user)) -> list[VipClient]:
    with get_conn() as cn:
        cur = cn.cursor()
        if user.is_falabella:
            cur.execute(
                """
                SELECT vip_id, match_type, match_value, empresa_id, tier, notes,
                       active, created_by, created_at
                FROM fpoc.vip_clients
                ORDER BY created_at DESC
                """
            )
        else:
            # Transport manager ve los globales (NULL) + los de su empresa
            cur.execute(
                """
                SELECT vip_id, match_type, match_value, empresa_id, tier, notes,
                       active, created_by, created_at
                FROM fpoc.vip_clients
                WHERE empresa_id IS NULL OR empresa_id = ?
                ORDER BY created_at DESC
                """,
                user.empresa_id,
            )
        rows = cur.fetchall()
    return [_row_to_vip(r) for r in rows]


@router.post("", response_model=VipClient)
def create_vip(req: VipClientCreate, user: CurrentUser = Depends(require_admin)) -> VipClient:
    with get_conn() as cn:
        cur = cn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO fpoc.vip_clients
                  (match_type, match_value, empresa_id, tier, notes, created_by)
                OUTPUT INSERTED.vip_id, INSERTED.match_type, INSERTED.match_value,
                       INSERTED.empresa_id, INSERTED.tier, INSERTED.notes,
                       INSERTED.active, INSERTED.created_by, INSERTED.created_at
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                req.match_type, req.match_value, req.empresa_id,
                req.tier, req.notes, user.user_id,
            )
            r = cur.fetchone()
            cn.commit()
        except Exception as e:  # noqa: BLE001
            cn.rollback()
            msg = str(e)
            if "UQ_vip_match" in msg or "duplicate" in msg.lower():
                raise HTTPException(409, "Ya existe un VIP con ese match")
            raise
    return _row_to_vip(r)


@router.delete("/{vip_id}")
def delete_vip(vip_id: int, user: CurrentUser = Depends(require_admin)) -> dict:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("DELETE FROM fpoc.vip_clients WHERE vip_id = ?", vip_id)
        n = cur.rowcount
        cn.commit()
    if not n:
        raise HTTPException(404, "VIP no encontrado")
    return {"deleted": vip_id}


def is_vip(title: str | None, customer_id: str | None, reference: str | None,
           empresa_id: int | None) -> bool:
    """Helper para usar desde scheduler / otros módulos."""
    conds = []
    params: list = []
    if title:
        conds.append("(match_type = 'title' AND match_value = ?)")
        params.append(title)
    if customer_id:
        conds.append("(match_type = 'customer_id' AND match_value = ?)")
        params.append(customer_id)
    if reference:
        conds.append("(match_type = 'reference' AND match_value = ?)")
        params.append(reference)
    if not conds:
        return False
    where = " OR ".join(conds)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""
            SELECT TOP 1 1 FROM fpoc.vip_clients
            WHERE active = 1 AND (empresa_id IS NULL OR empresa_id = ?) AND ({where})
            """,
            empresa_id, *params,
        )
        return cur.fetchone() is not None
