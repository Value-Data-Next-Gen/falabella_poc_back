"""Overrides de prioridad por visita (tracking_id).

PUT    /api/priorities/{tracking_id}    -> set priority
GET    /api/priorities                  -> lista overrides activos
DELETE /api/priorities/{tracking_id}    -> remueve override
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import CurrentUser, current_user
from db import get_conn

router = APIRouter(prefix="/api/priorities", tags=["priorities"])

ALLOWED = {"low", "normal", "high", "vip"}


class PriorityOverride(BaseModel):
    tracking_id: str
    priority: str
    reason: Optional[str] = None
    set_by: Optional[int] = None
    set_by_name: Optional[str] = None
    set_at: str


class PrioritySetRequest(BaseModel):
    priority: str = Field(pattern="^(low|normal|high|vip)$")
    reason: Optional[str] = Field(default=None, max_length=500)


@router.get("", response_model=list[PriorityOverride])
def list_priorities(
    priority: Optional[str] = Query(default=None),
    user: CurrentUser = Depends(current_user),
) -> list[PriorityOverride]:
    where = ""
    params: list = []
    if priority:
        if priority not in ALLOWED:
            raise HTTPException(400, f"priority inválida: {priority}")
        where = " WHERE p.priority = ?"
        params.append(priority)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""
            SELECT p.tracking_id, p.priority, p.reason, p.set_by,
                   u.display_name AS set_by_name, p.set_at
            FROM fpoc.visit_priority_overrides p
            LEFT JOIN fpoc.users u ON u.user_id = p.set_by
            {where}
            ORDER BY p.set_at DESC
            """,
            *params,
        )
        rows = cur.fetchall()
    return [
        PriorityOverride(
            tracking_id=r.tracking_id,
            priority=r.priority,
            reason=r.reason,
            set_by=int(r.set_by) if r.set_by is not None else None,
            set_by_name=r.set_by_name,
            set_at=r.set_at.isoformat(),
        )
        for r in rows
    ]


@router.put("/{tracking_id}", response_model=PriorityOverride)
def set_priority(
    tracking_id: str,
    req: PrioritySetRequest,
    user: CurrentUser = Depends(current_user),
) -> PriorityOverride:
    with get_conn() as cn:
        cur = cn.cursor()
        # Upsert portátil sqlite/sqlserver (ON CONFLICT es sqlite-only).
        cur.execute(
            "SELECT 1 FROM fpoc.visit_priority_overrides WHERE tracking_id = ?",
            tracking_id,
        )
        if cur.fetchone():
            cur.execute(
                """UPDATE fpoc.visit_priority_overrides
                      SET priority = ?, reason = ?, set_by = ?,
                          set_at = CURRENT_TIMESTAMP
                    WHERE tracking_id = ?""",
                req.priority, req.reason, user.user_id, tracking_id,
            )
        else:
            cur.execute(
                """INSERT INTO fpoc.visit_priority_overrides
                        (tracking_id, priority, reason, set_by)
                     VALUES (?, ?, ?, ?)""",
                tracking_id, req.priority, req.reason, user.user_id,
            )
        cn.commit()

        cur.execute(
            """
            SELECT p.tracking_id, p.priority, p.reason, p.set_by,
                   u.display_name AS set_by_name, p.set_at
            FROM fpoc.visit_priority_overrides p
            LEFT JOIN fpoc.users u ON u.user_id = p.set_by
            WHERE p.tracking_id = ?
            """,
            tracking_id,
        )
        r = cur.fetchone()
    return PriorityOverride(
        tracking_id=r.tracking_id,
        priority=r.priority,
        reason=r.reason,
        set_by=int(r.set_by) if r.set_by is not None else None,
        set_by_name=r.set_by_name,
        set_at=r.set_at.isoformat(),
    )


@router.delete("/{tracking_id}")
def delete_priority(tracking_id: str, user: CurrentUser = Depends(current_user)) -> dict:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute("DELETE FROM fpoc.visit_priority_overrides WHERE tracking_id = ?", tracking_id)
        n = cur.rowcount
        cn.commit()
    if not n:
        raise HTTPException(404, "override no encontrado")
    return {"deleted": tracking_id}


def get_priorities_map(tracking_ids: list[str]) -> dict[str, str]:
    """Helper: dado un set de tracking_ids, devuelve {tracking_id: priority}."""
    if not tracking_ids:
        return {}
    marks = ",".join(["?"] * len(tracking_ids))
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"SELECT tracking_id, priority FROM fpoc.visit_priority_overrides WHERE tracking_id IN ({marks})",
            *tracking_ids,
        )
        return {r.tracking_id: r.priority for r in cur.fetchall()}
