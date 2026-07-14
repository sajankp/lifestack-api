# Feature Spec: Automated Constituent Ingestion for Look-Through Analytics
**Status:** Archived - retired
**Spec ID:** 032

---

## Retirement Note (2026-06-19)

The Yahoo Finance constituent provider, its local session-cache JSON file, scheduler job, CLI entry,
configuration, and automated-ingestion tests have been removed. The unofficial provider was not
reliable enough for product use and generated runtime residue in the repository.

Constituent data remains supported through the workspace-facing CSV workflow in
[Spec 034](./spec-034-constituent-csv-import.md). This document is retained only as a historical
implementation record and must not be used as an active runtime contract.

---

## Implementation Notes (2026-06-13)

- Implemented instrument classification support:
  - holdings default to `stock`, preserving the direct-exposure flow for ordinary stocks
  - holding creation can declare `stock`, `etf`, or `mutual_fund`
  - existing instruments can be corrected with `PATCH /v1/investing/instruments/{instrument_id}`
  - investing-holdings CSV imports accept optional `instrument_type`, with legacy five-column files defaulting to `stock`
- Implemented scheduled constituent ingestion:
  - `YahooFinanceConstituentProvider` parses `quoteSummary?modules=topHoldings`
  - `ingest_constituents` processes active ETF/MF instruments only
  - fetched top-N weights are normalized and stored with `source="yahoo-finance-top-n-normalised"`
  - fresh snapshots are skipped according to `CONSTITUENT_INGESTION_STALENESS_DAYS`
  - `constituent_ingestion_job` is registered with advisory lock key `1008`
  - CLI support added through `python -m app.cli.run constituent_ingestion`
- Implemented UI support:
  - the holding form includes an `Asset Type` selector defaulting to Stock
  - the holdings table displays the resolved asset type
  - the analytics tab includes an instrument correction panel so old auto-created stock instruments can be marked as ETF/MF

---

## 1. Overview

Look-through analytics (Spec 012) already has a complete data model and analytics engine. Spec 032 adds
the ingestion and classification layer that makes those analytics useful without requiring manual
constituent seeding for every ETF or mutual fund holding.

The implemented constituent ingestion pipeline is modelled closely on the `fx_rate_ingestion_job`
(Spec 026), using the Yahoo Finance unofficial API — the same provider family already in use for stock
price fetches (Spec 031). It:

1. Runs as a scheduled background job (`constituent_ingestion_job`) that discovers all ETF/MF instruments in the
   system and attempts to fetch their top holdings.
2. Normalises the partial constituent lists returned by the provider (Yahoo typically returns only the top 10–25
   holdings, not a full 100% weighted list) into valid snapshots using proportional weight renormalisation.
3. Upserts snapshots into `investing_instrument_constituents` via the existing `ConstituentService` ingestion path.
4. Exposes the job via the CLI runner (`python -m app.cli.run constituent_ingestion`) consistent with Spec 030.
5. Introduces a new `CONSTITUENT_INGESTION_STALENESS_DAYS` config knob alongside the existing
   `EXCHANGERATE_API_KEY` pattern.

---

## 2. Goals

- Eliminate the manual step of seeding constituent data for common ETFs/MFs.
- Power look-through analytics automatically for standard market symbols (e.g. VUSA, SPY, QQQ, VWRL).
- Keep the provider decoupled behind a thin adapter interface so a future paid data provider (Polygon, Finnhub)
  can be swapped in without touching job orchestration.
- Fail gracefully: a provider failure for one instrument must not block ingestion for others.
- Remain idempotent: re-running the job for the same date produces the same snapshot.

---

## 3. Non-Goals

- Full constituent universe (100% weight coverage). Yahoo returns top-N only; we normalise and annotate the
  snapshot as `top_n_normalised` not `full_universe`.
- Real-time constituent refresh (daily is sufficient; ETF composition changes are rare intraday).
- Constituent data for direct-stock holdings (`instrument_type=stock`). The job only processes `etf` and
  `mutual_fund` instruments.
- Automatic company deduplication / ISIN cross-referencing. Company names from the provider are stored
  as-is and linked via the existing name-based `Company` lookup in `ConstituentService`. Identifier
  enrichment is a separate future concern.

---

## 4. Architecture and Design Decisions

### 4.1 Why not fetch constituents live at analytics request time?

Spec 012 explicitly evaluated and rejected this approach (Option B) in favour of day-level snapshot
persistence (Option C). This spec implements the ingestion side of that decision. The analytics layer
remains unchanged: it reads from snapshots, applies fallback rules, and emits staleness warnings.

