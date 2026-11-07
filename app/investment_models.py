"""Investment subledger. Bank transactions remain authoritative and unchanged."""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Integer, LargeBinary, Numeric, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Institution(Base):
    __tablename__ = "investment_institutions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class InvestmentImport(Base):
    __tablename__ = "investment_imports"
    __table_args__ = (UniqueConstraint("institution_id", "fingerprint", "mode", "status", name="uq_investment_import_document_mode_status"),
                      CheckConstraint("status IN ('draft','confirmed')"),
                      CheckConstraint("mode IN ('extend','replace')", name="ck_investment_import_mode"))
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    institution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investment_institutions.id"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(255))
    original_csv: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    parsed: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(12), default="draft")
    mode: Mapped[str] = mapped_column(String(10), default="extend", server_default="extend")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InvestmentProject(Base):
    __tablename__ = "investment_projects"
    __table_args__ = (UniqueConstraint("institution_id", "name"), CheckConstraint("status IN ('active','closed')"))
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    institution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investment_institutions.id"))
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(12), default="active")
    expected_total: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))


class InvestmentEvent(Base):
    __tablename__ = "investment_events"
    __table_args__ = (
        UniqueConstraint("institution_id", "signature", "occurrence"),
        CheckConstraint("amount > 0"),
        CheckConstraint("kind IN ('investment','reinvestment','repayment','refund','cashout','reinvestment_settlement')"),
        CheckConstraint("principal IS NULL OR (principal >= 0 AND principal <= amount)"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    institution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investment_institutions.id"), index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("investment_projects.id"), index=True)
    signature: Mapped[str] = mapped_column(String(64))
    occurrence: Mapped[int] = mapped_column(Integer)
    posting_date: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(30))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    description: Mapped[str] = mapped_column(Text)
    reported_profit: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    principal: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    ready_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    reported_capital: Mapped[Decimal] = mapped_column(Numeric(18, 2))


class InvestmentSource(Base):
    __tablename__ = "investment_sources"
    __table_args__ = (UniqueConstraint("import_id", "ordinal"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    import_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investment_imports.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(Integer)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investment_events.id"), index=True)


class ReturnAllocation(Base):
    __tablename__ = "investment_return_allocations"
    __table_args__ = (UniqueConstraint("source_id", "target_id"), CheckConstraint("amount > 0"), CheckConstraint("source_id <> target_id"))
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investment_events.id"), index=True)
    target_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investment_events.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))


class InvestmentBankLink(Base):
    __tablename__ = "investment_bank_links"
    __table_args__ = (UniqueConstraint("event_id", "transaction_id"), CheckConstraint("amount > 0"))
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investment_events.id"), index=True)
    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
