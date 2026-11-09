# Spec-106: MCP Tax What-If Tools (simulate_sale, list_tax_lots, harvest_candidates)

**Created:** 2026-11-09
**Status:** Draft - retroactive, pending Sajan approval
**Scope:** API (`app/investing/tax/simulation.py`, `app/investing/tax/engine.py`, `app/investing/tax_whatif_service.py`, `app/investing/tax_whatif_views.py`, `app/investing/repository.py`, `app/mcp/server.py`)
**Depends on:** spec-099 capital gains tax estimate, spec-044 FIFO order lots, spec-051 corporate actions

> **Process note.** This spec was written *after* the implementation (the pure functions
> and the MCP adapter were built in one pass). Per the repo's spec-first rule it needs
> explicit approval; if the approach below is rejected, the commits on
> `feat/mcp-tax-whatif-tools` are the thing to revise. No migration is involved.

---

## 1. Problem

spec-099 answers "what tax do I owe for realised sales?". Users also ask forward
questions - "what if I sell 40 units of INFY next Tuesday?", "when does this lot turn
long-term?", "which losses are worth harvesting?" - and today only an agent doing
mental arithmetic can answer them. The pure functions `simulate_sale`, `list_tax_lots`
and `harvest_candidates` exist (`app/investing/tax/simulation.py`) but are not callable
over MCP.

## 2. Scope

Three **read-only** MCP tools (scope `mcp:read`, workspace-authorised, never write a
holding, lot, order or price) plus the service/adapter beneath them. Out of scope: any
REST route or UI, executing a sale, multi-sale scenarios, loss carry-forward, cross-bucket
loss set-off (the engine nets within a regime/term bucket only - stated in the output).

## 3. Tools

All tools take `workspace_id` first. Decimals are strings; dates are ISO days in the
user's timezone; internal ids are never returned.

| Tool | Args | Returns |
|---|---|---|
| `simulate_sale` | `holding`, `qty`, `date`, `price`, `sell_fees="0"`, `account_id?` | status, per-lot gains/classification, FX rate + source, incremental tax, assumptions |
| `list_tax_lots` | `holding`, `account_id?`, `limit=200` (1-500), `offset=0` | open FIFO lots: acquired date, quantity, cost, `days_to_long_term`, `long_term_from`, unrealised P&L if priced |
| `harvest_candidates` | `stcg_rate_percent?`, `limit=50` (1-200) | unrealised-loss lots ranked by tax saved, price date/staleness per lot, skipped holdings, missing-FX currencies |

### 3.1 Holding resolution

`holding` is a **holding `public_id`** (preferred) or a **symbol**. A symbol is matched
case-insensitively inside the workspace; if it matches more than one holding (the same
ticker in two accounts) the tool returns `code: ambiguous_holding` with the candidates
(`holding_id`, `account_id`, `account_name`, `quantity`) instead of guessing. `account_id`
(an account public id) narrows a symbol, and must agree with a public id if both are given.
A holding from another workspace is indistinguishable from a missing one
(`holding_not_found`).

### 3.2 Statuses and errors

`simulate_sale.status`: `success`, `insufficient_quantity` (the maximum sellable
quantity is simulated and priced; `message` says how much is open),
`missing_fx_rate` (no Rule 115 rate -> **no tax figure**, gain in native currency only),
`reconciliation_failed` (the FY's recorded sells do not reconcile with the FIFO replay ->
no incremental tax, same guard as `get_tax_summary`). Bad input returns
`{"status":"error","code":...}` (`invalid_number`, `invalid_date`, `invalid_account`,
`account_not_found`, `holding_not_found`, `ambiguous_holding`, `invalid_page`).

## 4. Semantics

### 4.1 Incremental tax (fix of the earlier isolation assumption)

The first version of `simulate_sale` applied the full ₹1.25 lakh Sec 112A exemption as
if the simulated sale were the FY's only long-term gain, overstating the shelter once
gains were already booked. **Chosen design:** `compute_tax_estimate` gains an optional
`realised_lots` (already-classified `TaxLot`s, copied, filtered to the FY). The adapter
takes them from `TaxService.compute(fy=<FY of the sale date>)` - the same data
`get_tax_summary` uses - and `simulate_sale` prices the FY twice: without and with the
simulated sale. `tax_inr` is the **difference**, so the engine's own exemption, slab,
surcharge, cess and 87A logic see the whole FY. Output reports the tax before/after, the
realised LTCG already booked, the exemption remaining and the part this sale uses.

Alternatives rejected: (a) a bare `realised_ltcg_in_fy_inr` scalar - would re-implement
the exemption/slab arithmetic outside the engine and cannot represent a net realised loss
or foreign income that moves the slab; (b) replaying the whole order history plus a
synthetic sell - breaks on corporate-action lots (the DB's open lots include splits/bonus
that an order replay does not know) and on back-dated simulations.

