# Feature Spec 083: Security Identity Resolution & Reference Data

**Status:** Approved (implementation)
**Spec ID:** 083
**Depends on:** spec-012 (look-through analytics), spec-034 (constituent CSV import)
**Supersedes/closes:** spec-012 §8 "Identifier mapping drift"; spec-032 Open Question 4 (name-variant company duplication)

---

## 0. Roadmap Justification

Per the roadmap's 2026-07-08 revision, new finance specs require explicit justification
against briefing/health/capture priorities. **This is not net-new finance surface** — it is a
data-integrity correctness fix for an *existing* shipped feature (look-through exposure/overlap
analytics, spec-012). Today those analytics silently fragment a single real-world company into
multiple rows whenever its name is spelled differently across sources, producing wrong
concentration and overlap numbers. This spec makes the analytics trustworthy; it does not add a
new product surface.

---

## 1. Problem Statement

### 1.1 Company identity is resolved by name string
Look-through exposure/overlap already aggregate by `company_id` (int PK) at runtime
(`investing/service.py` `exposure()`/`overlap()`), which is correct. The defect is **upstream**:
`company_id` is assigned by **name string**. Both ingestion paths create a *new* `Company` row
whenever the name is unseen:
- CSV import: `company_cache` keyed on `name.strip().lower()`
  (`imports/investing_constituents_import.py`).
- API upsert: `ConstituentService.upsert_constituents` → `company_repo.get_by_names`
  (`investing/service.py`).

Consequently "Apple Inc" / "Apple Inc." / "AAPL" become **three** companies → three `company_id`
→ analytics treats one company as three. This is the root cause of overlap/concentration being
understated.

### 1.2 The manual-entry path stores the ticker *as the name*
`lifestack-web` `AnalyticsTab.tsx` manual constituent entry sets `company_name = ticker`
(single-column paste). So the field dedup runs on is sometimes a full name, sometimes a bare
ticker — guaranteeing fragmentation even within one workspace.

### 1.3 Stable identifiers exist as columns but are unreachable and unused
`Instrument.isin`, `Instrument.exchange`, `Company.isin` columns exist but are **never
populated** by any API path. `InstrumentUpdate` accepts only `name` + `instrument_type`, so an
existing instrument's identifiers **cannot be corrected from the UI or API** at all.

### 1.4 No existence/validity check and no reference data
Nothing validates that a symbol/ticker/ISIN corresponds to a real security. There is no
security-master concept in the codebase.

### 1.5 The canonical identifier differs by instrument type and market
There is no single universal key. Per owner direction:
- **US stocks** → **ticker** (e.g. `AAPL`).
- **Indian stocks** → **exchange symbol** (NSE/BSE, e.g. `RELIANCE`).
- **Indian mutual funds** → no ticker; identified by **ISIN / AMFI scheme code** (already
  available in `seed_data/mf_mapping.csv`).
- **ETFs** → market-dependent symbol, often **exchange-suffixed** (e.g. London `HIEU.L`).
- **ISIN** is the one cross-market stable key when present.

So identity must be resolved against a **(instrument_type, market/exchange)-aware** rule, not a
single global field.

---

## 2. Goals

1. Resolve `Company` (and `Instrument`) identity by a stable key — **ISIN → ticker(+exchange) →
   normalized name** — so `company_id` is stable across sources and the existing analytics
   aggregation becomes correct without touching the analytics math.
2. Make identifier fields (ISIN, ticker, exchange) **editable** for existing instruments via API
   and UI.
3. Enforce a **type-and-market-specific identifier mandate** on constituent CSV upload and manual
   entry, so name-only rows can no longer create fragmented companies.
4. Introduce a **reference-data layer** (bundled dataset + external-API fallback + DB cache) to
   validate/enrich identifiers without per-row remote calls at upload time.
5. Fix the `company_name = ticker` manual-entry quirk in the web UI.

## 3. Non-Goals

