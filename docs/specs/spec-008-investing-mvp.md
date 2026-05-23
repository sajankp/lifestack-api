# Feature Spec: Investing Module MVP
**Status:** Approved
**Spec ID:** 008

## 1. Overview
Investing MVP introduces workspace-scoped holdings and manual portfolio tracking to complete the core Lifestack triad (todo, spending, investing).

## 2. Goals
- Track holdings per workspace.
- Track cash balance snapshots.
- Provide a minimal portfolio summary endpoint.

## 3. Out of Scope
- Broker integrations.
- Real-time market data ingestion.
- Tax lot accounting.
- Rebalancing automation.
- Investing transaction ledger (deferred to V2).

## 4. Module Shape
Use standard module pattern:
- `app/investing/models.py`
- `app/investing/schemas.py`
- `app/investing/repository.py`
- `app/investing/service.py`
- `app/investing/router.py`

## 5. Data Model
### 5.1 Holding
- `id` int PK
- `public_id` UUID unique
- `workspace_id` int FK
- `user_id` int FK (creator metadata)
- `symbol` string
- `account_name` string default `"primary"`
- `quantity` numeric (`NUMERIC(18, 8)`)
- `avg_cost` numeric (`NUMERIC(12, 2)`)
- `currency` string (required, no default)
- `created_at`, `updated_at`

Constraints:
- unique `(workspace_id, symbol, account_name)` for stage 1

### 5.2 CashBalance
- `id` int PK
- `public_id` UUID unique
- `workspace_id` int FK
- `user_id` int FK (creator metadata)
- `account_name` string
- `balance` numeric
- `currency` string (required, no default)
- `as_of` timestamp

## 6. API Surface
- `GET /v1/investing/holdings`
- `POST /v1/investing/holdings`
- `PATCH /v1/investing/holdings/{public_id}`
- `DELETE /v1/investing/holdings/{public_id}`
- `GET /v1/investing/cash-balances`
- `POST /v1/investing/cash-balances`
- `PATCH /v1/investing/cash-balances/{public_id}`
- `DELETE /v1/investing/cash-balances/{public_id}`
- `GET /v1/investing/summary`

`GET /v1/investing/summary` response includes:
- `portfolio_value`
- `holdings_count`
- `cash_total`
- `currency_breakdown`
- `daily_change` (`null` in V1)

## 7. Auditing
- Holding create/update/delete emits audit events (`module=investing`).

## 8. Test Plan
- Integration CRUD tests for holdings.
- Workspace isolation tests.
- Summary correctness test for aggregated values.

## 9. Acceptance Criteria
- Endpoints available under `/v1/investing`.
- All repository operations scoped by `workspace_id`.
- RFC 7807 errors for not-found/validation conflicts.
- Audit events emitted for holding mutations.
- Cash-balance CRUD is available and workspace-scoped.
- Financial precision follows documented numeric scales.
- **Serialization Strictness:** Pydantic schemas MUST enforce `Decimal` to `str` serialization over the wire (`model_config = ConfigDict(json_encoders={Decimal: str})` or equivalent Pydantic v2 approach) to prevent frontend JavaScript floating-point rounding errors.

## 10. Observability Hooks
- Emit structured logs for holdings/cash-balance mutations.
- Emit counters for investing CRUD outcomes and summary requests.
- Emit trace spans for investing repository and summary operations.
