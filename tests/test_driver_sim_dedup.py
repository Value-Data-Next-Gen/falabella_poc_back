"""Test unitario PURO de la lógica de dedup de rutas en driver_sim._init_positions.

Sin esto, si una misma patente atiende N rutas distintas, los drivers 2..N se
descartaban silenciosamente y la ruta quedaba "huérfana" (visible en plan-diario
pero sin marcador de camión en el mapa). El test extrae la lógica de dedup como
pure function y verifica que TODAS las (patente, ruta_id) sobreviven.
"""
from __future__ import annotations


def _assign_vehicle_ids(rows: list[dict]) -> list[dict]:
    """Réplica EXACTA del bloque de driver_sim._init_positions que asigna
    vehicle_id por (patente, ruta_id):
      - primera ruta de una patente → vehicle_id = patente
      - rutas adicionales → vehicle_id sintético en rango [900M, 999M]
    """
    import hashlib as _hl
    seen_pat_ruta: set[tuple[int, str]] = set()
    seen_patente_first: set[int] = set()
    out: list[dict] = []
    for r in rows:
        pat = int(r["patente_falsa"])
        rid = str(r["ruta_id"])
        if (pat, rid) in seen_pat_ruta:
            continue
        seen_pat_ruta.add((pat, rid))
        if pat not in seen_patente_first:
            seen_patente_first.add(pat)
            assigned_vid = pat
        else:
            _h = _hl.md5(f"{pat}|{rid}".encode()).hexdigest()
            assigned_vid = 900_000_000 + (int(_h[:8], 16) % 99_999_999)
        out.append({
            "vehicle_id": assigned_vid,
            "patente_falsa": pat,
            "ruta_id": rid,
            "driver_name": r.get("driver_name"),
        })
    return out


def test_single_route_uses_patente_as_vehicle_id():
    rows = [{"patente_falsa": 5, "ruta_id": "R-001", "driver_name": "Ana"}]
    out = _assign_vehicle_ids(rows)
    assert len(out) == 1
    assert out[0]["vehicle_id"] == 5


def test_same_patente_two_routes_preserves_both():
    """Regresión: antes con dedup `seen_patentes` la 2ª ruta se descartaba."""
    rows = [
        {"patente_falsa": 5, "ruta_id": "R-001", "driver_name": "Ana"},
        {"patente_falsa": 5, "ruta_id": "R-002", "driver_name": "Ana"},
    ]
    out = _assign_vehicle_ids(rows)
    assert len(out) == 2, "ambas rutas deberían preservarse"
    # La 1ª conserva la patente como vehicle_id
    assert out[0]["vehicle_id"] == 5
    # La 2ª recibe un ID sintético en rango [900M, 1000M)
    assert 900_000_000 <= out[1]["vehicle_id"] < 1_000_000_000
    # Y los IDs no colisionan
    assert out[0]["vehicle_id"] != out[1]["vehicle_id"]


def test_synthetic_vehicle_id_is_deterministic():
    """Re-correr con los mismos inputs debe dar el mismo vehicle_id sintético."""
    rows = [
        {"patente_falsa": 5, "ruta_id": "R-001", "driver_name": "Ana"},
        {"patente_falsa": 5, "ruta_id": "R-002", "driver_name": "Ana"},
    ]
    out1 = _assign_vehicle_ids(rows)
    out2 = _assign_vehicle_ids(rows)
    assert out1[1]["vehicle_id"] == out2[1]["vehicle_id"], (
        "el vehicle_id sintético debe ser determinístico por (patente, ruta_id)"
    )


def test_duplicate_pat_ruta_pair_ignored():
    """Si la query devuelve la misma (patente, ruta_id) dos veces, deduplicamos."""
    rows = [
        {"patente_falsa": 5, "ruta_id": "R-001", "driver_name": "Ana"},
        {"patente_falsa": 5, "ruta_id": "R-001", "driver_name": "Ana"},
        {"patente_falsa": 5, "ruta_id": "R-001", "driver_name": "Ana"},
    ]
    out = _assign_vehicle_ids(rows)
    assert len(out) == 1


def test_different_patentes_no_collision():
    """Patentes distintas, cada una con su vehicle_id = patente."""
    rows = [
        {"patente_falsa": 1, "ruta_id": "R-A", "driver_name": "Ana"},
        {"patente_falsa": 2, "ruta_id": "R-B", "driver_name": "Beto"},
        {"patente_falsa": 3, "ruta_id": "R-C", "driver_name": "Cira"},
    ]
    out = _assign_vehicle_ids(rows)
    assert [r["vehicle_id"] for r in out] == [1, 2, 3]


def test_5_routes_same_patente():
    """Stress: 1 patente con 5 rutas. La 1ª usa la patente, las 4 restantes sintéticas únicas."""
    rows = [
        {"patente_falsa": 5, "ruta_id": f"R-{i:03d}", "driver_name": "Ana"}
        for i in range(1, 6)
    ]
    out = _assign_vehicle_ids(rows)
    assert len(out) == 5
    vids = [r["vehicle_id"] for r in out]
    assert vids[0] == 5
    assert len(set(vids)) == 5, f"los 5 vehicle_ids deben ser únicos, got {vids}"
