"""Read-only capital-gains what-if service (spec-106).

The DB-facing adapter for the pure functions in
``app/investing/tax/simulation.py``. It maps ``Holding`` rows and their open
``OrderLot`` rows into ``HoldingSnapshot``/``LotSnapshot``, reuses
``TaxService`` for the workspace tax profile, regime classification, the
Rule 115 FX lookup and the FY's already-realised lots, and hands everything
to the pure layer. It never writes: no holding, lot, order or price row is
touched (the only incidental write is ``TaxService``'s lazy default tax
profile row, which the MCP read tools never commit).

Time handling: a sale date is a *calendar day in the user's timezone*
(default Asia/Kolkata — Indian tax law counts IST days). Lot timestamps are
converted into that same zone before the pure layer collapses them to days,
so the holding-period test, the financial year and the Rule 115 month are
all decided by the local day rather than by UTC.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.finance.models import Account
from app.investing.models import Holding, HoldingPrice
from app.investing.repository import HoldingPriceRepository, HoldingRepository, LotRepository
from app.investing.tax.engine import REPORTING_CURRENCY, sale_month_key, specified_fx_date
from app.investing.tax.fy import fy_label_for_date
from app.investing.tax.simulation import (
    HarvestCandidate,
    HoldingSnapshot,
    LotSnapshot,
    SaleSimulation,
    TaxLotView,
    harvest_candidates,
    list_tax_lots,
    simulate_sale,
)
from app.investing.tax.tax_profile import TaxProfile
from app.investing.tax_service import TaxService

DEFAULT_TIMEZONE = "Asia/Kolkata"
STALE_PRICE_DAYS = 7
"""A holding price older than this many days is flagged ``stale`` (not dropped)."""

_MAX_MAGNITUDE = Decimal("1000000000000")  # keeps Decimal products inside context precision
_MIN_DATE = date(2000, 1, 1)
_MAX_DATE = date(2100, 12, 31)


class WhatIfError(Exception):
    """A request the what-if tools reject, with a stable machine code."""

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


# --------------------------------------------------------------------------- #
# Input parsing (pure helpers)
# --------------------------------------------------------------------------- #


def resolve_zone(name: str | None) -> ZoneInfo:
    """The user's IANA zone, or the Indian-tax default when unset/invalid."""
    for candidate in (name, DEFAULT_TIMEZONE):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")  # pragma: no cover - tzdata always ships Asia/Kolkata


