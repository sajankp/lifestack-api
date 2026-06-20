# Feature Spec: Automated Price Updates & Investing UI Enhancements
**Status:** Implemented
**Spec ID:** 031

## Implementation Notes (2026-06-13)

- Implemented `POST /v1/investing/prices/refresh` to fetch latest prices from Yahoo Finance and update
  current-day holding prices.
- Implemented current valuation fields on holding responses: current price, current value, gain/loss,
  and gain/loss percentage.
- Implemented manual single-holding price updates through the existing bulk price endpoint.
- Implemented frontend holdings-table enhancements: unit price, current value, gain/loss display,
  refresh action, and inline manual price editing.
- Remaining future work belongs in the roadmap as scheduled/background price cadence, richer return
  math, benchmarks, dividends, and deeper performance visualization.

## Extension: Indian Instruments (2026-06-20)

The refresh service routes by instrument type:

| Instrument type | Symbol input | Provider | Example |
|---|---|---|---|
| Stock | Exchange ticker, without Yahoo suffix for INR/NSE holdings | Yahoo chart API | `DRREDDY` becomes `DRREDDY.NS` |
| ETF | Exchange ticker, without Yahoo suffix for INR/NSE holdings | Yahoo chart API | `PHARMABEES` becomes `PHARMABEES.NS` |
| Indian mutual fund | Numeric AMFI scheme code | Official AMFI daily NAV feed | `122639` |

For mutual funds, users must enter the **AMFI scheme code** in the holding `symbol` field. They
must not enter the scheme name or ISIN. For example:

- Symbol: `122639`
- Type: `mutual_fund`
- Currency: `INR`
- Scheme: Parag Parikh Flexi Cap Fund - Direct Plan - Growth
- ISIN reference: `INF879O01027`

The official feed is `https://portal.amfiindia.com/spages/NAVAll.txt`. Its semicolon-delimited rows
contain scheme code, ISINs, scheme name, NAV, and NAV date. The refresh service matches the first
column exactly and stores the returned NAV/date as the holding price. The optional MFAPI endpoint
`https://api.mfapi.in/mf/{schemeCode}` is useful for discovery/history but is not the authoritative
refresh source.

Holding symbols are editable. A symbol correction must:

1. preserve quantity, average cost, currency, and account;
2. reject a duplicate `(workspace, symbol, account)` holding;
3. relink the holding to an instrument matching the corrected symbol and selected type;
4. leave historical holding-price rows attached to the holding while future refreshes use the new
   identifier.

Name/symbol validation and provider-side search suggestions are intentionally deferred.

## 1. Overview
Before this slice, the investing module tracked holdings, cash balances, and historical book cost. While a performance snapshot system existed on the backend to compute valuation, the holdings list UI only showed historical **Book Value** (`quantity * avg_cost`).

This feature introduced:
1. **Automated Price Updates:** Integration with an external API (Yahoo Finance unofficial chart API) to fetch stock/ETF unit prices automatically.
2. **On-Demand Refresh:** A backend endpoint and frontend action to trigger a real-time price fetch and portfolio snapshot update.
3. **UI Enhancements:** Display of current Unit Price, Current Value, and Gain/Loss (both absolute and percentage) in the holdings table, along with manual price update actions.

---

## 2. Goals
- Display real-time current market valuation and gain/loss statistics for holdings.
- Fetch current prices automatically from a public stock price provider without requiring complex api-keys.
- Provide interactive controls in the UI to refresh prices and manually update individual prices.
- Maintain full audit log records for any price updates.

---

## 3. Non-Goals
- Real-time streaming WebSocket price updates (pull-based is sufficient).
- Tracking transaction history/orders (avg_cost/quantity remains the source of truth).
- Historical charts for individual holdings.

---

## 4. API & Backend Changes

### 4.1. `HoldingResponse` Updates
We will modify the `HoldingResponse` schema in `app/investing/schemas.py` to include current price and valuation fields:
- `current_price: Decimal | None` (the latest unit price on or before today, falling back to `avg_cost` if none exists)
- `current_value: Decimal | None` (calculated as `quantity * current_price`)
- `gain_loss: Decimal | None` (`current_value - book_value`)
- `gain_loss_pct: Decimal | None` (`(gain_loss / book_value) * 100`)

To keep DB models clean, these will be computed fields populated in the router by joining the latest price from the `HoldingPrice` table.

### 4.2. On-Demand Fetch & Refresh Endpoint
- **Endpoint:** `POST /v1/investing/prices/refresh`
- **Behavior:**
  1. Identifies all unique symbols among active holdings in the workspace.
  2. Routes listed stocks/ETFs to Yahoo Finance
     (`https://query1.finance.yahoo.com/v8/finance/chart/{symbol}`) and Indian mutual funds to the
     official AMFI daily NAV feed.
  3. Extracts the latest `regularMarketPrice` from the response.
  4. Upserts the retrieved price into the `HoldingPrice` table for the current date.
  5. Forces a recreation of today's `PortfolioSnapshot` so the dashboard and performance cards are updated instantly.
  6. Emits a `holding_prices_submitted` audit log event.
  7. Returns a summary of updated symbols.

### 4.3. Manual Single-Holding Price Update
- Existing endpoint `POST /v1/investing/prices` allows bulk submissions. We will use this from the UI for manual updates of individual holdings by sending a single-item list.

---

## 5. Frontend UI Changes (`InvestingPage.tsx`)

### 5.1. Table Enhancements
We will update the Holdings table columns:
| Symbol | Account | Currency | Qty | Avg Cost | Book Value | Unit Price | Current Value | Gain / Loss | Actions |
|---|---|---|---|---|---|---|---|---|---|
| AAPL | Brokerage | USD | 10.0 | $150.00 | $1,500.00 | $180.00 | $1,800.00 | +$300.00 (+20.0%) | [Edit Price] [Delete] |

- Negative gain/loss will be styled in red; positive in green.
- The footer will display both Total Book Cost and Total Current Value (if single currency, or converted if reporting currency is configured).

### 5.2. New Actions
1. **"Refresh Prices" Button:** A button at the top of the holdings section to trigger the `POST /v1/investing/prices/refresh` API, showing a loading indicator during the process.
2. **"Update Price" Button:** An inline button/modal for each holding row allowing the user to type in a new price manually.

---

## 6. Security & Error Handling
- **API Call Failures:** If Yahoo Finance is unreachable or returns a 404/500 for a symbol, the system will log the failure, skip that symbol, and proceed with other symbols without failing the entire request.
- **Tenant Isolation:** Stock price fetching and snapshot regeneration will only run for holdings within the current workspace context.
- **Audit Logging:** Any manual price change or automatic ingestion will emit standard structured audit events.
