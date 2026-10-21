# Spec-099: Capital Gains Tax Estimate for Investments

**Created:** 2026-10-21
**Status:** Under Review (pending approval)
**Scope:** API (`app/investing/tax`, `app/finance`), Web (`lifestack-web/src/pages/investing/TaxTab.tsx`, `src/services/investing.ts`, `src/services/finance.ts`), MCP (`app/mcp/server.py`)
**Depends on:** spec-044 FIFO cost basis / order lots, spec-051 corporate actions, spec-075 display settings, spec-083 security identity

---

## 1. Problem Statement

Lifestack records investment orders with full FIFO lot history (`investing_orders`, `investing_order_lots`, `investing_corporate_actions`) and computes FIFO cost basis on every holding. It has no view of **what the user actually owes in tax** after selling.

The user reconciles against a broker (Ind Money → Drivewealth for US shares, Groww for Indian holdings) and finds the reported P&L % differs. Investigation showed:

- **Quantities match exactly** on all 20 US positions.
- **Cost basis differs** on multi-lot positions (NVDA, AMD, MU).
- The broker's *open-position* `invested_amount` is a **plain average over all buys ignoring sells**, not a FIFO tax basis. Lifestack's FIFO cost basis **matches the broker's own per-trade `avg_cost_at_sale` to sub-cent precision** on all 10 recorded sells.

Conclusion: **Lifestack's cost basis is already the tax-correct one.** The user nonetheless needs a statutory tax estimate, because:

1. A FIFO cost basis is not a tax liability. The regime, holding-period threshold, exemption and rate all differ by asset class and residency.
2. Foreign (US-listed) shares held via an Indian broker are taxed **materially differently** from Indian listed equity, and the difference is not intuitive.
3. There is no way in the product to answer "how much tax will I owe this year?"

### 1.1 The regime split (the core domain fact)

US-listed shares are **not on an Indian recognised stock exchange** and pay **no STT**. The concessional equity regime (Sec 111A STCG / Sec 112A LTCG) is reserved for STT-paid Indian listed securities. Foreign shares fall under the general **Section 112** regime.

| Aspect | Indian listed equity (STT paid) | US / foreign shares (no STT) |
|---|---|---|
| Holding period → LTCG | > 12 months | **> 24 months** (Sec 2(42A)) |
| STCG section / rate | 111A @ 20% flat | **112(1)(a)(ii) @ slab rate** |
| LTCG section / rate | 112A @ 12.5% | **112(1)(c) @ 12.5%**, no indexation |
| LTCG exemption | ₹1.25 lakh / year | **None** — taxable from the first rupee |
| Indexation on LTCG | Not available | Not available (transfers on/after 23-Jul-2024) |
| Loss set-off | STCL against any gain; LTCL against LTCG only | same (Sec 24(6)) |
| Return form | ITR-2 | ITR-2 **+ Schedule FA (Table A3)** |

Applying 111A/112A to foreign shares is the single most common error. It produces a plausible-looking number that is wrong in three ways at once: a 12-month instead of 24-month cutoff, a flat 20% STCG rate instead of the user's slab, and a spurious ₹1.25 lakh exemption that can zero out a real liability.

**24-month cutoff, precisely:** "more than 24 months" means the holding must exceed 24 **complete calendar months** — compare the sale date against the acquisition date plus 24 calendar months, strictly greater. Do not approximate with days ÷ 30.4375; that drifts by days and can misclassify a lot near the boundary.

### 1.2 Rates as currently in force

- LTCG on foreign shares: **12.5%** without indexation for transfers on/after 23-Jul-2024 (transfers before that date: 20% with indexation, computed on an index-adjusted cost).
- STCG on foreign shares: **slab rate** of the individual/HUF. Sec 111A's 20% does **not** apply.
- Health & education cess: **4%** on both buckets.
- Surcharge: 15% cap for Sec 112 LTCG; applies on total income if high enough.
- Rebate u/s 87A: up to ₹12,000 for total income up to ₹12,00,000 (resident individual).

### 1.3 The unresolved edge: IFSC / GIFT City

Sec 111A(1) carries a proviso that the STT requirement does not apply to a transaction on a recognised stock exchange located in an **International Financial Services Centre** where consideration is paid or payable in foreign currency. Whether an overseas-custodian account reached through GIFT City pulls foreign shares into 111A at 20% instead of slab rate is **not settled and depends on the account's structure**.

The product must therefore offer an explicit **explicit regime override** rather than silently picking a treatment. See §4.3.

### 1.4 What the codebase already provides (verified, not assumed)

Before designing the regime switch, the existing reference data was checked against the workspace's actual holdings:

