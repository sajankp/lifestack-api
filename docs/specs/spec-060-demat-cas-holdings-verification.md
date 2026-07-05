# Spec-060: Demat CAS PDF Import — Holdings Verification

**Created:** 2026-07-05
**Status:** Approved (owner confirmed 2026-07-05: NSDL is the target depository; `holding_verifications` table confirmed over preview-only, for auditability over time)
**Depends on:** spec-056 (CAMS CAS PDF import — parser/pipeline precedent), spec-051 (corporate actions — drift explanations), spec-057 (bhavcopy feed — referenced by the rejected alternative), spec-044 (FIFO lots — the numbers being verified)

---

## Problem

Lifestack's stock/ETF holdings replay from orders (spec-044); the direct holdings import was retired (`ImportModule.investing_holdings` is kept for backward-compat deserialization only, `app/imports/models.py:12-13`). There is currently **no way to check the replayed share counts against an authoritative external source**. The depositories publish exactly that source: the NSDL/CDSL Consolidated Account Statement, a monthly PDF listing, per demat account, every ISIN with the exact share balance the depository holds. When Lifestack's replayed quantity disagrees with the depository — a missed order, an unrecorded bonus/split (the spec-051 scenario: NVDA 10:1, GOOGL 20:1 sat unapplied in imported data), an off-market transfer, an IPO allotment — nothing in the product surfaces it today. The cash-correctness campaign's evidence bar ("portfolio numbers match the broker") has no product-level check for share counts.

### Why a Demat CAS is NOT an order source (design constraint, not a gap)

A CAMS CAS transaction row carries amount + units + NAV, which is why spec-056 could map it onto `InvestingOrderCreate` losslessly. A Demat CAS transaction section carries security **movements only** — date, ISIN, quantity credited/debited — with **no price**: settlement prices live at the broker, not the depository. Fabricating `price_per_unit` (e.g. from the spec-057 bhavcopy close of that date) would silently corrupt FIFO cost basis and realized gains with estimates dressed up as records — precisely what the campaign forbids. So this spec deliberately maps the Demat CAS to **verification**, not ingestion.

### Concrete shape of the source data (NSDL CAS, holdings section)

```
NSDL Demat Account          DP ID: IN300095   Client ID: 12345678
Equities (E)
ISIN          Security                            Current Bal.   Market Price   Value(Rs.)
INE002A01018  RELIANCE INDUSTRIES LTD                   50.000       2,970.50    148,525.00
INE467B01029  TATA CONSULTANCY SERVICES LTD             12.000       4,215.10     50,581.20
```

CDSL's layout differs (single "CDSL" section, other column order); NSDL is proposed first (single-layout scope, mirrors spec-056 shipping CAMS before other registrars). The PDF is **always password-protected** (PAN in a prescribed format).

## Solution

New import module `investing-demat-cas` reusing the spec-056 pipeline shape — upload → preview → commit on `ImportBatch` — where the *preview* is a per-ISIN comparison against the target account's current holdings, and *commit* persists the verification result as an auditable record. No holding, order, lot, or cash row is ever written.

### Upload

- `ImportModule.investing_demat_cas = "investing-demat-cas"`; `create_batch` accepts `.pdf` for this module (same gate pattern as spec-056).
- Reuses spec-056's `extra_json.target_account_id` mechanism (required, active brokerage account).
- New optional `file_password: str | None` on the upload request, passed to `pdfplumber.open(path, password=...)`, used in-memory only — **never persisted to `ImportBatch`, never logged** (PAN-derived secret). Because validation may run asynchronously (`RUN_BACKGROUND_TASKS_SYNCHRONOUSLY=False` in production) and the password is deliberately not persisted, `file_password` must be forwarded as an explicit argument through the background-validation entry point (`run_background_validate`) so the async parser can decrypt the PDF. Wrong/missing password → the clean `ValidationError` path spec-056 added for encrypted PDFs, with a message telling the user the expected password format.

### Parser: `app/imports/demat_cas_parser.py`

