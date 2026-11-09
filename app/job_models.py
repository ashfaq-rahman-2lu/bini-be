"""Durable operations; the queue row and financial writes commit together."""
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Job(Base):
    __tablename__ = 'operation_jobs'
    __table_args__ = (
        CheckConstraint("status IN ('queued','succeeded','failed')"),
        CheckConstraint("kind IN ('investment_confirm','bank_confirm','reconcile')"),
        Index('ix_operation_jobs_queue', 'available_at', 'created_at', postgresql_where=text("status = 'queued'")),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(30))
    target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    active_key: Mapped[str | None] = mapped_column(String(80), unique=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB, deferred=True)
    status: Mapped[str] = mapped_column(String(12), default='queued')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_status: Mapped[int | None] = mapped_column(Integer)
    error_detail: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