def local_today(tz: ZoneInfo, now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(tz).date()


def day_start(value: date, tz: ZoneInfo) -> datetime:
    """Midnight of ``value`` in ``tz`` (timezone-aware)."""
    return datetime(value.year, value.month, value.day, tzinfo=tz)


def parse_sale_date(raw: str, tz: ZoneInfo) -> datetime:
    """Parse ``YYYY-MM-DD`` (or an ISO datetime) into a local-day datetime.

    A bare date is already a calendar day. A datetime with an offset is
    converted into ``tz`` first (so ``2027-03-31T20:00Z`` is 1 April in
    IST); a naive datetime is taken as local time.
    """
    text = (raw or "").strip()
    if not text:
        raise WhatIfError("invalid_date", "date is required (YYYY-MM-DD).")
    try:
        parsed_date = date.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise WhatIfError("invalid_date", "date must be YYYY-MM-DD.") from None
        parsed_date = (parsed.astimezone(tz) if parsed.tzinfo else parsed).date()
    if not _MIN_DATE <= parsed_date <= _MAX_DATE:
        raise WhatIfError("invalid_date", "date must be between 2000-01-01 and 2100-12-31.")
    return day_start(parsed_date, tz)


def parse_decimal(
    label: str, raw: object, *, allow_zero: bool, max_places: int | None = None
) -> Decimal:
    """Parse a Decimal string with explicit bounds; never accepts NaN/Infinity."""
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise WhatIfError("invalid_number", f"{label} must be a decimal number.") from None
    if not value.is_finite():
        raise WhatIfError("invalid_number", f"{label} must be a finite decimal number.")
    if value < 0 or (value == 0 and not allow_zero):
        raise WhatIfError(
            "invalid_number",
            f"{label} must be {'zero or ' if allow_zero else ''}greater than zero.",
        )
    if value > _MAX_MAGNITUDE:
        raise WhatIfError("invalid_number", f"{label} is unreasonably large.")
    exponent = value.as_tuple().exponent
    if max_places is not None and isinstance(exponent, int) and -exponent > max_places:
        raise WhatIfError("invalid_number", f"{label} allows at most {max_places} decimal places.")
    return value


# --------------------------------------------------------------------------- #
# Result containers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PriceInfo:
    unit_price: Decimal
    price_date: date
    source: str
    age_days: int
    is_stale: bool


@dataclass(frozen=True)
class FxInfo:
    currency: str
    rate: Decimal
    as_of: date | None
    source: str


@dataclass
class HoldingContext:
    """A holding with everything the pure layer and the views need."""

    holding: Holding
    account: Account | None
    snapshot: HoldingSnapshot
    regime_detail: str
    lots_quantity: Decimal

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        held = Decimal(self.holding.quantity)
        if self.lots_quantity != held:
            out.append(
                f"Holding quantity is {held} but its open lots total {self.lots_quantity}; "
                "tax lots are built from recorded buy orders, so the difference is "
                "invisible to these tools (imported or manually-set holdings have no lots)."
            )
        return out


@dataclass
class LotListing:
    context: HoldingContext
    as_of: date
    timezone: str
    lots: list[TaxLotView]
    price: PriceInfo | None
    fx: FxInfo | None
    warnings: list[str] = field(default_factory=list)


@dataclass
class SimulationOutcome:
    context: HoldingContext
    simulation: SaleSimulation
    timezone: str
    sell_fees: Decimal
    specified_fx_date: date
    warnings: list[str] = field(default_factory=list)


@dataclass
class HarvestRow:
    candidate: HarvestCandidate
    context: HoldingContext
    price: PriceInfo
    fifo_position: int
    acquired_on: date


@dataclass
class HarvestReport:
    as_of: date
    timezone: str
    rows: list[HarvestRow]
    skipped: list[dict[str, str]]
    fx_rates: list[FxInfo]
    missing_fx_currencies: list[str]
    holdings_considered: int
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #


class TaxWhatIfService:
    """Builds snapshots from the DB and runs the pure what-if functions."""

    def __init__(
        self,
        tax_service: TaxService,
        holding_repository: HoldingRepository,
        lot_repository: LotRepository,
        price_repository: HoldingPriceRepository,
    ) -> None:
        self.tax_service = tax_service
        self.holding_repository = holding_repository
        self.lot_repository = lot_repository
        self.price_repository = price_repository

    # ------------------------------------------------------- resolution

    async def resolve_holding(
        self,
        workspace_id: int,
        holding_ref: str,
        account_public_id: str | None = None,
    ) -> Holding:
        """A holding by public id, or by symbol (+ account when ambiguous).

        Every lookup is workspace-scoped, so another workspace's holding is
        indistinguishable from a missing one. A symbol held in several
        accounts is rejected with the candidates rather than guessed.
        """
        ref = (holding_ref or "").strip()
        if not ref:
            raise WhatIfError("holding_required", "holding is required (public id or symbol).")

        account: Account | None = None
        if account_public_id:
            try:
                account_uuid = uuid.UUID(account_public_id)
            except (TypeError, ValueError):
                raise WhatIfError("invalid_account", "account_id must be a UUID.") from None
            account = await self.tax_service.account_repository.get_by_public_id(
                workspace_id, account_uuid
            )
            if account is None or account.id is None:
                raise WhatIfError("account_not_found", "Account not found in this workspace.")

        try:
            holding_uuid = uuid.UUID(ref)
        except ValueError:
            holding_uuid = None
        if holding_uuid is not None:
            found = await self.holding_repository.get_by_public_id(workspace_id, holding_uuid)
            if found is None or (account is not None and found.account_id != account.id):
                raise WhatIfError("holding_not_found", "Holding not found in this workspace.")
            return found

        matches, _ = await self.holding_repository.get_all(
            workspace_id,
            limit=None,
            quantity_state="all",
            symbol=ref.upper(),
            account_id=account.id if account is not None else None,
            sort_by="created_at",
            sort_direction="asc",
        )
        if not matches:
            raise WhatIfError("holding_not_found", f"No holding with symbol '{ref.upper()}'.")
        if len(matches) > 1:
            accounts = await self.tax_service.account_repository.get_by_ids(
                workspace_id, {h.account_id for h in matches}
            )
            raise WhatIfError(
                "ambiguous_holding",
                f"Symbol '{ref.upper()}' is held in {len(matches)} accounts; "
                "pass the holding's public id or an account_id.",
                candidates=[
                    {
                        "holding_id": str(h.public_id),
                        "account_id": str(accounts[h.account_id].public_id)
                        if h.account_id in accounts
                        else None,
                        "account_name": accounts[h.account_id].name
                        if h.account_id in accounts
                        else None,
                        "quantity": str(h.quantity),
                    }
                    for h in matches
                ],
            )
        return matches[0]

    # ------------------------------------------------------- snapshots

    async def _contexts(
        self,
        workspace_id: int,
        holdings: list[Holding],
        profile: TaxProfile,
        tz: ZoneInfo,
    ) -> list[HoldingContext]:
        """Snapshots for many holdings using a fixed number of queries."""
        if not holdings:
            return []
        accounts = await self.tax_service.account_repository.get_by_ids(
            workspace_id, {h.account_id for h in holdings}
        )
        lots_by_holding = await self.lot_repository.list_open_for_holdings(
            workspace_id, [h.id for h in holdings if h.id is not None]
        )
        regimes = await self.tax_service.regimes_for_holdings(
            workspace_id, profile, holdings, accounts
        )
        contexts: list[HoldingContext] = []
        for holding in holdings:
            assert holding.id is not None
            account = accounts.get(holding.account_id)
            regime_spec, source, detail = regimes[holding.id]
            lots = tuple(
                LotSnapshot(
                    lot_id=lot.id,
                    quantity=Decimal(lot.remaining_quantity),
                    cost_per_unit=Decimal(lot.cost_per_unit),
                    acquired_at=_aware(lot.acquired_at).astimezone(tz),
                )
                for lot in lots_by_holding.get(holding.id, [])
                if Decimal(lot.remaining_quantity) > 0
            )
            contexts.append(
                HoldingContext(
                    holding=holding,
                    account=account,
                    snapshot=HoldingSnapshot(
                        holding_id=holding.id,
                        symbol=holding.symbol,
                        account_id=str(account.public_id) if account else "",
                        account_name=account.name if account else "",
                        currency=holding.currency.upper(),
                        regime=regime_spec.key,
                        regime_source=source,
                        lots=lots,
                    ),
                    regime_detail=detail,
                    lots_quantity=sum((lot.quantity for lot in lots), Decimal("0")),
                )
            )
        return contexts

    async def _prices(
        self, workspace_id: int, holding_ids: list[int], today: date
    ) -> dict[int, PriceInfo]:
        rows: dict[int, HoldingPrice] = await self.price_repository.latest_prices_on_or_before_bulk(
            workspace_id, holding_ids, today
        )
        out: dict[int, PriceInfo] = {}
        for holding_id, row in rows.items():
            age = (today - row.price_date).days
            out[holding_id] = PriceInfo(
                unit_price=Decimal(row.unit_price),
                price_date=row.price_date,
                source=row.source,
                age_days=age,
                is_stale=age > STALE_PRICE_DAYS,
            )
        return out

    async def _market_fx(self, currencies: set[str], now: datetime) -> dict[str, FxInfo]:
        """Latest stored market rate to INR per currency (INR implicit)."""
        wanted = {c.upper() for c in currencies if c and c.upper() != REPORTING_CURRENCY}
        repo = self.tax_service.fx_rate_repository
        out: dict[str, FxInfo] = {}
        if wanted and repo is not None:
            rows = await repo.get_latest_rates_for_pairs(
                [(c, REPORTING_CURRENCY) for c in sorted(wanted)], as_of=now
            )
            for (base, _quote), row in rows.items():
                out[base] = FxInfo(
                    currency=base,
                    rate=Decimal(str(row.rate)),
                    as_of=row.as_of.date() if row.as_of else None,
                    source="app_market_rate",
                )
        return out

    # ------------------------------------------------------- list lots

    async def list_lots(
        self,
        workspace_id: int,
        *,
        holding_ref: str,
        account_id: str | None,
        timezone_name: str | None,
        now: datetime | None = None,
    ) -> LotListing:
        tz = resolve_zone(timezone_name)
        now = now or datetime.now(UTC)
        today = local_today(tz, now)
        holding = await self.resolve_holding(workspace_id, holding_ref, account_id)
        profile = await self.tax_service.load_profile(workspace_id)
        [context] = await self._contexts(workspace_id, [holding], profile, tz)

        assert holding.id is not None
        price = (await self._prices(workspace_id, [holding.id], today)).get(holding.id)
        fx = (await self._market_fx({context.snapshot.currency}, now)).get(
            context.snapshot.currency
        )
        fx_rate = (
            Decimal("1")
            if context.snapshot.currency == REPORTING_CURRENCY
            else (fx.rate if fx is not None else None)
        )
        views = list_tax_lots(
            context.snapshot,
            as_of=day_start(today, tz),
            current_price=price.unit_price if price else None,
            fx_rate=fx_rate,
        )
        warnings = list(context.warnings)
        if price is None:
            warnings.append("No stored price for this holding; unrealised P&L is not shown.")
        elif price.is_stale:
            warnings.append(
                f"Latest price is {price.age_days} days old ({price.price_date}); "
                "unrealised P&L may be stale."
            )
        if price is not None and fx_rate is None:
            warnings.append(
                f"No stored {context.snapshot.currency}->INR rate; INR P&L is not shown."
            )
        return LotListing(
            context=context,
            as_of=today,
            timezone=str(tz),
            lots=views,
            price=price,
            fx=(
                fx
                if fx is not None
                else (
                    FxInfo(REPORTING_CURRENCY, Decimal("1"), None, "reporting_currency")
                    if context.snapshot.currency == REPORTING_CURRENCY
                    else None
                )
            ),
            warnings=warnings,
        )

    # ------------------------------------------------------- simulate

    async def simulate_sale(
        self,
        workspace_id: int,
        *,
        holding_ref: str,
        account_id: str | None,
        qty: Decimal,
        sale_date: datetime,
        sale_price: Decimal,
        sell_fees: Decimal,
        timezone_name: str | None,
        now: datetime | None = None,
    ) -> SimulationOutcome:
        tz = resolve_zone(timezone_name)
        now = now or datetime.now(UTC)
        today = local_today(tz, now)
        holding = await self.resolve_holding(workspace_id, holding_ref, account_id)
        profile = await self.tax_service.load_profile(workspace_id)
        [context] = await self._contexts(workspace_id, [holding], profile, tz)

        fy_label = fy_label_for_date(sale_date)
        # The FY's already-realised lots come from the same computation
        # get_tax_summary uses, so the simulation prices the sale on top of
        # exactly the gains the real estimate shows.
        baseline = await self.tax_service.compute(workspace_id, fy=fy_label)
        realised = baseline.lots if baseline.status == "success" else []

        currency = context.snapshot.currency
        pairs = {(sale_month_key(sale_date.date()), currency)}
        pairs |= {(sale_month_key(lot.sold_at.date()), lot.currency.upper()) for lot in realised}
        app_fx = await self.tax_service.app_fx_rates(sorted(pairs))

        simulation = simulate_sale(
            context.snapshot,
            qty=qty,
            sale_date=sale_date,
            sale_price=sale_price,
            profile=profile,
            app_fx_rates=app_fx,
            sell_fees=sell_fees,
            realised_lots=realised,
        )
        warnings = list(context.warnings)
        if baseline.status != "success":
            # The FY's existing numbers cannot be trusted (or are missing a
            # rate), so an incremental figure on top of them would be wrong.
            # Surface the baseline's status instead of a guessed number.
            simulation = replace(
                simulation,
                status=baseline.status,
                message=baseline.message,
                gain_inr=None,
                tax_inr=None,
                tax_before_inr=None,
                tax_after_inr=None,
                fy_realised_lots=0,
                fy_realised_gain_inr=None,
                fy_realised_ltcg_inr=None,
                ltcg_exemption_remaining_inr=None,
                ltcg_exemption_used_inr=None,
                assumptions=[
                    *simulation.assumptions,
                    f"The FY's existing realised gains could not be computed "
                    f"({baseline.status}), so no incremental tax is shown.",
                ],
            )
        specified = specified_fx_date(sale_date.date())
        if simulation.fx_source == "app_rate_estimate" and specified > today:
            simulation = replace(
                simulation,
                assumptions=[
                    *simulation.assumptions,
                    f"The Rule 115 date {specified} has not occurred yet; the latest stored "
                    "rate was used as a placeholder.",
                ],
            )
        return SimulationOutcome(
            context=context,
            simulation=simulation,
            timezone=str(tz),
            sell_fees=sell_fees,
            specified_fx_date=specified,
            warnings=warnings,
        )

    # ------------------------------------------------------- harvest

    async def harvest(
        self,
        workspace_id: int,
        *,
        stcg_rate_percent: Decimal | None,
        timezone_name: str | None,
        now: datetime | None = None,
    ) -> HarvestReport:
        tz = resolve_zone(timezone_name)
        now = now or datetime.now(UTC)
        today = local_today(tz, now)
        profile = await self.tax_service.load_profile(workspace_id)
        holdings, _ = await self.holding_repository.get_all(
            workspace_id,
            limit=None,
            quantity_state="nonzero",
            sort_by="symbol",
            sort_direction="asc",
        )
        contexts = await self._contexts(workspace_id, list(holdings), profile, tz)
        by_id = {c.snapshot.holding_id: c for c in contexts}
        prices = await self._prices(workspace_id, list(by_id), today)
        fx = await self._market_fx({c.snapshot.currency for c in contexts}, now)

        skipped: list[dict[str, str]] = []
        eligible: list[HoldingSnapshot] = []
        for context in contexts:
            snap = context.snapshot
            label = {
                "holding_id": str(context.holding.public_id),
                "symbol": snap.symbol,
                "account_name": snap.account_name,
            }
            if not snap.lots:
                skipped.append({
                    **label,
                    "reason": "no_open_lots",
                    "detail": "Holding has no open tax lots (no recorded buy orders).",
                })
            elif snap.holding_id not in prices:
                skipped.append({
                    **label,
                    "reason": "no_price",
                    "detail": "No stored price for this holding; not guessed.",
                })
            else:
                eligible.append(snap)

        candidates = harvest_candidates(
            eligible,
            {hid: p.unit_price for hid, p in prices.items()},
            as_of=day_start(today, tz),
            stcg_rate_pct=(
                stcg_rate_percent / Decimal("100") if stcg_rate_percent is not None else None
            ),
            fx_rates={code: info.rate for code, info in fx.items()},
        )

        # Positions: the lot's FIFO rank within its holding.
        position: dict[tuple[int, int | None], int] = {}
        acquired_on: dict[tuple[int, int | None], date] = {}
        for snap in eligible:
            for index, lot in enumerate(
                sorted(snap.lots, key=lambda x: (x.acquired_at, x.lot_id or 0)), start=1
            ):
                position[(snap.holding_id, lot.lot_id)] = index
                acquired_on[(snap.holding_id, lot.lot_id)] = lot.acquired_at.astimezone(tz).date()
        rows = [
            HarvestRow(
                candidate=c,
                context=by_id[c.holding_id],
                price=prices[c.holding_id],
                fifo_position=position[(c.holding_id, c.lot_id)],
                acquired_on=acquired_on[(c.holding_id, c.lot_id)],
            )
            for c in candidates
        ]
        needed = {c.snapshot.currency for c in contexts if c.snapshot.lots}
        missing_fx = sorted(
            code for code in needed if code != REPORTING_CURRENCY and code not in fx
        )
        warnings: list[str] = []
        stale = sorted({r.context.snapshot.symbol for r in rows if r.price.is_stale})
        if stale:
            warnings.append(
                f"Prices older than {STALE_PRICE_DAYS} days were used for: {', '.join(stale)}."
            )
        if missing_fx:
            warnings.append(
                "No stored INR rate for "
                + ", ".join(missing_fx)
                + "; those lots' tax saved is shown in native currency only and ranked last."
            )
        return HarvestReport(
            as_of=today,
            timezone=str(tz),
            rows=rows,
            skipped=skipped,
            fx_rates=sorted(fx.values(), key=lambda f: f.currency),
            missing_fx_currencies=missing_fx,
            holdings_considered=len(contexts),
            warnings=warnings,
        )


def _aware(value: datetime) -> datetime:
    """DB timestamps are timestamptz; treat a stray naive value as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = [
    "DEFAULT_TIMEZONE",
    "STALE_PRICE_DAYS",
    "FxInfo",
    "HarvestReport",
    "HarvestRow",
    "HoldingContext",
    "LotListing",
    "PriceInfo",
    "SimulationOutcome",
    "TaxWhatIfService",
    "WhatIfError",
    "day_start",
    "local_today",
    "parse_decimal",
    "parse_sale_date",
    "resolve_zone",
]