- Real-time / intraday security master sync.
- Full global equity universe coverage. Bundled data targets US (S&P constituents + major ETFs),
  India (AMFI MF list + NSE/BSE common equities), and a starter London-ETF set; anything else
  falls through to API fallback + cache.
- Corporate-identity history (mergers, ticker changes over time). ISIN is treated as current.
- Changing the exposure/overlap **math** — it already aggregates by `company_id` and stays as-is.
- Auth/security-gated surfaces (out of scope of this spec's module boundary).

---

## 4. Data Model Changes

### 4.1 New table: `reference_securities` (global market/reference data)

Globally-scoped reference data (`workspace_id` NULL), following the FX-rate precedent (FX rates
are global system reference data). Populated by a bundled dataset loader and enriched on-demand by
the API fallback.

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | internal |
| `public_id` | uuid | external |
| `isin` | str(20), nullable, indexed | primary cross-market key when present |
| `ticker` | str(20), nullable, indexed | e.g. `AAPL`, `HIEU.L` |
| `exchange` | str(50), nullable | MIC or common code (`XNAS`, `XNSE`, `XLON`); disambiguates ticker |
| `amfi_code` | str(20), nullable, indexed | Indian MF scheme code |
| `security_type` | enum(`stock`,`etf`,`mutual_fund`) | |
| `name` | str(255) | canonical display name |
| `country_code` | str(10), nullable | |
| `source` | str(64) | `bundled:<origin>` (from `securities.json`) or `api:<provider>` (cached) |
| `fetched_at` | datetime(tz) | for API-sourced/cache-staleness |

Constraints:
- Partial unique on `isin` where `isin IS NOT NULL`.
- Partial unique on `(ticker, exchange)` where `ticker IS NOT NULL`.
- Partial unique on `amfi_code` where `amfi_code IS NOT NULL`.

Migration must downgrade cleanly (owner policy).

### 4.2 `Company` — add and use identity keys
`Company` already has `ticker`, `isin`. Add nothing structurally beyond ensuring `isin` is
populated. **Resolution order** for company identity becomes: `isin` → `(ticker, country/exchange)`
→ `normalized(name)`. `normalized(name)` = lowercased, trimmed, punctuation-stripped (drops the
"Apple Inc" vs "Apple Inc." split). Add a repository method `resolve_or_create_company(...)`
implementing this precedence; both ingestion paths call it (replacing `get_by_names`).

### 4.3 `Instrument` — make identifiers editable
Populate and expose `isin`, `exchange`, and (via linked `Company`) `ticker`. No new columns.

---

## 5. API Changes

### 5.1 `InstrumentCreate` — extend
Add optional `isin: str|20`, `exchange: str|50` (ticker already present). Validators normalize
(upper, trim).

### 5.2 `InstrumentUpdate` — extend (the key correction gap)
Currently only `name` + `instrument_type`. Add optional `ticker`, `isin`, `exchange`. This is what
makes existing instruments correctable from the UI.

### 5.3 `InstrumentConstituentCreate` / CSV schema — mandate + identifiers
- CSV template headers become:
  `instrument_symbol,company_name,company_isin,company_ticker,company_exchange,weight,as_of_date`
  (`company_isin`/`company_exchange` new; `company_ticker` already present).
- **Mandate (per §6):** at least one resolvable identifier appropriate to the constituent's market
  is required (ISIN, or ticker(+exchange)). Backward compatibility: legacy 5-column files without
  the new headers still parse, but rows lacking any identifier are handled per the mandate policy.

### 5.4 Reference-data validation endpoint (internal/enrichment)
- `GET /v1/investing/reference/resolve?isin=&ticker=&exchange=&type=` → returns a match from
  `reference_securities` (bundled first, API fallback + cache on miss). Used by import validation
  and (optionally) by the UI to autocomplete/confirm an identifier.

### 5.5 Response semantics
- Import validation surfaces per-row `identifier_status`: `resolved | unresolved | ambiguous`.
- Numeric values remain string-serialized; Zod schemas on web updated accordingly.

---

## 6. Type-and-Market-Specific Mandate

Validation selects the **required identifier field by (instrument_type, inferred market)**:

| Instrument type | Market | Required identifier | Notes |
|---|---|---|---|
| stock | US | `ticker` | exchange optional (`XNYS`/`XNAS`) |
| stock | India | `symbol`+`exchange` (NSE/BSE) | ticker == exchange symbol |
| mutual_fund | India | `isin` **or** `amfi_code` | no ticker exists |
| etf | US | `ticker` | |
| etf | UK/other | exchange-suffixed `ticker` (e.g. `HIEU.L`) or `isin` | suffix encodes exchange |
| any | any | `isin` always accepted as the universal key | preferred when present |

Market is inferred from explicit `exchange`, ticker suffix (`.L`, `.NS`, `.BO`), or currency
context; when ambiguous, `isin` is required. Owner-approved strictness: **require a resolvable
identifier; unresolved-but-well-formed rows are accepted with a warning flag** (`identifier_status
= unresolved`) so analytics can mark them partial rather than hard-blocking legitimate
custom/private funds (preserves spec-034's reason for existing).

### 6.1 Exchange / suffix inference rules (owner-clarified)

The symbol suffix is the primary exchange signal, following **Yahoo Finance convention** (the
provider already in use). Rules and their explicitly-accepted holes:

1. **Present suffix → infer exchange** (one-directional): `.NS`→NSE, `.BO`→BSE, `.L`→LSE, etc.,
   mapped to a chosen standard (MIC recommended, e.g. `XNSE`/`XBOM`/`XLON`) via a
   suffix→exchange table. `exchange` is **not mandatory input** — it is inferred when a suffix is
   present and left null otherwise.
2. **Absent suffix → default market, not an error.** Yahoo convention gives US/primary listings
   **no** suffix (`AAPL`). A bare symbol is therefore treated as the default market (US unless
   overridden), **not** rejected. Accepted hole: a bare `RELIANCE` where the user forgot `.NS` is
   indistinguishable from a US symbol — mitigated by the reference-data resolve step (a bare symbol
   that only resolves on an Indian exchange is flagged for confirmation rather than silently
   mismarked).
3. **Suffix distinguishes the *listing*, never fragments the *company*.** `INFY.NS` and `INFY.BO`
   are the same company (same ISIN). **ISIN stays top of the company-identity resolution order** so
   dual-listed securities collapse to one `Company`; ticker+exchange only distinguishes them at the
   `Instrument`/listing level, not for constituent aggregation.
4. **Suffix→exchange map is Yahoo-convention-based.** Documented as such; if a future provider uses
   different suffixes, the mapping table is the single point of change.

---

## 7. Reference Data: Bundled + API Fallback + Cache (owner-selected)

### 7.1 Master reference data — canonical JSON checked into the API repo (owner-selected)

The bundled master data is a **single, properly-formatted, hand-editable JSON file version-controlled
inside the API repo** — the source of truth an owner/agent updates per instrument over time. It is
**not** a scattered set of CSVs; the loader reads this one file into `reference_securities`.

**Location:** `lifestack-api/app/investing/reference_data/securities.json`
(a package-data file adjacent to the investing module, so it ships with the app and is importable via
`importlib.resources`; no dependency on the separate `seed_data/` dev workspace at runtime).

**Schema** — a top-level object with a `version` and a `securities` array; every entry carries the
full identity record for one security:

```jsonc
{
  "version": "2026-07-15",
  "securities": [
    {
      "isin": "US0378331005",          // nullable; universal key, preferred when known
      "ticker": "AAPL",                 // nullable
      "exchange": "XNAS",               // MIC standard; nullable
      "amfi_code": null,                // India MF scheme code; nullable
      "security_type": "stock",         // stock | etf | mutual_fund
      "name": "Apple Inc",              // canonical display name
      "aliases": ["Apple Inc.", "Apple"], // name variants that must resolve to this entry
      "country_code": "US",             // nullable
      "source": "bundled:manual"        // provenance
    },
    {
      "isin": "IE00B5BD5K76", "ticker": "HIEU.L", "exchange": "XLON",
      "amfi_code": null, "security_type": "etf",
      "name": "HSBC MSCI Europe UCITS ETF", "aliases": [],
      "country_code": "GB", "source": "bundled:manual"
    },
    {
      "isin": "INF209K01165", "ticker": null, "exchange": null,
      "amfi_code": "100033", "security_type": "mutual_fund",
      "name": "Aditya Birla Sun Life Large & Mid Cap Fund - Regular Growth",
      "aliases": [], "country_code": "IN", "source": "bundled:amfi"
    }
  ]
}
```

- `aliases` is the mechanism that kills name-variant fragmentation: the resolver matches an incoming
  `normalized(name)` against both `name` and every `aliases` entry, mapping it to the one canonical
  record (and its ISIN/ticker).
- Starter coverage to seed the file: US equities/ETFs (S&P + major ETFs), India MF (derived from the
  existing `seed_data/mf_mapping.csv` — AMFI code + ISIN), India equities (NSE/BSE common symbols),
  and a starter exchange-suffixed ETF set (incl. London examples).

**Loader:** `python -m app.cli.run load_reference_securities` — validates the JSON against the schema
(fail loudly on malformed entries), then idempotent-upserts into `reference_securities` keyed on the
partial uniques (advisory lock, consistent with other jobs). Re-runnable any time the JSON is edited.
The API-fallback path (§7.2) writes only into the DB table, never back into this file — the JSON stays
the curated, human-owned source; the table is JSON ∪ cached-API rows.

### 7.2 External API fallback (only on a miss, then cached)
On an unresolved identifier during resolve/validation, optionally query an external securities
lookup, then **write the result into `reference_securities`** so it is never re-fetched. Bounded
volume → no rate-limit exposure. **Provider: reuse the existing Yahoo Finance fetch path already
used for stock prices (spec-031, `_fetch_stock_price` in `investing/service.py`)** behind a thin
adapter. This is the Yahoo *quote/identity* lookup (symbol → name/exchange/currency), **not** the
retired `topHoldings` constituent provider (spec-032) — that path stays retired; only quote-level
identity resolution is reused here. Config knobs (per `lifestack-config-and-flags` conventions):
- `REFERENCE_DATA_API_ENABLED: bool = False` (opt-in; bundled works offline by default).
- `REFERENCE_DATA_CACHE_STALENESS_DAYS: int = 30`.

### 7.3 Building the bundled dataset (offline test/build script)
The `securities.json` in §7.1 is generated/augmented by an in-repo **build script** (under
`seed_data/scripts/`, consistent with the existing `convert_*` scripts) that fetches from Yahoo and
known public lists (AMFI `mf_mapping.csv` for India MF) and emits/updates the checked-in JSON. This
is a developer/build-time step, run manually to refresh the master data — it is **not** a runtime job
and never runs in the user request path. Hand-edits to `securities.json` remain first-class; the
script merges rather than clobbers curated entries where feasible.

### 7.4 Why not per-row live validation
Rejected for the same reasons spec-012 §7 rejected live constituent fetch: a 200-row ETF CSV = 200
lookups, external dependency in the user upload path, throttling risk. Bundled-first + cache keeps
uploads deterministic and offline-capable.

---

## 8. Frontend Changes (lifestack-web)

**Primary identity surface = the Holdings tab, NOT Analytics.** Rationale (fuller UX write-up may
live in a paired web-repo UI spec — see §8b): the Holdings tab is the daily-use surface that lists
*every* security the user owns (stocks, ETFs, MFs), and its **Edit Holding modal already edits
`symbol` + `instrument_type`** (`HoldingsTab.tsx`). Instrument identity lives on the `Instrument`
row, and a holding's `symbol` already resolves to its `Instrument`, so the modal edits the holding
*and* patches the linked instrument's identity in one save. The Analytics "Advanced" panel is an
`Advanced` disclosure on a read-heavy tab and is the wrong place for the main correction workflow.

1. **Holdings — Edit Holding modal (primary):** add editable `ticker`, `isin`, `exchange` fields to
   the existing modal (`HoldingsTab.tsx`). The required-field hint changes with the selected
   `instrument_type` (+ inferred market), per §6. On save, patch the linked instrument's identity.
2. **Analytics — Advanced panel (demoted to the exception path):** keep identity editing here **only**
   for pooled instruments that have *no* Holding row (analysis-only ETFs/MFs the user analyzes but
   doesn't hold). The instrument list edit graduates from cramped inline cells to a small
   **Edit Instrument modal** carrying the same identifier fields + per-type hint. Owned securities are
   corrected from Holdings (item 1); this panel no longer needs to be the primary path.
3. **Fix `company_name = ticker` quirk:** the Seed Constituents manual entry (`AnalyticsTab.tsx`) must
   capture company name *and* identifier (ticker/ISIN) separately, not overload name with the ticker.
   Constituent companies are the one entity that exists nowhere else, so this modal + the CSV import
   preview remain their identity-entry home.
4. **Import UI (`ImportsPage`):** new template columns; surface per-row `identifier_status`
   (`resolved` / `unresolved` warning / `ambiguous`) in the existing preview table.
5. **Zod schemas** (`src/types/investing.ts`) updated for the new response fields; response types
   stay `z.infer`-derived (architecture contract).

## 8b. Validation Parity — API and UI both enforce the mandate

The identifier mandate (§6) and the "data must be present" guarantee are enforced on **both** sides,
so neither a direct API caller nor the UI can create identifier-less securities:

| Layer | Enforcement | Failure mode |
|---|---|---|
| **API — request schemas** | `InstrumentCreate`/`InstrumentUpdate`/`InstrumentConstituentCreate` + CSV row validators apply the per-(type, market) required-identifier rule (§6) via Pydantic `model_validator`. | 422 with field-level error identifying which identifier is required for the given type. |
| **API — resolver** | `resolve_or_create_company` / instrument resolve stamps `identifier_status`; `unresolved` well-formed rows pass with a warning, name-only/blank-identifier rows for types that mandate one are rejected. | 422 (blank/invalid) or 200-with-warning (`unresolved`). |
| **UI — forms** | Holdings Edit modal, Analytics Edit Instrument modal, Seed Constituents, and Create Instrument disable/flag submit until the type-appropriate identifier is filled; inline resolve call shows `resolved/unresolved/ambiguous` before submit. | Submit blocked or warning shown pre-request. |
| **UI — import preview** | Per-row `identifier_status` column; rows failing the mandate are visibly flagged before commit. | Commit-blocked / row-flagged. |

The UI checks are **UX affordances only**; the API schema + resolver are the **authoritative** gate
(never trust the client). Test coverage must assert both independently (§11).

---

## 8a. Template & Form Discoverability (self-documenting inputs)

The mandate in §6 is only usable if the person filling in a CSV or a form can tell **which
identifier their row/instrument needs** without reading this spec. Both input surfaces must be
self-documenting.

### 8a.1 Downloaded CSV template
- The generated template file includes a **header comment block** (leading `#` lines the parser
  ignores) stating the per-type rule in plain terms, e.g.:
  ```
  # Required identifier by security type/market:
  #   US stock/ETF        -> company_ticker (e.g. AAPL)
  #   India stock         -> company_ticker + company_exchange (NSE/BSE, e.g. RELIANCE, XNSE)
  #   India mutual fund   -> company_isin (or AMFI code); no ticker exists
  #   UK/other ETF        -> exchange-suffixed ticker (e.g. HIEU.L) OR company_isin
  #   Any                 -> company_isin is always accepted and preferred when known
  ```
- Ships with **2–3 filled example rows** spanning a US ticker, an India-MF ISIN, and a
  London-suffixed ETF, so the expected shape of each identifier is visible.
- Column headers use explicit names (`company_isin`, `company_ticker`, `company_exchange`) rather
  than a single ambiguous "symbol" column.

### 8a.2 Manual add/correction forms (web)
- The **Holdings Edit modal (primary, §8.1)**, the Analytics Edit Instrument modal, the Create
  Instrument form, and the constituent manual-entry form all show the **required-identifier hint
  dynamically based on the selected `instrument_type` (and market/exchange when chosen)** — e.g.
  selecting "Mutual fund" surfaces an ISIN field labeled required and hides/deprioritizes ticker;
  selecting "US stock" makes ticker required.
- Inline helper text + placeholder examples per field (`AAPL`, `INF209K01165`, `HIEU.L`).
- On blur / submit, the form calls the resolve endpoint (§5.4) and shows the
  `resolved | unresolved | ambiguous` status inline, so the user gets immediate feedback rather
  than a post-upload error.
- The import preview screen surfaces the same per-row `identifier_status` with the reason, so an
  unresolved row is explainable in-context.

This section MAY be split into a paired **web-repo spec** (as done for corporate-actions UI) if the
API-side identity/reference work lands first; the backend contract in §5–§7 is the dependency, and
the template-generation copy lives with whichever repo owns template generation.

## 9. Backfill / Migration of Existing Data

**Owner decision:** the backfill/merge is a **dedicated CLI script that takes an explicit
`--workspace-id`** and operates on that workspace only — never an automatic deploy-time migration —
to avoid cross-workspace contamination while it deletes/repoints company rows. Invocation:
`python -m app.cli.run merge_company_identities --workspace-id <id>`.

Steps (idempotent, re-runnable, transactional per workspace):
1. Load `reference_securities` from the master `securities.json` (separate loader, §7.1).
2. For each `Company` **in the target workspace**, enrich `isin`/`ticker` by matching normalized
   name (and existing ticker) against `reference_securities`.
3. **Merge duplicate companies** that resolve to the same `isin`/`(ticker,exchange)`: pick a
   survivor, repoint `InstrumentConstituent.constituent_company_id` and `Instrument.company_id`,
   delete the losers. Scoped strictly to the target workspace.
4. Because holdings/exposure are derived (analytics computed on read), no snapshot rewrite is
   needed — the next exposure/overlap read reflects merged identity automatically.

Backfill is reversible in the sense that it only consolidates identity; original snapshots'
weights/dates are untouched.

---

## 10. Module Boundary

- `reference_securities` model + repository live under **`app/investing/`** (owner decision:
  security reference data is investing-domain; FX/dollar-to-INR is finance but only surfaces
  *through* investing valuation, so it does not pull this into `app/finance/`). Loader/enrichment
  orchestration and the cross-step backfill live in `app/application/` (workflows/jobs), consistent
  with the cross-module seam rule (modules never import each other).
- Company identity resolution (`resolve_or_create_company`) lives in the investing repository and
  is called by both the CSV importer and `ConstituentService`.

---

## 11. Test Strategy

- **Identity resolution unit tests:** ISIN match wins over ticker; ticker+exchange match;
  name-normalization collapses "Apple Inc"/"Apple Inc."; no false-merge across different ISINs
  sharing a ticker on different exchanges.
- **Backfill/merge integration test:** seed duplicate companies, run backfill, assert single
  survivor + repointed constituents + corrected overlap numbers (golden-style: overlap of a
  known-duplicated company goes from split to combined).
- **Mandate tests:** US stock without ticker rejected/flagged; India MF with ISIN accepted without
  ticker; London ETF `HIEU.L` accepted; ISIN accepted universally; unresolved-but-well-formed row
  flagged `unresolved` not blocked.
- **Reference-data tests:** `securities.json` schema-validates (malformed entry fails loudly);
  loader idempotent upsert from the JSON; `aliases` resolve to the canonical record; API fallback
  caches into the DB (never into the JSON) and is not re-fetched; `REFERENCE_DATA_API_ENABLED=False`
  works fully offline.
- **Validation-parity tests (both sides independently, §8b):**
  - *API:* `InstrumentCreate`/`InstrumentUpdate`/constituent schema + CSV validators return 422 with
    the correct field error when the type-mandated identifier is missing; `unresolved` well-formed
    row passes with warning; blank/name-only rejected for mandating types.
  - *Web:* Holdings Edit modal, Analytics Edit Instrument modal, Create Instrument, and Seed
    Constituents block/flag submit until the type-appropriate identifier is present; inline resolve
    shows `resolved/unresolved/ambiguous`.
- **CSV import tests:** new columns parse; legacy 5-column files still parse.
- **Web:** Zod schema validation for new fields; **Holdings Edit modal PATCHes instrument identity
  (ticker/isin/exchange)** as the primary path; manual constituent entry captures name+identifier
  separately; Playwright E2E for the Holdings correction flow + import flow.
- Coverage gates unchanged (api 80% / web 70%).

## 12. Acceptance Criteria

- [ ] `reference_securities` table exists (global-scoped) with loader CLI; downgrade clean.
- [ ] Master data is a single checked-in JSON at
      `app/investing/reference_data/securities.json` (schema-validated, hand-editable, with
      `aliases`), covering starter US equity/ETF, India MF (from `mf_mapping.csv`), India equity, and
      exchange-suffixed ETF (incl. London) entries; loader upserts it and is re-runnable.
- [ ] `InstrumentUpdate` accepts `ticker`/`isin`/`exchange`; the **Holdings Edit modal is the primary
      surface** that corrects an existing instrument's identity; Analytics Advanced handles only
      pooled instruments without a holding.
- [ ] Mandate is enforced on **both** API (schema + resolver, authoritative) and UI (form
      affordances) per §8b, with independent test coverage on each side.
- [ ] Company identity resolves by ISIN → ticker(+exchange) → normalized name; both ingestion paths
      use it; no new duplicate companies created for name variants.
- [ ] Backfill merges existing duplicate companies and repoints constituents transactionally;
      exposure/overlap reflect combined identity on next read.
- [ ] Constituent CSV + manual entry enforce the type/market-specific identifier mandate; unresolved
      well-formed rows are flagged, not hard-blocked.
- [ ] Optional external-API fallback resolves-then-caches into `reference_securities`; disabled by
      default; fully offline with bundled data only.
- [ ] Web `company_name = ticker` quirk removed; manual entry captures name + identifier separately.
- [ ] Analytics **math unchanged**; correctness improvement comes purely from stable `company_id`.
- [ ] Downloaded CSV template is self-documenting (per-type identifier rule in header + example
      rows spanning US ticker / India-MF ISIN / London-suffixed ETF).
- [ ] Manual add/correction forms show the required-identifier hint dynamically by instrument type
      and give inline `resolved/unresolved/ambiguous` feedback before upload/submit.

## 13. Resolved Decisions (were open questions)

1. **External provider:** reuse the existing **Yahoo Finance** quote/identity fetch (already in use
   for stock prices, spec-031); the bundled dataset is built by an offline test/build script under
   `seed_data/scripts/`. Not the retired Yahoo constituent (`topHoldings`) path.
2. **Module home:** `app/investing/` — security reference data is investing-domain (§10). Master
   data is a single hand-editable JSON checked into the API repo at
   `app/investing/reference_data/securities.json`, loaded into the `reference_securities` table
   (§7.1).
2a. **UI home:** the **Holdings tab Edit modal is the primary identity-correction surface** (most
   discoverable, lists every owned security); the Analytics Advanced panel is demoted to
   analysis-only pooled instruments; constituent identity stays in Seed Constituents + import
   preview (§8). Mandate enforced on both API and UI (§8b).
3. **Exchange code standard:** any consistent standard, **MIC recommended** (`XNAS`/`XNSE`/`XLON`).
   Exchange is **inferred from the symbol suffix (Yahoo convention) and is not mandatory input**;
   absent suffix = default market, not an error; ISIN outranks ticker+exchange for company identity
   so dual-listings collapse to one company (full rules + accepted holes in §6.1).
4. **Backfill:** a **dedicated CLI script taking `--workspace-id`**, run per-workspace, never an
   automatic deploy-time migration — to avoid cross-workspace contamination (§9).
