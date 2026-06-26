"""User ORM model — table `td.users`."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base


class User(Base):
    """Backoffice user with login + role + optional driver/empresa link.

    Roles (`role` column):
      - `falabella_admin` — full DDL + cross-empresa visibility
      - `falabella_ops`   — cross-empresa read + ops (no DDL)
      - `transport_manager` — scoped to a single empresa
      - `driver`          — no web login (only WhatsApp); kept for parity
    """

    __tablename__ = "users"
    # SQLite has no schemas; only apply `schema=` when configured (e.g., Azure SQL `td`).
    __table_args__ = ({"schema": settings.db_schema} if settings.db_schema else {})

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    email: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False)

    # Relations (FKs added in later CRs when empresas/drivers tables exist)
    empresa_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    driver_id: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Notification opt-in
    phone_e164: Mapped[str | None] = mapped_column(String(20), nullable=True)
    notify_whatsapp: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Activation (wa.me link onboarding)
    activation_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    activation_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # State
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Audit
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"User(user_id={self.user_id}, email={self.email!r}, role={self.role!r})"
