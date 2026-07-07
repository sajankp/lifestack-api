"""`ImportModule.investing_constituents` (ETF/mutual-fund holdings-composition
CSV) row validation and commit.

Unlike the other CSV modules, a constituents commit has a pre-step (delete
existing `csv_import`-sourced snapshot rows for the (instrument, as_of_date)
pairs in this batch, and warm a company-name cache) that runs once before the
chunked commit loop, not per chunk.
"""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.imports.models import ImportBatch, ImportError, ImportPreviewRow
from app.imports.repository import ImportRepository
from app.imports.shared import AddErrorFn, WeightEntry, norm
from app.investing.models import Company, Instrument, InstrumentConstituent, InstrumentType

TEMPLATE_ROW = "UMMA,Apple Inc,AAPL,0.082,2026-06-14"


def validate_investing_constituent_row(
    row: dict,
    add_error: AddErrorFn,
    instruments_map: dict[str, Instrument],
) -> tuple[dict, WeightEntry | None]:
    instrument_symbol_raw = norm(row.get("instrument_symbol"))
    company_name_raw = norm(row.get("company_name"))
    company_ticker_raw = norm(row.get("company_ticker"))
    weight_raw = norm(row.get("weight"))
    as_of_date_raw = norm(row.get("as_of_date"))

    inst = None
    if instrument_symbol_raw:
        inst = instruments_map.get(instrument_symbol_raw.upper())
        if (
            not inst
            or not inst.is_active
            or inst.instrument_type
            not in {InstrumentType.etf.value, InstrumentType.mutual_fund.value}
        ):
            add_error(
                "instrument_symbol",
                "invalid_instrument",
                "instrument_symbol must resolve to an active ETF/Mutual Fund instrument in the current workspace",
                instrument_symbol_raw,
            )
    else:
        add_error(
            "instrument_symbol",
            "required",
            "instrument_symbol is required",
            instrument_symbol_raw,
        )

    if not company_name_raw:
        add_error(
            "company_name",
            "required",
            "company_name is required",
            company_name_raw,
        )

    try:
        weight = Decimal(weight_raw)
        if not (Decimal("0.00000001") <= weight <= Decimal("1.0")):
            raise InvalidOperation
    except Exception:
        add_error(
            "weight",
            "invalid_decimal",
            "weight must be a positive decimal between 0 and 1",
            weight_raw,
        )
        weight = None

    try:
        as_of_date = datetime.strptime(as_of_date_raw, "%Y-%m-%d").date()
    except Exception:
        add_error(
            "as_of_date",
            "invalid_date",
            "as_of_date must be YYYY-MM-DD",
            as_of_date_raw,
        )
        as_of_date = None

    payload = {
        "instrument_symbol": instrument_symbol_raw.upper() if instrument_symbol_raw else None,
        "instrument_id": inst.id if inst else None,
        "company_name": company_name_raw,
        "company_ticker": company_ticker_raw or None,
        "weight": str(weight) if weight is not None else None,
        "as_of_date": as_of_date.isoformat() if as_of_date else None,
        "source": "csv_import",
    }

    weight_entry: WeightEntry | None = None
    if (
        instrument_symbol_raw
        and inst
        and inst.is_active
        and inst.instrument_type in {InstrumentType.etf.value, InstrumentType.mutual_fund.value}
        and weight is not None
        and as_of_date is not None
    ):
        weight_entry = ((instrument_symbol_raw.upper(), as_of_date_raw), weight)

    return payload, weight_entry


def check_weight_group_totals(
    batch_id: int, weight_groups: dict[tuple[str, str], list[Decimal]]
) -> list[ImportError]:
    """Flag (symbol, as_of_date) groups whose weights don't sum to ~1.0."""
    errors: list[ImportError] = []
    for (sym, dt_str), weights in weight_groups.items():
        total_w = sum(weights)
        if not (Decimal("0.99") <= total_w <= Decimal("1.01")):
            errors.append(
                ImportError(
                    import_batch_id=batch_id,
                    row_number=1,
                    field_name="weight",
                    error_code="invalid_weight_sum",
                    message=f"Total weight for instrument '{sym}' on date '{dt_str}' is {total_w}, which is outside the range 0.99 - 1.01.",
                    raw_value=str(total_w),
                )
            )
    return errors


