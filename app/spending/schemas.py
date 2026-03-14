import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.spending.models import TransactionType

# ---------------------------------------------------------------------------
# Category schemas
# ---------------------------------------------------------------------------


class CategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    color: str | None = Field(default=None, max_length=20)
    icon: str | None = Field(default=None, max_length=50)


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    color: str | None = Field(default=None, max_length=20)
    icon: str | None = Field(default=None, max_length=50)


class CategoryResponse(BaseModel):
    public_id: uuid.UUID
    workspace_id: int
    name: str
    is_system: bool
    color: str | None
    icon: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Transaction schemas
# ---------------------------------------------------------------------------


class TransactionCreate(BaseModel):
    category_id: uuid.UUID  # public_id of the category
    amount: Decimal = Field(..., gt=0, decimal_places=2)
    type: TransactionType
    occurred_at: datetime
    description: str | None = Field(default=None, max_length=500)

    @field_validator("amount")
    @classmethod
    def validate_scale(cls, v: Decimal) -> Decimal:
        # Enforce exactly 2 dp precision
        if v != v.quantize(Decimal("0.01")):
            raise ValueError("Amount must have at most 2 decimal places")
        return v


class TransactionUpdate(BaseModel):
    category_id: uuid.UUID | None = None  # public_id of the category
    amount: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    type: TransactionType | None = None
    occurred_at: datetime | None = None
    description: str | None = Field(default=None, max_length=500)

    @field_validator("amount")
    @classmethod
    def validate_scale(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v != v.quantize(Decimal("0.01")):
            raise ValueError("Amount must have at most 2 decimal places")
        return v


class TransactionResponse(BaseModel):
    public_id: uuid.UUID
    workspace_id: int
    category_id: uuid.UUID  # exposed as public_id
    amount: Decimal
    type: TransactionType
    occurred_at: datetime
    description: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True, use_enum_values=True)


# ---------------------------------------------------------------------------
# Budget schemas
# ---------------------------------------------------------------------------


class BudgetCreate(BaseModel):
    category_id: uuid.UUID  # public_id of the category
    amount: Decimal = Field(..., gt=0, decimal_places=2)
    month_start: date

    @field_validator("amount")
    @classmethod
    def validate_scale(cls, v: Decimal) -> Decimal:
        if v != v.quantize(Decimal("0.01")):
            raise ValueError("Amount must have at most 2 decimal places")
        return v


class BudgetUpdate(BaseModel):
    amount: Decimal = Field(..., gt=0, decimal_places=2)

    @field_validator("amount")
    @classmethod
    def validate_scale(cls, v: Decimal) -> Decimal:
        if v != v.quantize(Decimal("0.01")):
            raise ValueError("Amount must have at most 2 decimal places")
        return v


class BudgetResponse(BaseModel):
    public_id: uuid.UUID
    workspace_id: int
    category_id: uuid.UUID  # exposed as public_id
    amount: Decimal
    month_start: date
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
