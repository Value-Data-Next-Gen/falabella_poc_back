"""Test unitario PURO de la state machine del día operativo.

No requiere BD: testea VALID_TRANSITIONS + _TARGET_ALIAS directamente. Si
alguien agrega o quita una transición, este test la documenta y previene
regresiones.

Estados: BORRADOR / VALIDADO / EN_CURSO / CERRADO
Reglas:
  BORRADOR → VALIDADO          (validate)
  VALIDADO → BORRADOR          (back to edit)
  VALIDADO → EN_CURSO          (start day)
  EN_CURSO → CERRADO           (close day)
  CERRADO  → ∅                 (terminal)
"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def transitions() -> dict[str, set[str]]:
    """Importa directamente del módulo. Si el import falla, todos los tests
    de este archivo se marcan como erro."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from routers.day_state import VALID_TRANSITIONS, VALID_STATES
    # Sanity: los 4 estados están todos cubiertos
    assert set(VALID_TRANSITIONS.keys()) == set(VALID_STATES)
    return VALID_TRANSITIONS


def test_borrador_only_goes_to_validado(transitions):
    assert transitions["BORRADOR"] == {"VALIDADO"}


def test_validado_goes_to_borrador_or_en_curso(transitions):
    assert transitions["VALIDADO"] == {"BORRADOR", "EN_CURSO"}


def test_en_curso_only_closes(transitions):
    assert transitions["EN_CURSO"] == {"CERRADO"}


def test_cerrado_is_terminal(transitions):
    assert transitions["CERRADO"] == set(), "CERRADO no debería tener transiciones salientes"


def test_no_skip_validado(transitions):
    """No se puede ir de BORRADOR directo a EN_CURSO sin pasar por VALIDADO."""
    assert "EN_CURSO" not in transitions["BORRADOR"]


def test_no_reopen_after_close(transitions):
    """CERRADO es terminal: no hay forma de volver a EN_CURSO."""
    assert "EN_CURSO" not in transitions["CERRADO"]
    assert transitions["CERRADO"] == set()


def test_legacy_listo_alias_maps_to_validado():
    """Frontend viejo manda 'LISTO' → backend lo trata como VALIDADO."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from routers.day_state import _TARGET_ALIAS
    assert _TARGET_ALIAS.get("LISTO") == "VALIDADO"


def test_all_transitions_target_valid_states(transitions):
    """Todo destino de una transición debe ser un estado válido."""
    from routers.day_state import VALID_STATES
    valid_set = set(VALID_STATES)
    for src, targets in transitions.items():
        invalid = targets - valid_set
        assert not invalid, f"{src} apunta a estados desconocidos: {invalid}"


def test_no_self_loops(transitions):
    """Una transición no se aplica al mismo estado (no `BORRADOR → BORRADOR`)."""
    for src, targets in transitions.items():
        assert src not in targets, f"self-loop detectado en {src}"
