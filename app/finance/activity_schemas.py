"""Unified activity-feed schemas (spec-097).

A single paginated response type that merges spending transactions,
capital transfers, investing orders, and dividends into a chronological
stream.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class ActivityFeedItem(BaseModel):
    """One event in the unified activity feed."""

    id: uuid.UUID
    event_type: str  # "spend", "transfer", "order", "dividend"
    date: datetime
    description: str
    amount: str  # signed decimal string (negative = outflow)
    currency: str

    # Account context
    account_id: uuid.UUID
    account_name: str
    account_type: str  # "wallet", "bank", "brokerage", "card", "gift_card"

    # For transfers: the other side
    counterpart_account_id: uuid.UUID | None = None
    counterpart_account_name: str | None = None

    # For spending: category info
    category_name: str | None = None
    category_color: str | None = None
    category_icon: str | None = None

    # For orders: investment details
    symbol: str | None = None
    order_type: str | None = None  # "buy" or "sell"
    quantity: str | None = None
    price_per_unit: str | None = None

    # For cross-currency transfers
    fx_rate: str | None = None
    fx_display: str | None = None  # human-friendly, e.g. "95 INR/USD"

    # For dividends
    income_type: str | None = None  # "dividend", "interest", "coupon"

    # Back-reference to original entity
    source_ref: uuid.UUID

    model_config = ConfigDict(json_encoders={Decimal: str})


class ActivityFeedResponse(BaseModel):
    """Paginated activity feed response."""

    items: list[ActivityFeedItem]
    total: int
    limit: int
    offset: int
