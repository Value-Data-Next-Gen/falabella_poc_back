"""Empresa (tenant) ORM model — table `td.empresas`."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base


class Empresa(Base):
    """A transport company (transportista). Tenants of the platform.

    - `falabella_*` users see all empresas.
    - `transport_manager` users see only the empresa their `user.empresa_id` points to.
    """

    __tablename__ = "empresas"
    __table_args__ = ({"schema": settings.db_schema} if settings.db_schema else {})

    empresa_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    nombre: Mapped[str] = mapped_column(String(100), nullable=False)
    razon_social: Mapped[str | None] = mapped_column(String(200), nullable=True)
    rut: Mapped[str | None] = mapped_column(String(20), nullable=True, unique=True)

    region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    comuna: Mapped[str | None] = mapped_column(String(100), nullable=True)

    central_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    supervisor_phone_e164: Mapped[str | None] = mapped_column(String(20), nullable=True)

    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"Empresa(empresa_id={self.empresa_id}, nombre={self.nombre!r})"
