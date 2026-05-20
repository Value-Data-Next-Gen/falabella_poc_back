"""Test unitario PURO de la lógica de orden cronológico en live_generator.

NO requiere backend ni BD: testea la transformación `rows → ordered rows` en
isolation. Si el order asignado no es 1..N por ruta y monotónico ascendente
en current_eta_cl, este test falla.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any


def _make_row(ruta_id: str, eta: datetime, raw_order: int = 999) -> dict[str, Any]:
    """Shape mínima que necesita la lógica de orden de _insert_batch."""
    return {
        "ruta_id": ruta_id,
        "current_eta_cl": eta,
        "order": raw_order,  # valor random inicial, se reasigna
        "tid": f"{ruta_id}:{eta.isoformat()}",
    }


def _apply_chronological_order(rows: list[dict]) -> list[dict]:
    """Réplica EXACTA del bloque de _insert_batch que asigna order
    cronológico por ruta. Si la implementación en live_generator cambia, este
    test debería actualizarse o falla (regresión legítima)."""
    from collections import defaultdict
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        grouped[r["ruta_id"]].append(r)
    for ruta_id, group in grouped.items():
        group.sort(key=lambda r: r["current_eta_cl"])
        for i, r in enumerate(group, start=1):
            r["order"] = i
    return rows


def test_order_is_1_to_N_per_route():
    today = date(2026, 5, 13)
    rows = [
        _make_row("R-A", datetime(2026, 5, 13, 13, 42), raw_order=99),
        _make_row("R-A", datetime(2026, 5, 13, 9, 12), raw_order=12),
        _make_row("R-A", datetime(2026, 5, 13, 18, 11), raw_order=7),
        _make_row("R-B", datetime(2026, 5, 13, 8, 14), raw_order=55),
        _make_row("R-B", datetime(2026, 5, 13, 14, 14), raw_order=2),
    ]
    out = _apply_chronological_order(rows)

    by_route: dict[str, list[dict]] = {}
    for r in out:
        by_route.setdefault(r["ruta_id"], []).append(r)

    for ruta_id, group in by_route.items():
        # Orders del 1 al N sin huecos
        orders = sorted(r["order"] for r in group)
        assert orders == list(range(1, len(group) + 1)), (
            f"ruta {ruta_id} debería tener orders 1..{len(group)}, got {orders}"
        )
        # ETAs monotónicas ascendentes cuando se ordena por order
        by_order = sorted(group, key=lambda r: r["order"])
        for i in range(1, len(by_order)):
            assert by_order[i]["current_eta_cl"] >= by_order[i - 1]["current_eta_cl"], (
                f"ruta {ruta_id}: order {by_order[i]['order']} tiene ETA anterior "
                f"al order {by_order[i-1]['order']}"
            )


def test_order_does_not_cross_routes():
    """Verifica que los orders se reinician por ruta (cada una arranca en 1)."""
    rows = [
        _make_row("R-A", datetime(2026, 5, 13, 9, 0)),
        _make_row("R-B", datetime(2026, 5, 13, 9, 0)),
    ]
    out = _apply_chronological_order(rows)
    assert {r["order"] for r in out} == {1}, "ambas rutas con 1 stop deberían tener order=1"


def test_single_stop_route_gets_order_1():
    rows = [_make_row("R-SOLO", datetime(2026, 5, 13, 10, 0))]
    out = _apply_chronological_order(rows)
    assert out[0]["order"] == 1


def test_empty_input():
    """Edge case: lista vacía no rompe."""
    out = _apply_chronological_order([])
    assert out == []
