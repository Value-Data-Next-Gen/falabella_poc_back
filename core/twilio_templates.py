"""Twilio Content SIDs centralizados (CR fixes-qa M7).

Cada template Meta-approved tiene un `Content SID` (`HX...`) que Twilio
expone como configuración. Antes vivían hardcoded en 8+ archivos; cualquier
rotación obligaba a grep + edit + redeploy. Acá los centralizamos:

  - Cada constante toma su valor del env var correspondiente, con fallback
    al SID histórico (sandbox del POC).
  - Si en el futuro hay que rotar, basta cambiar la env var en Azure App
    Settings (sin tocar código).

NO importar este módulo en módulos que se cargan a import-time crítico —
no levanta excepciones, pero mejor mantenerlo lazy.

Templates actuales (al 2026-05-20):
  - `vd_alerta_motivo_v2` (6 vars): severity, motivo, vehiculo, conductor,
    cliente, comentario. Usado por comments + admin_day_notifications +
    state.auto_notify + (legacy) state.
  - `vd_vip_deadline_v2` (6 vars): cliente, deadline, mins_left, vehiculo,
    eta, slack. Usado por sims/vip_deadline_cron.
  - `vd_invitacion` (1 var: nombre). Usado por mantenedores invitations.
  - `vd_revision_ia_v2` (2 vars: motivo_reportado, motivo_sugerido). Usado
    por motivo_corrections (driver + manager).
  - `vd_cuenta_activada` (1 var: first_name). Usado por twilio_inbound
    en respuesta a ACTIVAR <TOKEN>.
"""
from __future__ import annotations

import os


# Fallback SIDs históricos (sandbox POC). Si la env var está seteada, gana.
_FALLBACKS = {
    "ALERTA_MOTIVO": "HX6821f9cad06ce1980bee5ad410006e43",
    "VIP_DEADLINE": "HX679d07e0eb57dec69f27ef169adee32e",
    "INVITACION": "HXb810bbcc6365876cdade57471d7f85ca",
    "REVISION_IA": "HXd49ad45c3dc35c4aa131ebcf3ab8522e",
    "CUENTA_ACTIVADA": "HX13bdf3c0eaecfb740ec3f21760790c38",
}


def _get(name: str) -> str:
    """Lookup env var `TWILIO_CONTENT_SID_<NAME>` con fallback al histórico."""
    return os.environ.get(f"TWILIO_CONTENT_SID_{name}", _FALLBACKS[name])


# API pública — usar como `from core.twilio_templates import ALERTA_MOTIVO`.
# Son funciones (no constantes) para que respeten cambios de env var en runtime
# (útil en tests con monkeypatch); si preferís constantes módulo-global, llamá
# a estos getters una vez al import del módulo consumidor.
def alerta_motivo_sid() -> str:
    """Content SID para `vd_alerta_motivo_v2`."""
    return _get("ALERTA_MOTIVO")


def vip_deadline_sid() -> str:
    """Content SID para `vd_vip_deadline_v2`."""
    return _get("VIP_DEADLINE")


def invitacion_sid() -> str:
    """Content SID para `vd_invitacion`."""
    return _get("INVITACION")


def revision_ia_sid() -> str:
    """Content SID para `vd_revision_ia_v2`."""
    return _get("REVISION_IA")


def cuenta_activada_sid() -> str:
    """Content SID para `vd_cuenta_activada`."""
    return _get("CUENTA_ACTIVADA")
