"""Reconcile an external holdings list against LifeStack holdings (spec-103).

The external list comes from another source (INDmoney, Paasa, a broker
statement, ...). This module is read-only: it diffs and *suggests*; it never
writes. Two halves:

* pure helpers (symbol/ISIN normalisation, input parsing, the diff) with no I/O
* ``HoldingReconciliationService`` which loads LifeStack's side with a fixed
  number of bulk queries (holdings, instruments, companies, accounts) -- never
  one query per holding.

Buckets: ``matched``, ``quantity_drift``, ``cost_basis_drift``,
``missing_in_lifestack`` (external has it, LifeStack does not) and
``missing_at_depository`` (LifeStack has it, the external list does not).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.finance.repository import AccountRepository
from app.investing.repository import CompanyRepository, HoldingRepository, InstrumentRepository

MAX_EXTERNAL_ENTRIES = 500
MAX_SYMBOL_LENGTH = 64
# Absolute share tolerance: absorbs float noise in the external payload
# (0.1 + 0.2 style artefacts) and sub-paisa fractional rounding, nothing more.
DEFAULT_QUANTITY_TOLERANCE = Decimal("0.0001")
# Relative average-cost tolerance, in percent. Brokerage/tax mis-recording
# typically moves the average by well under 1%, so the default is tight.
DEFAULT_COST_TOLERANCE_PCT = Decimal("0.5")
MAX_COST_TOLERANCE_PCT = Decimal("100")

STATUS_ORDER = (
    "missing_in_lifestack",
    "quantity_drift",
    "cost_basis_drift",
    "missing_at_depository",
    "matched",
)

_EXCHANGE_PREFIX = re.compile(r"^[A-Z0-9_]{2,10}:")
_SYMBOL_SUFFIXES = (".NS", ".BO", ".NSE", ".BSE", "-EQ", "-BE", "-BZ", "-SM")
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


class ReconciliationInputError(ValueError):
    """The external list or a tolerance is malformed (caller error)."""


def normalize_symbol(raw: str) -> str:
    """Canonical form for matching: upper-case, no exchange prefix/suffix.

    ``"nse:RELIANCE"``, ``"RELIANCE.NS"`` and ``"RELIANCE-EQ"`` all become
    ``"RELIANCE"``. Class-share dots (``BRK.B``) are left alone.
    """
    symbol = re.sub(r"\s+", "", str(raw or "")).upper()
    symbol = _EXCHANGE_PREFIX.sub("", symbol)
    for suffix in _SYMBOL_SUFFIXES:
        if symbol.endswith(suffix) and len(symbol) > len(suffix):
            symbol = symbol[: -len(suffix)]
            break
    return symbol


def normalize_isin(raw: Any) -> str | None:
    """Upper-cased, whitespace-free ISIN, or ``None`` when absent/invalid shape."""
    if raw is None:
        return None
    isin = re.sub(r"\s+", "", str(raw)).upper()
    return isin if _ISIN.match(isin) else None


def _to_decimal(value: Any, label: str, row: int) -> Decimal:
    if isinstance(value, bool):
        raise ReconciliationInputError(f"row {row}: {label} must be a decimal number.")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ReconciliationInputError(f"row {row}: {label} must be a decimal number.") from exc
    if not number.is_finite():
        raise ReconciliationInputError(f"row {row}: {label} must be finite.")
    return number


@dataclass
class ExternalPosition:
    indexes: list[int]
    symbol: str
    isin: str | None
    quantity: Decimal
    avg_cost: Decimal | None
    currency: str | None
    account_public_id: uuid.UUID | None
    sources: list[str] = field(default_factory=list)
    external_refs: list[str] = field(default_factory=list)


def parse_external_list(raw: list[dict[str, Any]]) -> list[ExternalPosition]:
    """Validate and normalise the external list; merge duplicate rows.

    Rows with the same ISIN-or-symbol and the same account are one position
    reported in several lots: quantities add and the average cost is
    quantity-weighted (``None`` if any contributing row lacks a cost).
    """
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_EXTERNAL_ENTRIES:
        raise ReconciliationInputError(
            f"external_list must contain between 1 and {MAX_EXTERNAL_ENTRIES} entries."
        )
    merged: dict[tuple[str, uuid.UUID | None], ExternalPosition] = {}
    cost_complete: dict[tuple[str, uuid.UUID | None], bool] = {}
    cost_numerators: dict[tuple[str, uuid.UUID | None], Decimal] = {}
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ReconciliationInputError(f"row {idx}: each entry must be an object.")
        raw_symbol = str(entry.get("symbol") or "").strip()
        if len(raw_symbol) > MAX_SYMBOL_LENGTH:
            raise ReconciliationInputError(f"row {idx}: symbol is too long.")
        symbol = normalize_symbol(raw_symbol)
        isin = normalize_isin(entry.get("isin"))
        if entry.get("isin") not in (None, "") and isin is None:
            raise ReconciliationInputError(f"row {idx}: isin is not a valid ISIN.")
        if not symbol and not isin:
            raise ReconciliationInputError(f"row {idx}: symbol or isin is required.")
        if entry.get("quantity") is None:
            raise ReconciliationInputError(f"row {idx}: quantity is required.")
        quantity = _to_decimal(entry["quantity"], "quantity", idx)
        if quantity < 0:
            raise ReconciliationInputError(f"row {idx}: quantity must not be negative.")
        avg_cost: Decimal | None = None
        if entry.get("avg_cost") is not None:
            avg_cost = _to_decimal(entry["avg_cost"], "avg_cost", idx)
            if avg_cost < 0:
                raise ReconciliationInputError(f"row {idx}: avg_cost must not be negative.")
        currency = str(entry.get("currency") or "").strip().upper() or None
        if currency is not None and not _CURRENCY.match(currency):
            raise ReconciliationInputError(f"row {idx}: currency must be a 3-letter code.")
        account_public_id: uuid.UUID | None = None
        if entry.get("account_id") not in (None, ""):
            try:
                account_public_id = uuid.UUID(str(entry["account_id"]))
            except ValueError as exc:
                raise ReconciliationInputError(f"row {idx}: account_id must be a UUID.") from exc

        key = (isin or symbol, account_public_id)
        existing = merged.get(key)
        if existing is None:
            merged[key] = ExternalPosition(
                indexes=[idx],
                symbol=symbol,
                isin=isin,
                quantity=quantity,
                avg_cost=avg_cost,
                currency=currency,
                account_public_id=account_public_id,
            )
            cost_complete[key] = avg_cost is not None
            cost_numerators[key] = (avg_cost or Decimal("0")) * quantity
            existing = merged[key]
        else:
            existing.indexes.append(idx)
            existing.quantity += quantity
            existing.symbol = existing.symbol or symbol
            existing.isin = existing.isin or isin
            if existing.currency and currency and existing.currency != currency:
                raise ReconciliationInputError(
                    f"row {idx}: currency {currency} conflicts with {existing.currency} "
                    f"on another row for {existing.isin or existing.symbol}."
                )
            existing.currency = existing.currency or currency
            cost_complete[key] = cost_complete[key] and avg_cost is not None
            cost_numerators[key] += (avg_cost or Decimal("0")) * quantity
        for label, bucket in (
            ("source", existing.sources),
            ("external_ref", existing.external_refs),
        ):
            value = entry.get(label)
            if value not in (None, "") and str(value) not in bucket:
                bucket.append(str(value))

    for key, position in merged.items():
        if len(position.indexes) > 1:
            if cost_complete[key] and position.quantity > 0:
                position.avg_cost = cost_numerators[key] / position.quantity
            else:
                position.avg_cost = None
    return list(merged.values())


@dataclass
class LifestackHolding:
    public_id: uuid.UUID
    account_internal_id: int
    account_public_id: uuid.UUID
    account_name: str
    symbol: str
    isin: str | None
    quantity: Decimal
    avg_cost: Decimal
    currency: str


def parse_tolerances(quantity_tolerance: str, cost_tolerance_pct: str) -> tuple[Decimal, Decimal]:
    try:
        qty = Decimal(str(quantity_tolerance).strip())
        cost = Decimal(str(cost_tolerance_pct).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ReconciliationInputError("tolerances must be decimal numbers.") from exc
    if not qty.is_finite() or qty < 0:
        raise ReconciliationInputError("quantity_tolerance must be a non-negative number.")
    if not cost.is_finite() or not Decimal("0") <= cost <= MAX_COST_TOLERANCE_PCT:
        raise ReconciliationInputError(
            f"cost_tolerance_pct must be between 0 and {MAX_COST_TOLERANCE_PCT}."
        )
    return qty, cost


def _s(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), "f")


def _s2(value: Decimal) -> str:
    """Fixed two-decimal string (percentages and money differences)."""
    return format(value.quantize(Decimal("0.01")), "f")


def _aggregate(holdings: list[LifestackHolding]) -> tuple[Decimal, Decimal | None, str | None]:
    quantity = sum((h.quantity for h in holdings), Decimal("0"))
    currencies = {h.currency.upper() for h in holdings}
    if len(currencies) != 1 or quantity <= 0:
        return quantity, None, next(iter(currencies)) if len(currencies) == 1 else None
    weighted = sum((h.avg_cost * h.quantity for h in holdings), Decimal("0")) / quantity
    return quantity, weighted, next(iter(currencies))


def diff_holdings(
    external: list[ExternalPosition],
    lifestack: list[LifestackHolding],
    *,
    quantity_tolerance: Decimal = DEFAULT_QUANTITY_TOLERANCE,
    cost_tolerance_pct: Decimal = DEFAULT_COST_TOLERANCE_PCT,
) -> dict[str, Any]:
    """Pure diff. Both inputs are already normalised/scoped by the caller."""
    by_isin: dict[str, list[LifestackHolding]] = {}
    by_symbol: dict[str, list[LifestackHolding]] = {}
    for h in lifestack:
        if h.isin:
            by_isin.setdefault(h.isin, []).append(h)
        by_symbol.setdefault(h.symbol, []).append(h)

    consumed: set[uuid.UUID] = set()
    items: list[dict[str, Any]] = []
    warnings: list[str] = []

    for ext in external:
        candidates: list[LifestackHolding] = []
        if ext.isin:
            candidates = list(by_isin.get(ext.isin, []))
        if not candidates and ext.symbol:
            candidates = list(by_symbol.get(ext.symbol, []))
        if ext.account_public_id is not None:
            candidates = [c for c in candidates if c.account_public_id == ext.account_public_id]
        label = ext.isin or ext.symbol

        if any(c.public_id in consumed for c in candidates):
            warnings.append(
                f"{label}: the same LifeStack holding matched more than one external row; "
                "give each external row an account_id to disambiguate."
            )
        ls_qty, ls_cost, ls_currency = _aggregate(candidates)
        consumed.update(c.public_id for c in candidates)

        quantity_delta = ext.quantity - ls_qty
        cost_delta_pct: Decimal | None = None
        currency_mismatch = bool(
            ext.currency and ls_currency and ext.currency.upper() != ls_currency.upper()
        )
        if (
            ext.avg_cost is not None
            and ls_cost is not None
            and ls_cost > 0
            and not currency_mismatch
        ):
            cost_delta_pct = (ext.avg_cost - ls_cost) / ls_cost * Decimal("100")

        if not candidates:
            if ext.quantity <= quantity_tolerance:
                status = "matched"
                suggestion = {
                    "action": "none",
                    "message": f"{label}: zero external quantity and nothing in LifeStack.",
                }
            else:
                status = "missing_in_lifestack"
                price_hint = (
                    f" at about {_s(ext.avg_cost)} {ext.currency or ''}".rstrip()
                    if ext.avg_cost is not None
                    else ""
                )
                suggestion = {
                    "action": "record_buy_orders",
                    "quantity": _s(ext.quantity),
                    "price_per_unit": _s(ext.avg_cost),
                    "message": (
                        f"{label}: external statement holds {_s(ext.quantity)}{price_hint} "
                        "but LifeStack has no such holding -- record the missed buy order(s) "
                        "(create_investment_order) or import the missing statement."
                    ),
                }
        elif abs(quantity_delta) > quantity_tolerance:
            status = "quantity_drift"
            if quantity_delta > 0:
                suggestion = {
                    "action": "record_buy_orders",
                    "quantity": _s(quantity_delta),
                    "message": (
                        f"{label}: external holds {_s(ext.quantity)} vs LifeStack "
                        f"{_s(ls_qty)} (short by {_s(quantity_delta)}) -- look for a missed "
                        "buy, a bonus/split not applied, or an order recorded with the wrong "
                        "quantity."
                    ),
                }
            else:
                suggestion = {
                    "action": "record_sell_orders",
                    "quantity": _s(-quantity_delta),
                    "message": (
                        f"{label}: external holds {_s(ext.quantity)} vs LifeStack "
                        f"{_s(ls_qty)} (excess {_s(-quantity_delta)}) -- look for a missed "
                        "sell, a transfer out, or a buy recorded twice."
                    ),
                }
        elif cost_delta_pct is not None and abs(cost_delta_pct) > cost_tolerance_pct:
            status = "cost_basis_drift"
            assert ext.avg_cost is not None and ls_cost is not None
            total_diff = (ext.avg_cost - ls_cost) * ext.quantity
            suggestion = {
                "action": "review_order_fees",
                "cost_delta_pct": _s2(cost_delta_pct),
                "implied_total_cost_difference": _s2(total_diff),
                "message": (
                    f"{label}: average cost differs (LifeStack {_s(ls_cost)} vs external "
                    f"{_s(ext.avg_cost)}, {_s2(cost_delta_pct)}%, about "
                    f"{_s2(total_diff)} in total) -- check the buy "
                    "orders' brokerage_fee / tax_amount / other_fees for a fee recorded "
                    "twice, missed, or in the wrong currency."
                ),
            }
        else:
            status = "matched"
            suggestion = {"action": "none", "message": f"{label}: matches LifeStack."}
            if currency_mismatch:
                suggestion["message"] += (
                    f" Cost not compared: currency {ext.currency} vs LifeStack {ls_currency}."
                )

        items.append({
            "status": status,
            "symbol": ext.symbol or (candidates[0].symbol if candidates else ""),
            "isin": ext.isin or next((c.isin for c in candidates if c.isin), None),
            "external": {
                "quantity": _s(ext.quantity),
                "avg_cost": _s(ext.avg_cost),
                "currency": ext.currency,
                "account_id": str(ext.account_public_id) if ext.account_public_id else None,
                "sources": ext.sources,
                "external_refs": ext.external_refs,
                "row_indexes": ext.indexes,
            },
            "lifestack": (
                {
                    "quantity": _s(ls_qty),
                    "avg_cost": _s(ls_cost),
                    "currency": ls_currency,
                    "holdings": [
                        {
                            "public_id": str(c.public_id),
                            "account_id": str(c.account_public_id),
                            "account_name": c.account_name,
                            "quantity": _s(c.quantity),
                            "avg_cost": _s(c.avg_cost),
                            "currency": c.currency,
                        }
                        for c in sorted(candidates, key=lambda c: str(c.public_id))
                    ],
                }
                if candidates
                else None
            ),
            "quantity_delta": _s(quantity_delta),
            "cost_delta_pct": (_s2(cost_delta_pct) if cost_delta_pct is not None else None),
            "suggestion": suggestion,
        })

    for h in lifestack:
        if h.public_id in consumed:
            continue
        items.append({
            "status": "missing_at_depository",
            "symbol": h.symbol,
            "isin": h.isin,
            "external": None,
            "lifestack": {
                "quantity": _s(h.quantity),
                "avg_cost": _s(h.avg_cost),
                "currency": h.currency,
                "holdings": [
                    {
                        "public_id": str(h.public_id),
                        "account_id": str(h.account_public_id),
                        "account_name": h.account_name,
                        "quantity": _s(h.quantity),
                        "avg_cost": _s(h.avg_cost),
                        "currency": h.currency,
                    }
                ],
            },
            "quantity_delta": _s(-h.quantity),
            "cost_delta_pct": None,
            "suggestion": {
                "action": "verify_or_remove_stale_holding",
                "quantity": _s(h.quantity),
                "message": (
                    f"{h.symbol}: LifeStack holds {_s(h.quantity)} in {h.account_name} but "
                    "the external statement does not list it -- record the missed sell / "
                    "transfer-out, or confirm the external list covers this account."
                ),
            },
        })

    rank = {status: i for i, status in enumerate(STATUS_ORDER)}
    items.sort(
        key=lambda it: (
            rank[it["status"]],
            it["symbol"],
            (it["lifestack"]["holdings"][0]["public_id"] if it["lifestack"] else ""),
            it["external"]["row_indexes"][0] if it["external"] else -1,
        )
    )
    counts = dict.fromkeys(STATUS_ORDER, 0)
    for it in items:
        counts[it["status"]] += 1
    return {
        "summary": {
            "total_external": len(external),
            "total_lifestack": len(lifestack),
            **counts,
        },
        "items": items,
        "suggestions": [
            {"symbol": it["symbol"], "status": it["status"], **it["suggestion"]}
            for it in items
            if it["status"] != "matched"
        ],
        "warnings": warnings,
    }


class HoldingReconciliationService:
    """Loads LifeStack's side (4 bulk queries) and runs :func:`diff_holdings`."""

    def __init__(
        self,
        holding_repo: HoldingRepository,
        instrument_repo: InstrumentRepository,
        company_repo: CompanyRepository,
        account_repo: AccountRepository,
    ):
        self.holding_repo = holding_repo
        self.instrument_repo = instrument_repo
        self.company_repo = company_repo
        self.account_repo = account_repo

    async def reconcile(
        self,
        workspace_id: int,
        external: list[ExternalPosition],
        *,
        account_public_id: uuid.UUID | None = None,
        quantity_tolerance: Decimal = DEFAULT_QUANTITY_TOLERANCE,
        cost_tolerance_pct: Decimal = DEFAULT_COST_TOLERANCE_PCT,
    ) -> dict[str, Any]:
        accounts, _ = await self.account_repo.list_workspace_accounts(
            workspace_id, limit=10000, offset=0
        )
        account_by_public = {a.public_id: a for a in accounts if a.id is not None}
        account_by_id = {a.id: a for a in accounts if a.id is not None}

        account_filter_id: int | None = None
        if account_public_id is not None:
            scoped = account_by_public.get(account_public_id)
            if scoped is None:
                raise ReconciliationInputError("Account not found in this workspace.")
            account_filter_id = scoped.id
        for ext in external:
            if ext.account_public_id is None:
                continue
            if ext.account_public_id not in account_by_public:
                raise ReconciliationInputError(
                    f"row {ext.indexes[0]}: account_id not found in this workspace."
                )
            if account_public_id is not None and ext.account_public_id != account_public_id:
                raise ReconciliationInputError(
                    f"row {ext.indexes[0]}: account_id differs from the account_id filter."
                )

        holdings, _ = await self.holding_repo.get_all(
            workspace_id,
            limit=None,
            offset=0,
            quantity_state="nonzero",
            account_id=account_filter_id,
            sort_by="symbol",
            sort_direction="asc",
        )
        instruments = await self.instrument_repo.get_by_ids([
            h.instrument_id for h in holdings if h.instrument_id is not None
        ])
        companies = await self.company_repo.get_by_ids([
            i.company_id for i in instruments.values() if i.company_id is not None
        ])

        lifestack: list[LifestackHolding] = []
        for h in holdings:
            account = account_by_id.get(h.account_id)
            if account is None or h.quantity <= 0:
                continue
            instrument = instruments.get(h.instrument_id) if h.instrument_id else None
            company = (
                companies.get(instrument.company_id)
                if instrument is not None and instrument.company_id
                else None
            )
            isin = normalize_isin(instrument.isin if instrument else None) or normalize_isin(
                company.isin if company else None
            )
            lifestack.append(
                LifestackHolding(
                    public_id=h.public_id,
                    account_internal_id=h.account_id,
                    account_public_id=account.public_id,
                    account_name=account.name,
                    symbol=normalize_symbol(h.symbol),
                    isin=isin,
                    quantity=h.quantity,
                    avg_cost=h.avg_cost,
                    currency=h.currency,
                )
            )

        result = diff_holdings(
            external,
            lifestack,
            quantity_tolerance=quantity_tolerance,
            cost_tolerance_pct=cost_tolerance_pct,
        )
        if account_public_id is None and len({h.account_internal_id for h in lifestack}) > 1:
            result["warnings"].append(
                "No account_id given: LifeStack holdings in every account were compared. "
                "missing_at_depository rows are false positives for accounts the external "
                "list does not cover -- pass account_id to scope the comparison."
            )
        return result
