"""EmpresaContacto ORM model — table `td.empresa_contactos`.

A non-driver contact bound to ONE empresa. Receives WhatsApp alerts. No web login.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base


def _fk(table: str, col: str) -> str:
    return f"{settings.db_schema + '.' if settings.db_schema else ''}{table}.{col}"


class EmpresaContacto(Base):
    __tablename__ = "empresa_contactos"
    __table_args__ = (
        CheckConstraint(
            "rol IN ('jefe','coordinador','otro')",
            name="ck_contactos_rol",
        ),
        *(({"schema": settings.db_schema},) if settings.db_schema else ()),
    )

    contact_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    empresa_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(_fk("empresas", "empresa_id"), ondelete="CASCADE",
                   name="fk_empresa_contactos_empresa_id_empresas"),
        nullable=False,
        index=True,
    )

    nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    rol: Mapped[str] = mapped_column(String(50), nullable=False)

    phone_e164: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)

    opted_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activation_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    activation_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # JSON-as-string filters (Pydantic validates shape).
    notify_severities: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notify_motivos: Mapped[str | None] = mapped_column(String(500), nullable=True)

    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(_fk("users", "user_id"), ondelete="NO ACTION",
                   name="fk_empresa_contactos_created_by_user_id_users"),
        nullable=True,
    )

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
        return (
            f"EmpresaContacto(contact_id={self.contact_id}, empresa_id={self.empresa_id}, "
            f"rol={self.rol!r}, nombre={self.nombre!r})"
        )
