"""`merge_company_identities` backfill (spec-083 §9).

Consolidates duplicate `Company` rows created by the pre-spec-083 name-string
identity bug (e.g. "Apple Inc" / "Apple Inc." / "AAPL" as three separate
companies) within a single workspace. Deliberately a dedicated, explicitly
`--workspace-id`-scoped CLI job — never an automatic deploy-time migration —
so it can never repoint identity across workspace boundaries.

Idempotent and re-runnable: a workspace with no duplicates produces an
all-zero summary and touches nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.investing.models import Company, Instrument, InstrumentConstituent, ReferenceSecurity
from app.investing.repository import normalize_company_name


@dataclass
class MergeSummary:
    workspace_id: int
    dry_run: bool
    groups_merged: int = 0
    companies_deleted: int = 0
    instruments_repointed: int = 0
    constituent_rows_repointed: int = 0
    constituent_rows_dropped_collision: int = 0
    enriched_from_reference_data: int = 0
    merged_pairs: list[tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "dry_run": self.dry_run,
            "groups_merged": self.groups_merged,
            "companies_deleted": self.companies_deleted,
            "instruments_repointed": self.instruments_repointed,
            "constituent_rows_repointed": self.constituent_rows_repointed,
            "constituent_rows_dropped_collision": self.constituent_rows_dropped_collision,
            "enriched_from_reference_data": self.enriched_from_reference_data,
            "merged_pairs": self.merged_pairs,
        }


def _same_market(a: Company, b: Company) -> bool:
    if a.country_code and b.country_code:
        return a.country_code.strip().upper() == b.country_code.strip().upper()
    return True


def _identity_matches(a: Company, b: Company) -> bool:
    if a.isin and b.isin:
        return a.isin.strip().upper() == b.isin.strip().upper()
    if a.ticker and b.ticker:
        return a.ticker.strip().upper() == b.ticker.strip().upper() and _same_market(a, b)
    return normalize_company_name(a.name) == normalize_company_name(b.name)


def _group_duplicates(companies: list[Company]) -> list[list[Company]]:
    """Group companies whose identity resolves to the same key.

    Mirrors `CompanyRepository.resolve_or_create_company`'s precedence
    (ISIN -> ticker(+market) -> normalized name), comparing each company
    against each existing group's survivor (oldest/lowest-id member).
    """
    groups: list[list[Company]] = []
    for company in sorted(companies, key=lambda c: c.id or 0):
        matched_group = None
        for group in groups:
            if _identity_matches(company, group[0]):
                matched_group = group
                break
        if matched_group is not None:
            matched_group.append(company)
        else:
            groups.append([company])
    return groups


async def _enrich_from_reference_data(
    session: AsyncSession, companies: list[Company], summary: MergeSummary, *, dry_run: bool
) -> None:
    """Best-effort: fill missing isin/ticker from `reference_securities`.

    A company still lacking both isin and ticker cannot be grouped with a
    same-company row that already carries an identifier; enrichment lets
    such pre-spec-083 rows join the correct group before grouping runs.
    Safe to skip entirely (no-op) when `reference_securities` is empty —
    duplicates already sharing an isin/ticker still merge without it.
    """
    # Pre-fetch all reference securities to avoid N+1 queries loading 15k+ rows per company
    result = await session.execute(select(ReferenceSecurity))
    ref_securities = result.scalars().all()

    # Build in-memory lookup maps for O(1) lookups
    ref_by_ticker = {r.ticker.upper(): r for r in ref_securities if r.ticker}
    ref_by_norm_name = {}
    for r in ref_securities:
        norm_name = normalize_company_name(r.name)
        ref_by_norm_name.setdefault(norm_name, r)
        for alias in r.aliases or []:
            ref_by_norm_name.setdefault(normalize_company_name(alias), r)

    for company in companies:
        if company.isin:
            continue
        match = None
        if company.ticker:
            match = ref_by_ticker.get(company.ticker.upper())
        if match is None:
            match = ref_by_norm_name.get(normalize_company_name(company.name))
        if match is None:
            continue
        changed = bool((match.isin and not company.isin) or (match.ticker and not company.ticker))
        if not changed:
            continue
        summary.enriched_from_reference_data += 1
        if dry_run:
            # Don't mutate the managed object in preview mode — grouping in a
            # dry run therefore won't see enrichment-enabled merges; the
            # counted-but-not-applied gap is an accepted preview limitation.
            continue
        if match.isin and not company.isin:
            company.isin = match.isin
        if match.ticker and not company.ticker:
            company.ticker = match.ticker


async def merge_company_identities(
    session: AsyncSession, workspace_id: int, *, dry_run: bool = False
) -> MergeSummary:
    summary = MergeSummary(workspace_id=workspace_id, dry_run=dry_run)

    result = await session.execute(select(Company).where(Company.workspace_id == workspace_id))
    companies = list(result.scalars().all())
    if len(companies) < 2:
        return summary

    await _enrich_from_reference_data(session, companies, summary, dry_run=dry_run)

    for group in _group_duplicates(companies):
        if len(group) < 2:
            continue
        survivor, *losers = group
        summary.groups_merged += 1

        for loser in losers:
            summary.merged_pairs.append((loser.name, survivor.name))
            if not dry_run:
                if not survivor.isin and loser.isin:
                    survivor.isin = loser.isin
                if not survivor.ticker and loser.ticker:
                    survivor.ticker = loser.ticker
                if not survivor.country_code and loser.country_code:
                    survivor.country_code = loser.country_code

            const_result = await session.execute(
                select(InstrumentConstituent).where(
                    InstrumentConstituent.constituent_company_id == loser.id
                )
            )
            loser_rows = list(const_result.scalars().all())

            survivor_result = await session.execute(
                select(InstrumentConstituent).where(
                    InstrumentConstituent.constituent_company_id == survivor.id
                )
            )
            survivor_keys = {
                (row.instrument_id, row.as_of_date, row.source)
                for row in survivor_result.scalars().all()
            }

            for row in loser_rows:
                key = (row.instrument_id, row.as_of_date, row.source)
                if key in survivor_keys:
                    # Collision against uq_investing_constituent_snapshot: the
                    # survivor already has a row for this
                    # (instrument_id, as_of_date, source). Documented policy
                    # (spec-083 §9 step 3): keep the survivor's existing row,
                    # drop the loser's duplicate rather than repoint it.
                    summary.constituent_rows_dropped_collision += 1
                    if not dry_run:
                        await session.delete(row)
                else:
                    summary.constituent_rows_repointed += 1
                    survivor_keys.add(key)
                    if not dry_run:
                        row.constituent_company_id = survivor.id

            instrument_result = await session.execute(
                select(Instrument).where(Instrument.company_id == loser.id)
            )
            for instrument in instrument_result.scalars().all():
                summary.instruments_repointed += 1
                if not dry_run:
                    instrument.company_id = survivor.id

            summary.companies_deleted += 1

        if not dry_run:
            # Flush the constituent-row repoints/deletes and instrument
            # repoints before deleting the loser Company rows: Company and
            # InstrumentConstituent/Instrument have no ORM `relationship()`
            # between them (plain FK columns only), so the unit of work
            # cannot infer that the child rows must be gone first — without
            # this explicit flush the Company DELETE can be emitted first
            # and trip the FK constraint.
            await session.flush()
            for loser in losers:
                await session.delete(loser)
            await session.flush()

    if not dry_run:
        await session.commit()

    return summary