| Asset | Status | Detail |
|---|---|---|
| `reference_securities` table | **Exists** | spec-083, global (workspace-less) security master |
| Bundled dataset | **Exists** | `app/investing/reference_data/securities.json.gz`, 31,659 entries, version 2026-07-15 |
| `country_code` per security | **Exists** | `IN` 18,637 / `US` 13,019 / `GB` 3 |
| US ticker coverage | **22/22** | every US holding resolves with the right country and MIC exchange |
| Indian ticker coverage | **30/30** | every Groww holding resolves (`XNSE`, `IN`) |
| Mutual fund coverage | 16,243 `amfi_code` entries | MF NAV lookup works for the Indian funds |
| `list_by_ticker` repo method | **Exists** | `app/investing/repository.py:659` |
| Ambiguity handling | **Exists** | the resolve service returns `"ambiguous"` rather than guessing (spec-083 §6.1) |
| FIFO lot replay | **Exists** | `app/investing/order_service.py` `_replay_orders` — already implements Sec 45(2A) FIFO |
| `OrderLot` model | **Exists** | per-lot `cost_per_unit`, `remaining_quantity`, `acquired_at` |
| Corporate actions | **Exists** | spec-051, replayed in the same chronological stream |

**The feature needs no new cost-basis machinery.** The two things it must add are (a) a *statutory* classification and rate layer on top of the existing FIFO basis, and (b) the FX rate, which is the one input the app cannot derive.

---

## 2. Design Principles

1. **The engine is pure and stateless.** `app/investing/tax/` contains no DB access and no I/O. It is a set of pure functions over orders, instruments, accounts and a profile. This is what makes it testable and what lets the same engine serve the API, the MCP tool and the CLI.
2. **Compute from orders, never from holding aggregates.** A holding's `avg_cost` is a weighted average of remaining open lots and carries no per-lot acquisition dates, so it cannot answer a holding-period question. Always replay.
3. **Reconcile every sell before reporting.** Each sell stores `realized_gain_loss` and `avg_cost_at_sale`. The engine asserts the FIFO replay reproduces both to sub-cent precision. A mismatch means the replay, fee treatment or lot ordering is wrong — the engine returns a `reconciliation_failed` status rather than a number.
4. **Show the assumptions, always.** The estimate is only as good as the FX rate, the slab derivation and the regime choice. Every response carries the assumptions used, and the UI renders them.
5. **This is an estimate, not a filing.** The UI must not say "tax payable". It says "tax estimate" and links the assumptions that drive it.
6. **The user owns the regime flag.** A capital-market foreign account's tax treatment is not always derivable from data the app holds. Explicit flags beat inference.

---

## 3. Data Model

### 3.1 `accounts.market_scope` (new column)

```python
class MarketScope(StrEnum):
    india = "india"      # Indian-listed securities (STT paid) -> 111A/112A
    foreign = "foreign"  # US/foreign listed, no STT -> Sec 112
    mixed = "mixed"      # account holds both
```

- New nullable column on `accounts`, default `"india"` for existing rows (backfill), then user-editable.
- **Semantics:** a *default regime hint*, consulted **only** when the security master has no entry for the ticker and no holding-level override is set. **Not** the statutory test — see §4.1.
- Distinct from the existing `AccountType` enum (`bank | brokerage | wallet | card | gift_card`), which describes the account's nature, not its market. Mixing them would be a category error.
- Only meaningful for `account_type == "brokerage"`.

### 3.2 Regime classification source — the bundled security master (no new column)

**The `reference_securities` table (spec-083) already carries exactly the data this feature needs.** It is a *global, workspace-less* security master, populated from the bundled `app/investing/reference_data/securities.json.gz` (31,659 entries, version 2026-07-15) and enriched on demand by the Yahoo identity fallback. Each row has `ticker`, `exchange`, `country_code`, `security_type`, `isin` and `amfi_code`.

Verified coverage against the workspace's actual holdings:

- **22 of 22** US tickers resolve, with correct `country_code="US"` and exchange (`XNAS` / `XNYS` / `ARCX`).
- **30 of 30** Groww Indian tickers resolve, with `country_code="IN"` and exchange `XNSE`.
- 16,243 entries carry an `amfi_code`, so mutual funds resolve too.
- Country distribution: `IN` 18,637 / `US` 13,019 / `GB` 3.

So the regime test is simply:

```
reference_security.country_code == "IN"  ->  Indian regime (Sec 111A/112A)
reference_security.country_code != "IN"  ->  foreign regime (Sec 112)
```

**No `exchange_country` column is needed on `instruments`.** The `instruments.exchange` column already exists and is `null` for every row in this workspace — but it is a *denormalised copy* of what the security master already knows. The engine reads the master; it does not need the instrument to carry the country.

`ReferenceSecurityRepository.list_by_ticker(ticker)` already exists (`app/investing/repository.py:659`) and returns every row for a ticker. When a ticker resolves to multiple exchanges, the resolve service returns `"ambiguous"` rather than guessing (spec-083 §6.1) — the tax engine must follow the same rule and classify such a holding via the account default, not by picking one.

**What the instrument still needs:** `instruments.exchange` stays as the *user-visible* label, populated opportunistically from the master when the instrument is created or when the user sets it. It is descriptive, not authoritative for the regime.

### 3.2b `investing_instruments.exchange_country_override` (new column)

| Column | Type | Default | Notes |
|---|---|---|---|
| `exchange_country_override` | str(2) nullable | NULL | `"IN"` / `"US"` / NULL (inherit). Beats the account default and the security master. |

