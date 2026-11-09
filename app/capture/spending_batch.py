"""Pure helpers for idempotent spending writes and date-ranged spending reads.

Spec-101. Kept free of database access so the dedupe/multiplicity planning,
local-day windows and cursor handling can be unit-tested directly; the
``AgentTools`` methods own all I/O.
"""

import base64
import binascii
import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from decimal import Decimal
from typing import Any

MAX_BATCH_ITEMS = 50

# Fields a batch item may carry. Anything else is rejected rather than ignored:
# a typo such as ``sourceref`` would otherwise silently drop idempotency.
ALLOWED_ITEM_FIELDS = frozenset({
    "amount",
    "category_name",
    "description",
    "account_name",
    "occurred_at",
    "tags",
    "transaction_type",
    "source_ref",
    "allow_duplicate",
})

_CENT = Decimal("0.01")


def normalize_description(value: str | None) -> str:
    """Lower-case, trim and collapse whitespace so ``" Chai  "`` == ``"chai"``."""
    return " ".join((value or "").strip().lower().split())


def normalize_amount(value: Decimal) -> Decimal:
    """Canonical 2dp form so ``Decimal('50')`` and ``Decimal('50.00')`` share a key."""
    return value.quantize(_CENT)


def local_day_range_utc(start_day: date, end_day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    """UTC ``[start, end)`` covering ``start_day``..``end_day`` inclusive in ``tz``.

    The end is the *start of the following local day* (exclusive), so a record at
    23:59:59.999999 local is in the range and one at 00:00:00 the next day is not,
    whatever the UTC offset or DST shift on those days.
    """
    start = datetime.combine(start_day, time.min, tzinfo=tz)
    end = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


@dataclass
class ResolvedSpend:
    """One validated batch item, with every reference already resolved."""

    index: int
    amount: Decimal
    category: Any
    account: Any
    occurred_at: datetime
    local_day: date
    description: str | None
    tx_type: Any
    source_ref: str | None
    tags: list[str]
    allow_duplicate: bool
    warnings: list[str] = field(default_factory=list)

    @property
    def normalized_description(self) -> str:
        return normalize_description(self.description)

    @property
    def natural_key(self) -> tuple:
        """date + amount + account + normalised description (within one type)."""
        return natural_key(
            self.account.id,
            getattr(self.tx_type, "value", self.tx_type),
            self.amount,
            self.local_day,
            self.description,
        )


def natural_key(
    account_id: int | None,
    tx_type: str,
    amount: Decimal,
    local_day: date,
    description: str | None,
) -> tuple:
    return (
        account_id,
        tx_type,
        normalize_amount(amount),
        local_day,
        normalize_description(description),
    )


def group_existing_by_natural_key(rows: Iterable[Any], tz: tzinfo) -> dict[tuple, list[Any]]:
    """Bucket existing ledger rows by natural key, oldest first within a bucket."""
    grouped: dict[tuple, list[Any]] = defaultdict(list)
    for row in rows:
        grouped[
            natural_key(
                row.account_id,
                getattr(row.type, "value", row.type),
                row.amount,
                row.occurred_at.astimezone(tz).date(),
                row.description,
            )
        ].append(row)
    return grouped


@dataclass
class NaturalKeyMatch:
    existing: Any


def plan_natural_key_matches(
    items: Sequence[ResolvedSpend],
    existing_by_key: Mapping[tuple, Sequence[Any]],
) -> dict[int, NaturalKeyMatch | None]:
    """Multiplicity-aware natural-key matching.

    For each key, the ledger holds ``e`` rows and the batch brings ``k`` items.
    The first ``min(e, k)`` items (in batch order) are matched one-to-one with
    existing rows; the remaining ``k - e`` are genuinely new. So one existing
    chai plus two requested chais -> one match, one create (neither "skip both"
    nor "create both"), and replaying the same batch afterwards matches both.
    Returns ``{item.index: match or None}``.
    """
    consumed: dict[tuple, int] = defaultdict(int)
    plan: dict[int, NaturalKeyMatch | None] = {}
    for item in items:
        key = item.natural_key
        bucket = existing_by_key.get(key, ())
        position = consumed[key]
        if position < len(bucket):
            plan[item.index] = NaturalKeyMatch(existing=bucket[position])
            consumed[key] = position + 1
        else:
            plan[item.index] = None
    return plan


def same_payload(existing: Any, item: ResolvedSpend, tz: tzinfo) -> bool:
    """Whether an existing row (matched by source_ref) agrees with the request."""
    return (
        normalize_amount(existing.amount) == normalize_amount(item.amount)
        and getattr(existing.type, "value", existing.type)
        == getattr(item.tx_type, "value", item.tx_type)
        and existing.account_id == item.account.id
        and normalize_description(existing.description) == item.normalized_description
        and existing.occurred_at.astimezone(tz).date() == item.local_day
    )


def same_item_payload(a: ResolvedSpend, b: ResolvedSpend) -> bool:
    """Whether two requests in one batch describe the same transaction."""
    return (
        normalize_amount(a.amount) == normalize_amount(b.amount)
        and getattr(a.tx_type, "value", a.tx_type) == getattr(b.tx_type, "value", b.tx_type)
        and a.account.id == b.account.id
        and a.normalized_description == b.normalized_description
        and a.local_day == b.local_day
    )


def item_result(
    index: int,
    status: str,
    *,
    match_reason: str | None = None,
    existing_id: str | None = None,
    errors: Sequence[str] = (),
    warnings: Sequence[str] = (),
    **extra: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "index": index,
        "status": status,
        "match_reason": match_reason,
        "existing_id": existing_id,
        "errors": list(errors),
        "warnings": list(warnings),
    }
    result.update(extra)
    return result


def count_results(results: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = {"create": 0, "duplicate": 0, "possible_duplicate": 0, "invalid": 0}
    for result in results:
        status = result["status"]
        if status in ("created", "would_create"):
            counts["create"] += 1
        elif status in counts:
            counts[status] += 1
    return counts


# ---- cursor pagination -------------------------------------------------------


class CursorError(ValueError):
    """The supplied pagination cursor is malformed or belongs to other filters."""


def filter_fingerprint(**parts: Any) -> str:
    """Short stable hash of the query filters a cursor was issued for."""
    blob = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def encode_cursor(offset: int, fingerprint: str) -> str:
    payload = json.dumps({"o": max(0, int(offset)), "f": fingerprint}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("ascii")).decode("ascii")


def decode_cursor(cursor: str | None, fingerprint: str) -> int:
    """Offset encoded in ``cursor`` (0 when absent); raises CursorError if invalid.

    The fingerprint binds a cursor to the filters it was issued for, so a cursor
    can't be replayed against a different date range / search and silently skip
    or repeat rows.
    """
    if cursor is None or (isinstance(cursor, str) and not cursor.strip()):
        return 0
    if not isinstance(cursor, str):
        raise CursorError("cursor must be the opaque string returned as next_cursor.")
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.strip().encode("ascii")))
        offset = int(data["o"])
        cursor_fingerprint = data["f"]
    except (binascii.Error, ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise CursorError("cursor is invalid; use the next_cursor from the previous page.") from exc
    if offset < 0 or cursor_fingerprint != fingerprint:
        raise CursorError("cursor does not match these filters; restart from the first page.")
    return offset
