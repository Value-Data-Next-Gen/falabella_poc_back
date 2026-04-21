"""Preferencias del usuario autenticado: phone, umbrales de notificación.

GET  /api/me/preferences
PUT  /api/me/preferences
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from auth import CurrentUser, current_user
from db import get_conn

router = APIRouter(prefix="/api/me/preferences", tags=["preferences"])


class PreferencesResponse(BaseModel):
    phone_e164: Optional[str] = None
    notify_whatsapp: bool
    notify_pfallo_threshold: float   # 0.0 - 1.0
    notify_slack_min_threshold: int  # minutos
    notify_only_vip: bool


class PreferencesUpdate(BaseModel):
    phone_e164: Optional[str] = Field(default=None, max_length=20)
    notify_whatsapp: Optional[bool] = None
    notify_pfallo_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    notify_slack_min_threshold: Optional[int] = Field(default=None, ge=0, le=240)
    notify_only_vip: Optional[bool] = None


def _fetch(user_id: int) -> PreferencesResponse:
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            """
            SELECT phone_e164, notify_whatsapp, notify_pfallo_threshold,
                   notify_slack_min_threshold, notify_only_vip
            FROM fpoc.users WHERE user_id = ?
            """,
            user_id,
        )
        r = cur.fetchone()
    return PreferencesResponse(
        phone_e164=r.phone_e164,
        notify_whatsapp=bool(r.notify_whatsapp),
        notify_pfallo_threshold=float(r.notify_pfallo_threshold),
        notify_slack_min_threshold=int(r.notify_slack_min_threshold),
        notify_only_vip=bool(r.notify_only_vip),
    )


@router.get("", response_model=PreferencesResponse)
def get_prefs(user: CurrentUser = Depends(current_user)) -> PreferencesResponse:
    return _fetch(user.user_id)


@router.put("", response_model=PreferencesResponse)
def update_prefs(req: PreferencesUpdate, user: CurrentUser = Depends(current_user)) -> PreferencesResponse:
    sets = []
    params: list = []
    for field, col in [
        ("phone_e164", "phone_e164"),
        ("notify_whatsapp", "notify_whatsapp"),
        ("notify_pfallo_threshold", "notify_pfallo_threshold"),
        ("notify_slack_min_threshold", "notify_slack_min_threshold"),
        ("notify_only_vip", "notify_only_vip"),
    ]:
        val = getattr(req, field)
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(val if not isinstance(val, bool) else (1 if val else 0))
    if sets:
        params.append(user.user_id)
        with get_conn() as cn:
            cur = cn.cursor()
            cur.execute(
                f"UPDATE fpoc.users SET {', '.join(sets)} WHERE user_id = ?",
                *params,
            )
            cn.commit()
    return _fetch(user.user_id)
