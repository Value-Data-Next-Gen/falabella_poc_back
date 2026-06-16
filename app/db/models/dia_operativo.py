"""Dia operativo — one row per day per empresa."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base

ESTADOS_DIA = ("BORRADOR", "VALIDADO", "EN_CURSO", "CERRADO")


class DiaOperativo(Base):
    __tablename__ = "dias_operativos"
    __table_args__ = ({"schema": settings.db_schema} if settings.db_schema else {})

    dia_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    empresa_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    fecha: Mapped[date] = mapped_column(Date, nullable=False)
    estado: Mapped[str] = mapped_column(String(20), nullable=False, default="BORRADOR")
    notas: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    validado_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    iniciado_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cerrado_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
