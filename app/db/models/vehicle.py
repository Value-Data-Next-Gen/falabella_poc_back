"""Vehicle ORM model — table `td.vehicles`.

A vehicle belongs to exactly ONE empresa (FK NOT NULL). Driver assignment
lives in `td.drivers.vehicle_id` (CR-007), not here — to keep the relation
single-direction.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base


def _empresas_fk() -> str:
    """FK target: `td.empresas.empresa_id` (or just `empresas.empresa_id` on SQLite)."""
    return f"{settings.db_schema + '.' if settings.db_schema else ''}empresas.empresa_id"


class Vehicle(Base):
    __tablename__ = "vehicles"
    __table_args__ = ({"schema": settings.db_schema} if settings.db_schema else {})

    vehicle_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    empresa_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(_empresas_fk(), ondelete="CASCADE", name="fk_vehicles_empresa_id_empresas"),
        nullable=False,
        index=True,
    )

    nombre: Mapped[str] = mapped_column(String(100), nullable=False)
    plate: Mapped[str | None] = mapped_column(String(20), nullable=True, unique=True)
    tipo: Mapped[str | None] = mapped_column(String(50), nullable=True)
    capacity_m3: Mapped[int | None] = mapped_column(Integer, nullable=True)
    descripcion: Mapped[str | None] = mapped_column(String(500), nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)

    depot_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    depot_lon: Mapped[float | None] = mapped_column(Float, nullable=True)

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
        return (
            f"Vehicle(vehicle_id={self.vehicle_id}, empresa_id={self.empresa_id}, "
            f"nombre={self.nombre!r}, plate={self.plate!r})"
        )