### 4.2 Weight normalisation for partial constituent lists

Yahoo Finance returns the top-N holdings with weights that sum to less than 1.0 (e.g. top 10 holdings
of SPY sum to ~0.28). The existing `ConstituentService.upsert_constituents` validator requires weights to
sum to within `[0.99, 1.01]` and will reject any partial snapshot.

Implemented behavior:

1. Fetch top-N constituent entries from the provider.
2. Sum the returned weights.
3. Proportionally scale each weight so the set sums to 1.0, with eight-decimal quantisation and the
   final row absorbing any rounding remainder.
4. Record `source="yahoo-finance-top-n-normalised"` to distinguish from full-universe snapshots.

Manual or future external callers may also submit `renormalise=true` on `InstrumentConstituentUpsert`
when they want the service to normalize weights before validation.

This preserves the relative composition signal while making the snapshot mathematically valid. The
`analysis_status` in the exposure response remains `partial` unless all pooled holdings have a fresh
snapshot within the staleness window (existing behaviour, unchanged).

### 4.3 Provider adapter abstraction

A thin `ConstituentProvider` protocol is introduced in `app/application/constituent_provider.py`:

```python
class ConstituentProviderResult:
    symbol: str
    constituents: list[ConstituentEntry]  # (name, ticker, raw_weight)
    fetched_at: datetime
    provider_key: str


class ConstituentProvider(Protocol):
    async def fetch(self, symbol: str) -> ConstituentProviderResult | None: ...
```

V1 ships with one concrete implementation: `YahooFinanceConstituentProvider`. This mirrors how
`_fetch_stock_price` is currently a free function in `investing/service.py` — but elevated to a
proper adapter class here because the constituent fetch is more complex (response parsing, partial
list detection) and is more likely to be swapped.

### 4.4 Advisory lock

The job acquires `pg_try_advisory_xact_lock(ADVISORY_LOCK_CONSTITUENT_INGESTION)` (key `1008`) to
prevent concurrent execution across replicas, consistent with all other jobs.

---

## 5. Yahoo Finance Constituent Endpoint

The unofficial endpoint used:

```
GET https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?count=25&scrIds=top_etfs
```

However, for per-symbol constituent data the more targeted endpoint is:

```
GET https://query1.finance.yahoo.com/v1/finance/quoteSummary/{SYMBOL}?modules=topHoldings
```

Response structure (relevant subset):

```json
{
  "quoteSummary": {
    "result": [{
      "topHoldings": {
        "holdings": [
          { "holdingName": "Apple Inc", "symbol": "AAPL", "holdingPercent": { "raw": 0.0734 } },
          ...
        ]
      }
    }]
  }
}
```

The `holdingPercent.raw` field is the un-normalised weight for that holding within the full portfolio.
The job collects these, filters out entries with missing names or zero weights, and renormalises the
collected subset to sum to 1.0 before upsert.

**Failure modes handled:**
- HTTP 404/429/5xx → log warning, skip symbol, continue.
- Response missing `topHoldings` or empty `holdings` list → log warning, skip symbol, continue.
- All fetches fail → job completes without upsert, emits a summary log event with failure count.

---

## 6. Backend Changes

### 6.1 New advisory lock constant

**File:** `app/core/constants.py`

```python
#   1008 – constituent_ingestion_job
ADVISORY_LOCK_CONSTITUENT_INGESTION: int = 1008
```

### 6.2 New config knob

**File:** `app/config.py`

```python
# Constituent ingestion (Spec 032)
CONSTITUENT_INGESTION_ENABLED: bool = True  # opt-out kill-switch per-instance
CONSTITUENT_INGESTION_HOUR_UTC: int = (
    6  # daily run hour (after FX at 02:00, prices implied via 031)
)
CONSTITUENT_INGESTION_STALENESS_DAYS: int = 7  # skip re-fetch if snapshot is fresher than this
```

The `CONSTITUENT_INGESTION_STALENESS_DAYS` knob is intentionally separate from the analytics-layer
`staleness_window_days` (currently hardcoded to 30 in `ExposureAnalyticsService`). The ingestion knob
controls when the job re-fetches data; the analytics knob controls when stale snapshots are excluded
from exposure computation.

### 6.3 Provider adapter module

**File:** `app/application/constituent_provider.py` *(new)*

Defines the `ConstituentProvider` Protocol and `YahooFinanceConstituentProvider` concrete implementation.
Uses `httpx.AsyncClient` (already a project dependency) with a 10-second timeout, consistent with
`_fetch_stock_price` in `investing/service.py`.

