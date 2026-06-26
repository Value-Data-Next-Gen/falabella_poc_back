"""Twilio Content SIDs — Meta-approved WhatsApp templates.

Ported from v1. Each template has a Content SID (HX...) stored as env var
with fallback to the sandbox POC SID.
"""
from __future__ import annotations

import os

_FALLBACKS = {
    "CUENTA_ACTIVADA": "HX13bdf3c0eaecfb740ec3f21760790c38",
    "INVITACION": "HXb810bbcc6365876cdade57471d7f85ca",
    "ALERTA_MOTIVO": "HX6821f9cad06ce1980bee5ad410006e43",
    "VIP_DEADLINE": "HX679d07e0eb57dec69f27ef169adee32e",
    "REVISION_IA": "HXd49ad45c3dc35c4aa131ebcf3ab8522e",
    # End-of-day report push (CR-3b). Submitted to Meta for approval on
    # 2026-06-16; the push is a no-op until WhatsApp approves it.
    "REPORTE_DIA": "HXa8aa137c623e7bb564d7ed2f611a6e36",
}


def _get(name: str) -> str:
    return os.environ.get(f"TWILIO_CONTENT_SID_{name}", _FALLBACKS[name])


def cuenta_activada_sid() -> str:
    return _get("CUENTA_ACTIVADA")


def invitacion_sid() -> str:
    return _get("INVITACION")


def alerta_motivo_sid() -> str:
    """Generic operational alert template (eta_breach / eta_preview / manual)."""
    return _get("ALERTA_MOTIVO")


def vip_deadline_sid() -> str:
    return _get("VIP_DEADLINE")


def revision_ia_sid() -> str:
    return _get("REVISION_IA")


def reporte_dia_sid() -> str:
    """End-of-day report template (6 vars: empresa, fecha, visitas, entregadas,
    %exito, %puntualidad)."""
    return _get("REPORTE_DIA")
