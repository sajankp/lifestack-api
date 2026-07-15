"""Identifier resolution against `reference_securities` (spec-083 §5.4).

Bundled data first, then — only on a miss and only when opted in — the
Yahoo quote/identity API fallback, caching the result so it's never
re-fetched (§7.2). Used by the import-validation path and the
`GET /v1/investing/reference/resolve` endpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import httpx

from app.config import settings
from app.investing.models import ReferenceSecurity
from app.investing.repository import ReferenceSecurityRepository
from app.investing.service import fetch_yahoo_identity

IdentifierStatus = Literal["resolved", "unresolved", "ambiguous"]


class ReferenceResolveService:
    def __init__(self, reference_repo: ReferenceSecurityRepository):
        self.reference_repo = reference_repo

    async def resolve(
        self,
        *,
        isin: str | None = None,
        ticker: str | None = None,
        exchange: str | None = None,
        security_type: str | None = None,
    ) -> tuple[ReferenceSecurity | None, IdentifierStatus]:
        if isin:
            match = await self.reference_repo.get_by_isin(isin)
            if match is not None:
                return match, "resolved"

        if ticker:
            match = await self.reference_repo.get_by_ticker_exchange(ticker, exchange)
            if match is not None:
                return match, "resolved"
            if exchange is None:
                candidates = await self.reference_repo.list_by_ticker(ticker)
                if len(candidates) > 1:
                    # Same ticker string, multiple markets, and the caller
                    # didn't say which — don't guess (spec-083 §6.1).
                    return None, "ambiguous"

        if settings.REFERENCE_DATA_API_ENABLED and ticker:
            fetched = await self._fetch_and_cache(ticker, security_type)
            if fetched is not None:
                return fetched, "resolved"

        return None, "unresolved"

    async def _fetch_and_cache(
        self, ticker: str, security_type: str | None
    ) -> ReferenceSecurity | None:
        async with httpx.AsyncClient() as client:
            identity = await fetch_yahoo_identity(client, ticker)
        if identity is None:
            return None
        row = ReferenceSecurity(
            isin=None,
            ticker=identity["ticker"],
            exchange=identity.get("exchange"),
            amfi_code=None,
            security_type=security_type or identity.get("security_type") or "stock",
            name=identity["name"],
            aliases=[],
            country_code=identity.get("country_code"),
            source="api:yahoo",
            fetched_at=datetime.now(UTC),
        )
        return await self.reference_repo.upsert(row)
