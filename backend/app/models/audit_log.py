from datetime import datetime
from typing import Optional
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class AuditLog(BaseModel):
    __tablename__ = "audit_logs"

    action: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
    )

    username: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
    )

    ip_address: Mapped[Optional[str]] = mapped_column(
        String(45),
        nullable=True,
    )

    details: Mapped[Optional[str]] = mapped_column(
        String(1000),
        nullable=True,
    )
