"""Driver ORM model — table `td.drivers`."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base


def _fk(table: str, col: str) -> str:
    return f"{settings.db_schema + '.' if settings.db_schema else ''}{table}.{col}"


class Driver(Base):
    """A delivery driver. PK is a human-readable string (DRV-NNNNN).

    - Belongs to ONE empresa (CASCADE: borrar empresa borra sus drivers).
    - Optionally assigned to ONE vehicle (SET NULL: borrar vehicle deja al driver sin vehículo).
    - WhatsApp opt-in via `activation_token` redeemed by sending "ACTIVAR <token>"
      to the Twilio sender (workflow in later CR).
    """

    __tablename__ = "drivers"
    __table_args__ = ({"schema": settings.db_schema} if settings.db_schema else {})

    driver_id: Mapped[str] = mapped_column(String(20), primary_key=True)

    empresa_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(_fk("empresas", "empresa_id"), ondelete="CASCADE",
                   name="fk_drivers_empresa_id_empresas"),
        nullable=False,
        index=True,
    )
    vehicle_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(_fk("vehicles", "vehicle_id"), ondelete="NO ACTION",
                   name="fk_drivers_vehicle_id_vehicles"),
        nullable=True,
        index=True,
    )

    nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    rut: Mapped[str | None] = mapped_column(String(20), nullable=True)

    phone_e164: Mapped[str | None] = mapped_column(String(20), nullable=True, unique=True)
    notify_whatsapp: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    opted_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    activation_token: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True)
    activation_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
        return f"Driver(driver_id={self.driver_id!r}, empresa_id={self.empresa_id}, nombre={self.nombre!r})"
