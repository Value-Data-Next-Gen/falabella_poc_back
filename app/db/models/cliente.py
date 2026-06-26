"""Cliente master — table `td.clientes`.

A cliente represents a delivery recipient (B2C end customer). Holds identity
(nombre, RUT, telefono, email), default address + geocoded coords, VIP flag,
and operational rules (ventana horaria, dias no disponible, prioridad).

CR-027: a cliente is IDENTITY-ONLY. It does NOT carry any link to a
transportista (empresa). The operational relationship cliente <-> empresa is
always derived live from the chain:

    empresa <- dias_operativos <- rutas <- visitas -> cliente

Historical model notes (for git archaeology only):
  - CR-019 introduced cliente with a rigid `empresa_id` FK.
  - CR-023 made `empresa_id` NULLable and added a M2M `cliente_empresas` table
    with denormalized counters.
  - CR-027 dropped BOTH `empresa_id` and `cliente_empresas`. The cliente master
    is now strictly tenant-agnostic; per-empresa stats are computed on demand
    by joining `visitas` to `dias_operativos`.

Created in CR-019 to support the Falabella XLSX ingest pipeline, where each
distinct `do` (delivery order) becomes a cliente row keyed by surrogate
`FAL-{do}` until the real client master is available.
"""
from __future__ import annotations

from datetime import datetime, time

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Time, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base


class Cliente(Base):
    __tablename__ = "clientes"
    __table_args__ = ({"schema": settings.db_schema} if settings.db_schema else {})

    cliente_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    rut: Mapped[str | None] = mapped_column(String(20), nullable=True)
    telefono: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)

    es_vip: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    vip_razon: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notas_operativas: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # "No entregar" / retener: block deliveries to this cliente (fraud, theft,
    # dispute). Surfaced PROMINENTLY to the driver bot and can fire a WhatsApp
    # alert to the assigned driver(s).
    retener: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retener_motivo: Mapped[str | None] = mapped_column(String(500), nullable=True)

    direccion_default: Mapped[str | None] = mapped_column(String(300), nullable=True)
    comuna_default: Mapped[str | None] = mapped_column(String(100), nullable=True)
    region_default: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lat_default: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon_default: Mapped[float | None] = mapped_column(Float, nullable=True)

    # CR-020: geocoding lifecycle tracking. `geocoding_status` is the source of
    # truth for the lifespan background loop. `geocoding_attempts` caps retries
    # (MAX 3). `geocoded_at` is set when Nominatim succeeds.
    geocoding_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending", server_default="pending"
    )
    geocoding_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    geocoded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # CR-024: operational rules — all nullable, opt-in per cliente.
    # `ventana_horaria_*` are local-time TIME values (CL operational hours).
    # `dias_no_disponible` is a JSON-encoded array of ISO weekday codes
    #   ("mon".."sun"); validation happens in Pydantic (no DB CHECK so the
    #   migration stays portable).
    # `prioridad` is 1..5, 1=highest. NULL = unset (default planner behavior).
    ventana_horaria_inicio: Mapped[time | None] = mapped_column(
        Time, nullable=True
    )
    ventana_horaria_fin: Mapped[time | None] = mapped_column(
        Time, nullable=True
    )
    dias_no_disponible: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    prioridad: Mapped[int | None] = mapped_column(Integer, nullable=True)

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
            f"Cliente(cliente_id={self.cliente_id}, "
            f"nombre={self.nombre!r}, rut={self.rut!r}, es_vip={self.es_vip})"
        )
