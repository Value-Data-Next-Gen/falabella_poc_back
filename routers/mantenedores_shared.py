"""Helpers compartidos entre los sub-módulos de mantenedores admin.

Extraído en R7-F4 para no duplicar lógica entre drivers/vehicles/clients.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException

from core.auth import CurrentUser, current_user


def refresh_state_maestros() -> None:
    """Llamar tras CRUD de drivers/vehicles/clients. Recarga el cache de lookup
    en STATE para que los handlers que dependen de STATE.drivers / STATE.vehicles_ext
    vean los cambios sin esperar a un restart."""
    try:
        from core.state import STATE
        STATE.reload_maestros()
    except Exception:  # noqa: BLE001
        pass  # Tolerante: el endpoint igual devolvió OK al cliente.


def require_fleet_access(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    """Drivers/Vehicles: permite admin/ops o transport_manager (scopeado a su empresa).

    Los endpoints que la usan deben validar empresa_id contra user.empresa_id en
    el body/recurso vía enforce_fleet_empresa.
    """
    if user.is_falabella:
        return user
    if user.role == "transport_manager" and user.empresa_id is not None:
        return user
    raise HTTPException(403, "Requiere rol falabella o transport_manager con empresa")


def enforce_fleet_empresa(user: CurrentUser, empresa_id: Optional[int]) -> None:
    """transport_manager solo puede tocar recursos de SU empresa."""
    if user.is_falabella:
        return
    if empresa_id is None:
        raise HTTPException(400, "empresa_id requerido")
    if user.empresa_id != empresa_id:
        raise HTTPException(403, "Solo podés gestionar tu empresa")


def can_access_empresa(user: CurrentUser, empresa_id: int) -> None:
    """Valida que el user puede operar sobre esa empresa. Falabella ve todo;
    transport_manager solo la suya."""
    if user.is_falabella:
        return
    if user.role == "transport_manager" and user.empresa_id == empresa_id:
        return
    raise HTTPException(403, "sin permisos para esa empresa")
