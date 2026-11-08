# Spec-102: LRS Remittance Summary + Stored FX Rate Read/Audit

**Created:** 2026-11-08
**Status:** Draft (retroactive; pending Sajan approval) — written after the code (kanban t_38616759), which the spec-driven rule forbids. Treat the implementation as provisional until this is approved.
**Scope:** API (`app/finance`, `app/investing/tax_service.py`), MCP (`app/mcp/server.py`)
**Depends on:** spec-099 capital-gains tax estimate (owns `set_fx_rate` / `WorkspaceTaxProfile.fx_overrides`), capital transfers (spec-050)

---

## 1. Problem

1. Foreign-brokerage funding (Paasa, US brokers) is an RBI **LRS** remittance: USD 250,000 per financial year, with TCS on the excess over a threshold. Nothing in the product tells the user how much of the cap is used or whether TCS applies.
2. `set_fx_rate` stores user-provided Rule 115 rates in `WorkspaceTaxProfile.fx_overrides` (key `YYYY-MM-DD:CURRENCY`) but they could only be written, never read back or audited.

## 2. Surface

| REST (prefix `/v1/finance`) | MCP tool (scope `mcp:read`) |
|---|---|
| `GET /lrs/summary?fy=2026-27` | `get_lrs_summary(workspace_id, fy?)` |
| `GET /fx-rates/{as_of_date}?currency=USD` | `get_fx_rate(workspace_id, date_str, currency)` |
| `GET /fx-rates/missing?from_date=&to_date=&currency=` | `list_missing_fx_rates(workspace_id, from_date, to_date, currency)` |

All reads are scoped by `workspace_id`; none writes (a read never creates a tax-profile row).

## 3. Decisions

### 3.1 FY, not calendar year
The LRS cap and the s.206C(1G) TCS threshold are both **per financial year, 1 Apr – 31 Mar**, per resident individual (PAN). FY and each transfer's date are evaluated in **IST**: a transfer at 00:30 IST on 1 April belongs to the new FY although it is still 31 March in UTC. `fy` defaults to the current FY; malformed or out-of-range labels (before 2004, after next FY) are 422.

### 3.2 What counts as an outward remittance
A capital transfer with `from_module=spending`, `to_module=investing`, `from_currency=INR` and `to_currency != INR`. Excluded: investing→spending repatriations, INR→INR (domestic broker) funding, transfers outside the FY. USD value = `gross_amount * fx_rate_used` (the remitted amount before fees); for non-USD destinations it is converted with the system FX rate (currency→USD) on the IST transfer date. A transfer with no usable rate stays in `total_inr`/`transfer_count`, is excluded from `total_usd`, and is listed in `unconverted_transfer_ids` plus an assumption note. All money is `Decimal`, rounded half-up to 2 dp, returned as strings.

### 3.3 TCS
20% on the INR remitted above the FY threshold (investment purpose; education/medical rates and loan-financed education are out of scope). `tcs_applicable = total_inr > threshold`; `tcs_amount_inr = 20% × (total_inr − threshold)`.

**Open question for Sajan — threshold.** The card asked for INR 7 lakh. Finance Act 2025 raised the s.206C(1G) threshold to **INR 10 lakh from 1 Apr 2025**. The implementation therefore uses **7 lakh for FYs before 2025-26 and 10 lakh from 2025-26** (`tcs_threshold_inr()` in `app/finance/lrs_service.py`); for FY 2026-27 this means no TCS at INR 8 lakh. If the card's literal 7 lakh is wanted, it is a one-line change. FYs before 2024-25 (rate changed mid-FY on 1 Oct 2023) are marked indicative.

### 3.4 Known limits (surfaced in the response `assumptions`)
Totals cover one workspace; the legal cap/threshold are per PAN across all banks and workspaces. Only INR-sourced transfers are counted (money already abroad, or sent from a foreign-currency account, is not LRS). The purpose is assumed to be investment.

### 3.5 FX reads
`get_fx_rate`: exact `YYYY-MM-DD:CURRENCY` key only (no nearest-date fallback, unlike the tax engine); absent → REST 404 / MCP `status: "not_found"`. `list_missing_fx_rates`: every date in `[from_date, to_date]` without a stored rate, ascending, with `missing_count`; `to_date < from_date`, a bad currency (not `[A-Z]{3}`, case-insensitive input) or a range over **366 days** (one FY) is 422. Rates are returned as the stored string.

## 4. Errors
RFC 7807 via `ValidationError` (422) / `NotFoundError` (404). MCP tools return `{"status": "error" | "not_found", "message"}` like sibling tools.

## 5. Tests
`app/tests/integration/test_lrs.py` (REST: empty, FY filter, repatriation/INR-domestic/out-of-FY exclusion, IST FY boundary, TCS threshold per FY and at-threshold, cap exceeded, non-USD destination, invalid FY, workspace isolation, auth, 404/422 paths, read-creates-no-row), `app/tests/finance/test_lrs_service.py` (decimal math, rounding, IST dates, validation), `app/tests/test_mcp_tools_suite.py` (catalog now 40 tools; scope `mcp:read` + workspace on each new tool; success/error/not_found).
