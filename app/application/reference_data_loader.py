"""`load_reference_securities` (spec-083 §7.1): idempotent upsert of the
bundled `app/investing/reference_data/securities.json.gz` master data into
the `reference_securities` table.

Global (workspace-less) reference data — re-runnable any time the JSON is
hand-edited or the build script (`seed_data/scripts/build_securities_json.py`)
regenerates it. Reads via `importlib.resources` so it has no dependency on
the `seed_data/` dev workspace at runtime (the file ships inside the `app`
package). Gzipped, not plain `.json`: the full dataset is ~4.3MB as compact
JSON — over the repo's pre-commit large-file limit — and gzip brings that to
~280KB with zero data loss on this highly repetitive tabular data.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.investing.models import ReferenceSecurity

_ALLOWED_SECURITY_TYPES = {"stock", "etf", "mutual_fund"}
_REQUIRED_FIELDS = {"security_type", "name"}
_LOAD_CHUNK_SIZE = 1000


class SecuritiesJsonValidationError(ValueError):
    pass


@dataclass
class LoadSummary:
    total_entries: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "total_entries": self.total_entries,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "errors": self.errors,
        }


def _read_bundled_json() -> dict:
    raw = (
        resources.files("app.investing.reference_data").joinpath("securities.json.gz").read_bytes()
    )
    return json.loads(gzip.decompress(raw))


def validate_securities_payload(payload: dict) -> list[dict]:
    """Fail loudly on a malformed entry (spec-083 §7.1) rather than silently
    skip it — a bad bundled file should never partially load.
    """
    if "securities" not in payload or not isinstance(payload["securities"], list):
        raise SecuritiesJsonValidationError(
            "securities.json must have a top-level 'securities' list"
        )

    entries = payload["securities"]
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise SecuritiesJsonValidationError(f"entry {index} is not an object")
        missing = _REQUIRED_FIELDS - entry.keys()
        if missing:
            raise SecuritiesJsonValidationError(f"entry {index} missing required fields: {missing}")
        if entry["security_type"] not in _ALLOWED_SECURITY_TYPES:
            raise SecuritiesJsonValidationError(
                f"entry {index} has invalid security_type {entry['security_type']!r}"
            )
        if not entry.get("name"):
            raise SecuritiesJsonValidationError(f"entry {index} has an empty name")
        if not (entry.get("isin") or entry.get("ticker") or entry.get("amfi_code")):
            raise SecuritiesJsonValidationError(
                f"entry {index} ({entry['name']!r}) has no isin, ticker, or amfi_code"
            )
    return entries


def _row_needs_update(row: ReferenceSecurity, entry: dict) -> bool:
    return (
        row.isin != entry.get("isin")
        or row.ticker != entry.get("ticker")
        or row.exchange != entry.get("exchange")
        or row.amfi_code != entry.get("amfi_code")
        or row.security_type != entry["security_type"]
        or row.name != entry["name"]
        or list(row.aliases or []) != list(entry.get("aliases") or [])
        or row.country_code != entry.get("country_code")
    )


def _apply(row: ReferenceSecurity, entry: dict, *, fetched_at: datetime) -> None:
    row.isin = entry.get("isin")
    row.ticker = entry.get("ticker")
    row.exchange = entry.get("exchange")
    row.amfi_code = entry.get("amfi_code")
    row.security_type = entry["security_type"]
    row.name = entry["name"]
    row.aliases = list(entry.get("aliases") or [])
    row.country_code = entry.get("country_code")
    row.source = entry.get("source", "bundled:unknown")
    row.fetched_at = fetched_at


async def load_reference_securities(session: AsyncSession) -> LoadSummary:
    payload = _read_bundled_json()
    entries = validate_securities_payload(payload)

    summary = LoadSummary(total_entries=len(entries))
    fetched_at = datetime.now(UTC)

    existing_rows = (await session.execute(select(ReferenceSecurity))).scalars().all()
    by_isin = {r.isin: r for r in existing_rows if r.isin}
    by_ticker_exchange = {(r.ticker, r.exchange): r for r in existing_rows if r.ticker}
    by_amfi = {r.amfi_code: r for r in existing_rows if r.amfi_code}

    pending_new: list[ReferenceSecurity] = []
    processed_since_flush = 0

    for entry in entries:
        existing = None
        if entry.get("isin"):
            existing = by_isin.get(entry["isin"])
        if existing is None and entry.get("ticker"):
            existing = by_ticker_exchange.get((entry["ticker"], entry.get("exchange")))
        if existing is None and entry.get("amfi_code"):
            existing = by_amfi.get(entry["amfi_code"])

        if existing is not None:
            if _row_needs_update(existing, entry):
                _apply(existing, entry, fetched_at=fetched_at)
                summary.updated += 1
            else:
                summary.unchanged += 1
            continue

        row = ReferenceSecurity(
            isin=entry.get("isin"),
            ticker=entry.get("ticker"),
            exchange=entry.get("exchange"),
            amfi_code=entry.get("amfi_code"),
            security_type=entry["security_type"],
            name=entry["name"],
            aliases=list(entry.get("aliases") or []),
            country_code=entry.get("country_code"),
            source=entry.get("source", "bundled:unknown"),
            fetched_at=fetched_at,
        )
        session.add(row)
        pending_new.append(row)
        if row.isin:
            by_isin[row.isin] = row
        if row.ticker:
            by_ticker_exchange[(row.ticker, row.exchange)] = row
        if row.amfi_code:
            by_amfi[row.amfi_code] = row
        summary.created += 1

        processed_since_flush += 1
        if processed_since_flush >= _LOAD_CHUNK_SIZE:
            await session.flush()
            processed_since_flush = 0

    await session.flush()
    await session.commit()
    return summary
