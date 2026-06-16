"""Unit tests for `app.core.config.Settings` fail-fast behavior."""
from __future__ import annotations

import importlib

import pytest


def _reload_config():
    """Force re-evaluation of `Settings()` with current env."""
    import app.core.config as cfg
    importlib.reload(cfg)
    return cfg


def test_settings_loads_with_valid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "a" * 48)
    monkeypatch.setenv("DB_PASSWORD", "real-pwd")
    monkeypatch.setenv("DB_SERVER", "tcp:host,1433")
    monkeypatch.setenv("DB_NAME", "db")
    monkeypatch.setenv("DB_USER", "u")
    cfg = _reload_config()
    assert cfg.settings.jwt_secret.get_secret_value() == "a" * 48
    assert cfg.settings.db_name == "db"


@pytest.mark.parametrize("placeholder", ["", "replace-me", "change-me"])
def test_settings_fails_when_jwt_secret_placeholder(
    monkeypatch: pytest.MonkeyPatch, placeholder: str,
) -> None:
    monkeypatch.setenv("JWT_SECRET", placeholder)
    monkeypatch.setenv("DB_PASSWORD", "real-pwd")
    monkeypatch.setenv("DB_SERVER", "tcp:host,1433")
    monkeypatch.setenv("DB_NAME", "db")
    monkeypatch.setenv("DB_USER", "u")
    with pytest.raises(Exception, match="JWT_SECRET"):
        _reload_config()


def test_settings_fails_when_db_password_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_SECRET", "a" * 48)
    monkeypatch.setenv("DB_PASSWORD", "replace-me")
    monkeypatch.setenv("DB_SERVER", "tcp:host,1433")
    monkeypatch.setenv("DB_NAME", "db")
    monkeypatch.setenv("DB_USER", "u")
    with pytest.raises(Exception, match="DB_PASSWORD"):
        _reload_config()


def test_settings_fails_when_jwt_too_short(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "tooShort")
    monkeypatch.setenv("DB_PASSWORD", "real")
    monkeypatch.setenv("DB_SERVER", "tcp:host,1433")
    monkeypatch.setenv("DB_NAME", "db")
    monkeypatch.setenv("DB_USER", "u")
    with pytest.raises(Exception, match="JWT_SECRET"):
        _reload_config()


def test_cors_origins_list_parses_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "a" * 48)
    monkeypatch.setenv("DB_PASSWORD", "real-pwd")
    monkeypatch.setenv("DB_SERVER", "tcp:host,1433")
    monkeypatch.setenv("DB_NAME", "db")
    monkeypatch.setenv("DB_USER", "u")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://a.com, https://b.com,https://c.com")
    cfg = _reload_config()
    assert cfg.settings.cors_origins_list == [
        "https://a.com",
        "https://b.com",
        "https://c.com",
    ]
