import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.finance.statement_models import StatementLineMatchLeg


class AccountStatementResponse(BaseModel):
    public_id: uuid.UUID
    account_public_id: uuid.UUID
    period_start: date
    period_end: date
    closing_balance: Decimal | None
    currency_code: str
    reconciled_through: date | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})


class StatementLineResponse(BaseModel):
    public_id: uuid.UUID
    occurred_at: date
    description: str
    amount: Decimal
    balance: Decimal | None
    matched_transaction_id: uuid.UUID | None = None
    matched_transfer_id: uuid.UUID | None = None
    matched_transfer_leg: StatementLineMatchLeg | None = None
    matched_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})


class MatchCandidate(BaseModel):
    """A deterministic suggestion only — never persisted until confirmed
    (spec-078 §Match engine: suggest-only)."""

    kind: str  # "transaction" | "transfer"
    id: uuid.UUID
    occurred_at: date
    amount: Decimal
    description: str
    leg: StatementLineMatchLeg | None = None

    model_config = ConfigDict(json_encoders={Decimal: str})


class UnmatchedStatementLineView(BaseModel):
    line: StatementLineResponse
    candidates: list[MatchCandidate]


class UnmatchedLedgerRow(BaseModel):
    kind: str  # "transaction" | "transfer"
    id: uuid.UUID
    occurred_at: date
    amount: Decimal
    description: str
    leg: StatementLineMatchLeg | None = None

    model_config = ConfigDict(json_encoders={Decimal: str})


class ReconciliationView(BaseModel):
    statement: AccountStatementResponse
    matched_lines: list[StatementLineResponse]
    unmatched_lines: list[UnmatchedStatementLineView]
    unmatched_ledger_rows: list[UnmatchedLedgerRow]


class MatchLineRequest(BaseModel):
    transaction_id: uuid.UUID | None = None
    transfer_id: uuid.UUID | None = None
    leg: StatementLineMatchLeg | None = None
