"""VisitaEvento ORM model — table `td.visita_eventos`.

CR-028 Part A. Append-only audit log of every operator action on a visita.

`tipo` enumerates the action:
  - `orden_change`   — PATCH /visitas/{id}/orden
  - `estado_change`  — PATCH /visitas/{id} (any estado mutation)
  - `cancelada`      — POST  /visitas/{id}/cancel
  - `ruta_change`    — POST  /visitas/{id}/move-route
  - `promoted_vip`   — POST  /rutas/{id}/promote-vips
  - `eta_recalc`     — POST  /dias/{id}/plan-etas (per affected visita)

`payload_json` is free-form text (NVARCHAR(2000)). We do NOT enforce schema in
the DB — callers serialize old/new values and any context (motivo, comentario).
Consumers parse on read.

CHECK constraint on `tipo` mirrors the migration (0026).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
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

TIPOS = (
    "orden_change",
    "estado_change",
    "cancelada",
    "ruta_change",
    "promoted_vip",
    "eta_recalc",
)


def _fk(table: str, col: str) -> str:
    return f"{settings.db_schema + '.' if settings.db_schema else ''}{table}.{col}"


class VisitaEvento(Base):
    __tablename__ = "visita_eventos"
    __table_args__ = (
        CheckConstraint(
            "tipo IN ('orden_change','estado_change','cancelada','ruta_change',"
            "'promoted_vip','eta_recalc')",
            name="ck_visita_eventos_tipo",
        ),
        *(({"schema": settings.db_schema},) if settings.db_schema else ()),
    )

    evento_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    visita_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            _fk("visitas", "visita_id"),
            ondelete="CASCADE",
            name="fk_visita_eventos_visita_id_visitas",
        ),
        nullable=False,
        index=True,
    )

    tipo: Mapped[str] = mapped_column(String(40), nullable=False)

    user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            _fk("users", "user_id"),
            ondelete="SET NULL",
            name="fk_visita_eventos_user_id_users",
        ),
        nullable=True,
    )

    payload_json: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"VisitaEvento(evento_id={self.evento_id}, visita_id={self.visita_id}, "
            f"tipo={self.tipo!r}, user_id={self.user_id})"
        )
