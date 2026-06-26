"""Security-hardening unit tests: db_schema validator + verify_password."""
from __future__ import annotations

import pytest
from app.core.config import Settings
from app.core.security.passwords import hash_password, verify_password

_BASE = dict(jwt_secret="x" * 40, db_server="tcp:s,1433", db_name="n",
             db_user="u", db_password="pwd-not-placeholder")


@pytest.mark.parametrize("schema", ["td", "fpoc", "", "My_Schema1"])
def test_db_schema_accepts_valid(schema):
    assert Settings(db_schema=schema, **_BASE).db_schema == schema


@pytest.mark.parametrize("schema", ["bad; DROP TABLE x", "a b", "x]", "1abc", "a-b"])
def test_db_schema_rejects_injection(schema):
    with pytest.raises(Exception):  # noqa: B017 -- pydantic ValidationError
        Settings(db_schema=schema, **_BASE)


def test_verify_password_roundtrip():
    h = hash_password("admin123")
    assert verify_password("admin123", h) is True
    assert verify_password("wrong", h) is False


@pytest.mark.parametrize("bad", ["", "x", "not-a-hash", "$2b$invalid"])
def test_verify_password_bad_hash_is_false_not_raise(bad):
    # Unrecognizable stored hash → False (failed auth), never an exception.
    assert verify_password("whatever", bad) is False