async def prepare_constituents_commit(
    session: AsyncSession,
    repository: ImportRepository,
    workspace_id: int,
    batch: ImportBatch,
) -> dict[str, Company]:
    """Pre-step run once before the chunked commit loop: delete existing
    `csv_import`-sourced snapshots for the (instrument, as_of_date) pairs in
    this batch, and warm a company-name cache for the chunk loop.
    """
    preview_rows = await repository.iter_preview_rows(batch.id)
    unique_snapshots = set()
    for row in preview_rows:
        p = row.payload_json
        inst_id = p.get("instrument_id")
        as_of_date_str = p.get("as_of_date")
        if inst_id is not None and as_of_date_str:
            unique_snapshots.add((
                int(inst_id),
                datetime.strptime(as_of_date_str, "%Y-%m-%d").date(),
            ))

    # Batched into a single DELETE (Gemini review, api PR#128) rather than one
    # query per (instrument, as_of_date) pair. The `if unique_snapshots:` guard
    # is deliberate, not just an optimization: `tuple_(...).in_(())` is safe on
    # its own, but this stays explicit so an empty/falsy set can never end up
    # executing a DELETE that only carries the `source == "csv_import"` filter
    # — that would silently wipe every CSV-imported constituent snapshot in
    # the workspace instead of doing nothing.
    if unique_snapshots:
        await session.execute(
            delete(InstrumentConstituent).where(
                InstrumentConstituent.source == "csv_import",
                tuple_(InstrumentConstituent.instrument_id, InstrumentConstituent.as_of_date).in_(
                    unique_snapshots
                ),
            )
        )

    company_rows = (
        (await session.execute(select(Company).where(Company.workspace_id == workspace_id)))
        .scalars()
        .all()
    )
    return {c.name.strip().lower(): c for c in company_rows}


async def commit_constituents_chunk(
    session: AsyncSession,
    workspace_id: int,
    rows: list[ImportPreviewRow],
    company_cache: dict[str, Company],
) -> int:
    """Insert/update one chunk of preview rows, mutating `company_cache` and
    the session in place. Returns the number of rows committed in this chunk.
    """
    # Pre-load existing constituents for the chunk to prevent unique constraint violation
    keys = []
    for row in rows:
        p = row.payload_json
        company_name_raw = p.get("company_name")
        company_name_norm = company_name_raw.strip().lower() if company_name_raw else ""
        company = company_cache.get(company_name_norm)
        if company is not None and p.get("instrument_id") is not None:
            try:
                dt = datetime.strptime(p["as_of_date"], "%Y-%m-%d").date()
                keys.append((int(p["instrument_id"]), company.id, dt))
            except Exception:
                pass

    existing_consts = {}
    if keys:
        const_rows = (
            (
                await session.execute(
                    select(InstrumentConstituent).where(
                        InstrumentConstituent.source == "csv_import",
                        tuple_(
                            InstrumentConstituent.instrument_id,
                            InstrumentConstituent.constituent_company_id,
                            InstrumentConstituent.as_of_date,
                        ).in_(keys),
                    )
                )
            )
            .scalars()
            .all()
        )
        existing_consts = {
            (c.instrument_id, c.constituent_company_id, c.as_of_date): c for c in const_rows
        }

    # Pre-create all missing companies in a single batch and flush once to
    # populate their DB-assigned IDs. This avoids N+1 flushes (one per new
    # company) when a large ETF constituent list contains many unseen companies.
    new_companies: dict[str, Company] = {}
    for row in rows:
        p = row.payload_json
        company_name_raw = p.get("company_name")
        company_name_norm = company_name_raw.strip().lower() if company_name_raw else ""
        if (
            company_name_norm
            and company_name_norm not in company_cache
            and company_name_norm not in new_companies
        ):
            company = Company(
                workspace_id=workspace_id,
                name=company_name_raw,
                ticker=p.get("company_ticker") or None,
            )
            session.add(company)
            new_companies[company_name_norm] = company

    if new_companies:
        await session.flush()
        company_cache.update(new_companies)

    inserted = 0
    for row in rows:
        p = row.payload_json
        company_name_raw = p.get("company_name")
        company_name_norm = company_name_raw.strip().lower() if company_name_raw else ""

        company = company_cache.get(company_name_norm)

        instrument_id = p.get("instrument_id")
        if instrument_id is None:
            raise ValidationError(detail="Instrument ID is missing in preview payload")

        if company is None:
            # Company still missing after the batch-create pass — this can only
            # happen if company_name_norm was empty (validation should have
            # caught this upstream, but be safe rather than crash).
            continue

        as_of_date = datetime.strptime(p["as_of_date"], "%Y-%m-%d").date()
        const_key = (int(instrument_id), company.id, as_of_date)
        existing_const = existing_consts.get(const_key)

        if existing_const:
            existing_const.weight = Decimal(p["weight"])
            existing_const.fetched_at = datetime.now(UTC)
        else:
            constituent = InstrumentConstituent(
                instrument_id=int(instrument_id),
                constituent_company_id=company.id,
                weight=Decimal(p["weight"]),
                as_of_date=as_of_date,
                source="csv_import",
                fetched_at=datetime.now(UTC),
            )
            session.add(constituent)
            existing_consts[const_key] = constituent
        inserted += 1

    return inserted
