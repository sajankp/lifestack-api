# Spec-056: CAMS CAS PDF Import

**Created:** 2026-07-04
**Status:** Approved (owner directive 2026-07-04, Rule 7 — Tasks 5–13 pre-approved; spec still written in house style, approval pause waived)
**Depends on:** spec-041 (investing orders), spec-044 (FIFO cost basis), spec-051 (corporate actions — referenced for price-discontinuity flagging)

---

## Problem

Every mutual-fund transaction today must be entered one row at a time via the `investing-orders` CSV template, or typed manually. A CAMS Consolidated Account Statement (CAS) — the standard PDF every Indian mutual-fund investor can download from camsonline.com, covering every folio across every AMC serviced by CAMS — already contains this exact data (scheme, date, transaction type, amount, units, NAV) but there is no way to import it; the existing import pipeline only accepts `.csv`/`.xlsx` (`ImportService.create_batch`, `app/imports/service.py:432-434`).

### Concrete shape of the source data

A CAMS CAS PDF is organized as nested sections — one block per folio, one sub-block per scheme within that folio, then one row per transaction:

```
Folio No: 12345678 / PAN: ABCDE1234F
KYC: OK  PAN: OK

HDFC Flexi Cap Fund - Growth Option - Direct Plan  (ISIN: INF179K01WV6)
Registrar : CAMS

Date         Transaction                          Amount(Rs.)   Units       NAV(Rs.)   Unit Balance
15-Jan-2025  Purchase                              10,000.00    62.774      159.35     62.774
15-Feb-2025  SIP Purchase                          10,000.00    58.309      171.51     121.083
20-Jun-2025  Redemption                            -5,000.00    -25.432     196.60     95.651
```

Every AMC/scheme block repeats this shape. `Amount`/`Units` are signed (negative for redemptions); `NAV` is the per-unit price on that transaction date — exactly `InvestingOrder.price_per_unit`.

## Solution

Add a new import module, `investing-cams-cas`, that accepts a PDF upload, parses it into the **same `InvestingOrderCreate` shape** the existing `investing-orders` CSV import already produces, and then **reuses that commit path unchanged** — this is the key design choice: CAMS CAS is a new *parser*, not a new *pipeline*.

### New `ImportModule` value

```python
class ImportModule(StrEnum):
    ...
    investing_cams_cas = "investing-cams-cas"
```

### Upload: accept PDF for this module only

`ImportService.create_batch` currently rejects anything but `.csv`/`.xlsx` (`app/imports/service.py:430-434`). Add a module-gated branch: `investing_cams_cas` requires `.pdf`, every other module keeps requiring `.csv`/`.xlsx` — not loosened for anyone else.

CAMS statements have no notion of a Lifestack account (they're per-folio, not per-brokerage-account), so `create_batch` gains a required `target_account_id: uuid.UUID` parameter for this module only, validated as an existing active brokerage account (same check `InvestingOrderService._validate_brokerage_account` already does) — every transaction parsed from the PDF is bound to that one account. A user with mutual funds split across a Zerodha Coin account and a Groww account uploads the statement twice, picking the matching account each time (mirrors the existing per-account scoping of orders/lots — see spec-044's CBDT Circular 768 citation).

### Parsing: text-line regex, not table extraction