### 6.4 Ingestion workflow

**File:** `app/application/workflows.py`

New async function `ingest_constituents(session, provider, staleness_days)`:

1. Query `investing_instruments` for all rows where `instrument_type IN ('etf', 'mutual_fund')`.
2. For each instrument, check if a snapshot from today already exists in
   `investing_instrument_constituents`. If yes and `staleness_days` guard is satisfied → skip.
3. Call `provider.fetch(instrument.symbol)`.
4. On success: renormalise weights, call `ConstituentService.upsert_constituents` (bypassing the
   HTTP router layer — called directly as a service, consistent with how `ingest_fx_rates` calls
   `FxRateService` directly).
5. On failure: log and continue.
6. Return a result dict keyed by `{workspace_id}:{symbol}` with values `"ok" | "skipped" | "failed"`
   for job-level logging.

> **Workspace scoping note:** Unlike FX rates (global market data), instruments are workspace-scoped in
> the current model. The ingestion workflow iterates across **all workspace-scoped ETF/MF instruments**
> and stores snapshots per instrument. This intentionally accepts duplicate snapshots for the same market
> symbol across workspaces until a future global catalog migration is selected.

### 6.5 Job wrapper

**File:** `app/application/jobs.py`

New `constituent_ingestion_job()` following the exact same pattern as `fx_rate_ingestion_job`:

```python
async def constituent_ingestion_job() -> None:
    """
    Daily job that fetches ETF/MF constituent holdings from the provider
    and upserts day-level snapshots for look-through analytics (Spec 032).
    """
    start_time = datetime.now(UTC)
    logger.info("constituent_ingestion_job_start", job_name="constituent_ingestion_job")

    async with postgres.async_session_maker() as session, session.begin():
        lock_res = await session.execute(
            select(func.pg_try_advisory_xact_lock(CONSTITUENT_INGESTION_LOCK_KEY))
        )
        if not lock_res.scalar():
            logger.info("constituent_ingestion_job_skipped_lock_held", ...)
            return

        try:
            provider = YahooFinanceConstituentProvider()
            result = await ingest_constituents(
                session, provider, settings.CONSTITUENT_INGESTION_STALENESS_DAYS
            )
            logger.info("constituent_ingestion_job_completed", ..., result_summary=result)
        except Exception as e:
            logger.error("constituent_ingestion_job_failed", ..., error=str(e), exc_info=True)
            raise
```

### 6.6 Scheduler registration

**File:** `app/main.py`

```python
if settings.CONSTITUENT_INGESTION_ENABLED:
    register_daily_job(
        constituent_ingestion_job,
        job_id="constituent_ingestion",
        hour_utc=settings.CONSTITUENT_INGESTION_HOUR_UTC,
    )
```

### 6.7 CLI runner extension

**File:** `app/cli/run.py`

```python
"constituent_ingestion": constituent_ingestion_job,
```

Invocation:

```bash
python -m app.cli.run constituent_ingestion
```

No `--workspace-id` support (job is instrument-scoped, not workspace-scoped).

### 6.8 Weight tolerance relaxation for `top-n-normalised` sources

**File:** `app/investing/service.py` — `ConstituentService.upsert_constituents`

The current validator enforces `0.99 ≤ sum(weights) ≤ 1.01` and rejects all other inputs. The
ingestion workflow normalises weights to 1.0 before calling the service, so the validator will
pass without changes in the normal case.

However, to allow external callers (manual API, future providers) to also submit pre-normalised
partial lists without the caller needing to do the maths, add an optional `renormalise: bool = False`
field to `InstrumentConstituentUpsert`. When `True`, the service normalises weights before validation
rather than rejecting.

---

## 7. Follow-Up Decisions

1. **Cross-workspace constituent ownership:** Currently `investing_instrument_constituents` links to
   `investing_instruments.id` which is workspace-scoped. Two workspaces holding VUSA will have two
   separate `Instrument` rows and therefore two separate constituent snapshots fetched and stored. This
   duplication is accepted for V1; global instrument catalog work remains a future migration.

2. **Rate limiting from Yahoo Finance:** Yahoo's unofficial API has no published rate limits but
   will throttle aggressive scrapers. The V1 workflow processes instruments sequentially and skips failed
   symbols; a configurable delay or bounded concurrent fetcher can be added if production volume requires it.

3. **Top-N cutoff:** Yahoo returns up to 25 holdings. Should we expose a
   `CONSTITUENT_INGESTION_MAX_HOLDINGS` config to cap the number stored per snapshot?

