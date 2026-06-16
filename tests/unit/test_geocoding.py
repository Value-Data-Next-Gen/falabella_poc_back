"""Unit tests for geocoding helpers + Cliente schema fields (CR-020).

Scope: pure-Python only — no DB, no network. Live integration is verified by
hitting Azure SQL after a real ingest.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.geocoding import (
    GEOCODING_MAX_ATTEMPTS,
    _COMUNA_CENTROIDS,
    centroide_comuna,
)
from app.schemas.cliente import ClienteOut


def test_centroide_las_condes_returns_known_coords() -> None:
    """Known reference point used by ingest fallback. Locks in the value
    against accidental edits of the centroids table."""
    coords = centroide_comuna("LAS CONDES")
    assert coords is not None
    lat, lon = coords
    assert lat == pytest.approx(-33.4172, abs=1e-4)
    assert lon == pytest.approx(-70.5476, abs=1e-4)


def test_centroide_handles_accents_and_case() -> None:
    """Ingest sees comuna text in many casings ('Ñuñoa', 'NUNOA', etc.).
    Centroide must resolve all of them to the same coords."""
    for label in ("nunoa", "ñuñoa", "Ñuñoa", "NUÑOA"):
        coords = centroide_comuna(label)
        assert coords is not None, f"failed for {label!r}"


def test_centroide_unknown_returns_none() -> None:
    assert centroide_comuna("Atlantis") is None
    assert centroide_comuna(None) is None
    assert centroide_comuna("") is None


def test_centroide_table_is_nonempty() -> None:
    # Guards against accidental wipe of the lookup table.
    assert len(_COMUNA_CENTROIDS) >= 40


def test_max_attempts_is_three() -> None:
    """CR-020 spec: cap retries at 3 to avoid hammering Nominatim forever."""
    assert GEOCODING_MAX_ATTEMPTS == 3


def test_cliente_out_exposes_geocoding_fields() -> None:
    """ClienteOut MUST include `geocoding_status`, `geocoding_attempts`,
    `geocoded_at` so the frontend can show pending/failed badges."""
    fields = ClienteOut.model_fields
    assert "geocoding_status" in fields
    assert "geocoding_attempts" in fields
    assert "geocoded_at" in fields


def test_cliente_out_geocoding_status_defaults() -> None:
    """When the source row has no explicit status (legacy), the schema must
    still validate and surface the default `pending`."""
    payload = {
        "cliente_id": 1,
        "empresa_id": 1,
        "nombre": "Cliente Test",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    c = ClienteOut.model_validate(payload)
    assert c.geocoding_status == "pending"
    assert c.geocoding_attempts == 0
    assert c.geocoded_at is None


def test_cliente_out_geocoding_fields_roundtrip() -> None:
    """Geocoded payload deserializes correctly."""
    ts = datetime.now(UTC)
    payload = {
        "cliente_id": 7,
        "empresa_id": 1,
        "nombre": "Cliente Geocoded",
        "geocoding_status": "nominatim_ok",
        "geocoding_attempts": 1,
        "geocoded_at": ts,
        "created_at": ts,
        "updated_at": ts,
    }
    c = ClienteOut.model_validate(payload)
    assert c.geocoding_status == "nominatim_ok"
    assert c.geocoding_attempts == 1
    assert c.geocoded_at == ts
