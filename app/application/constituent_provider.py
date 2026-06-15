import contextlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

import httpx
import structlog

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

logger = structlog.get_logger(__name__)

CACHE_FILE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".yahoo_session_cache.json",
)


@contextmanager
def _cache_lock() -> Iterator[None]:
    if fcntl is None:
        yield
        return

    cache_lock_path = f"{CACHE_FILE_PATH}.lock"
    lock_dir = os.path.dirname(cache_lock_path)
    if lock_dir:
        os.makedirs(lock_dir, exist_ok=True)
    with open(cache_lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


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

    def _load_cache(self) -> tuple[dict | None, str | None, float | None]:
        with _cache_lock():
            if os.path.exists(CACHE_FILE_PATH):
                try:
                    with open(CACHE_FILE_PATH) as f:
                        data = json.load(f)
                        return data.get("cookies"), data.get("crumb"), data.get("backoff_until")
                except Exception:
                    pass
        return None, None, None

    def _save_cache(
        self, cookies: dict | None, crumb: str | None, backoff_until: float | None = None
    ) -> None:
        try:
            with _cache_lock():
                existing = {}
                if os.path.exists(CACHE_FILE_PATH):
                    with contextlib.suppress(Exception), open(CACHE_FILE_PATH) as f:
                        existing = json.load(f)

                if cookies is not None:
                    existing["cookies"] = cookies
                if crumb is not None:
                    existing["crumb"] = crumb
                if backoff_until is not None:
                    existing["backoff_until"] = backoff_until
                elif "backoff_until" not in existing:
                    existing["backoff_until"] = None

                cache_dir = os.path.dirname(CACHE_FILE_PATH)
                fd, temp_path = tempfile.mkstemp(
                    prefix=".yahoo_session_cache.", suffix=".tmp", dir=cache_dir or None
                )
                try:
                    with os.fdopen(fd, "w") as f:
                        json.dump(existing, f)
                    os.replace(temp_path, CACHE_FILE_PATH)
                finally:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(temp_path)
        except Exception as exc:
            logger.warning("failed_to_save_yahoo_cache", error=str(exc))

    def _clear_cache(self) -> None:
        try:
            _, _, backoff_until = self._load_cache()
            existing = {"cookies": None, "crumb": None, "backoff_until": backoff_until}
            with _cache_lock():
                cache_dir = os.path.dirname(CACHE_FILE_PATH)
                fd, temp_path = tempfile.mkstemp(
                    prefix=".yahoo_session_cache.", suffix=".tmp", dir=cache_dir or None
                )
                try:
                    with os.fdopen(fd, "w") as f:
                        json.dump(existing, f)
                    os.replace(temp_path, CACHE_FILE_PATH)
                finally:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(temp_path)
        except Exception as exc:
            logger.warning("failed_to_clear_yahoo_cache", error=str(exc))

    async def _fetch_cookie_and_crumb(self, client: httpx.AsyncClient) -> tuple[dict, str]:
        cookies, crumb, backoff_until = self._load_cache()
        now = datetime.now(UTC).timestamp()
        if backoff_until and now < backoff_until:
            logger.info("skipping_cookie_crumb_fetch_due_to_cooldown", backoff_until=backoff_until)
            return cookies or {}, crumb or ""

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        }

        try:
            # 1. Fetch cookie
            resp = await client.get("https://fc.yahoo.com", headers=headers, follow_redirects=True)
            if resp.status_code == 429:
                self._save_cache(None, None, backoff_until=now + 900)  # 15 minutes backoff
                raise Exception("fc.yahoo.com returned 429 too many requests")
            cookies_dict = dict(client.cookies.items())

            # 2. Fetch crumb
            resp2 = await client.get(
                "https://query2.finance.yahoo.com/v1/test/getcrumb", headers=headers
            )
            crumb = resp2.text.strip()

            if resp2.status_code == 429 or (crumb and "too many requests" in crumb.lower()):
                self._save_cache(cookies_dict, None, backoff_until=now + 900)  # 15 minutes backoff
                raise Exception("getcrumb returned 429 too many requests")

            if resp2.status_code == 200 and crumb:
                self._save_cache(cookies_dict, crumb, backoff_until=0.0)
                return cookies_dict, crumb
            else:
                self._save_cache(cookies_dict, None, backoff_until=now + 300)  # 5 minutes backoff
                raise Exception(f"Failed to fetch crumb: {resp2.status_code} - {resp2.text}")
        except Exception as exc:
            _, _, b = self._load_cache()
            if not b or now >= b:
                self._save_cache(None, None, backoff_until=now + 300)
            raise exc

    async def fetch(self, symbol: str) -> ConstituentProviderResult | None:
        normalized_symbol = symbol.upper().strip()

        # Try loading cache
        cookies, crumb, backoff_until = self._load_cache()
        now = datetime.now(UTC).timestamp()

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "application/json",
        }

        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            if not cookies or not crumb:
                if not backoff_until or now >= backoff_until:
                    try:
                        cookies, crumb = await self._fetch_cookie_and_crumb(client)
                    except Exception as exc:
                        logger.warning(
                            "constituent_provider_auth_failed",
                            symbol=normalized_symbol,
                            error=str(exc),
                        )
                        cookies = {}
                        crumb = ""
                else:
                    logger.info("skip_fetch_cookie_crumb_in_backoff", symbol=normalized_symbol)
                    cookies = cookies or {}
                    crumb = crumb or ""

            # Try query2 v10 quoteSummary with crumb and cookies
            if crumb:
                url = (
                    f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/"
                    f"{normalized_symbol}?modules=topHoldings&crumb={crumb}"
                )
                client.cookies.update(cookies)
            else:
                # Basic fallback URL without crumb
                url = (
                    f"https://query1.finance.yahoo.com/v1/finance/quoteSummary/"
                    f"{normalized_symbol}?modules=topHoldings"
                )

            try:
                response = await client.get(url, headers=headers)

                # If 401/403/429 occurs, it suggests the cached crumb/cookie is invalid or blocked
                if response.status_code in (401, 403, 429) and (cookies or crumb):
                    _, _, b = self._load_cache()
                    now = datetime.now(UTC).timestamp()
                    if not b or now >= b:
                        logger.info(
                            "clearing_invalid_yahoo_cache",
                            symbol=normalized_symbol,
                            status=response.status_code,
                        )
                        self._clear_cache()

                        # Retry once with a fresh fetch
                        client.cookies.clear()
                        try:
                            cookies, crumb = await self._fetch_cookie_and_crumb(client)
                            if crumb:
                                url = (
                                    f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/"
                                    f"{normalized_symbol}?modules=topHoldings&crumb={crumb}"
                                )
                                client.cookies.update(cookies)
                                response = await client.get(url, headers=headers)
                        except Exception:
                            pass
                    else:
                        logger.info(
                            "skipping_retry_in_backoff",
                            symbol=normalized_symbol,
                            status=response.status_code,
                        )

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
