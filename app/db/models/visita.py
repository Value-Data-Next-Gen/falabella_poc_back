"""Visita — individual delivery within a ruta."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base

ESTADOS_VISITA = ("pendiente", "en_camino", "entregado", "no_entregado", "cancelado")


class Visita(Base):
    __tablename__ = "visitas"
    __table_args__ = ({"schema": settings.db_schema} if settings.db_schema else {})

    visita_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ruta_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    dia_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    empresa_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    orden: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Client info
    cliente_nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    cliente_rut: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cliente_telefono: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Address
    direccion: Mapped[str] = mapped_column(String(500), nullable=False)
    comuna: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Delivery
    estado: Mapped[str] = mapped_column(String(20), nullable=False, default="pendiente")
    motivo: Mapped[str | None] = mapped_column(String(100), nullable=True)
    motivo_comentario: Mapped[str | None] = mapped_column(String(500), nullable=True)
    motivo_ia_sugerido: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Timing
    eta_estimada: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    llegada_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completada_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Package
    n_bultos: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    referencia: Mapped[str | None] = mapped_column(String(100), nullable=True)
    es_vip: Mapped[bool | None] = mapped_column(Integer, nullable=True, default=0)
    notas: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # CR-019: Falabella-source fields + link to clientes master.
    cliente_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    folio_cliente: Mapped[str | None] = mapped_column(String(50), nullable=True)
    subfolio_bulto: Mapped[str | None] = mapped_column(String(50), nullable=True)
    parent_order: Mapped[str | None] = mapped_column(String(50), nullable=True)
    tipo_documento: Mapped[str | None] = mapped_column(String(50), nullable=True)
    region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    fecha_pactada: Mapped[date | None] = mapped_column(Date, nullable=True)
    estado_fuente: Mapped[str | None] = mapped_column(String(50), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
