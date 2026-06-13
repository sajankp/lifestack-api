from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

import httpx
import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ConstituentEntry:
    company_name: str
    company_ticker: str | None
    raw_weight: Decimal


@dataclass(frozen=True)
class ConstituentProviderResult:
    symbol: str
    constituents: list[ConstituentEntry]
    fetched_at: datetime
    provider_key: str


class ConstituentProvider(Protocol):
    async def fetch(self, symbol: str) -> ConstituentProviderResult | None: ...


class YahooFinanceConstituentProvider:
    provider_key = "yahoo-finance-top-n-normalised"

    async def fetch(self, symbol: str) -> ConstituentProviderResult | None:
        normalized_symbol = symbol.upper().strip()
        url = (
            "https://query1.finance.yahoo.com/v1/finance/quoteSummary/"
            f"{normalized_symbol}?modules=topHoldings"
        )
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
            )
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()
            except Exception as exc:
                logger.warning(
                    "constituent_provider_fetch_failed",
                    symbol=normalized_symbol,
                    provider=self.provider_key,
                    error=str(exc),
                )
                return None

        result = data.get("quoteSummary", {}).get("result")
        if not result:
            logger.warning("constituent_provider_empty_result", symbol=normalized_symbol)
            return None

        holdings = result[0].get("topHoldings", {}).get("holdings") or []
        constituents: list[ConstituentEntry] = []
        for item in holdings:
            name = str(item.get("holdingName") or "").strip()
            ticker = str(item.get("symbol") or "").strip().upper() or None
            raw_weight = item.get("holdingPercent", {}).get("raw")
            if not name or raw_weight is None:
                continue
            weight = Decimal(str(raw_weight))
            if weight <= 0:
                continue
            constituents.append(
                ConstituentEntry(company_name=name, company_ticker=ticker, raw_weight=weight)
            )

        if not constituents:
            logger.warning("constituent_provider_no_holdings", symbol=normalized_symbol)
            return None

        return ConstituentProviderResult(
            symbol=normalized_symbol,
            constituents=constituents,
            fetched_at=datetime.now(UTC),
            provider_key=self.provider_key,
        )
