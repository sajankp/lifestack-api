# Spec-057: NSE Bhavcopy Price Feed

**Created:** 2026-07-04
**Status:** Implemented (api#106, merged 2026-07-04)
**Depends on:** none (additive to the existing `investment_closing_prices_job` / `PerformanceService.refresh_workspace_prices` pipeline)

---

## Problem

`PerformanceService.refresh_workspace_prices` (`app/investing/performance_service.py:68`) already refreshes daily closing prices for every holding, split by instrument type: mutual funds go through the official AMFI NAV feed (`_fetch_all_amfi_navs`), everything else — including NSE/BSE-listed Indian stocks — goes through `_fetch_stock_price` (`app/investing/service.py:600`), which scrapes Yahoo Finance's undocumented chart API with a spoofed `User-Agent` and a `.NS` ticker suffix. That endpoint is not an official data source: it can rate-limit, silently omit a day, or return stale data with no error, and Lifestack has no way to distinguish "Yahoo has no data" from "Yahoo failed silently" (`_fetch_stock_price` swallows all exceptions and returns `None`).

NSE (the National Stock Exchange of India) publishes an official, free, daily end-of-day **bhavcopy** — a CSV of every listed security's OHLC and close price for that trading day — which is the authoritative settlement-price source Indian brokers and depositories themselves reconcile against. Using it for INR-denominated stock holdings removes Lifestack's only unofficial-scrape dependency for daily valuation of Indian equities.

## Solution

Add a new daily job, `bhavcopy_price_feed_job`, that downloads the NSE bhavcopy for the most recent completed trading day and upserts `HoldingPrice` rows (`source="bhavcopy"`) for every INR stock holding whose symbol matches a row in the bhavcopy — **before** `investment_closing_prices_job` runs. No changes to `refresh_workspace_prices` are needed: it already skips holdings that already have a `HoldingPrice` row dated `expected_close_date` (`app/investing/performance_service.py:85-93`), so any symbol the bhavcopy job successfully priced is automatically skipped by the Yahoo-backed job later in the morning; only symbols bhavcopy didn't cover (delisted, BSE-only, ETFs bhavcopy excludes, non-INR) fall through to the existing Yahoo path. This is purely additive — a second, earlier, more-authoritative pass over the same `HoldingPrice` table, not a pipeline rewrite.

### Fetch and parse (`app/investing/service.py`, alongside `_fetch_all_amfi_navs`)

```python
async def _fetch_nse_bhavcopy(
    client: httpx.AsyncClient, trade_date: date
) -> dict[str, tuple[date, Decimal]]:
    """Official NSE end-of-day security-wise price CSV.

    URL/format changes periodically (NSE has rotated the bhavcopy path and
    compression at least twice in recent years); this returns {} on any
    failure so a feed outage degrades to the existing Yahoo fallback rather
    than blocking the whole refresh cycle — same failure contract as
    ``_fetch_all_amfi_navs``.
    """
```

- URL: `https://archives.nseindia.com/products/content/sec_bhavdata_full_{DDMMYYYY}.csv` for `trade_date`.
- NSE requires session cookies from a prior request to its main site before archives requests succeed reliably (documented anti-scraping behavior); the client first does a warm-up `GET https://www.nseindia.com` with browser-like headers (same spoofed `User-Agent` pattern already used in `_fetch_stock_price`) to acquire cookies, then requests the CSV with those cookies attached.
- Parse: CSV columns include `SYMBOL`, `SERIES`, `CLOSE_PRICE` (or `CLOSE` — NSE has renamed this column before; check both). Keep only `SERIES == "EQ"` (main equity series — excludes bonds, preference shares, SME-platform series that would collide with unrelated symbols).
- Returns `{symbol.upper(): (trade_date, close_price)}`.
- Any HTTP error, timeout, missing column, or unparseable row: caught, logged, function returns `{}` (matches `_fetch_all_amfi_navs`'s `except httpx.HTTPError: return {}` contract, broadened to `except Exception` since CSV-shape errors aren't `httpx` errors).

### Job (`app/application/jobs.py`, copies `investment_closing_prices_job`'s per-workspace-isolated-session pattern)

```python
BHAVCOPY_PRICE_FEED_LOCK_KEY = ADVISORY_LOCK_BHAVCOPY_PRICE_FEED  # new constant, app/core/constants.py

async def bhavcopy_price_feed_job() -> None:
    """Pre-fill HoldingPrice from NSE's official bhavcopy before the Yahoo-backed
    investment_closing_prices_job runs, for INR stock holdings only."""
```

1. Acquire `pg_try_advisory_xact_lock(BHAVCOPY_PRICE_FEED_LOCK_KEY)` (same rolling-deploy guard every other job uses) — skip the run entirely if not acquired, same as the pattern documented in `docs/JOBS.md`.
2. Compute `expected_close_date = _previous_weekday(today)` (reuse the existing helper — bhavcopy is only published for trading days).
3. Fetch the bhavcopy once (`_fetch_nse_bhavcopy`), shared across all workspaces — this is a single global file, not per-workspace, so the download happens once per job run, not once per workspace (unlike AMFI NAVs today, which are also fetched once but per-workspace inside the loop — see Follow-up note below).
4. If the fetch returned `{}` (feed unavailable / trading holiday / bhavcopy not yet published), log and exit — `investment_closing_prices_job` will cover everything via Yahoo as it does today, so there's no correctness regression, only a missed optimization for that day.
5. Iterate active workspaces (same `select(Workspace.id).where(Workspace.is_active)` query `investment_closing_prices_job` uses); for each, in its own isolated session/transaction:
   - Fetch holdings via `HoldingRepository.get_all`, filter to `currency == "INR"` and `instrument_type != mutual_fund` (bhavcopy is equities/ETFs only).
   - For each holding whose `symbol.upper()` is a key in the bhavcopy map, `HoldingPriceRepository.upsert_price(..., source="bhavcopy")`.
   - One workspace's failure is logged and skipped, same as `investment_closing_prices_job`'s existing `try/except` per workspace.
6. Register in `app/main.py` via `register_daily_job(bhavcopy_price_feed_job, job_id="bhavcopy_price_feed", hour_utc=2, minute_utc=0)` — scheduled **before** `investment_closing_prices` (currently `hour_utc=2, minute_utc=30`), so the 30-minute gap gives the bhavcopy job room to complete first.

**Follow-up note (out of scope for this spec):** `_fetch_all_amfi_navs` is currently called once per workspace inside `refresh_workspace_prices`'s per-workspace loop (`investment_closing_prices_job` iterates workspaces, and each iteration constructs a fresh `PerformanceService.refresh_workspace_prices` call, which re-fetches the whole AMFI file). This bhavcopy job intentionally does **not** repeat that inefficiency — it fetches once up front — but does not fix the pre-existing AMFI duplication, which is a separate, already-existing inefficiency this spec doesn't touch.

### Source distinction in `HoldingPrice.source`

Adds a new value, `"bhavcopy"`, alongside the existing `"manual"` (user-submitted, `submit_prices`) and `"api"` (Yahoo, `refresh_workspace_prices`). `HoldingPrice.source` is a plain `str` column (`max_length=16`, no enum/CHECK constraint), so no migration needed — this is consistent with how `"api"` itself was added without one.

## Backend impact (`lifestack-api`)

- `app/investing/service.py`: new `_fetch_nse_bhavcopy(client, trade_date) -> dict[str, tuple[date, Decimal]]`, module-level function (same visibility as `_fetch_stock_price`/`_fetch_all_amfi_navs`, patchable the same way in tests).
- `app/core/constants.py`: new `ADVISORY_LOCK_BHAVCOPY_PRICE_FEED: int = 1008` (next free advisory-lock key after `1007`).
- `app/application/jobs.py`: new `bhavcopy_price_feed_job()`.
- `app/main.py`: register the new job at `hour_utc=2, minute_utc=0`, thirty minutes ahead of `investment_closing_prices`.
- `docs/JOBS.md`: new "NSE Bhavcopy Price Feed Job" entry (job 9, house style matching the existing 8 entries).
- No schema migration (see Source distinction above).
- No API-surface change — this only affects background-computed `HoldingPrice.source` values and `GET /v1/investing/holdings`'s existing `current_price` field is unaffected in shape, only in *which* upstream feed produced the number for INR stocks.

## Golden test scenarios (required before merge)

New tests in `app/tests/integration/test_investing.py` (co-located with the existing `_fetch_stock_price`/`_fetch_all_amfi_navs` mock-patch tests) or a new `app/tests/application/test_bhavcopy_price_feed_job.py`:

1. **Bhavcopy hit** — mock `_fetch_nse_bhavcopy` to return a symbol at a known close price; a workspace has an INR stock holding with that symbol; running `bhavcopy_price_feed_job` creates a `HoldingPrice` row with `source="bhavcopy"` and the mocked close.
2. **Bhavcopy miss, Yahoo fallback still works** — mock `_fetch_nse_bhavcopy` to return `{}` (feed unavailable); running `bhavcopy_price_feed_job` creates no rows; a subsequent `refresh_workspace_prices` call (mocking `_fetch_stock_price` as existing tests already do) still prices the holding via Yahoo — proves the fallback chain isn't broken.
3. **Bhavcopy pre-fill skips the Yahoo call** — mock both `_fetch_nse_bhavcopy` (hit) and `_fetch_stock_price` (spy); run `bhavcopy_price_feed_job` then `refresh_workspace_prices` for the same workspace/date — assert `_fetch_stock_price` was never called for that symbol, proving the "already priced for `expected_close_date`" skip in `refresh_workspace_prices` correctly treats a `source="bhavcopy"` row the same as a `source="api"` one.
4. **Mutual funds and non-INR holdings are never touched** — a workspace has an INR mutual-fund holding and a USD stock holding whose symbols happen to collide with bhavcopy rows; `bhavcopy_price_feed_job` must not create `HoldingPrice` rows for either (filtered out by the `currency == "INR" and instrument_type != mutual_fund` guard).
5. **Non-idempotent-job registration guard respected** — `bhavcopy_price_feed_job` is a plain daily job like `investment_closing_prices_job` (idempotent — upsert, not insert-only), so it must register cleanly under `register_daily_job` without needing `SCHEDULER_ALLOW_NON_IDEMPOTENT_JOBS`; a smoke test asserts the job is present in `app/main.py`'s registration call list (mirrors how other jobs are smoke-tested, if such a test already exists — otherwise a direct call to `bhavcopy_price_feed_job()` against an empty DB completing without raising is sufficient).

## Out of scope

- **BSE bhavcopy.** NSE covers the overwhelming majority of Indian retail brokerage volume; adding BSE as a second feed (different CSV format, different archive URL) is a follow-up if BSE-only-listed holdings turn out to be common enough to matter — flagged in the task's own framing ("NSE/BSE") as a nice-to-have, not required for this spec's Done criteria.
- **Historical backfill.** This job only ever fetches "yesterday's" bhavcopy; it does not backfill missing `HoldingPrice` history for holdings created before this feature shipped. `refresh_workspace_prices`'s existing Yahoo path already has the same limitation (10-day lookback window in `_fetch_stock_price`'s `range` param), so this isn't a new gap.
- **Retiring the Yahoo path.** `_fetch_stock_price` stays as the fallback for delisted-from-bhavcopy symbols, BSE-only listings, and all non-INR holdings — this spec adds a preferred-first source, it doesn't replace the existing one.
- **Corporate-action-aware bhavcopy reconciliation.** A stock split changes both quantity (spec-051, already handled via `CorporateAction` replay) and the bhavcopy close price simultaneously; this spec fetches whatever close NSE publishes for the day with no special-casing — same behavior the existing Yahoo path already has around split days.