The security master is global and must not be workspace-editable; the instrument row is workspace-scoped, so the per-holding override lives there.

### 3.3 `workspace_tax_profiles` (new table, 1 row per workspace)

| Column | Type | Default | Notes |
|---|---|---|---|
| `workspace_id` | FK, unique | — | |
| `resident_status` | enum | `resident` | `resident` \| `resident_not_ordinarily_resident` \| `non_resident` |
| `fy_label` | str | current FY | `"2026-27"` — the FY the profile describes |
| `tax_regime` | enum | `old` | `old` \| `new` — which slab table applies (see §4.5) |
| `other_income` | Numeric(18,2) | 0 | Salary + business + other heads, for slab derivation |
| `slab_override_pct` | Numeric(5,2) nullable | NULL | Explicit slab; NULL = derive from `other_income` + gains |
| `surcharge_override_pct` | Numeric(5,2) nullable | NULL | NULL = derive |
| `cess_pct` | Numeric(5,2) | 4.00 | Health & education cess |
| `explicit_regime` | enum nullable | NULL | `indian` \| `foreign` — overrides instrument-derived classification |
| `apply_carry_forward` | bool | True | Whether to net prior-year losses |
| `created_at` / `updated_at` | timestamptz | now() | |

**Why `other_income` rather than a slab rate:** a slab is a *marginal* rate. STCG is added to the user's other income and the *combined* total determines the marginal rate. Storing only a slab makes the estimate wrong whenever other income is material. Storing other income lets the engine derive slab, surcharge, cess and the 87A rebate correctly. `slab_override_pct` remains as an escape hatch for users who know their marginal rate.

### 3.4 `tax_loss_carry_forward` (new table, Phase 4)

| Column | Type | Notes |
|---|---|---|
| `workspace_id` | FK | |
| `fy_label` | str | FY the loss arose in |
| `bucket` | enum | `short_term` \| `long_term` |
| `amount` | Numeric(18,2) | Absolute value of the unused loss |
| `expires_fy_label` | str | Arising FY + 8 years |

Sec 24(6) ordering: short-term loss against long-term gain first, then against short-term gain; long-term loss against long-term gain only; unused loss carries forward 8 years. A prior-year *gain* never shelters this year's gains.

### 3.5 `tax_fx_rates` (new table, Phase 5 — not in this spec's scope)

Manual FX overrides are stored in the tax profile for now (§4.2). A dedicated per-month TTBR table is deferred.

---

## 4. The Engine

Location: `app/investing/tax/` — a package, not a module, because the regime table alone is worth its own file.

```
app/investing/tax/
  __init__.py
  regime.py      # RegimeType, RegimeSpec, REGIMES, classify_regime()
  exchanges.py   # exchange-name -> MIC + country map (§4.1c)
  fy.py          # Indian FY bounds, add_months()
  lots.py        # FIFO replay -> TaxLot list
  gains.py       # Lot -> gain, bucket totals, fee allocation
  slab.py        # Slab tables (old + new regime), surcharge, cess, rebate, derive_slab()
  engine.py      # compute_tax() orchestrator
  schemas.py     # Pydantic request/response models
```

`regime.py`'s `classify_regime()` takes the security-master country as an input. The service layer that assembles the engine's inputs (orders, instruments, accounts, reference securities, profile) lives in `app/investing/tax_service.py` — it does the DB reads and calls the pure engine. The engine itself stays DB-free.

### 4.1 Regime classification

```python
def classify_regime(
    instrument: Instrument | None,
    account: Account | None,
    profile: WorkspaceTaxProfile | None,
) -> RegimeSpec:
```

Precedence, highest first:

1. `profile.explicit_regime` — the user's override wins over everything.
2. **Holding-level override** — the instrument's own `exchange_country_override` (see §3.2b), when set. This is the "flag individual stocks differently" affordance.
3. `reference_security.country_code == "IN"` → `RegimeType.indian`.
4. `reference_security.country_code` set to a non-IN country → `RegimeType.foreign`.
5. `account.market_scope` → `india` / `foreign` / `mixed` (mixed falls through to 6).
6. **Default → `foreign` if the holding's currency is not INR, else `indian`.**

Step 6 is the fallback for a ticker that is not in the security master. The currency heuristic is imperfect (an ADR quoted in USD on an Indian exchange is possible) which is exactly why steps 1–5 exist and why the UI must show which rule fired.

The engine returns the **reason** alongside the regime so the UI can say *why* a holding was classified the way it was — `classified_by` is one of `explicit_regime` | `instrument_override` | `security_master` | `account_market_scope` | `currency_fallback`. A silently-classified holding is a support ticket waiting to happen.

### 4.1b Holding-level override — the "flag stocks differently" affordance

The user asked to "have clear flags to mark brokers or stocks with different type separately". Two levels:

- **Broker level** → `accounts.market_scope` (a default for the whole account).
- **Stock level** → a per-instrument override that beats the account default.

A per-instrument override needs somewhere to live. The security master is global and must not be workspace-editable. The instrument row is workspace-scoped, so the override belongs there:

```python
# investing_instruments
exchange_country_override: str | None = Field(default=None, max_length=2)
```