4. **Company identity collision:** The `Company` model has a `UNIQUE(workspace_id, name)` constraint.
   If Yahoo returns "Apple Inc" for one ETF and "Apple Inc." (with a period) for another, they become
   two separate company rows. Should the ingestion workflow apply any name normalisation before lookup?

---

## 8. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Yahoo Finance endpoint changes or becomes unavailable | Provider adapter abstraction makes it easy to swap. Advisory lock + graceful skip means no blast radius on failure. |
| Normalised top-N weights misrepresent actual composition | `source` field records `yahoo-finance-top-n-normalised`; analytics `analysis_status` remains `partial`; no user-facing claim of full universe coverage. |
| Duplicate company rows from name variation | Accepted for V1 (noted as Open Question 4). Can be addressed with a normalisation pass or ISIN mapping later. |
| Cross-workspace instrument duplication | Accepted for V1 (noted as Open Question 1). Global catalog is a future migration, not a V1 requirement. |
| High instrument count causing slow job runs | V1 uses sequential fetches and a staleness guard to reduce provider load; bounded concurrency or delay settings can be added if volume grows. |

---

## 9. Test Strategy

### 9.1 Unit tests — `app/tests/finance/test_constituent_ingestion.py`

- `test_fetch_success`: mock httpx, assert constituent list parsed and renormalised correctly.
- `test_fetch_partial_weights_renormalised`: assert that raw weights [0.05, 0.04, 0.03] become
  [0.417, 0.333, 0.25] after normalisation.
- `test_fetch_provider_failure_skips_symbol`: assert HTTP 500 is caught, symbol marked `failed`,
  job continues.
- `test_fetch_empty_holdings_skips_symbol`: assert missing `topHoldings` key is handled gracefully.
- `test_ingest_constituents_staleness_guard`: assert symbol with fresh snapshot is skipped.
- `test_constituent_ingestion_job_orchestration`: mock provider, assert `ingest_constituents`
  is called and job logs completion.
- `test_constituent_ingestion_job_propagates_exception`: assert unhandled provider exception
  is re-raised after logging.

### 9.2 Integration tests

- `test_constituent_ingestion_end_to_end`: using testcontainers Postgres, create workspace +
  ETF instrument, run `ingest_constituents` with a mocked provider returning valid top-3 holdings,
  assert rows inserted in `investing_instrument_constituents`, assert exposure endpoint returns
  `analysis_status=complete`.

### 9.3 CLI test

- Extend `app/tests/test_cli.py` to assert `constituent_ingestion` is a valid job name and
  that `--workspace-id` is rejected (not supported).

### 9.4 Manual verification

```bash
# Trigger via CLI against running Docker instance
docker compose exec api python -m app.cli.run constituent_ingestion

# Verify rows inserted
docker compose exec postgres psql -U lifestack -d lifestack \
  -c "SELECT i.symbol, count(*) AS constituents, ic.as_of_date, ic.source
      FROM investing_instrument_constituents ic
      JOIN investing_instruments i ON i.id = ic.instrument_id
      GROUP BY i.symbol, ic.as_of_date, ic.source
      ORDER BY ic.as_of_date DESC;"

# Verify analytics endpoint reflects updated data
curl -s http://localhost:8000/v1/investing/analytics/exposure?as_of=$(date +%Y-%m-%d) | jq .analysis_status
```

---

## 10. Acceptance Criteria

- [x] `constituent_ingestion_job` runs daily at `CONSTITUENT_INGESTION_HOUR_UTC` when `SCHEDULER_ENABLED=true`
      and `CONSTITUENT_INGESTION_ENABLED=true`.
- [x] Running `python -m app.cli.run constituent_ingestion` is supported by the CLI runner.
- [x] The workflow fetches and upserts constituent snapshots for active ETF/MF instruments in the database.
- [x] Fetched weights are renormalised to sum to 1.0 before storage.
- [x] Snapshot `source` is recorded as `yahoo-finance-top-n-normalised` to distinguish from full-universe data.
- [x] Provider failure for one symbol does not prevent processing of remaining symbols.
- [x] Running the job again while snapshots are fresh is a no-op for those instruments (staleness guard).
- [x] Existing analytics endpoints (`/exposure`, `/overlap`) reflect the ingested constituent data without
      analytics-layer changes.
- [x] Unit and integration coverage exists for provider parsing, ingestion workflow, staleness guard, job
      wrapper, CLI support, classification, and CSV import behavior.
- [x] CLI runner rejects `--workspace-id` for this job.
- [x] README lists `constituent_ingestion` in the supported job names list.
