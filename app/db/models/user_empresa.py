"""Junction table: user ↔ empresa many-to-many."""
from __future__ import annotations

from sqlalchemy import Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base


class UserEmpresa(Base):
    __tablename__ = "user_empresas"
    __table_args__ = ({"schema": settings.db_schema} if settings.db_schema else {})

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    empresa_id: Mapped[int] = mapped_column(Integer, primary_key=True)
