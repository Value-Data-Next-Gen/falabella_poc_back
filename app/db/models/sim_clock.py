"""Simulation clock — singleton state for time acceleration."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base


class SimClock(Base):
    __tablename__ = "sim_clock"
    __table_args__ = ({"schema": settings.db_schema} if settings.db_schema else {})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    sim_now: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    speed: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    running: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_tick_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