Text-line regex over `pdfplumber` text, mirroring `cams_cas_parser.py`'s structure: track the current demat-account context (`DP ID`/`Client ID` header), match holdings rows by a full-capture pattern anchored on the leading ISIN — e.g. `^(?P<isin>IN[A-Z0-9]{9}\d)\s+(?P<name>.+?)\s+(?P<quantity>[\d,]+\.\d{3})` against the whitespace-stripped line (exact quantity precision to be confirmed against real fixtures) — emitting `{isin, security_name, quantity}`. Only the **Equities (E)** section rows are parsed; mutual-fund folio sections inside an NSDL CAS are skipped with a per-row reason (already covered by the CAMS import, spec-056) and surfaced in the preview's `skipped` list, not silently dropped.

### Preview: the verification report

For the target account, compare each parsed ISIN against `Holding.quantity` (symbols are ISINs for Indian instruments per spec-056's symbol choice; a non-ISIN local symbol simply reports as unmatched):

| Status | Meaning |
|---|---|
| `match` | quantities equal (Decimal-exact) |
| `quantity_drift` | both sides have the ISIN, quantities differ — report both values and the delta |
| `missing_in_lifestack` | depository holds it, no Lifestack holding (unrecorded IPO/transfer/purchase) |
| `missing_at_depository` | Lifestack holds it, depository shows none (sold but sale unrecorded? wrong account?) |

Drift rows where the ratio matches a plausible split (integer or simple fraction) get an advisory `corporate_action_suspected` hint, filtered against already-recorded `CorporateAction` rows — same batched-query approach spec-056 used for price discontinuities.

### Commit: persist the verification snapshot

New table `holding_verifications` (one row per batch commit): workspace/account FKs (composite, matching `Holding`'s pattern), `source_import_id` FK → `import_batches.id` (the rollback key, indexed — same convention as `Holding.source_import_id`), `statement_date` (parsed from the CAS header), `source` (`"nsdl_cas"`), counts per status, and the full per-ISIN report as JSON. Migration with working `downgrade()`; enum-free (plain strings) to avoid the named-enum migration trap. Rollback (`_rollback` branch keyed by `source_import_id`) deletes the verification row — trivially safe since nothing else references it.

This gives the campaign an auditable trail: "as of the June statement, depository and Lifestack agreed on 14 of 15 ISINs; the drift on X was resolved by recording the missing bonus issue" — exactly the G4-style evidence the proof toolkit asks for.

### UI (lifestack-web, small)

The existing import flow already renders module-specific previews; add the report table with status badges and the target-account picker (both patterns exist from spec-054/056 work). A later iteration could surface the latest verification result on the Investing page; not in this spec.

## Golden tests

Synthetic-but-structurally-accurate fixture PDF via `reportlab` (spec-056 precedent), password-protected like the real thing:

1. All-match: seeded holdings equal the fixture balances → every row `match`; commit writes one `holding_verifications` row with the right counts.
2. Quantity drift + split hint: fixture shows 10× the seeded quantity → `quantity_drift` with `corporate_action_suspected`; hint disappears once the matching `CorporateAction` is recorded.
3. Both `missing_*` directions in one statement.
4. Wrong password → clean `ValidationError`, no batch row leaked; correct password parses.
5. Rollback deletes the verification row.

## Out of scope

- **Order inference / price backfill from demat movements** — rejected above; would need its own spec with an explicit "estimated cost basis" model if ever wanted.
- **CDSL layout** — follow-up spec once NSDL is proven (layout differs enough to need its own fixtures).
- **Mutual-fund sections of the NSDL CAS** — covered by the CAMS import (spec-056); rows are skipped with a reason.
- **Auto-creating `CorporateAction` or orders from drift** — the report explains, the owner acts; no automated writes to money-bearing tables.
- **Multi-account statements** — a CAS covering several demat accounts verifies only the chosen target account this pass; other sections are skipped with a reason.

## Resolved questions

1. **NSDL vs CDSL first** — NSDL confirmed (owner, 2026-07-05). CDSL stays out of scope as a follow-up spec.
2. **`holding_verifications` table vs preview-only** — table confirmed (owner, 2026-07-05): the audit trail across statements is the point of the feature.
