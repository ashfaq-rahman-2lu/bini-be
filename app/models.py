import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, LargeBinary,
    Numeric, String, Text, UniqueConstraint, Uuid, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("bank", "identifier", "currency"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    bank: Mapped[str] = mapped_column(String(20), default="EBL")
    identifier: Mapped[str] = mapped_column(String(80))
    currency: Mapped[str] = mapped_column(String(3), default="BDT")
    label: Mapped[str] = mapped_column(String(120))
    revision: Mapped[int] = mapped_column(Integer, default=0)


class Statement(Base):
    __tablename__ = "statements"
    __table_args__ = (CheckConstraint("status IN ('draft','confirmed')"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    filename: Mapped[str] = mapped_column(String(255))
    original_pdf: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    period_from: Mapped[date] = mapped_column(Date)
    period_to: Mapped[date] = mapped_column(Date)
    opening_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    closing_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    controls: Mapped[dict] = mapped_column(JSONB)
    checks: Mapped[list] = mapped_column(JSONB)
    parser_version: Mapped[str] = mapped_column(String(30), default="ebl-v1")
    status: Mapped[str] = mapped_column(String(20), default="draft")
    review_revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("direction IN ('debit','credit')"),
        CheckConstraint("amount > 0"),
        Index("ix_transaction_match", "account_id", "posting_date", "direction", "amount"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"))
    posting_date: Mapped[date] = mapped_column(Date)
    direction: Mapped[str] = mapped_column(String(6))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    balance: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    description: Mapped[str] = mapped_column(Text)
    normalized_description: Mapped[str] = mapped_column(Text)
    reference: Mapped[str] = mapped_column(Text, default="")


class StatementRow(Base):
    __tablename__ = "statement_rows"
    __table_args__ = (UniqueConstraint("statement_id", "ordinal"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    statement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("statements.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(Integer)
    page: Mapped[int] = mapped_column(Integer)
    location: Mapped[dict] = mapped_column(JSONB)
    raw_text: Mapped[str] = mapped_column(Text)
    posting_date: Mapped[date] = mapped_column(Date)
    direction: Mapped[str] = mapped_column(String(6))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    balance: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    description: Mapped[str] = mapped_column(Text)
    normalized_description: Mapped[str] = mapped_column(Text)
    reference: Mapped[str] = mapped_column(Text, default="")
    decision: Mapped[str | None] = mapped_column(String(10))
    match_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("transactions.id"))


class SourceLink(Base):
    __tablename__ = "source_links"
    __table_args__ = (UniqueConstraint("statement_row_id"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"), index=True)
    statement_row_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("statement_rows.id", ondelete="CASCADE"))