`NULL` = inherit. Set to `"IN"` or `"US"` to force a regime for that one holding, overriding both the account default and the security master. This covers the case of a broker account that holds both Indian and US securities, and the case of an ADR that is Indian-listed for tax purposes.

### 4.1c Exchange-code map for the MCP tool

The `update_investing_account_status` tool takes a human exchange name and derives the country. The security master uses MIC codes, so the map must cover both:

| User input | MIC | Country |
|---|---|---|
| NSE, XNSE, NSE India | `XNSE` | IN |
| BSE, XBOM, BSE India | `XBOM` | IN |
| NASDAQ, XNAS | `XNAS` | US |
| NYSE, XNYS | `XNYS` | US |
| NYSE ARCA, ARCX, ARCA | `ARCX` | US |
| AMEX, XASE | `XASE` | US |
| LSE, XLON | `XLON` | GB |

Anything unrecognised → the country is left `None` and the tool says so explicitly rather than guessing. The tool must **not** silently apply a default country, because a wrong country silently picks the wrong tax regime.

### 4.2 The regime table

`regime.py` holds the statutory data as data, not as scattered `if` statements:

```python
@dataclass(frozen=True)
class RegimeSpec:
    key: RegimeType
    ltcg_holding_months: int        # 12 or 24
    stcg_section: str               # "111A" | "112(1)(a)(ii)"
    ltcg_section: str               # "112A" | "112(1)(c)"
    ltcg_rate_pct: Decimal          # 0.125 for both
    ltcg_exemption_inr: Decimal     # 125000 indian, 0 foreign
    stcg_is_flat: bool              # True indian (20%), False foreign (slab)
    stcg_flat_rate_pct: Decimal     # 0.20 indian, None foreign
    indexation_available: bool      # False for both post-23-Jul-2024
    cess_pct: Decimal

INDIAN = RegimeSpec(
    key=RegimeType.indian,
    ltcg_holding_months=12,
    stcg_section="111A",
    ltcg_section="112A",
    ltcg_rate_pct=Decimal("0.125"),
    ltcg_exemption_inr=Decimal("125000"),
    stcg_is_flat=True,
    stcg_flat_rate_pct=Decimal("0.20"),
    indexation_available=False,
    cess_pct=Decimal("0.04"),
)

FOREIGN = RegimeSpec(
    key=RegimeType.foreign,
    ltcg_holding_months=24,
    stcg_section="112(1)(a)(ii)",
    ltcg_section="112(1)(c)",
    ltcg_rate_pct=Decimal("0.125"),
    ltcg_exemption_inr=Decimal("0"),
    stcg_is_flat=False,
    stcg_flat_rate_pct=None,
    indexation_available=False,
    cess_pct=Decimal("0.04"),
)
```

**Pre-23-Jul-2024 transfers** (FY 2024-25 and earlier) used 20% with indexation on Sec 112. The engine must not apply the current table to those years. Scope decision: **this spec covers the current and future FY only**. Historical years return `status: "unsupported_fy"` with an explanatory message rather than a wrong number. (A future spec may add the grandfathered regime.)

### 4.3 The IFSC / GIFT City flag

`profile.explicit_regime` covers this: setting it to `indian` forces 111A/112A (20% flat STCG) for the workspace, which is the treatment a GIFT City-linked foreign-custodian account may attract. The UI labels this explicitly and warns that it overrides the automatic classification.

A more granular per-account override is **out of scope** for this spec: the user has one foreign account, and `explicit_regime` at workspace level is sufficient. If per-account granularity is later needed, `accounts.market_scope` already provides the extension point.

### 4.4 FX — manual, user-supplied (Option B)

**Decision: the user supplies the Rule 115 rate; the app does not look it up.**

Rule 115 (carried forward as Rule 206 under the Income-tax Act, 2025 from FY 2026-27) requires the **State Bank of India telegraphic transfer (TT) buying rate on the specified date**. For capital gains the specified date is **the last day of the month preceding the month of transfer**.

The app's existing `fx_rate_ingestion_job` stores a generic daily market rate. **That is not the statutory rate** and must not be used silently.

Design:

- The tax request carries `fx_overrides: dict[str, str]` keyed by `"YYYY-MM"` (the month of transfer), values are INR per 1 unit of the sale currency.
- If a month has no override, the engine falls back to the app's stored FX rate for the specified date **and marks the result `fx_source: "app_rate_estimate"`** with the rate used and its as-of date.
- If an override is present, `fx_source: "manual"` and the override is echoed back.
- The response always includes, per month: the specified date (last day of preceding month), the rate, the source, and whether it was estimated.
- The UI shows a per-month FX table with an edit affordance, and marks estimated rows visually.

This is honest: the app never pretends a generic rate is the statutory one, and the user can correct it against SBI's published card in seconds.

Published cards for a given date can differ slightly between revisions and transaction slabs (₹10 lakh vs ₹20 lakh bands), so the UI copy should say "verify against SBI's published TT buying rate for the date shown".

### 4.5 Slab derivation

