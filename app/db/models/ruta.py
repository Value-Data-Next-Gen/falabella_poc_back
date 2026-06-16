"""Ruta — a driver+vehicle assignment for a day. Groups visitas."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base


class Ruta(Base):
    __tablename__ = "rutas"
    __table_args__ = ({"schema": settings.db_schema} if settings.db_schema else {})

    ruta_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dia_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    driver_id: Mapped[str] = mapped_column(String(20), nullable=False)
    vehicle_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    orden: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notas: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # CR-019: Falabella-source folio (e.g. idruta="14246798") + optional subfolio.
    folio: Mapped[str | None] = mapped_column(String(50), nullable=True)
    subfolio: Mapped[str | None] = mapped_column(String(50), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
