"""Theme tags, look-through exposure and allocation-vs-target (spec-105).

Three public entry points:

* :class:`HoldingThemeTagService` - set/get theme tags (with optional target
  weights) on holdings.
* :class:`ThemeExposureService.lookthrough_exposure` - roll the portfolio up to
  company / sector / country / theme, decomposing ETF and mutual-fund holdings
  through their latest stored constituent snapshot.
* :class:`ThemeExposureService.allocation_vs_target` - theme drift vs target and
  a "no sell to rebalance" new-cash deployment plan.

Domain rules (see ``docs/specs/spec-105-lookthrough-exposure-theme-targets.md``):

* Exactly ONE constituent snapshot per fund is used: the newest by
  ``(as_of_date, fetched_at)``. Duplicate snapshots never double count.
* Placeholder rows (``OTHER-...``), funds without a snapshot, the part of a
  fund not covered by its snapshot and unknown companies are reported as
  *Not itemised* and are never rescaled to 100%.
* All arithmetic is ``Decimal``. Rounded buckets are allocated with the
  largest-remainder method so partitions sum *exactly* to the total.
* A holding or FX rate that cannot be valued is excluded with an explicit
  warning, never silently valued at zero.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from app.core.currency import build_required_pairs, convert_amount, effective_display_as_of
from app.core.currency import fx_rates_used as build_fx_rates_used
from app.core.exceptions import NotFoundError, ValidationError
from app.finance.models import FxRate
from app.finance.repository import FinanceSettingRepository, FxRateRepository
from app.investing.models import Company, Holding, HoldingThemeTag, Instrument, InstrumentType
from app.investing.repository import (
    CompanyRepository,
    HoldingPriceRepository,
    HoldingRepository,
    InstrumentRepository,
)
from app.investing.theme_repository import (
    HoldingThemeTagRepository,
    LatestConstituentSnapshotRepository,
)
from app.investing.theme_schemas import (
    AllocationHoldingRow,
    AllocationThemeRow,
    AllocationVsTargetResponse,
    FundCoverageRow,
    GroupBy,
    HoldingThemeTagResponse,
    HoldingThemeTagsInput,
    HoldingThemeTagsResult,
    LookthroughBucketRow,
    LookthroughCompanyRow,
    LookthroughExposureResponse,
    LookthroughThemeRow,
    ThemeTargetRow,
)

CENT = Decimal("0.01")
ZERO = Decimal("0")
HUNDRED = Decimal("100")
DEFAULT_REPORTING_CURRENCY = "INR"
NOT_ITEMISED_KEY = "not_itemised"
NOT_ITEMISED_LABEL = "Not itemised"
UNCLASSIFIED_SECTOR = "Unclassified"
UNKNOWN_COUNTRY = "Unknown"
MAX_NEW_CASH = Decimal("1000000000000")  # 1e12 - sanity bound, not a business rule
_WEIGHT_TOLERANCE = Decimal("0.0001")
_PLACEHOLDER_RE = re.compile(r"^OTHER-", re.IGNORECASE)


def _q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _pct(part: Decimal, whole: Decimal) -> Decimal:
    """``part / whole * 100`` rounded to 2 dp; 0 when *whole* is not positive."""
    if whole <= 0:
        return ZERO
    return _q(part / whole * HUNDRED)


def _floor_cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_FLOOR)


def allocate_exact[K: Hashable](raw: Mapping[K, Decimal], total: Decimal) -> dict[K, Decimal]:
    """Round non-negative *raw* amounts to cents so they sum exactly to ``round(total)``.

    Largest-remainder method: floor every amount to a cent, then hand the
    leftover cents to the amounts with the largest discarded fraction. *raw*
    must sum to *total* (within Decimal precision).
    """
    floors = {key: _floor_cents(value) for key, value in raw.items()}
    remaining = int((_q(total) - sum(floors.values(), ZERO)) / CENT)
    order = sorted(raw, key=lambda key: raw[key] - floors[key], reverse=True)
    index = 0
    while remaining > 0 and order:
        key = order[index % len(order)]
        floors[key] += CENT
        remaining -= 1
        index += 1
    return floors


def validate_new_cash(new_cash: Decimal) -> Decimal:
    """Return *new_cash* if it is a finite, non-negative amount with at most 2 dp."""
    if not new_cash.is_finite():
        raise ValidationError(detail="new_cash must be a finite number")
    if new_cash < 0:
        raise ValidationError(detail="new_cash must not be negative")
    if new_cash > MAX_NEW_CASH:
        raise ValidationError(detail="new_cash is too large")
    if new_cash != _q(new_cash):
        raise ValidationError(detail="new_cash must have at most 2 decimal places")
    return _q(new_cash)


# ---------------------------------------------------------------------------
# Theme tags
# ---------------------------------------------------------------------------


class HoldingThemeTagService:
    """Reads and writes theme tags on holdings, scoped to one workspace."""

    def __init__(self, tag_repo: HoldingThemeTagRepository) -> None:
        self.tag_repo = tag_repo

    async def set_tags(
        self, workspace_id: int, assignments: Sequence[HoldingThemeTagsInput]
    ) -> HoldingThemeTagsResult:
        """Replace the tag set of each listed holding (bulk, atomic, idempotent).

        A holding carries at most one tag per theme (case-insensitive) and an
        empty ``tags`` list clears it. A theme's target is the sum of its tags'
        targets and the targets of ALL themes in the workspace may not total
        more than 100%; otherwise nothing is written and a 422 is raised. A
        theme already used in the workspace keeps its existing spelling.
        """
        holding_ids = [a.holding_id for a in assignments]
        if len(set(holding_ids)) != len(holding_ids):
            raise ValidationError(detail="Each holding may appear only once per request")
        holdings = await self.tag_repo.holdings_by_public_ids(workspace_id, holding_ids)
        missing = [str(hid) for hid in holding_ids if hid not in holdings]
        if missing:
            raise NotFoundError(detail=f"Holding not found: {', '.join(missing)}")

        for assignment in assignments:
            seen: set[str] = set()
            for tag in assignment.tags:
                key = tag.theme.casefold()
                if key in seen:
                    raise ValidationError(
                        detail=f"Duplicate theme '{tag.theme}' on holding {assignment.holding_id}"
                    )
                seen.add(key)

        replaced_ids = {holdings[hid].id for hid in holding_ids}
        existing = await self.tag_repo.list_for_workspace(workspace_id)
        kept = [(tag, holding) for tag, holding in existing if holding.id not in replaced_ids]
        canonical: dict[str, str] = {}
        for tag, _holding in kept:
            canonical.setdefault(tag.theme.casefold(), tag.theme)

        planned: dict[uuid.UUID, list[tuple[str, Decimal | None]]] = {}
        for assignment in assignments:
            rows: list[tuple[str, Decimal | None]] = []
            for tag in assignment.tags:
                theme = canonical.setdefault(tag.theme.casefold(), tag.theme)
                rows.append((theme, tag.target_weight_pct))
            planned[assignment.holding_id] = rows

        total = sum((t.target_weight_pct or ZERO for t, _ in kept), ZERO)
        total += sum((w or ZERO for rows in planned.values() for _, w in rows), ZERO)
        if total > HUNDRED:
            raise ValidationError(
                detail=(
                    f"Theme targets would total {total}% of the portfolio; the total "
                    "across all themes may not exceed 100%"
                )
            )

        written: list[tuple[HoldingThemeTag, uuid.UUID]] = []
        for hid, rows in planned.items():
            holding = holdings[hid]
            assert holding.id is not None
            stored = await self.tag_repo.replace_for_holding(workspace_id, holding.id, rows)
            written.extend((tag, hid) for tag in stored)

        all_tags = [(t, h.public_id) for t, h in kept] + written
        return self._result(written, all_tags)

    async def get_tags(
        self, workspace_id: int, holding_public_id: uuid.UUID
    ) -> HoldingThemeTagsResult:
        """Tags of one holding plus the workspace-wide theme targets."""
        holdings = await self.tag_repo.holdings_by_public_ids(workspace_id, [holding_public_id])
        if holding_public_id not in holdings:
            raise NotFoundError(detail="Holding not found")
        everything = await self.tag_repo.list_for_workspace(workspace_id)
        mine = [(t, h.public_id) for t, h in everything if h.public_id == holding_public_id]
        return self._result(mine, [(t, h.public_id) for t, h in everything])

    @staticmethod
    def _result(
        tags: Sequence[tuple[HoldingThemeTag, uuid.UUID]],
        all_tags: Sequence[tuple[HoldingThemeTag, uuid.UUID]],
    ) -> HoldingThemeTagsResult:
        by_theme: dict[str, list[tuple[HoldingThemeTag, uuid.UUID]]] = defaultdict(list)
        for tag, hid in all_tags:
            by_theme[tag.theme.casefold()].append((tag, hid))
        targets: list[ThemeTargetRow] = []
        for rows in by_theme.values():
            declared = [t.target_weight_pct for t, _ in rows if t.target_weight_pct is not None]
            targets.append(
                ThemeTargetRow(
                    theme=rows[0][0].theme,
                    target_weight_pct=sum(declared, ZERO) if declared else None,
                    holdings_count=len({hid for _, hid in rows}),
                )
            )
        targets.sort(key=lambda row: row.theme.casefold())
        total = sum((row.target_weight_pct or ZERO for row in targets), ZERO)
        return HoldingThemeTagsResult(
            tags=[
                HoldingThemeTagResponse(
                    public_id=tag.public_id,
                    holding_id=hid,
                    theme=tag.theme,
                    target_weight_pct=tag.target_weight_pct,
                    created_at=tag.created_at,
                    updated_at=tag.updated_at,
                )
                for tag, hid in sorted(tags, key=lambda x: (x[0].theme.casefold(), str(x[1])))
            ],
            theme_targets=targets,
            total_target_pct=total,
            unallocated_target_pct=max(HUNDRED - total, ZERO),
        )


# ---------------------------------------------------------------------------
# Portfolio valuation shared by both analytics
# ---------------------------------------------------------------------------


@dataclass
class _Position:
    holding: Holding
    instrument: Instrument | None
    name: str
    value: Decimal  # reporting currency, unrounded


@dataclass
class _Portfolio:
    currency: str | None
    positions: list[_Position]
    warnings: list[str]
    fx_as_of: datetime | None
    fx_rates_used: dict[str, Decimal]
    tags: list[tuple[HoldingThemeTag, Holding]]
    themes: dict[str, str] = field(default_factory=dict)  # casefold -> display

    @property
    def total(self) -> Decimal:
        return sum((p.value for p in self.positions), ZERO)


class ThemeExposureService:
    """Look-through exposure and theme allocation analytics."""

    def __init__(
        self,
        holding_repo: HoldingRepository,
        instrument_repo: InstrumentRepository,
        company_repo: CompanyRepository,
        snapshot_repo: LatestConstituentSnapshotRepository,
        holding_price_repo: HoldingPriceRepository | None = None,
        finance_setting_repo: FinanceSettingRepository | None = None,
        fx_rate_repo: FxRateRepository | None = None,
        tag_repo: HoldingThemeTagRepository | None = None,
        staleness_window_days: int = 90,
    ) -> None:
        self.holding_repo = holding_repo
        self.instrument_repo = instrument_repo
        self.company_repo = company_repo
        self.snapshot_repo = snapshot_repo
        self.holding_price_repo = holding_price_repo
        self.finance_setting_repo = finance_setting_repo
        self.fx_rate_repo = fx_rate_repo
        self.tag_repo = tag_repo
        self.staleness_window_days = staleness_window_days

    # -- valuation ---------------------------------------------------------

    async def _load_portfolio(self, workspace_id: int, as_of: date) -> _Portfolio:
        holdings, _ = await self.holding_repo.get_all(
            workspace_id, limit=None, quantity_state="nonzero"
        )
        warnings: list[str] = []
        used = sorted({h.currency.upper() for h in holdings})

        setting = None
        if self.finance_setting_repo is not None:
            setting = await self.finance_setting_repo.get_by_workspace(workspace_id)
        reporting: str | None = None
        if setting and setting.reporting_currency_code:
            reporting = setting.reporting_currency_code.upper()
        elif len(used) == 1:
            reporting = used[0]
        elif len(used) > 1:
            reporting = DEFAULT_REPORTING_CURRENCY
            warnings.append(
                f"No reporting currency configured; valuing in {DEFAULT_REPORTING_CURRENCY}"
            )

        fx_lookup: dict[tuple[str, str], FxRate] = {}
        fx_as_of: datetime | None = None
        if reporting and any(c != reporting for c in used) and self.fx_rate_repo is not None:
            pairs = build_required_pairs(used, reporting)
            fx_lookup = await self.fx_rate_repo.get_latest_rates_for_pairs(
                list(pairs),
                effective_display_as_of(datetime.combine(as_of, datetime.max.time(), tzinfo=UTC)),
            )
            if fx_lookup:
                fx_as_of = max(rate.as_of for rate in fx_lookup.values())

        instruments_by_id = await self.instrument_repo.get_by_ids([
            h.instrument_id for h in holdings if h.instrument_id is not None
        ])
        by_symbol = await self.instrument_repo.get_by_symbols(
            workspace_id, [h.symbol for h in holdings if h.instrument_id is None]
        )
        prices = (
            await self.holding_price_repo.latest_prices_on_or_before_bulk(
                workspace_id, [h.id for h in holdings if h.id is not None], as_of
            )
            if self.holding_price_repo is not None
            else {}
        )

        positions: list[_Position] = []
        for h in holdings:
            instrument = instruments_by_id.get(h.instrument_id) if h.instrument_id else None
            if instrument is None:
                instrument = by_symbol.get(h.symbol)
            name = instrument.name if instrument and instrument.name else h.symbol
            price = prices.get(h.id) if h.id is not None else None
            if price is None:
                warnings.append(
                    f"Excluded {name}: no market price on or before {as_of.isoformat()}"
                )
                continue
            native = h.quantity * price.unit_price
            currency = h.currency.upper()
            value = convert_amount(native, currency, reporting, fx_lookup) if reporting else native
            if value is None:
                warnings.append(f"Excluded {name}: no FX rate {currency}->{reporting}")
                continue
            positions.append(_Position(h, instrument, name, value))

        tags = (
            await self.tag_repo.list_for_workspace(workspace_id)
            if self.tag_repo is not None
            else []
        )
        themes: dict[str, str] = {}
        for tag, _holding in tags:
            themes.setdefault(tag.theme.casefold(), tag.theme)

        rates = (
            {k: Decimal(v) for k, v in build_fx_rates_used(used, reporting, fx_lookup).items()}
            if reporting
            else {}
        )
        return _Portfolio(reporting, positions, warnings, fx_as_of, rates, tags, themes)

    @staticmethod
    def _theme_members(
        portfolio: _Portfolio,
    ) -> dict[str, list[tuple[HoldingThemeTag, Holding]]]:
        members: dict[str, list[tuple[HoldingThemeTag, Holding]]] = defaultdict(list)
        for tag, holding in portfolio.tags:
            members[tag.theme.casefold()].append((tag, holding))
        return members

    # -- look-through exposure --------------------------------------------

    async def lookthrough_exposure(
        self, workspace_id: int, as_of: date, group_by: GroupBy | None = None
    ) -> LookthroughExposureResponse:
        """Aggregate exposure by company, sector, country and theme.

        Direct stocks count as themselves. An ETF / mutual fund is split over
        its single latest constituent snapshot; the uncovered remainder, funds
        without a snapshot, ``OTHER-`` placeholders and unknown companies go
        to the *Not itemised* bucket. Themes come from holding-level tags (a
        holding counts fully toward each of its themes, so theme rows overlap
        and may total more than 100%). ``group_by`` limits the response to one
        section.
        """
        portfolio = await self._load_portfolio(workspace_id, as_of)
        warnings = list(portfolio.warnings)

        funds: dict[int, _Position] = {}
        for pos in portfolio.positions:
            inst = pos.instrument
            if (
                inst is not None
                and inst.id is not None
                and inst.instrument_type != InstrumentType.stock.value
                and not _PLACEHOLDER_RE.match(pos.holding.symbol)
            ):
                funds[pos.holding.id or 0] = pos
        snapshots = await self.snapshot_repo.get_single_latest_many(
            [p.instrument.id for p in funds.values() if p.instrument and p.instrument.id],
            as_of,
        )
        company_ids: set[int] = set()
        for pos in portfolio.positions:
            if pos.instrument and pos.instrument.company_id is not None:
                company_ids.add(pos.instrument.company_id)
        for rows in snapshots.values():
            company_ids.update(r.constituent_company_id for r in rows)
        companies = await self.company_repo.get_by_ids(sorted(company_ids))

        # Atoms partition the portfolio value: ("direct"|"look", company_id) or the
        # single not-itemised atom.
        ni_key: tuple[str, int] = ("ni", 0)
        atoms: dict[tuple[str, int], Decimal] = defaultdict(lambda: ZERO)
        coverage: list[FundCoverageRow] = []

        def is_placeholder_company(company: Company) -> bool:
            return bool(company.ticker and _PLACEHOLDER_RE.match(company.ticker)) or bool(
                _PLACEHOLDER_RE.match(company.name or "")
            )

        for pos in portfolio.positions:
            inst, value, h = pos.instrument, pos.value, pos.holding
            if _PLACEHOLDER_RE.match(h.symbol):
                atoms[ni_key] += value
                continue
            if inst is None:
                atoms[ni_key] += value
                warnings.append(f"{pos.name}: instrument not found; counted as not itemised")
                continue
            if inst.instrument_type == InstrumentType.stock.value:
                company = companies.get(inst.company_id) if inst.company_id else None
                if company is None or is_placeholder_company(company):
                    atoms[ni_key] += value
                    warnings.append(f"{pos.name}: not linked to a company; counted as not itemised")
                else:
                    atoms[("direct", inst.company_id or 0)] += value
                continue

            rows = snapshots.get(inst.id or 0, [])
            if not rows:
                atoms[ni_key] += value
                warnings.append(f"{pos.name}: no constituent snapshot; counted as not itemised")
                coverage.append(self._coverage_row(pos, ZERO, None, None, 0))
                continue
            snap_date, source = rows[0].as_of_date, rows[0].source
            if (as_of - snap_date).days > self.staleness_window_days:
                warnings.append(f"{pos.name}: constituent snapshot of {snap_date} is stale")
            weight_sum = sum((r.weight for r in rows), ZERO)
            scale = Decimal(1)
            if weight_sum > 1:
                scale = Decimal(1) / weight_sum
                if weight_sum > 1 + _WEIGHT_TOLERANCE:
                    warnings.append(
                        f"{pos.name}: constituent weights total {weight_sum}; scaled down to 1"
                    )
            allocated = ZERO
            itemised = ZERO
            for row in rows:
                portion = value * row.weight * scale
                allocated += portion
                company = companies.get(row.constituent_company_id)
                if company is None or is_placeholder_company(company):
                    atoms[ni_key] += portion
                else:
                    atoms[("look", row.constituent_company_id)] += portion
                    itemised += portion
            atoms[ni_key] += value - allocated  # part of the fund the snapshot does not cover
            coverage.append(
                self._coverage_row(pos, _pct(itemised, value), snap_date, source, len(rows))
            )

        total_raw = portfolio.total
        total = _q(total_raw)
        rounded = allocate_exact(dict(atoms), total_raw) if atoms else {}
        not_itemised = rounded.get(ni_key, ZERO)

        direct: dict[int, Decimal] = defaultdict(lambda: ZERO)
        look: dict[int, Decimal] = defaultdict(lambda: ZERO)
        for (kind, cid), amount in rounded.items():
            if kind == "direct":
                direct[cid] += amount
            elif kind == "look":
                look[cid] += amount

        want = group_by
        by_company = by_sector = by_country = by_theme = None
        unthemed: Decimal | None = None

        if want in (None, "company"):
            rows_out = []
            for cid in set(direct) | set(look):
                company = companies[cid]
                combined = direct.get(cid, ZERO) + look.get(cid, ZERO)
                rows_out.append(
                    LookthroughCompanyRow(
                        company_id=company.public_id,
                        company_name=company.name,
                        company_ticker=company.ticker,
                        sector=company.sector,
                        country_code=company.country_code,
                        direct_value=direct.get(cid, ZERO),
                        lookthrough_value=look.get(cid, ZERO),
                        total_value=combined,
                        pct=_pct(combined, total),
                    )
                )
            rows_out.sort(key=lambda r: (-r.total_value, r.company_name))
            by_company = rows_out

        def bucketed(label_of) -> list[LookthroughBucketRow]:
            sums: dict[str, Decimal] = defaultdict(lambda: ZERO)
            for cid in set(direct) | set(look):
                sums[label_of(companies[cid])] += direct.get(cid, ZERO) + look.get(cid, ZERO)
            out = [
                LookthroughBucketRow(key=label, label=label, value=amount, pct=_pct(amount, total))
                for label, amount in sums.items()
            ]
            out.sort(key=lambda r: (-r.value, r.label))
            if not_itemised or not out:
                out.append(
                    LookthroughBucketRow(
                        key=NOT_ITEMISED_KEY,
                        label=NOT_ITEMISED_LABEL,
                        value=not_itemised,
                        pct=_pct(not_itemised, total),
                    )
                )
            return out

        if want in (None, "sector"):
            by_sector = bucketed(lambda c: c.sector or UNCLASSIFIED_SECTOR)
        if want in (None, "country"):
            by_country = bucketed(lambda c: c.country_code or UNKNOWN_COUNTRY)
        if want in (None, "theme"):
            value_by_holding = {p.holding.id: p.value for p in portfolio.positions}
            theme_rows: list[LookthroughThemeRow] = []
            themed: set[int] = set()
            for key, members in self._theme_members(portfolio).items():
                holding_ids = {h.id for _, h in members if h.id in value_by_holding}
                themed |= {i for i in holding_ids if i is not None}
                amount = sum((value_by_holding[i] for i in holding_ids), ZERO)
                theme_rows.append(
                    LookthroughThemeRow(
                        theme=portfolio.themes[key],
                        holdings_count=len({h.id for _, h in members}),
                        value=_q(amount),
                        pct=_pct(amount, total_raw),
                    )
                )
            theme_rows.sort(key=lambda r: (-r.value, r.theme))
            by_theme = theme_rows
            unthemed = _q(sum((v for i, v in value_by_holding.items() if i not in themed), ZERO))

        return LookthroughExposureResponse(
            as_of_date=as_of,
            currency=portfolio.currency,
            analysis_status="complete" if not warnings else "partial",
            group_by=group_by,
            total_value=total,
            itemised_value=total - not_itemised,
            not_itemised_value=not_itemised,
            not_itemised_pct=_pct(not_itemised, total),
            by_company=by_company,
            by_sector=by_sector,
            by_country=by_country,
            by_theme=by_theme,
            unthemed_value=unthemed,
            fund_coverage=coverage,
            warnings=warnings,
            fx_as_of=portfolio.fx_as_of,
            fx_rates_used=portfolio.fx_rates_used,
        )

    @staticmethod
    def _coverage_row(
        pos: _Position,
        itemised_pct: Decimal,
        snapshot_date: date | None,
        source: str | None,
        row_count: int,
    ) -> FundCoverageRow:
        inst = pos.instrument
        assert inst is not None
        return FundCoverageRow(
            instrument_id=inst.public_id,
            symbol=inst.symbol,
            name=inst.name,
            instrument_type=str(inst.instrument_type),
            value=_q(pos.value),
            itemised_pct=itemised_pct,
            snapshot_date=snapshot_date,
            source=source,
            constituent_rows=row_count,
        )

    # -- allocation vs target ---------------------------------------------

    async def allocation_vs_target(
        self, workspace_id: int, as_of: date, new_cash: Decimal = ZERO
    ) -> AllocationVsTargetResponse:
        """Theme drift vs target plus a buy-only plan for *new_cash*.

        ``drift_pct = current_pct - target_pct`` (negative = underweight, both
        measured against the current portfolio value). Each targeted theme's
        shortfall is ``max(0, target% x (portfolio + new_cash) - theme value)``.
        New cash goes only to under-weight themes, in proportion to shortfall
        when it cannot cover all of them, capped at each theme's shortfall;
        whatever is left stays ``unallocated_cash``. Nothing is ever sold.
        Because a holding may carry several themes, a purchase can reduce more
        than one theme's shortfall; suggestions are per theme.
        """
        cash = validate_new_cash(new_cash)
        portfolio = await self._load_portfolio(workspace_id, as_of)
        warnings = list(portfolio.warnings)
        value_by_holding = {p.holding.id: p.value for p in portfolio.positions}
        portfolio_value = portfolio.total
        post_total = portfolio_value + cash

        members = self._theme_members(portfolio)
        theme_value: dict[str, Decimal] = {}
        theme_target: dict[str, Decimal | None] = {}
        for key, rows in members.items():
            holding_ids = {h.id for _, h in rows if h.id in value_by_holding}
            theme_value[key] = sum((value_by_holding[i] for i in holding_ids), ZERO)
            declared = [t.target_weight_pct for t, _ in rows if t.target_weight_pct is not None]
            theme_target[key] = sum(declared, ZERO) if declared else None

        total_target = sum((t for t in theme_target.values() if t is not None), ZERO)
        if total_target > HUNDRED:
            warnings.append(f"Theme targets total {total_target}%, above 100%")

        shortfall: dict[str, Decimal] = {}
        for key, target in theme_target.items():
            if target is None:
                shortfall[key] = ZERO
                continue
            gap = post_total * target / HUNDRED - theme_value[key]
            shortfall[key] = gap if gap > 0 else ZERO

        suggested = self._plan_deployment(cash, shortfall)
        total_suggested = sum(suggested.values(), ZERO)

        tagged_with_targets: dict[int, int] = defaultdict(int)
        for tag, holding in portfolio.tags:
            if tag.target_weight_pct is not None and holding.id is not None:
                tagged_with_targets[holding.id] += 1
        if any(n > 1 for n in tagged_with_targets.values()):
            warnings.append(
                "Some holdings carry several targeted themes; theme percentages overlap "
                "and one purchase can reduce several shortfalls"
            )

        themed_ids = {h.id for rows in members.values() for _, h in rows}
        unthemed = sum((v for i, v in value_by_holding.items() if i not in themed_ids), ZERO)

        out: list[AllocationThemeRow] = []
        for key, rows in members.items():
            target = theme_target[key]
            current_pct = _pct(theme_value[key], portfolio_value)
            holdings_rows: dict[int, AllocationHoldingRow] = {}
            for tag, holding in rows:
                if holding.id is None:
                    continue
                holdings_rows[holding.id] = AllocationHoldingRow(
                    holding_id=holding.public_id,
                    symbol=holding.symbol,
                    current_value=_q(value_by_holding.get(holding.id, ZERO)),
                    target_weight_pct=tag.target_weight_pct,
                )
            out.append(
                AllocationThemeRow(
                    theme=portfolio.themes[key],
                    holdings_count=len(holdings_rows),
                    current_value=_q(theme_value[key]),
                    current_pct=current_pct,
                    target_weight_pct=target,
                    drift_pct=(current_pct - target) if target is not None else None,
                    target_value=_q(post_total * target / HUNDRED) if target is not None else None,
                    shortfall=_q(shortfall[key]),
                    suggested_new_cash=suggested.get(key, ZERO),
                    post_deployment_pct=_pct(
                        theme_value[key] + suggested.get(key, ZERO), post_total
                    ),
                    holdings=sorted(holdings_rows.values(), key=lambda r: r.symbol),
                )
            )
        # Most under-weight first; themes without a target last.
        out.sort(key=lambda r: (r.drift_pct is None, r.drift_pct or ZERO, r.theme))

        return AllocationVsTargetResponse(
            as_of_date=as_of,
            currency=portfolio.currency,
            analysis_status="complete" if not warnings else "partial",
            portfolio_value=_q(portfolio_value),
            new_cash=cash,
            post_deployment_value=_q(post_total),
            total_target_pct=total_target,
            unallocated_target_pct=max(HUNDRED - total_target, ZERO),
            unthemed_value=_q(unthemed),
            total_suggested=total_suggested,
            unallocated_cash=cash - total_suggested,
            themes=out,
            warnings=warnings,
            fx_as_of=portfolio.fx_as_of,
            fx_rates_used=portfolio.fx_rates_used,
        )

    @staticmethod
    def _plan_deployment(cash: Decimal, shortfall: Mapping[str, Decimal]) -> dict[str, Decimal]:
        """Split *cash* over shortfalls: proportional, capped, buy-only, exact cents.

        Returns amounts in whole cents with ``sum <= cash`` and every amount
        ``<= its shortfall``.
        """
        needy = {k: v for k, v in shortfall.items() if v > 0}
        if cash <= 0 or not needy:
            return {}
        total_gap = sum(needy.values(), ZERO)
        spend = min(cash, total_gap)
        raw = {k: (v if cash >= total_gap else cash * v / total_gap) for k, v in needy.items()}
        caps = {k: _floor_cents(v) for k, v in needy.items()}
        amounts = {k: min(_floor_cents(v), caps[k]) for k, v in raw.items()}
        budget = int((_floor_cents(spend) - sum(amounts.values(), ZERO)) / CENT)
        for key in sorted(raw, key=lambda k: raw[k] - amounts[k], reverse=True):
            if budget <= 0:
                break
            if amounts[key] + CENT <= caps[key]:
                amounts[key] += CENT
                budget -= 1
        return {k: v for k, v in amounts.items() if v > 0}