```python
def derive_slab(
    other_income: Decimal,
    stcg_gain: Decimal,
    profile: WorkspaceTaxProfile | None,
) -> SlabResolution
```

Indian resident individual slabs (old regime) for FY 2026-27:

| Income band (₹) | Rate |
|---|---|
| 0 – 2,50,000 | 0% |
| 2,50,001 – 5,00,000 | 5% |
| 5,00,001 – 10,00,000 | 20% |
| Above 10,00,000 | 30% |

Surcharge (on total income):

| Total income | Surcharge |
|---|---|
| > 50,00,000 | 10% |
| > 1,00,00,000 | 15% |
| > 2,00,00,000 | 25% |
| > 5,00,00,000 | 37% (Sec 112A capped at 15%) |

Cess: 4% on tax + surcharge. Rebate u/s 87A: up to ₹12,000 where total income ≤ ₹12,00,000.

Resolution order:

1. If `profile.slab_override_pct` is set → use it, `slab_source: "override"`.
2. Else derive from `other_income + stcg_gain`, `slab_source: "derived"`.

`SlabResolution` carries: `rate_pct`, `source`, `surcharge_pct`, `surcharge_source`, `cess_pct`, `rebate_amount`, `total_income_considered`, and a `caveats: list[str]` noting when the derived figure is uncertain (e.g. other income is stale, or the new regime is assumed).

