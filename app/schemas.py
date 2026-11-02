from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class AccountView(BaseModel):
    id: UUID
    label: str
    currency: str


class TransactionView(BaseModel):
    id: UUID
    account_id: UUID
    posting_date: date
    direction: Literal["debit", "credit"]
    amount: Decimal
    balance: Decimal
    description: str
    reference: str


class ReviewRow(TransactionView):
    ordinal: int
    page: int
    raw_text: str
    location: dict
    proposal: Literal["new", "duplicate", "ambiguous"]
    decision: Literal["new", "match"] | None
    effective: Literal["new", "match", "unresolved"]
    target_id: UUID | None
    explanation: str
    candidates: list[TransactionView]


class ValidationCheck(BaseModel):
    code: str
    label: str
    passed: bool
    detail: str


class ImportView(BaseModel):
    id: UUID
    filename: str
    status: str
    account: AccountView
    period_from: date
    period_to: date
    opening_balance: Decimal
    closing_balance: Decimal
    controls: dict
    checks: list[ValidationCheck]
    rows: list[ReviewRow]
    revision: str
    summary: dict[str, int]
    can_confirm: bool


class Decision(BaseModel):
    row_id: UUID
    decision: Literal["new", "match"] | None
    transaction_id: UUID | None = None

    @model_validator(mode="after")
    def validate_target(self):
        if (self.decision == "match") != (self.transaction_id is not None):
            raise ValueError("A transaction target is required only for a match decision.")
        return self


class Decisions(BaseModel):
    decisions: list[Decision] = Field(max_length=5000)


class Confirmation(BaseModel):
    expected_revision: str = Field(pattern=r"^\d+:\d+$")


class TransactionList(BaseModel):
    items: list[TransactionView]
    total: int
    page: int
    page_size: int
    debit_total: Decimal
    credit_total: Decimal
    net: Decimal
