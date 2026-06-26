"""Role-based tool exposure: each actor type is only shown / allowed the tools
appropriate to its role (driver vs contacto vs falabella vs anon)."""
from __future__ import annotations

import json
import os

os.environ.setdefault("DB_TEST_URL", "sqlite+aiosqlite:///:memory:")

import pytest
from app.core.ai_tools import actor_role, execute_tool, tool_definitions_for
from app.db.models.driver import Driver
from app.db.models.empresa_contacto import EmpresaContacto
from app.db.models.user import User


def _names(actor) -> set[str]:
    return {t["function"]["name"] for t in tool_definitions_for(actor)}


def test_actor_role_mapping():
    assert actor_role(Driver(driver_id="D1", empresa_id=1, nombre="x")) == "driver"
    assert actor_role(EmpresaContacto(contact_id=1, empresa_id=1, nombre="x")) == "contacto"
    assert actor_role(User(user_id=1, role="falabella_admin", email="a@td.cl", display_name="A")) == "falabella"
    assert actor_role(User(user_id=2, role="transport_manager", email="m@td.cl", display_name="M")) == "manager"
    assert actor_role(None) == "anon"


def test_driver_sees_only_driver_tools():
    names = _names(Driver(driver_id="D1", empresa_id=1, nombre="x"))
    assert "clasificar_motivo" in names
    assert "obtener_info_cliente_por_folio" in names
    # admin/oversight tools must NOT be offered to a driver
    assert "listar_conductores" not in names
    assert "resumen_empresa" not in names
    assert "contar_entidades" not in names


def test_oversight_roles_see_admin_tools():
    for actor in (
        EmpresaContacto(contact_id=1, empresa_id=1, nombre="x"),
        User(user_id=1, role="falabella_admin", email="a@td.cl", display_name="A"),
        User(user_id=2, role="transport_manager", email="m@td.cl", display_name="M"),
    ):
        names = _names(actor)
        assert {"listar_conductores", "resumen_empresa", "contar_entidades"} <= names
        assert "clasificar_motivo" in names  # they keep the driver tools too


def test_anon_sees_nothing():
    assert tool_definitions_for(None) == []


@pytest.mark.asyncio
async def test_execute_tool_rejects_out_of_role_call():
    # A driver-originated turn must not be able to run an oversight tool even if
    # the LLM is tricked into emitting the call. Guard returns before any DB use.
    out = await execute_tool(None, "listar_conductores", {}, actor=Driver(driver_id="D1", empresa_id=1, nombre="x"))
    assert "no disponible" in json.loads(out)["error"].lower()