**New regime note:** the default regime in the income-tax portal changed from AY 2024-25. Which regime applies materially changes the slab table (the new regime's 15% band starts at ₹10 lakh rather than ₹5 lakh, and its slabs are different above that). The profile carries `tax_regime: "old" | "new"` (default `"old"`), and the slab table is selected accordingly. Surcharge/cess/rebate are computed the same way under both.

**The estimate is a marginal-rate estimate.** It is the tax on the capital gain at the user's marginal rate, not a full return computation. The UI must say so. A full projection would need every income head; out of scope.

### 4.6 FIFO replay and gain computation

Reuse the existing `_replay_orders` logic in `app/investing/order_service.py` rather than writing a second implementation. The engine's `lots.py` calls it and maps the result to `TaxLot`:

```python
@dataclass(frozen=True)
class TaxLot:
    symbol: str
    instrument_type: str
    regime: RegimeType
    quantity: Decimal
    cost_per_unit: Decimal       # buy cost incl. capitalised buy-side fees
    acquired_at: datetime
    sold_at: datetime
    sale_price_per_unit: Decimal
    gain_native: Decimal
    holding_months: int
    is_long_term: bool
```

Rules:

- Buy-side fees **capitalise into cost**: `eff_cost_per_unit = (gross_amount + brokerage_fee + tax_amount + other_fees) / quantity`.
- Sell-side fees reduce **proceeds**, not the cost of units sold. Deduct from the bucket they belong to; when one sell spans both buckets, allocate pro-rata by gross gain.
- Holding period is **per lot**, compared against `sale_date > add_months(acquired_at, regime.ltcg_holding_months)`.
- A sell can span the long-term boundary — classify **each consumed lot** independently.
- The currency of the gain is the order's currency; conversion to INR happens after bucketing.

### 4.7 Reconciliation guard

Before returning any figure, the engine asserts for every sell in the FY:

```
computed_gain == qty × (price_per_unit − FIFO cost_per_unit) − sell_fees   ==  recorded realized_gain_loss
computed_fifo_cost_per_unit                                                ==  recorded avg_cost_at_sale
```

On a correct replay these agree to sub-cent precision. On mismatch, the engine returns:

```json
{
  "status": "reconciliation_failed",
  "message": "FIFO replay does not reproduce the recorded gain for 2 of 7 sells.",
  "failures": [ { "symbol": "MU", "public_id": "...", "expected": "...", "computed": "..." } ]
}
```

and **no tax number**. A tax estimate that has not passed this check must not be presented. This is the cheapest available guard against a silently wrong regime and is the single most important correctness property in the engine.

---

## 5. API

### 5.1 `GET /v1/investing/tax/summary`

Query params: `fy` (optional, default current FY), `account_id` (optional filter).

Response:

```json
{
  "status": "success",
  "fy_label": "2026-27",
  "currency": "INR",
  "buckets": {
    "long_term": { "gain_native": "751.37", "gain_inr": "71695.60", "tax": "8906.50" },
    "short_term": { "gain_native": "1097.56", "gain_inr": "104728.85", "tax": "32473.22" }
  },
  "total_gain_inr": "176424.45",
  "total_tax_inr": "41379.72",
  "effective_rate_pct": "23.45",
  "slab": {
    "rate_pct": "30.00",
    "source": "derived",
    "surcharge_pct": "10.00",
    "cess_pct": "4.00",
    "rebate_amount": "0.00",
    "total_income_considered": "1500000.00"
  },
  "fx": [
    { "month": "2026-10", "specified_date": "2026-09-30", "currency": "USD",
      "rate": "95.42", "source": "manual", "is_estimated": false }
  ],
  "positions": [ /* per-lot rows, see §5.2 */ ],
  "assumptions": [
    "Regime 'foreign' (Sec 112) applied to 5 of 7 sells; 24-month holding threshold.",
    "STCG taxed at 30% marginal slab derived from other income of ₹12,00,000.",
    "No prior-year losses applied."
  ],
  "disclaimers": [
    "This is an estimate, not a filing-ready liability. Verify slab, FX rate and regime with your CA."
  ]
}
```

### 5.2 `GET /v1/investing/tax/positions`

Same query params. Returns the per-lot rows: symbol, instrument, account, quantity, acquisition date, the regime's long-term mark date, months held, bucket, gain in native currency and in INR, and **the rule that classified the regime** (`classified_by: "explicit_regime" | "instrument_override" | "security_master" | "account_market_scope" | "currency_fallback"`).

### 5.3 `GET/PATCH /v1/finance/tax-profile`

- `GET` → the profile (creating a default row on first access).
- `PATCH` → partial update. Validates `fy_label` format (`^\d{4}-\d{2}$`), slab range (0–100), cess (0–10).

### 5.4 `PATCH /v1/finance/accounts/{id}`

Accepts the existing fields plus `market_scope`. Only brokerage accounts accept `market_scope`; a non-brokerage account with `market_scope` set returns 422.

### 5.5 `PATCH /v1/investing/instruments/{id}`

Accepts `exchange` (descriptive label) and `exchange_country_override` (`"IN"` / `"US"` / `null` to clear). The override beats both the account default and the security master for that holding.

### 5.6 `GET /v1/investing/tax/regimes`

Returns the two regime specs as data — the same table the engine uses — so the UI's `i` tooltips and the MCP tool can render the regime rules without hardcoding them. Optionally takes `exchange` or `ticker` and returns the resolved regime for that instrument with its `classified_by` reason.

### 5.7 MCP tools

Two new tools on the Lifestack MCP server, following the existing authorization pattern (`authorize_workspace` with `required_scope="mcp:read"` for reads, `mcp:write` + `confirmed` for writes):

#### `get_tax_summary`

```
@mcp.tool
async def get_tax_summary(
    workspace_id: int,
    fy: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Get the capital gains tax estimate for a workspace for one financial year."""
```

Returns the same shape as §5.1, plus the per-lot `positions`. Read scope.

#### `update_investing_account_status`

```
@mcp.tool
async def update_investing_account_status(
    workspace_id: int,
    account_id: str,
    market_scope: str | None = None,
    exchange_name: str | None = None,
    symbol: str | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Set a brokerage account's market scope, or flag one holding's exchange, after confirmation."""
```

This is the tool the user asked for: it lets any AI set the regime flag on a broker account, or on an individual holding.

- `account_id` — the brokerage account's public id.
- `market_scope` — `"india" | "foreign" | "mixed"`; sets `accounts.market_scope`, the account-level default.
- `exchange_name` — optional. Sets the `exchange` label on the account's holdings. With `symbol` supplied it applies to that one holding only; without `symbol` it applies to every holding in the account whose exchange is still unset. The country is derived from the exchange name via the map in §4.1c.
- `symbol` — optional. Restricts the `exchange_name` update to a single holding and sets that instrument's `exchange_country_override`, the holding-level override that beats the account default.
- `confirmed` — required `True` to write, matching the existing write-tool convention (`"status": "needs_confirmation"` when `False`).

**Two distinct levels, deliberately:**

| Level | Field | Beats | Use when |
|---|---|---|---|
| Broker | `accounts.market_scope` | the currency fallback | the whole account is Indian-listed or foreign-listed |
| Stock | `instruments.exchange_country_override` | the account default **and** the security master | one holding differs — e.g. an ADR, or a mixed account |

The response reports what actually changed and which rule now governs each affected holding:

```json
{
  "status": "success",
  "account": { "public_id": "...", "name": "IND Money", "market_scope": "foreign" },
  "instruments_updated": [
    { "symbol": "AAPL", "exchange": "XNAS", "exchange_country_override": null }
  ],
  "instruments_skipped": [
    { "symbol": "MSFT", "reason": "exchange already set to XNYS" }
  ],
  "notes": [
    "Account default set to 'foreign'.",
    "Holding-level overrides take precedence over the account default.",
    "Unrecognised exchange names leave the country unset rather than guessing."
  ]
}
```

**Rationale:** the account flag is a default; an individual holding may need to differ. The response says explicitly which wins, because "why was this classified as foreign?" is the most likely user question.

A separate `update_instrument_exchange` tool is **not** added — `update_investing_account_status` covers both levels, and a thinner tool surface is easier to reason about.

### 5.8 Web

#### 5.8.1 New `TaxTab.tsx`

`lifestack-web/src/pages/investing/TaxTab.tsx`, following the `AnalyticsTab.tsx` pattern (self-fetching via `useQuery`, props limited to display formatting).

Route registration in `InvestingPage.tsx`:
- `InvestingTab` union gains `'tax'`.
- `INVESTING_TAB_ROUTES` gains `tax: 'tax'`.
- A `TabsTrigger` with `data-testid="investing-tab-tax"`, label **"Tax"**.
- A `TabsContent value="tax"` rendering `<TaxTab currencyDisplayPreference={...} />`.

Layout:

1. **Header strip** — FY selector (dropdown of available FYs with recorded sells), and the headline: total estimated tax, total gain, effective rate.
2. **Bucket cards** — LTCG and STCG, each showing gain (native + INR), the rate applied, the section, and the exemption applied.
3. **Per-transaction table** — symbol, account, quantity, acquisition date, the regime's long-term mark date, months held, bucket, gain native, gain INR. Sortable by gain.
4. **Assumptions panel** — the assumptions list from the API, rendered as a definition list.
5. **FX table** — one row per sale month: month, specified date, rate, source (manual / estimated), and an edit button. Estimated rows are visually marked.

#### 5.8.2 The `i` information affordance

The user asked for a clear `i` symbol giving this information. The codebase already has the exact pattern in `HoldingsTab.tsx:1042-1047` — a `group relative inline-flex` wrapper around a lucide `Info` icon with an absolutely-positioned tooltip that appears on `group-hover`.

Reuse it, with a shared component so the copy lives in one place:

`lifestack-web/src/components/investing/TaxInfoTip.tsx`

```tsx
export const TaxInfoTip: React.FC<{ content: React.ReactNode; label?: string }> = ({ content, label = 'Info' }) => (
  <span className="group relative inline-flex">
    <Info className="h-3.5 w-3.5 text-slate-500 hover:text-slate-300 cursor-default" aria-label={label} />
    <span
      role="tooltip"
      className="pointer-events-none absolute bottom-full left-1/2 z-20 mb-1.5 hidden w-max max-w-[280px] -translate-x-1/2 rounded border border-slate-700 bg-slate-950 px-2.5 py-1.5 text-xs font-normal text-slate-300 shadow-xl group-hover:block"
    >
      {content}
    </span>
  </span>
);
```

Placed next to:
- The "Tax estimate" headline — explains that it is an estimate, not a filing.
- Each bucket card's rate — explains the section, the threshold and the exemption for that regime.
- The FX table header — explains the Rule 115 specified date and that estimated rates must be verified against SBI's published card.
- The "Estimated" badge on an FX row — explains what "estimated" means.
- The regime classification column — explains which rule classified the holding and that holding-level overrides beat the account default.

The tooltip must be keyboard-reachable, not hover-only. The `HoldingsTab` pattern is hover-only; the new component adds `focus-within:block` so keyboard users can reach it, and `aria-describedby` wiring. This is a small deviation from the existing pattern, in the right direction.

#### 5.8.3 Tax profile settings

A new section in the finance settings surface (not inside the Tax tab — configuration is not a report):

- Resident status
- Tax regime (old / new)
- Other income
- Slab override (leave blank to derive)
- Explicit regime override — with a warning that it overrides the automatic classification and that the GIFT City / IFSC treatment is unsettled
- Apply carry-forward toggle

### 5.9 Service + types

`src/services/investing.ts` gains `getTaxSummary(fy?, accountId?)` and `getTaxPositions(fy?, accountId?)`, with the response types in `src/types/investing.ts` (this repo uses Zod schemas in `src/types/` — follow the existing pattern).

`src/services/finance.ts` gains `getTaxProfile()` and `updateTaxProfile(patch)`.

`queryKeys` gains `investing.tax.summary(fy)` and `finance.taxProfile`.

---

## 6. Deliverables & Sequencing

| Phase | Deliverable | Notes |
|---|---|---|
| **1** | `app/investing/tax/` engine + unit tests | Pure logic, no migration. Proves the algorithm against the reference implementation in the skill's `scripts/capital-gains-tax.py`. |
| **2** | Schema: `accounts.market_scope`, `instruments.exchange_country_override`, `workspace_tax_profiles` + migration | Alembic revision `0068`. Backfill `market_scope='india'`. No new country column on instruments — the security master supplies it. |
| **3** | `GET /investing/tax/summary`, `/positions`, tax-profile endpoints, MCP `get_tax_summary` + `update_investing_account_status`, Tax tab | The payoff. Manual FX overrides only. |
| **4** | `tax_loss_carry_forward` table + Sec 24(6) engine | Correctness for multi-year use. |
| **5** | SBI TTBR ingestion (per-month statutory rates) | Removes the weakest link in the chain. |

Phases 1–3 are a coherent PR-able unit. Phase 4 is where the feature stops being a calculator and becomes return-prep.

---

## 7. Testing

### 7.1 Engine unit tests (Phase 1) — the bulk of the value

- **FIFO replay reproduces `realized_gain_loss` and `avg_cost_at_sale` exactly** on a seeded order set, to sub-cent precision. This is the reconciliation guard, and it is the test that would have caught the original "Ind Money uses a different method" confusion.
- **24-month boundary, exact cases:**
  - acquired 2024-01-15, sold 2026-01-15 → exactly 24 months → **short-term** (not more than 24).
  - acquired 2024-01-15, sold 2026-01-16 → 24 months + 1 day → **long-term**.
  - acquired 2024-01-31, sold 2026-02-28 → 24 months + 28 days → **long-term**.
  - A leap-year case: acquired 2024-02-29, sold 2026-02-28 → exactly 24 months → **short-term**.
  - A month-end clamp: acquired 2024-01-31, sold 2026-01-30 → 23 months 30 days → **short-term**.
- **Mixed-bucket sell:** one sell consuming a 30-month and a 10-month lot splits into LTCG + STCG with fees allocated pro-rata.
- **Buy-side fees capitalise into cost; sell-side fees reduce proceeds.**
- **Foreign regime has no ₹1.25 lakh exemption; Indian regime applies it once per year, not per holding.**
- **Indian STCG is flat 20% regardless of slab; foreign STCG uses the slab.**
- **Slab derivation:** boundary incomes (₹2,50,000 / ₹2,50,001 / ₹5,00,000 / ₹5,00,001 / ₹10,00,000 / ₹10,00,001); surcharge thresholds; 87A rebate at exactly ₹12,00,000; override beats derivation.
- **Reconciliation failure returns no number** — a deliberately corrupted `realized_gain_loss` produces `status: "reconciliation_failed"`, not a tax figure.
- **FX fallback:** a month with no manual override returns `fx_source: "app_rate_estimate"` and `is_estimated: true`.
- **Regime precedence:** explicit override beats holding-level override beats security-master country beats account scope beats currency fallback, and `classified_by` reports the rule that fired.
- **Security-master classification:** a ticker present in `reference_securities` with `country_code="IN"` classifies as Indian; `"US"` as foreign; a ticker absent from the master falls through to the account scope; a ticker present on multiple exchanges is treated as ambiguous and falls through rather than guessed.
- **Exchange-name map:** every entry in §4.1c maps to the right MIC and country; an unrecognised name yields country `None` and never a guessed default.

### 7.2 API tests (Phase 3)

- Endpoint returns 200 with a valid shape for a workspace with sells; 404/empty for a workspace with none.
- `PATCH /finance/tax-profile` validates slab range, cess range, FY label format.
- `PATCH /finance/accounts/{id}` rejects `market_scope` on a non-brokerage account.
- `PATCH /investing/instruments/{id}` rejects a malformed `exchange_country`.

### 7.3 MCP tests (Phase 3)

- `get_tax_summary` with `mcp:read` scope; with `mcp:write`; and rejected without either.
- `update_investing_account_status` with `confirmed=False` → no write, returns a confirmation-required status.
- `update_investing_account_status` with `confirmed=True` → updates `market_scope`, updates the exchange on holdings whose exchange is unset, and **skips** holdings whose exchange is already set, reporting both lists.
- `update_investing_account_status` with `symbol` → updates only that holding, setting its `exchange_country_override`.
- Invalid `market_scope` value → error status, no write.
- Unrecognised `exchange_name` → the exchange is set but the country is left unset, and the response says so; the regime then falls through to the account scope rather than a guessed country.

### 7.4 Web tests (Phase 3)

- Tax tab renders the bucket cards, the position table and the assumptions panel from a mocked response.
- The `i` tip renders its content and is keyboard-reachable (`focus-within`).
- An estimated FX row shows the "estimated" badge; a manual row does not.
- The slab override field, when set, is sent on save.
- Following the repo's MSW/Vitest conventions — see the `lifestack` skill's `references/lifestack-web-testing-patterns.md`.

### 7.5 E2E (Phase 3)

- Set the account's `market_scope` to `foreign` via the API, then open the Tax tab and assert the regime shown is "Foreign (Sec 112)".
- The Info tooltip is visible on hover and on keyboard focus.

### 7.6 Fixture data

The E2E suite needs a seeded brokerage account with a foreign market scope and at least one sell that spans the 24-month boundary. Add it to the E2E seed rather than relying on production-shaped data.

---

## 8. Non-Goals

Explicitly out of scope, so they are not smuggled in later:

- **A full income-tax return computation.** The engine estimates the tax on capital gains. It does not compute total income, other heads, or the total liability.
- **Historical FY 2024-25 and earlier** (pre-23-Jul-2024 regime, 20% with indexation, 36-month threshold). Those years return `unsupported_fy`.
- **Dividend and interest taxation** (US withholding, Schedule FA reporting of dividends, the 25% foreign tax credit via Form 67).
- **NRIA / non-resident computation.** `resident_status` is stored, but the engine implements the resident-individual case only. A non-resident profile returns `status: "unsupported_residency"`.
- **TCS / LRS reporting** on remittances into India.
- **Automatic SBITTBR ingestion** (Phase 5).
- **Indexation** for grandfathered LTCG.

---

## 9. Open Questions for the User

1. **Is the Drivewealth account a GIFT City / IFSC-linked route?** This decides whether STCG is 20% flat (111A) or slab (112). The `explicit_regime` flag exists either way, but the default matters.
2. **Old regime or new regime?** The slab table differs. Default is old regime.
3. **Should the Tax tab show Indian holdings too?** The engine can classify them via `exchange_country`, but the user's immediate need is the US portfolio. Recommendation: yes, but label the two regimes distinctly so the difference is visible.
4. **Which FYs should be selectable?** Only FYs with recorded sells, or a free-form FY picker?
