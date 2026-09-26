# Spec-097: Unified Money Flow — Activity Feed Endpoint & Dividend Credit Flexibility

**Created:** 2026-09-26
**Status:** Approved (implementation)
**Scope:** API (`app/finance`, `app/investing`), Web (new Money Flow page, TransferModal FX fix, Portfolio cleanup)
**Depends on:** spec-048 reconciliation, spec-049 transfers, spec-050 one-currency, spec-073 dividends
**Branch:** `feat/unified-money-flow` (single branch across api + web, deployed together)

---

## 1. Problem

The current UX distributes money-related information across 3 pages with 14 tabs. A single
transfer (e.g., ICICI → Groww) appears in 4 different views: Spending Account Activity,
Investing Cash Transfers, Investing Cash Balances, and Net Worth. Even the sole user finds
this confusing.

Additionally:
- **Dividends** can only credit brokerage accounts, but Indian-market dividends often land
  directly in bank accounts (e.g., Groww dividends → linked ICICI bank).
- **FX rate input** expects `1/rate` (e.g., 0.01053 for INR→USD) when users think in
  "95 INR per dollar" — a frontend-only fix.
- **No unified endpoint** exists to fetch all money events (transactions, transfers, orders,
  dividends) in one paginated, chronologically-sorted stream.

## 2. Solution

### A. Unified Activity Feed Endpoint (API)

New endpoint that merges all money-movement events into one paginated stream:

```
GET /v1/finance/activity-feed
  ?account_id=<uuid>          # optional: filter to one account
  &event_types=spend,transfer,order,dividend  # optional: CSV filter
  &from_date=2026-01-01       # optional
  &to_date=2026-07-31         # optional
  &limit=50                   # default 50, max 200
  &offset=0
```

**Response:**
```json
{
  "items": [
    {
      "id": "uuid",
      "event_type": "spend" | "transfer" | "order" | "dividend",
      "date": "2026-07-07T00:00:00Z",
      "description": "Grocery",
      "amount": "-57.00",
      "currency": "INR",
      "account_id": "uuid",
      "account_name": "ICICI",
      "account_type": "wallet",
      "counterpart_account_id": null,
      "counterpart_account_name": null,
      "category_name": "Grocery",
      "category_color": "#22c55e",
      "category_emoji": "🛒",
      "symbol": null,
      "order_type": null,
      "quantity": null,
      "price": null,
      "fx_rate": null,
      "fx_display": null,
      "source_ref": "uuid"
    }
  ],
  "total": 450,
  "limit": 50,
  "offset": 0
}
```

**Event type mapping:**

| Event | Source table | `date` field | `amount` sign | `account_id` | `counterpart_account_id` |
|-------|-------------|-------------|---------------|--------------|--------------------------|
| `spend` (income) | `spending_transactions` | `occurred_at` | positive | transaction account | null |
| `spend` (expense) | `spending_transactions` | `occurred_at` | negative | transaction account | null |
| `transfer` | `capital_transfers` | `occurred_at` | negative (from) | from_account | to_account |
| `transfer` | `capital_transfers` | `occurred_at` | positive (to) | to_account | from_account |
| `order` (buy) | `investing_orders` | `ordered_at` | negative net | order account | null |
| `order` (sell) | `investing_orders` | `ordered_at` | positive net | order account | null |
| `dividend` | `investing_dividends` | `pay_date` | positive net | credit_account (see §B) | null |

**Implementation notes:**
- Each transfer produces **two** activity rows (one per account side) — the from-side with
  negative amount and the to-side with positive amount.
- Pagination is by date descending then by id descending (stable sort).
- The endpoint lives in `app/finance/router.py` and uses a new `ActivityFeedService` in
  `app/finance/service.py` (or a new `app/finance/activity_service.py` to keep size manageable).
- The service queries each source table independently for the date range, merges in Python,
  sorts, and applies limit/offset. At the current scale (~500 total events) this is efficient.
  If scale grows 10x+, a materialized view or UNION ALL query is the optimization path.

### B. Dividend Credit Account Flexibility (API)

Add optional `credit_account_id` to the Dividend model, allowing dividends to credit
either a brokerage account (current behavior) or a spending account (bank/wallet).

**Model change:** Add `credit_account_id: int | None` to `investing_dividends` table.
- When `credit_account_id` is null → current behavior: credits the dividend's `account_id`
  (must be brokerage, existing validation unchanged).
- When `credit_account_id` is set → credits that account instead.
  - If the credit account is brokerage → write a cash balance snapshot (existing `_credit_cash`).
  - If the credit account is spending (wallet/bank) → write a spending transaction with
    type=`income`, category=system "Dividend" category (auto-created if missing), and
    description referencing the symbol.

**Schema changes:**
- `DividendCreate`: add `credit_account_id: uuid.UUID | None = None`
- `DividendResponse`: add `credit_account_id: uuid.UUID | None`, `credit_account_name: str | None`
- `DividendBulkImportRow`: add `credit_account_id: uuid.UUID | None = None`