**Dependency:** add `pdfplumber` (pure-Python, MIT license, already the more robust choice for CAMS's inconsistent internal table structure — real-world CAMS parsers use text-line regex over `extract_tables()` because column boundaries vary by AMC/registrar template).

`app/imports/cams_cas_parser.py` (new module):

1. `pdfplumber.open(file_path)`, concatenate `page.extract_text()` across all pages.
2. Regex-match folio headers (`Folio No:\s*([\w/-]+)`) and scheme headers (a line ending in an ISIN pattern `\(ISIN:\s*([A-Z]{2}[A-Z0-9]{9}\d)\)`) to track current `(folio, scheme_name, isin)` context per line.
3. Regex-match transaction lines: `^(\d{2}-\w{3}-\d{4})\s+(.+?)\s+([-\d,]+\.\d{2})\s+([-\d,]+\.\d{3})\s+([\d,]+\.\d{2})\s+([-\d,]+\.\d{3})$` → date, description, amount, units, nav, balance.
4. Classify `description` into an order type via keyword match (case-insensitive substring):
   - contains `"purchase"` (incl. `"sip purchase"`) → `buy`
   - contains `"redemption"` → `sell`
   - anything else (`"dividend"`, `"switch"`, `"stamp duty"`, `"reversal"`, ...) → **not parsed as an order**; collected into a separate `skipped_rows` list with the reason, surfaced in the preview response (see below) rather than silently dropped.
5. For buy/sell rows, emit a dict matching `InvestingOrderCreate`'s shape exactly:
   ```python
   {
       "symbol": isin,                      # see "Symbol choice" below
       "order_type": "buy" | "sell",
       "instrument_type": "mutual_fund",
       "instrument_name": scheme_name,
       "quantity": abs(units),
       "price_per_unit": nav,
       "currency": "INR",
       "occurred_at": parsed_date.isoformat(),
       "notes": f"CAMS CAS import — folio {folio}",
   }
   ```

**Symbol choice — ISIN, not AMFI scheme code.** Lifestack's existing NAV auto-refresh (`_get_amfi_nav` in `performance_service.py`) keys mutual funds by numeric AMFI scheme code, which CAMS statements don't reliably print. Using the ISIN (always present, ISIN-checksum-validated by the regex, globally unique per scheme) means auto NAV refresh **will not** work out of the box for CAMS-imported holdings until the user manually corrects the symbol to the matching AMFI code — called out explicitly in Out of Scope, not silently masked.

### Preview: same rows, same review step, plus two extra signals

`ImportPreviewRow.payload_json` holds the dict above — identical shape to what `investing-orders` CSV rows already produce, so the existing preview UI (edit/delete a row before commit) needs **no changes**. Two additions surfaced in the preview *response* (not new DB columns):

1. **Skipped rows** (dividend/switch/stamp-duty lines) — returned as a `skipped: list[{folio, scheme_name, date, description, reason}]` alongside the normal preview rows, so the user knows what wasn't imported and why, rather than the row count silently not matching the PDF's transaction count.
2. **Price-discontinuity warnings** — for each scheme (grouped by ISIN), walk transactions in date order; if consecutive NAVs differ by more than 40% in either direction (`ratio < 0.6 or ratio > 1.67` — chosen wide enough that normal NAV volatility never trips it, but a 2:1/1:2-class split or bonus always does) with no `CorporateAction` already recorded for that symbol/account/date range (spec-051), add a `corporate_action_suspected: list[{symbol, scheme_name, from_date, from_nav, to_date, to_nav, ratio}]` warning list to the preview response. This does not block commit — it's advisory, telling the user "record a split via `/investing/corporate-actions` first, or your cost basis will be wrong across this boundary" (exactly the problem spec-051 solves).

### Commit: literally the same branch as `investing-orders`

In `ImportService.commit_batch`, `investing_cams_cas` is added to the existing `investing_orders` commit branch's condition (`app/imports/service.py:1516`, `1790`) — same `InvestingOrderCreate` construction, same `order_service.bulk_import_orders(...)` call, same rollback path (`_rollback_investing_orders`, keyed by `source_import_id`). No new commit code.

## Backend impact (`lifestack-api`)

- `app/imports/models.py`: add `ImportModule.investing_cams_cas`.
- `app/imports/cams_cas_parser.py` (new): `parse_cams_cas(file_path) -> CamsCasParseResult` (dataclass: `orders: list[dict]`, `skipped: list[dict]`, `corporate_action_suspected: list[dict]`).
- `app/imports/service.py`:
  - `create_batch`: module-gated file-extension check (`.pdf` for `investing_cams_cas`, `.csv`/`.xlsx` otherwise); new `target_account_id` parameter for this module.
  - `validate_batch_file`: new branch calling `parse_cams_cas`, building `ImportPreviewRow`s from `orders`, storing `skipped`/`corporate_action_suspected` on the batch (new `ImportBatch.extra_json: dict | None` column — generic enough to reuse for any future module's advisory-only preview metadata, not CAMS-specific).
  - `commit_batch`: add `investing_cams_cas` alongside `investing_orders` in the existing commit-dispatch condition.
- `app/imports/router.py`: `POST /imports/batches` gains an optional `target_account_id` form field (required when `module=investing-cams-cas`, rejected as extraneous otherwise); preview response schema gains optional `skipped`/`corporate_action_suspected` fields.
- `pyproject.toml`: add `pdfplumber` dependency.
- `alembic/versions/`: new migration for `ImportBatch.extra_json` (nullable JSON column, no backfill) and the inline `ImportModule` value is just a Python-side string enum member — no DB enum to migrate (module is stored as `sa.String(length=64)`, not a Postgres enum).

## API / schema impact

- `POST /v1/imports/batches` — `module=investing-cams-cas` accepts a `.pdf` file and requires `target_account_id` (new optional field, 422 if missing for this module or present for any other).
- `GET /v1/imports/batches/{id}/preview` — response gains optional `skipped: list[...]` and `corporate_action_suspected: list[...]` arrays, both empty for every existing module.
- No changes to `POST /v1/imports/batches/{id}/commit` or `.../rollback` request/response shapes.

## Golden test scenarios (required before merge)

New `app/tests/imports/test_cams_cas_import.py`, using a synthetic-but-structurally-accurate fixture PDF built with `pdfplumber`'s own PDF-writing test helpers (or a minimal `reportlab` snippet) reproducing the layout in the Problem section — not real user data, since none is available, but byte-accurate to the real CAMS layout (folio/scheme/ISIN headers, the exact column regex).

1. **Basic import** — 1 folio, 1 scheme, 3 transactions (2 purchases + 1 redemption) → preview shows 3 rows with correct `symbol=ISIN`, `quantity`, `price_per_unit`, `order_type`; commit creates a `Holding` whose FIFO-replayed `quantity`/`avg_cost` matches hand-computed values from the fixture's units/NAV.
2. **Skipped rows** — fixture includes a dividend-reinvestment line and a switch-out line → preview's `skipped` array has 2 entries with the right `reason`, and commit creates exactly the 3 orders from scenario 1 (the skipped lines are not silently included).
3. **Price-discontinuity flag** — fixture has a scheme with NAV 159.35 → (unrecorded) 2:1 split → NAV ~79 on the next transaction → preview's `corporate_action_suspected` has one entry with `ratio≈0.5`; recording the matching `CorporateAction` first (spec-051) and re-previewing makes the warning disappear.
4. **Multi-scheme, multi-folio** — 2 folios × 2 schemes each → 4 independent `Holding` rows, correctly attributed by ISIN, all bound to the single `target_account_id`.
5. **Rollback** — commit, then rollback the batch → `Holding`/`OrderLot`/`InvestingOrder` rows are gone (reuses the existing `investing_orders` rollback path — this scenario mainly proves `source_import_id` tagging survived the new parser unchanged).

## Out of scope

- **ISIN → AMFI scheme code resolution.** Imported holdings use ISIN as `symbol`; NAV auto-refresh (`investment_closing_prices_job`) silently no-ops for them until a user manually corrects the symbol (same behavior any manually-created mutual-fund holding with a non-numeric symbol already has today — not a new gap, just one CAMS import doesn't close). A future spec could add an ISIN→AMFI lookup (AMFI publishes this mapping) if it becomes a real pain point.
- **Dividend reinvestment, switches, and STT/stamp-duty-only lines.** These need different economic modeling (dividend reinvestment is a zero-net-cash buy; a switch is a simultaneous sell in one scheme + buy in another; stamp duty is a fee with no quantity change) — surfaced as `skipped` rows for manual entry, not auto-imported. A follow-up spec can add these once there's a concrete case.
- **Non-CAMS registrars (KFin/Karvy).** KFin CAS PDFs have a different layout; this spec's regex is CAMS-specific. Explicitly not attempting a unified parser across registrars.
- **lifestack-web UI changes.** The existing generic import upload/preview/commit UI already handles arbitrary modules and an optional preview-warnings display is a small, separate follow-up once the API ships; this spec is backend-only (per Task 6's stated scope — "then lifestack-web if UI tweaks needed").
