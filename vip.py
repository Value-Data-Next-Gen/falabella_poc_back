"""Clientes VIP: CRUD y helpers de matching.

Matcheo por:
  match_type='customer_id'  -> coincide exact con pipeline (C0001…)
  match_type='title'        -> coincide con simpli_visits.title y visits sintéticas
  match_type='reference'    -> coincide con reference (FAL-123456)

empresa_id=NULL → VIP global (aplica a todas las empresas)

Sprint 2: VIP con deadline_time + alert_minutes_before. Cron en
`vip_deadline_cron.py` revisa cada 60s y dispara alertas WhatsApp cuando se acerca
el deadline. El admin puede usar `POST /api/vip-clients/parse-notes` para extraer
deadline y antelación desde un texto libre con LLM.
"""
from __future__ import annotations

import json as _json
import os
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, Field

from auth import CurrentUser, current_user, require_admin
from db import get_conn
from state import STATE

router = APIRouter(prefix="/api/vip-clients", tags=["vip"])


_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _validate_hhmm(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return None
    v = str(value).strip()
    if not _HHMM_RE.match(v):
        raise HTTPException(400, f"deadline_time inválido (formato HH:MM): {v!r}")
    return v


class VipClient(BaseModel):
    vip_id: int
    match_type: str
    match_value: str
    empresa_id: Optional[int] = None
    tier: str
    notes: Optional[str] = None
    deadline_time: Optional[str] = None             # HH:MM
    alert_minutes_before: int = 60
    last_alert_sent_at: Optional[str] = None
    active: bool
    created_by: Optional[int] = None
    created_at: str


class VipClientCreate(BaseModel):
    match_type: str = Field(pattern="^(customer_id|title|reference)$")
    match_value: str = Field(min_length=1, max_length=200)
    empresa_id: Optional[int] = None
    tier: str = Field(default="VIP", max_length=20)
    notes: Optional[str] = Field(default=None, max_length=500)
    deadline_time: Optional[str] = Field(default=None, max_length=5)
    alert_minutes_before: Optional[int] = Field(default=None, ge=5, le=720)
    parse_notes: bool = False  # si true, extrae deadline desde notes con LLM


class VipClientUpdate(BaseModel):
    tier: Optional[str] = Field(default=None, max_length=20)
    notes: Optional[str] = Field(default=None, max_length=500)
    deadline_time: Optional[str] = Field(default=None, max_length=5)
    alert_minutes_before: Optional[int] = Field(default=None, ge=5, le=720)
    active: Optional[bool] = None
    parse_notes: bool = False


def _row_to_vip(r) -> VipClient:
    last_alert = None
    last = getattr(r, "last_alert_sent_at", None)
    if last is not None:
        last_alert = last.isoformat() if hasattr(last, "isoformat") else str(last)
    deadline = getattr(r, "deadline_time", None)
    deadline = str(deadline) if deadline else None
    amb = getattr(r, "alert_minutes_before", 60)
    return VipClient(
        vip_id=int(r.vip_id),
        match_type=r.match_type,
        match_value=r.match_value,
        empresa_id=int(r.empresa_id) if r.empresa_id is not None else None,
        tier=r.tier,
        notes=r.notes,
        deadline_time=deadline,
        alert_minutes_before=int(amb) if amb is not None else 60,
        last_alert_sent_at=last_alert,
        active=bool(r.active),
        created_by=int(r.created_by) if r.created_by is not None else None,
        created_at=r.created_at.isoformat() if hasattr(r.created_at, "isoformat") else str(r.created_at),
    )


_SELECT_VIP_COLS = (
    "vip_id, match_type, match_value, empresa_id, tier, notes, "
    "deadline_time, alert_minutes_before, last_alert_sent_at, "
    "active, created_by, created_at"
)


# =============================================================================
# LLM parser de notas VIP
# =============================================================================
class ParseNotesRequest(BaseModel):
    notes: str = Field(min_length=1, max_length=2000)


class ParseNotesResponse(BaseModel):
    deadline_time: Optional[str] = None
    alert_minutes_before: int = 60
    razonamiento: str = ""
    fallback: bool = False


_PARSE_SYSTEM_PROMPT = (
    "Eres un parser de instrucciones VIP. Recibís un texto en español que contiene "
    "información de una entrega VIP. Tu tarea: extraer si hay una hora límite de "
    "entrega.\n\n"
    "Responde SOLO en JSON estricto:\n"
    '{ "deadline_time": "HH:MM" o null, "alert_minutes_before": number, '
    '"razonamiento": "una frase corta" }\n\n'
    "Reglas:\n"
    "- Si la nota dice \"entregar antes de las 4 PM\" -> deadline_time=\"16:00\", alert_minutes_before=60\n"
    "- Si dice \"antes de las 20:00\" -> \"20:00\", 60\n"
    "- Si dice \"máximo 14 hrs\" -> \"14:00\", 60\n"
    "- Si dice \"urgente, antes de las 10 con 2 horas de aviso\" -> \"10:00\", 120\n"
    "- Si NO menciona hora -> deadline_time=null, alert_minutes_before=60\n"
    "- alert_minutes_before default 60 si la nota no especifica anticipación.\n"
)


def _parse_notes_keywords(notes: str) -> dict:
    """Fallback regex: extrae HH:MM si aparece, alert default 60."""
    text = notes.lower()
    deadline: Optional[str] = None
    amb = 60

    # Pattern 1: HH:MM o HH directamente
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)
    if m:
        h, mm = int(m.group(1)), int(m.group(2))
        deadline = f"{h:02d}:{mm:02d}"

    # Pattern 2: "antes de las 4 pm" / "antes de las 16"
    if not deadline:
        m2 = re.search(r"(?:antes de|hasta|maximo|máximo)\s+(?:las\s+)?(\d{1,2})\s*(am|pm|hrs?|h)?", text)
        if m2:
            h = int(m2.group(1))
            sufx = (m2.group(2) or "").lower()
            if "pm" in sufx and h < 12:
                h += 12
            if "am" in sufx and h == 12:
                h = 0
            if 0 <= h <= 23:
                deadline = f"{h:02d}:00"

    # Pattern 3: "X horas de aviso" / "X horas antes" / "X horas de anticipación"
    m3 = re.search(
        r"(\d{1,3})\s*(?:hora|horas|h)\s+(?:de\s+aviso|antes|de\s+anticipaci[oó]n)",
        text,
    )
    if m3:
        amb = max(5, min(720, int(m3.group(1)) * 60))
    else:
        # "X minutos antes" / "X minutos de aviso"
        m4 = re.search(
            r"(\d{1,3})\s*(?:min|minutos)\s+(?:de\s+aviso|antes|anticipaci[oó]n)",
            text,
        )
        if m4:
            amb = max(5, min(720, int(m4.group(1))))

    return {
        "deadline_time": deadline,
        "alert_minutes_before": amb,
        "razonamiento": "Fallback regex (sin LLM disponible)" if not deadline
        else f"Detectado deadline {deadline} via regex",
        "fallback": True,
    }