A loss sale can give a negative `tax_inr` (it reduces tax on gains already realised).

### 4.2 Calendar-day arithmetic and timezones

* A sale date is a **calendar day in the user's timezone** (`User.timezone`, default
  `Asia/Kolkata` because Indian tax law counts IST days). A date-time input is converted
  into that zone first (20:00Z on 31 Mar is 1 Apr IST -> next FY).
* Lot `acquired_at` (timestamptz) is converted to the same zone; the pure layer then
  collapses every timestamp to its own local date, so holding period, financial year and
  the Rule 115 month are decided by local days, not by UTC instants. Naive datetimes are
  rejected by the pure layer (`ValueError`), never silently assumed.
* "More than N months" means a sale is long-term only the day **after** the N-month
  anniversary. `days_to_long_term` counts to that first long-term day (`long_term_from`);
  it is 0 only when the lot is already long-term. (The earlier value was 0 on the
  anniversary itself while the lot was still short-term - a defect, fixed.)
* Real orders in the engine keep their stored UTC instants (unchanged spec-099
  behaviour), so a lot bought within hours of a boundary can classify differently in a
  filing than in a simulation. Documented, not changed here.

### 4.3 Lots

Open lots are `investing_order_lots` with `remaining_quantity > 0`, FIFO by
`(acquired_at, id)` (numeric id; the earlier string tie-break sorted lot 10 before 2).
Lots acquired after the sale date are excluded (noted in `assumptions`). Quantities are
exact `Decimal` (8 dp); `qty` rejects NaN/Infinity/zero/negatives, more than 8 dp and
values above 10^12. If a holding's open-lot total differs from `Holding.quantity`
(imported/manual holdings have no lots) a warning says so rather than hiding it.

### 4.4 FX

`simulate_sale` uses the engine's own `resolve_fx_rate` (manual daily override ->
nearest manual -> legacy month -> app stored rate), returning the rate and its source.
Missing rate -> `missing_fx_rate`. If the Rule 115 date is still in the future the latest
stored rate is flagged as a placeholder.

### 4.5 Harvest

Loss = (cost - latest stored price) x open quantity per lot. Saving = loss x statutory
base rate (12.5% LT; 20% Indian ST; foreign ST is slab-dependent and unranked unless
`stcg_rate_percent` is given). Ranking is by INR saving using the latest stored market
rate per currency (INR implicit); currencies with no rate are listed in
`missing_fx_currencies` and their lots rank after priced ones. A holding with no stored
price (or no lots) appears in `skipped_holdings` - prices are never guessed. A price older
than 7 days is returned but flagged `price_is_stale` with its `price_date`. Cess,
surcharge, the exemption and the need for same-FY gains to set the loss against are not
modelled and are stated in the output.

## 5. Layering and performance

`MCP tool -> TaxWhatIfService -> repositories`. New repository reads:
`LotRepository.list_open_for_holdings` (one workspace-scoped query for many holdings) and
`ReferenceSecurityRepository.list_by_tickers`. Regime classification is shared with the
estimate through `classify_symbol` / `TaxService.regimes_for_holdings`, so a simulation
cannot classify differently from the filing. Query count is independent of the number of
lots and holdings (asserted in a test: 60 lots -> <= 15 queries for the listing, <= 25 for
a simulation). Lots are paged (`limit`/`offset`) in `list_tax_lots`.

## 6. Tests

* Pure: `test_tax_simulation.py` (existing, two assertions updated for the countdown fix
  and the new assumption text), `test_tax_simulation_incremental.py` (exemption remaining,
  exhausted, realised loss, other-FY ignored, no input mutation, foreign, missing FX,
  IST/UTC day boundaries, naive datetimes, lot ordering, lots after sale date).
* Service helpers: `test_tax_whatif_service.py`.
* Integration (real Postgres, real FIFO lots via REST): `test_tax_whatif_tools.py` -
  foreign lot with `days_to_long_term` and FX source, missing-FX, insufficient quantity,
  Indian exemption vs `get_tax_summary`, ambiguity, cross-workspace isolation, read-only
  row counts, harvest ranking/stale/missing price/missing FX, no N+1, user timezone.
* MCP catalogue: `test_mcp_tools_suite.py` lists the three tools.

## 7. Known limits / open questions for approval

1. The engine applies the 87A rebate to special-rate gains; incremental tax inherits that
   (tests use a high `other_income` to avoid it). Whether that is statutorily right is a
   spec-099 question, not changed here.
2. Default timezone `Asia/Kolkata` when the user has none - confirm vs UTC.
3. Harvest saving ignores cess/surcharge by design (it is a ranking aid).