**Migration:** `0067_dividend_credit_account.py` — add nullable `credit_account_id` column
with FK to `accounts(id, workspace_id)`.

**Validation rules:**
- `credit_account_id` must belong to the same workspace.
- `credit_account_id` currency must match `dividend.currency` (one-currency-per-account rule).
- `account_id` (the holding's brokerage account) remains required and must still be brokerage.

**Retroactivity:** Forward-only. Existing dividends keep `credit_account_id=null` (credits
brokerage account as before). No backfill.

### C. FX Rate Input Fix (Web — TransferModal)

Frontend-only change to `TransferModal.tsx`:
- When source and destination currencies differ, show rate input as:
  `[amount] FROM_CURRENCY = [amount] TO_CURRENCY` (e.g., "95 INR = 1 USD")
- Default direction: source-currency per one unit of target-currency (the natural direction).
- "Flip" button to switch to target-per-source if user prefers.
- Before API submission: if direction is "natural" (source per target), compute
  `api_rate = 1 / user_entered_rate`. The backend formula `gross × rate = converted_gross`
  is unchanged.
- Auto-fetch suggestion from `GET /v1/finance/fx-rates/{from}/{to}` when both accounts
  are selected and currencies differ.
- Display the preview as: "You send ₹95,000 → You receive $1,000.00 (Rate: 95 INR/USD)"

### D. Money Flow Page (Web — new route `/money`)

New unified page replacing the fragmented Spending/Investing/Net Worth navigation for
money tracking. Portfolio (holdings, returns, analytics) stays on its own page.

**Hero section — "My Money" cards:**
- Spending Cash total (sum of wallet/bank/card balances from ledger)
- Investing Cash total (sum of brokerage cash from snapshots)
- Total Net Worth (spending + investing cash + holdings market value)
- This Month: Spent / Income / Net

**Account Map:**
Three-column visual layout:
- Left: Cash accounts (wallet, bank, card) with balances
- Middle: Brokerage accounts with cash balances
- Right: Link to Portfolio page with total holdings value
- Recent transfer arrows between accounts (last 30 days)
- Click any account → Account Detail view

**Activity Feed tab:**
Consumes `GET /v1/finance/activity-feed` with filters:
- Event type toggle (spend / transfer / order / dividend)
- Account filter
- Date range
- Search

**Account Detail view** (`/money/account/:id`):
- Account hero: name, type, current balance, period stats
- Balance sparkline (ledger-based for spending accounts, snapshot-based for brokerage)
- Activity log for just that account (same endpoint with `account_id` filter)
- Running balance column

### E. Portfolio Page Cleanup (Web)

Rename "Investing" → "Portfolio" in nav. Remove:
- Cash tab (cash balances, transfers, dividends → moved to Money Flow)
- Cash-related hero card

Keep:
- Holdings tab (the large holdings table)
- Orders tab (renamed "Trade History")
- Analytics tab (exposure, concentration, overlap)
- Performance Returns panel
- Place Order modal

### F. Navigation Restructure (Web)

```
MONEY section in sidebar:
  Money Flow  (NEW — /money)
  Portfolio   (renamed Investing — /portfolio, redirects from /investing)
  Net Worth   (unchanged — /net-worth)

Remove "Spending" from sidebar (absorbed into Money Flow).
Redirect /spending/* → /money with appropriate tab/filter.
```

Budgets, KPIs, and Recurring Rules become sub-tabs within Money Flow under a
"Planning" section, or remain accessible via the Money Flow page's tab bar.

## 3. Out of Scope

- Backend UNION ALL / materialized view optimization for the activity feed (deferred
  until scale justifies it).
- Per-(account, currency) reconciliation changes.
- Retroactive backfill of `credit_account_id` on existing dividends.
- Mobile-specific responsive breakpoints (existing responsive behavior carries over).
- Changes to the MCP server or weekly summaries.

## 4. Test Plan

**API:**
- Activity feed endpoint: filter by account, event_types, date range; pagination; sort order.
- Dividend with `credit_account_id` pointing to bank account → spending transaction created.
- Dividend with `credit_account_id` null → brokerage snapshot created (existing behavior).
- Dividend `credit_account_id` currency mismatch → validation error.
- Transfer arithmetic and FX rate storage unchanged (regression).
- Migration up/down.

**Web:**
- Money Flow page renders hero cards with correct totals.
- Account Map shows all accounts grouped by type.
- Activity Feed displays merged events, filters work.
- Account Detail shows correct running balance.
- TransferModal FX rate: entering "95" as INR/USD → API receives 0.01052... → net preview correct.
- TransferModal FX rate flip button works.
- Portfolio page no longer shows Cash tab.
- Old /spending and /investing routes redirect correctly.
- Coverage gates: api ≥ 80%, web ≥ 70%.

## 5. Migration Safety

- `0067_dividend_credit_account.py`: nullable column addition — safe, no data mutation.
- Downgrade: drop the column.
- No existing data modified.
