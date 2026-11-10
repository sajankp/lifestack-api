"""One-time data repair: IDEX Corp wrongly carries Indian Energy Exchange's ISIN.

Background (constituent bug 1): a fund row with ISIN INE022Q01020 (Indian Energy
Exchange, NSE: IEX) plus ticker IEX used to resolve by ticker to the US company
IDEX Corp, and the write then copied the Indian row's sector (Financials) onto it.
The resolver is fixed; this repairs the data it already damaged:

  1. detach INE022Q01020 from IDEX Corp and restore its sector (Industrials);
  2. create the workspace company "Indian Energy Exchange Ltd" (ticker IEX, that ISIN,
     Financials) unless one already carries the ISIN;
  3. re-point the two reviewed constituent rows at it.

    uv run python -m scripts.repair_idex_isin             # dry run: prints the plan
    uv run python -m scripts.repair_idex_isin --apply     # writes, one transaction

Dry-run by default. Every targeted row is checked against what was reviewed (fund
symbol and weight, and that it still points at IDEX Corp); if anything has moved the
script refuses and writes nothing. Re-running after a successful apply is a no-op.
IDEX Corp's own holdings (e.g. HLAL) are never touched.

Not a migration on purpose: a data correction must be reviewed against live data and
run once, deliberately, not replayed on every database that upgrades.
"""

import argparse
import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

import app.core.database.models  # noqa: F401  (register every table so FKs resolve on INSERT)
from app.core.database import postgres
from app.investing.models import Company, Instrument, InstrumentConstituent

INDIAN_ENERGY_EXCHANGE_ISIN = "INE022Q01020"
INDIAN_ENERGY_EXCHANGE_NAME = "Indian Energy Exchange Ltd"

# Production values, reviewed against the live database on 2026-11-10.
PRODUCTION_WORKSPACE_ID = 1
PRODUCTION_IDEX_PUBLIC_ID = uuid.UUID("cd647124-fb0b-424f-a8c5-c711f08bda52")


class RepairRefusedError(Exception):
    """The data no longer matches what was reviewed; nothing was written."""


@dataclass(frozen=True)
class RepairTarget:
    row_id: int
    fund_symbol: str
    weight: Decimal


PRODUCTION_TARGETS = (
    RepairTarget(3115, "122639", Decimal("0.0062")),  # Parag Parikh, 2026-09-30
    RepairTarget(3164, "SMALLCAP", Decimal("0.00882363")),  # Mirae small cap, 2026-09-30
)


@dataclass
class RepairReport:
    applied: bool
    actions: list[str] = field(default_factory=list)
    already_repaired: bool = False


async def repair_idex_isin(
    session,
    *,
    workspace_id: int,
    idex_company_id: int,
    targets,
    apply: bool,
) -> RepairReport:
    """Plan (and with ``apply=True``, perform) the repair. Flushes; the caller commits."""
    idex = await session.get(Company, idex_company_id)
    if idex is None or idex.workspace_id != workspace_id:
        raise RepairRefusedError(f"IDEX Corp company {idex_company_id} not found in workspace")

    indian = (
        (
            await session.execute(
                select(Company).where(
                    Company.workspace_id == workspace_id,
                    Company.isin == INDIAN_ENERGY_EXCHANGE_ISIN,
                    Company.id != idex.id,
                )
            )
        )
        .scalars()
        .first()
    )

    # Validate every target before any write, so a refusal leaves the data untouched.
    to_repoint: list[tuple[InstrumentConstituent, RepairTarget]] = []
    for target in targets:
        found = (
            await session.execute(
                select(InstrumentConstituent, Instrument.symbol)
                .join(Instrument, Instrument.id == InstrumentConstituent.instrument_id)
                .where(InstrumentConstituent.id == target.row_id)
            )
        ).first()
        if found is None:
            raise RepairRefusedError(f"row {target.row_id} ({target.fund_symbol}) not found")
        row, symbol = found
        if symbol != target.fund_symbol or row.weight != target.weight:
            raise RepairRefusedError(
                f"row {target.row_id} ({target.fund_symbol}) is now fund {symbol!r} weight "
                f"{row.weight}, reviewed as weight {target.weight}; data has moved"
            )
        if row.constituent_company_id == idex.id:
            to_repoint.append((row, target))
        elif indian is not None and row.constituent_company_id == indian.id:
            continue  # already repaired
        else:
            raise RepairRefusedError(
                f"row {target.row_id} ({target.fund_symbol}) points at company "
                f"{row.constituent_company_id}, not IDEX Corp ({idex.id})"
            )

    needs_detach = idex.isin == INDIAN_ENERGY_EXCHANGE_ISIN
    actions: list[str] = []
    if needs_detach:
        actions.append(
            f"detach ISIN {INDIAN_ENERGY_EXCHANGE_ISIN} from IDEX Corp ({idex.id}); "
            f"sector {idex.sector!r} -> 'Industrials'"
        )
    if indian is None and (needs_detach or to_repoint):
        actions.append(
            f"create {INDIAN_ENERGY_EXCHANGE_NAME!r} in workspace {workspace_id} "
            f"(ticker IEX, ISIN {INDIAN_ENERGY_EXCHANGE_ISIN}, sector Financials)"
        )
    actions.extend(
        f"re-point constituent row {row.id} ({target.fund_symbol}, weight {row.weight}) "
        f"from IDEX Corp to {INDIAN_ENERGY_EXCHANGE_NAME!r}"
        for row, target in to_repoint
    )

    report = RepairReport(
        applied=apply, actions=actions, already_repaired=not actions and indian is not None
    )
    if not apply or not actions:
        return report

    now = datetime.now(UTC)
    if needs_detach:
        idex.isin = None
        idex.sector = "Industrials"
        idex.updated_at = now
    if indian is None:
        indian = Company(
            workspace_id=workspace_id,
            name=INDIAN_ENERGY_EXCHANGE_NAME,
            ticker="IEX",
            isin=INDIAN_ENERGY_EXCHANGE_ISIN,
            sector="Financials",
            country_code="IN",
        )
        session.add(indian)
    await session.flush()
    assert indian.id is not None  # populated by the flush above
    for row, _ in to_repoint:
        row.constituent_company_id = indian.id
    await session.flush()
    return report


async def _main(apply: bool, workspace_id: int) -> int:
    async with postgres.async_session_maker() as session:
        idex = (
            await session.execute(
                select(Company).where(Company.public_id == PRODUCTION_IDEX_PUBLIC_ID)
            )
        ).scalar_one_or_none()
        if idex is None:
            print("REFUSED: IDEX Corp company not found")
            return 2
        try:
            report = await repair_idex_isin(
                session,
                workspace_id=workspace_id,
                idex_company_id=idex.id,
                targets=PRODUCTION_TARGETS,
                apply=apply,
            )
        except RepairRefusedError as exc:
            print(f"REFUSED: {exc}")
            return 2
        if apply:
            await session.commit()
    if report.already_repaired:
        print("nothing to do: already repaired")
        return 0
    prefix = "applied" if apply else "would"
    for action in report.actions:
        print(f"{prefix}: {action}")
    if not apply:
        print("dry run: nothing written. Re-run with --apply to perform it.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--workspace-id", type=int, default=PRODUCTION_WORKSPACE_ID)
    args = parser.parse_args()
    return asyncio.run(_main(args.apply, args.workspace_id))


if __name__ == "__main__":
    raise SystemExit(main())