def _parse_notes_llm(notes: str) -> Optional[dict]:
    endpoint = (
        os.environ.get("AZURE_OPENAI_ENDPOINT")
        or os.environ.get("AZURE_ENDPOINT")
        or ""
    ).strip().strip('"').strip("'")
    api_key = (
        os.environ.get("AZURE_OPENAI_API_KEY")
        or os.environ.get("AZURE_API_KEY")
        or ""
    ).strip().strip('"').strip("'")
    deployment = (
        os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT")
        or os.environ.get("AZURE_CHAT_DEPLOYMENT")
        or "gpt-4o-mini"
    ).strip().strip('"').strip("'")
    api_version = (
        os.environ.get("AZURE_OPENAI_API_VERSION")
        or os.environ.get("AZURE_API_VERSION")
        or "2024-08-01-preview"
    ).strip().strip('"').strip("'")
    if not endpoint or not api_key:
        return None
    try:
        from openai import AzureOpenAI
        client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
        )
        resp = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": _PARSE_SYSTEM_PROMPT},
                {"role": "user", "content": f"NOTA VIP: {notes}\n\nResponde JSON estricto."},
            ],
            temperature=0,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        contenido = resp.choices[0].message.content.strip()
        data = _json.loads(contenido)
        deadline_raw = data.get("deadline_time")
        deadline: Optional[str] = None
        if deadline_raw and isinstance(deadline_raw, str) and _HHMM_RE.match(deadline_raw):
            deadline = deadline_raw
        amb_raw = data.get("alert_minutes_before", 60)
        try:
            amb = int(amb_raw)
        except (TypeError, ValueError):
            amb = 60
        amb = max(5, min(720, amb))
        return {
            "deadline_time": deadline,
            "alert_minutes_before": amb,
            "razonamiento": str(data.get("razonamiento") or "")[:300],
            "fallback": False,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[vip-parse-notes] LLM fallo: {e}")
        return None


def parse_notes_text(notes: str) -> dict:
    """Helper público: corre LLM + fallback. Devuelve dict."""
    return _parse_notes_llm(notes) or _parse_notes_keywords(notes)


@router.post("/parse-notes", response_model=ParseNotesResponse)
def parse_notes(req: ParseNotesRequest, user: CurrentUser = Depends(current_user)) -> ParseNotesResponse:
    if not user.is_falabella:
        raise HTTPException(403, "Solo falabella_admin/ops puede parsear notas VIP")
    out = parse_notes_text(req.notes)
    return ParseNotesResponse(
        deadline_time=out.get("deadline_time"),
        alert_minutes_before=int(out.get("alert_minutes_before", 60)),
        razonamiento=str(out.get("razonamiento") or ""),
        fallback=bool(out.get("fallback", False)),
    )


# =============================================================================
# CRUD VIP
# =============================================================================
@router.get("", response_model=list[VipClient])
def list_vip(
    q: Optional[str] = Query(default=None, description="Búsqueda libre por match_value o tracking_id (TRK...)"),
    user: CurrentUser = Depends(current_user),
) -> list[VipClient]:
    """Lista VIPs accesibles por el usuario.

    Si `q` viene seteado:
      - Si q empieza con 'TRK', se busca el tracking_id en el snapshot vivo y, si
        matchea, se filtra por title/customer_id/reference de esa visita.
      - Caso contrario, se aplica LIKE %q% sobre `match_value`.
    """
    extra_where = ""
    extra_params: list = []
    if q:
        q_clean = q.strip()
        if q_clean.upper().startswith("TRK") and STATE.snapshot_df is not None:
            # Resolver tracking → títulos posibles para matchear VIPs
            df = STATE.snapshot_df
            matching = df[df["tracking_id"].astype(str) == q_clean]
            if not matching.empty:
                row = matching.iloc[0]
                title = str(row.get("title", ""))
                customer_id = str(row.get("customer_id", ""))
                reference = str(row.get("reference", ""))
                extra_where = (
                    " AND ("
                    "  (match_type='title' AND match_value = ?)"
                    "  OR (match_type='customer_id' AND match_value = ?)"
                    "  OR (match_type='reference' AND match_value = ?)"
                    ")"
                )
                extra_params = [title, customer_id, reference]
            else:
                # Tracking no existe → no devuelve nada
                return []
        else:
            extra_where = " AND match_value LIKE ?"
            extra_params = [f"%{q_clean}%"]

    with get_conn() as cn:
        cur = cn.cursor()
        if user.is_falabella:
            cur.execute(
                f"""
                SELECT {_SELECT_VIP_COLS}
                FROM fpoc.vip_clients
                WHERE 1=1{extra_where}
                ORDER BY created_at DESC
                """,
                *extra_params,
            )
        else:
            # Transport manager ve los globales (NULL) + los de su empresa
            cur.execute(
                f"""
                SELECT {_SELECT_VIP_COLS}
                FROM fpoc.vip_clients
                WHERE (empresa_id IS NULL OR empresa_id = ?){extra_where}
                ORDER BY created_at DESC
                """,
                user.empresa_id, *extra_params,
            )
        rows = cur.fetchall()
    return [_row_to_vip(r) for r in rows]


@router.post("", response_model=VipClient)
def create_vip(req: VipClientCreate, user: CurrentUser = Depends(require_admin)) -> VipClient:
    deadline = _validate_hhmm(req.deadline_time)
    amb = req.alert_minutes_before if req.alert_minutes_before is not None else 60

    # Si parse_notes y notes está set, intentamos extraer
    if req.parse_notes and req.notes:
        parsed = parse_notes_text(req.notes)
        if parsed.get("deadline_time") and not deadline:
            deadline = parsed["deadline_time"]
        if parsed.get("alert_minutes_before") and req.alert_minutes_before is None:
            amb = int(parsed["alert_minutes_before"])

    with get_conn() as cn:
        cur = cn.cursor()
        try:
            cur.execute(
                f"""
                INSERT INTO fpoc.vip_clients
                  (match_type, match_value, empresa_id, tier, notes,
                   deadline_time, alert_minutes_before, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING {_SELECT_VIP_COLS}
                """,
                req.match_type, req.match_value, req.empresa_id,
                req.tier, req.notes,
                deadline, amb,
                user.user_id,
            )
            r = cur.fetchone()
            cn.commit()
        except Exception as e:  # noqa: BLE001
            cn.rollback()
            msg = str(e)
            if "UNIQUE" in msg or "UQ_vip_match" in msg or "duplicate" in msg.lower():
                raise HTTPException(409, "Ya existe un VIP con ese match")
            raise
    return _row_to_vip(r)


@router.put("/{vip_id}", response_model=VipClient)
def update_vip(
    vip_id: int,
    req: VipClientUpdate,
    user: CurrentUser = Depends(require_admin),
) -> VipClient:
    deadline_provided = req.deadline_time is not None
    deadline = _validate_hhmm(req.deadline_time) if deadline_provided else None

    # Si parse_notes y notes está provisto, sobreescribimos defaults
    amb = req.alert_minutes_before
    if req.parse_notes and req.notes:
        parsed = parse_notes_text(req.notes)
        if parsed.get("deadline_time") and not deadline_provided:
            deadline = parsed["deadline_time"]
            deadline_provided = True
        if parsed.get("alert_minutes_before") and amb is None:
            amb = int(parsed["alert_minutes_before"])

    sets: list[str] = []
    params: list = []
    if req.tier is not None:
        sets.append("tier = ?")
        params.append(req.tier)
    if req.notes is not None:
        sets.append("notes = ?")
        params.append(req.notes)
    if deadline_provided:
        sets.append("deadline_time = ?")
        params.append(deadline)
        # Reset el contador de alerta si cambia el deadline
        sets.append("last_alert_sent_at = NULL")
    if amb is not None:
        sets.append("alert_minutes_before = ?")
        params.append(amb)
    if req.active is not None:
        sets.append("active = ?")
        params.append(1 if req.active else 0)

    if not sets:
        raise HTTPException(400, "Sin campos para actualizar")

    params.append(vip_id)
    with get_conn() as cn:
        cur = cn.cursor()
        cur.execute(
            f"UPDATE fpoc.vip_clients SET {', '.join(sets)} WHERE vip_id = ?",
            *params,
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "VIP no encontrado")
        cur.execute(
            f"SELECT {_SELECT_VIP_COLS} FROM fpoc.vip_clients WHERE vip_id = ?",
            vip_id,
        )
        r = cur.fetchone()
        cn.commit()
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
            SELECT 1 FROM fpoc.vip_clients
            WHERE active = 1 AND (empresa_id IS NULL OR empresa_id = ?) AND ({where})
            LIMIT 1
            """,
            empresa_id, *params,
        )
        return cur.fetchone() is not None
