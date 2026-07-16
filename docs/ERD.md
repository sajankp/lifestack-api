# Lifestack ER Diagrams

These diagrams are split by domain so each module is readable on its own.
Shared links to `USERS` and `WORKSPACES` are repeated where they help explain
ownership and tenant boundaries.

## Auth

```mermaid
erDiagram
    USERS {
        int id PK
        uuid public_id UK
        string email UK
        string username UK
        string hashed_password
        boolean is_active
        datetime created_at
        datetime updated_at
    }

    AUTH_SESSIONS {
        int id PK
        uuid public_id UK
        int user_id FK
        string sid UK
        datetime expires_at
        datetime revoked_at
        string current_token_hash
        string previous_token_hash
        datetime rotated_at
        datetime last_seen_at
        datetime created_at
    }

    PASSWORD_RESET_TOKENS {
        int id PK
        int user_id FK
        string token_hash UK
        datetime expires_at
        datetime used_at
        datetime created_at
    }

    USERS ||--o{ AUTH_SESSIONS : authenticates
    USERS ||--o{ PASSWORD_RESET_TOKENS : requests
```

## Workspace Core

```mermaid
erDiagram
    USERS {
        int id PK
        uuid public_id UK
        string email UK
        string username UK
    }

    WORKSPACES {
        int id PK
        uuid public_id UK
        string name
        string description
        boolean is_active
        datetime created_at
        datetime updated_at
    }

    WORKSPACE_MEMBERSHIPS {
        int id PK
        int workspace_id FK
        int user_id FK
        string role
        datetime created_at
    }

    AUDIT_LOGS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int actor_id FK
        string action
        string module
        string entity_type
        int entity_id
        json details
        datetime timestamp
    }

    USERS ||--o{ WORKSPACE_MEMBERSHIPS : joins
    WORKSPACES ||--o{ WORKSPACE_MEMBERSHIPS : includes
    WORKSPACES ||--o{ AUDIT_LOGS : records
    USERS ||--o{ AUDIT_LOGS : acts_as_actor
```

## Todo

```mermaid
erDiagram
    USERS {
        int id PK
        uuid public_id UK
        string email UK
        string username UK
    }

    WORKSPACES {
        int id PK
        uuid public_id UK
        string name
    }

    TODOS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int user_id FK
        int parent_id FK
        string title
        string description
        datetime due_date
        string priority
        boolean completed
        string system_key
        datetime reminded_at
        datetime created_at
        datetime updated_at
    }

    RECURRING_TODO_RULES {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int user_id FK
        string title
        string description
        string priority
        string frequency
        int interval
        date anchor_date
        time due_time
        string timezone
        date next_due_date
        date end_date
        boolean is_active
        datetime last_generated_at
        string monthly_mode
        int by_weekday
        int by_ordinal
        datetime created_at
        datetime updated_at
    }

    USERS ||--o{ TODOS : owns
    WORKSPACES ||--o{ TODOS : scopes
    TODOS ||--o{ TODOS : "parent_id (one level, ON DELETE CASCADE)"
    USERS ||--o{ RECURRING_TODO_RULES : configures
    WORKSPACES ||--o{ RECURRING_TODO_RULES : scopes
```

## Health (spec-069)

Medications + weight only in v1 (owner decision D9) — sleep/workouts/vitals/labs are later slices,
same table patterns. Dose slots are never stored: they're derived on read from
(frequency, interval, anchor_date, days_of_week, times, timezone, end_date) via
`app/health/schedule.py`, reusing the `RecurringTodoRule` recurrence vocabulary
(`core/recurrence.py::advance_due_date` semantics for daily/monthly, with
`days_of_week` as the one weekly extension). Every table carries the
`source_type`/`source_ref`/`source_import_id` triplet (the same pattern as
`spending_transactions`) so device sync/extraction can slot in later without
migration churn — `source_type` is always `"manual"` in v1.

```mermaid
erDiagram
    MEDICATIONS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int user_id FK
        string name
        string dose_text
        string refill_note
        string frequency
        int interval
        json days_of_week
        date anchor_date
        date end_date
        string timezone
        json times
        boolean is_active
        boolean reminders_enabled
        datetime last_reminded_slot
        string source_type
        string source_ref
        int source_import_id FK
        datetime created_at
        datetime updated_at
    }

    MEDICATION_EVENTS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int user_id FK
        int medication_id FK
        datetime scheduled_for
        string status
        datetime logged_at
        string note
        string source_type
        string source_ref
        int source_import_id FK
        datetime created_at
        datetime updated_at
    }

    WEIGHT_ENTRIES {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int user_id FK
        datetime measured_at
        decimal weight_kg
        string note
        string source_type
        string source_ref
        int source_import_id FK
        datetime created_at
        datetime updated_at
    }

    USERS ||--o{ MEDICATIONS : owns
    WORKSPACES ||--o{ MEDICATIONS : scopes
    MEDICATIONS ||--o{ MEDICATION_EVENTS : "ON DELETE CASCADE"
    USERS ||--o{ WEIGHT_ENTRIES : owns
    WORKSPACES ||--o{ WEIGHT_ENTRIES : scopes
```

A dose slot's status is computed on read (never stored): `taken`/`skipped` if a
`MEDICATION_EVENTS` row exists for that `(medication_id, scheduled_for)` slot
(unique constraint enforces one answer per slot), `pending` if in the future,
`missed` if more than `HEALTH_DOSE_GRACE_HOURS` (default 4) past with no
event. Logging late flips a missed slot to taken/skipped — no reconciliation
job needed.

## Spending

```mermaid
erDiagram
    USERS {
        int id PK
        uuid public_id UK
        string email UK
        string username UK
    }

    WORKSPACES {
        int id PK
        uuid public_id UK
        string name
    }

    CATEGORY_GROUPS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        string name
        string normalized_name
        string color
        string icon
        datetime created_at
        datetime updated_at
    }

    SPENDING_CATEGORIES {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int category_group_id FK
        string name
        string normalized_name
        boolean is_system
        string color
        string icon
        datetime created_at
        datetime updated_at
    }

    SPENDING_TRANSACTIONS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int user_id FK
        int category_id FK
        int account_id FK
        int recurring_transaction_id FK
        decimal amount
        string type
        datetime occurred_at
        string description
        string wallet_name
        string labels
        string source_type
        string source_ref
        int source_import_id FK
        datetime created_at
        datetime updated_at
    }

    SPENDING_BUDGETS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int category_id FK
        int category_group_id FK
        decimal amount
        date start_month
        date end_month
        string source_type
        string source_ref
        int source_import_id FK
        datetime created_at
        datetime updated_at
    }

    RECURRING_TRANSACTIONS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int user_id FK
        int category_id FK
        decimal amount
        string type
        string description
        string frequency
        int interval
        date anchor_date
        date next_due_date
        date end_date
        boolean is_active
        datetime last_generated_at
        string monthly_mode
        int by_weekday
        int by_ordinal
        datetime created_at
        datetime updated_at
    }

    WORKSPACES ||--o{ CATEGORY_GROUPS : scopes
    WORKSPACES ||--o{ SPENDING_CATEGORIES : scopes
    CATEGORY_GROUPS ||--o{ SPENDING_CATEGORIES : groups
    WORKSPACES ||--o{ SPENDING_TRANSACTIONS : scopes
    USERS ||--o{ SPENDING_TRANSACTIONS : records
    SPENDING_CATEGORIES ||--o{ SPENDING_TRANSACTIONS : categorizes
    WORKSPACES ||--o{ SPENDING_BUDGETS : scopes
    SPENDING_CATEGORIES ||--o{ SPENDING_BUDGETS : budgets
    CATEGORY_GROUPS ||--o{ SPENDING_BUDGETS : "group budgets"
    WORKSPACES ||--o{ RECURRING_TRANSACTIONS : scopes
    USERS ||--o{ RECURRING_TRANSACTIONS : configures
    SPENDING_CATEGORIES ||--o{ RECURRING_TRANSACTIONS : categorizes
    RECURRING_TRANSACTIONS ||--o{ SPENDING_TRANSACTIONS : generates

    FINANCIAL_KPIS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        string name
        enum metric_type
        enum evaluation_window
        int category_id FK
        int category_group_id FK
        int account_id FK
        string currency_code FK
        decimal target_value
        enum target_direction
        string display_format
        boolean is_active
        datetime created_at
        datetime updated_at
    }

    WORKSPACES ||--o{ FINANCIAL_KPIS : scopes
```

A budget scopes to exactly one of `category_id` or `category_group_id` (DB check
constraint `ck_budget_scope`), and `start_month`/`end_month` (nullable = ongoing)
replaced the old single `month_start` field so budgets can be date-ranged
(spec-064). Category delete/merge (spec-062) reassigns transactions and
recurring rules to a target category before removing the source.

## Finance

```mermaid
erDiagram
    USERS {
        int id PK
        uuid public_id UK
        string email UK
        string username UK
    }

    WORKSPACES {
        int id PK
        uuid public_id UK
        string name
    }

    CURRENCIES {
        string code PK
        string name
        string symbol
        int minor_unit
        boolean is_active
        datetime created_at
        datetime updated_at
    }

    WORKSPACE_CURRENCIES {
        int id PK
        int workspace_id FK
        string currency_code FK
        datetime created_at
    }

    ACCOUNTS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        string name
        string account_type
        string default_currency_code FK
        boolean is_active
        datetime created_at
        datetime updated_at
    }

    WORKSPACE_FINANCE_SETTINGS {
        int id PK
        int workspace_id FK
        string reporting_currency_code FK
        string currency_display_preference
        decimal lookthrough_min_weight_pct
        int default_spending_account_id FK
        datetime created_at
        datetime updated_at
    }

    USER_FINANCE_SETTINGS {
        int id PK
        int workspace_id FK
        int user_id FK
        string reporting_currency_override_code FK
        string currency_display_preference_override
        datetime created_at
        datetime updated_at
    }

    FX_RATES {
        int id PK
        string base_currency_code FK
        string quote_currency_code FK
        decimal rate
        datetime as_of
        datetime fetched_at
        string source
        datetime created_at
        datetime updated_at
    }

    CAPITAL_TRANSFERS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int actor_id FK
        string from_module
        string to_module
        int from_account_id FK
        int to_account_id FK
        string from_currency_code FK
        string to_currency_code FK
        decimal gross_amount
        decimal fx_rate_used
        decimal fx_fee_amount
        decimal platform_fee_amount
        decimal tax_amount
        decimal net_amount_received
        datetime occurred_at
        string notes
        string source_type
        string source_ref
        int source_import_id FK
        datetime created_at
        datetime updated_at
    }

    WORKSPACES ||--o{ WORKSPACE_CURRENCIES : allows
    CURRENCIES ||--o{ WORKSPACE_CURRENCIES : listed_in
    WORKSPACES ||--o{ ACCOUNTS : owns
    CURRENCIES ||--o{ ACCOUNTS : default_currency
    WORKSPACES ||--o| WORKSPACE_FINANCE_SETTINGS : config
    CURRENCIES ||--o| WORKSPACE_FINANCE_SETTINGS : reporting_currency
    WORKSPACES ||--o{ USER_FINANCE_SETTINGS : user_overrides
    USERS ||--o{ USER_FINANCE_SETTINGS : configures
    CURRENCIES ||--o{ USER_FINANCE_SETTINGS : reporting_override
    CURRENCIES ||--o{ FX_RATES : base_currency
    CURRENCIES ||--o{ FX_RATES : quote_currency
    NET_WORTH_SNAPSHOTS {
        int id PK
        int workspace_id FK
        date snapshot_date
        string reporting_currency
        decimal holdings_value
        decimal investing_cash
        decimal spending_cash
        decimal total_net_worth
        json fx_rates_used
        datetime created_at
    }

    WORKSPACES ||--o{ CAPITAL_TRANSFERS : scopes
    USERS ||--o{ CAPITAL_TRANSFERS : initiates
    ACCOUNTS ||--o{ CAPITAL_TRANSFERS : from_account
    ACCOUNTS ||--o{ CAPITAL_TRANSFERS : to_account
    CURRENCIES ||--o{ CAPITAL_TRANSFERS : from_currency
    CURRENCIES ||--o{ CAPITAL_TRANSFERS : to_currency
    WORKSPACES ||--o{ NET_WORTH_SNAPSHOTS : "one per day"
```

`net_worth_snapshots` is unique on `(workspace_id, snapshot_date)` and is
upserted two ways (spec-065): opportunistically on every `GET /finance/net-worth`
read for today, and by the daily `net_worth_snapshot_job` cron. Both paths
compute holdings/cash live via `InvestingSummaryService` rather than reading
the (investing-only, cache-prone) `portfolio_snapshots` table, so the job
doesn't depend on a dashboard visit having happened that day.

## Finance / Wallet Reconciliation (spec-078)

Statement matching is metadata-only — these tables never mutate `spending_transactions`,
`capital_transfers`, or `investing_cash_balances`. `reconciled_through` is informational,
not a lock.

```mermaid
erDiagram
    ACCOUNTS {
        int id PK
        string name
    }

    WORKSPACES {
        int id PK
    }

    ACCOUNT_STATEMENTS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int account_id FK
        date period_start
        date period_end
        decimal closing_balance
        string currency_code FK
        int import_batch_id FK
        date reconciled_through
        datetime created_at
        datetime updated_at
    }

    STATEMENT_LINES {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int account_id FK
        int statement_id FK
        date occurred_at
        string description
        decimal amount
        decimal balance
        string external_ref UK
        int matched_transaction_id FK
        int matched_transfer_id FK
        enum matched_transfer_leg
        datetime matched_at
        datetime created_at
        datetime updated_at
    }

    WORKSPACES ||--o{ ACCOUNT_STATEMENTS : scopes
    ACCOUNTS ||--o{ ACCOUNT_STATEMENTS : has_statements
    ACCOUNT_STATEMENTS ||--o{ STATEMENT_LINES : contains
    ACCOUNTS ||--o{ STATEMENT_LINES : scopes
```

## Investing

```mermaid
erDiagram
    USERS {
        int id PK
        uuid public_id UK
        string email UK
        string username UK
    }

    WORKSPACES {
        int id PK
        uuid public_id UK
        string name
    }

    INVESTING_COMPANIES {
        int id PK
        uuid public_id UK
        int workspace_id FK
        string name
        string ticker
        string isin
        string sector
        string country_code
        datetime created_at
        datetime updated_at
    }

    INVESTING_INSTRUMENTS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        string symbol
        string name
        string instrument_type
        string isin
        string exchange
        string provider_key
        int company_id FK
        boolean is_active
        datetime created_at
        datetime updated_at
    }

    INVESTING_INSTRUMENT_CONSTITUENTS {
        int id PK
        int instrument_id FK
        int constituent_company_id FK
        decimal weight
        date as_of_date
        string source
        datetime fetched_at
        datetime created_at
    }

    INVESTING_HOLDINGS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int user_id FK
        int instrument_id FK
        string symbol
        int account_id FK
        decimal quantity
        decimal avg_cost
        string currency
        string source_type
        string source_ref
        int source_import_id FK
        datetime created_at
        datetime updated_at
    }

    INVESTING_CASH_BALANCES {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int user_id FK
        int account_id FK
        decimal balance
        string currency
        datetime as_of
        string source_type
        string source_ref
        int source_import_id FK
        string trigger_type
        uuid trigger_ref
        datetime created_at
        datetime updated_at
    }

    INVESTING_ORDERS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int user_id FK
        int account_id FK
        string order_type
        string symbol
        int instrument_id FK
        decimal quantity
        decimal price_per_unit
        decimal gross_amount
        decimal brokerage_fee
        decimal tax_amount
        decimal other_fees
        decimal net_amount
        string currency
        string exchange_name
        datetime occurred_at
        string notes
        decimal realized_gain_loss
        decimal avg_cost_at_sale
        string source_type
        string source_ref
        int source_import_id FK
        datetime created_at
        datetime updated_at
    }

    INVESTING_ORDER_LOTS {
        int id PK
        int workspace_id FK
        int holding_id FK
        int buy_order_id FK
        int corporate_action_id FK
        decimal original_quantity
        decimal remaining_quantity
        decimal cost_per_unit
        datetime acquired_at
        datetime created_at
    }

    INVESTING_LOT_CONSUMPTIONS {
        int id PK
        int sell_order_id FK
        int lot_id FK
        decimal quantity_consumed
        decimal cost_per_unit
        datetime created_at
    }

    INVESTING_CORPORATE_ACTIONS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int user_id FK
        int account_id FK
        string symbol
        string action_type
        decimal ratio_base
        decimal ratio_quote
        date ex_date
        string notes
        datetime created_at
        datetime updated_at
    }

    HOLDING_PRICES {
        int id PK
        int workspace_id FK
        int holding_id FK
        date price_date
        decimal unit_price
        string source
        datetime created_at
    }

    PORTFOLIO_SNAPSHOTS {
        int id PK
        int workspace_id FK
        date snapshot_date
        decimal total_value
        decimal total_cost
        decimal holdings_value
        decimal cash_value
        string currency_code
        json fx_rates_used
        datetime created_at
    }

    INVESTING_HOLDING_VERIFICATIONS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int account_id FK
        int source_import_id FK
        string source
        date statement_date
        int match_count
        int quantity_drift_count
        int missing_in_lifestack_count
        int missing_at_depository_count
        json report_json
        datetime created_at
    }

    WORKSPACES ||--o{ INVESTING_COMPANIES : scopes
    WORKSPACES ||--o{ INVESTING_INSTRUMENTS : scopes
    INVESTING_COMPANIES ||--o{ INVESTING_INSTRUMENTS : issuer
    INVESTING_INSTRUMENTS ||--o{ INVESTING_INSTRUMENT_CONSTITUENTS : has_constituents
    INVESTING_COMPANIES ||--o{ INVESTING_INSTRUMENT_CONSTITUENTS : constituent
    WORKSPACES ||--o{ INVESTING_HOLDINGS : scopes
    USERS ||--o{ INVESTING_HOLDINGS : owns
    INVESTING_INSTRUMENTS ||--o{ INVESTING_HOLDINGS : linked_instrument
    WORKSPACES ||--o{ INVESTING_CASH_BALANCES : scopes
    USERS ||--o{ INVESTING_CASH_BALANCES : owns
    WORKSPACES ||--o{ INVESTING_ORDERS : scopes
    USERS ||--o{ INVESTING_ORDERS : places
    INVESTING_INSTRUMENTS ||--o{ INVESTING_ORDERS : linked_instrument
    ACCOUNTS ||--o{ INVESTING_HOLDINGS : holds
    ACCOUNTS ||--o{ INVESTING_CASH_BALANCES : holds
    ACCOUNTS ||--o{ INVESTING_ORDERS : traded_in
    WORKSPACES ||--o{ HOLDING_PRICES : scopes
    INVESTING_HOLDINGS ||--o{ HOLDING_PRICES : has_prices
    WORKSPACES ||--o{ PORTFOLIO_SNAPSHOTS : scopes
    INVESTING_HOLDINGS ||--o{ INVESTING_ORDER_LOTS : fifo_lots
    INVESTING_ORDERS ||--o{ INVESTING_ORDER_LOTS : buy_creates
    INVESTING_CORPORATE_ACTIONS ||--o{ INVESTING_ORDER_LOTS : bonus_creates
    INVESTING_ORDERS ||--o{ INVESTING_LOT_CONSUMPTIONS : sell_consumes
    INVESTING_ORDER_LOTS ||--o{ INVESTING_LOT_CONSUMPTIONS : consumed_from
    WORKSPACES ||--o{ INVESTING_CORPORATE_ACTIONS : scopes
    ACCOUNTS ||--o{ INVESTING_CORPORATE_ACTIONS : applies_to
    WORKSPACES ||--o{ INVESTING_HOLDING_VERIFICATIONS : scopes
    ACCOUNTS ||--o{ INVESTING_HOLDING_VERIFICATIONS : verifies
    IMPORT_BATCHES ||--o{ INVESTING_HOLDING_VERIFICATIONS : produces

    REFERENCE_SECURITIES {
        int id PK
        uuid public_id UK
        string isin UK
        string ticker
        string exchange
        string amfi_code UK
        enum security_type
        string name
        string[] aliases
        string country_code
        string source
        datetime fetched_at
    }

    INVESTING_DIVIDENDS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int user_id FK
        int account_id FK
        int holding_id FK
        string symbol
        enum income_type
        decimal gross_amount
        decimal tax_withheld
        decimal net_amount
        string currency FK
        date pay_date
        string external_ref UK
        string notes
        datetime created_at
        datetime updated_at
    }

    WORKSPACES ||--o{ INVESTING_DIVIDENDS : scopes
    USERS ||--o{ INVESTING_DIVIDENDS : records
    ACCOUNTS ||--o{ INVESTING_DIVIDENDS : credited_to
    INVESTING_HOLDINGS ||--o{ INVESTING_DIVIDENDS : attributed_to
```

## Notifications & Summaries

```mermaid
erDiagram
    USERS {
        int id PK
        uuid public_id UK
    }

    WORKSPACES {
        int id PK
        uuid public_id UK
    }

    NOTIFICATIONS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int user_id FK
        string category
        string severity
        string title
        string body
        string module
        string entity_type
        uuid entity_public_id
        boolean is_read
        datetime read_at
        datetime created_at
    }

    NOTIFICATION_PREFERENCES {
        int id PK
        int workspace_id FK
        int user_id FK
        string category
        boolean channel_in_app
        boolean channel_email
        boolean channel_push
        boolean is_muted
        datetime created_at
        datetime updated_at
    }

    NOTIFICATION_DELIVERIES {
        int id PK
        int notification_id FK
        string channel
        string status
        datetime attempted_at
        string error_detail
        datetime created_at
    }

    PUSH_SUBSCRIPTIONS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int user_id FK
        string endpoint UK
        string p256dh
        string auth
        string device_label
        boolean is_active
        datetime last_success_at
        datetime last_failure_at
        datetime created_at
        datetime updated_at
    }

    WEEKLY_SUMMARIES {
        int id PK
        uuid public_id UK
        int workspace_id FK
        date week_start
        date week_end
        datetime generated_at
        json todo_summary
        json spending_summary
        json investing_summary
        json highlights
        datetime created_at
    }

    WORKSPACES ||--o{ NOTIFICATIONS : scopes
    USERS ||--o{ NOTIFICATIONS : receives
    WORKSPACES ||--o{ NOTIFICATION_PREFERENCES : scopes
    USERS ||--o{ NOTIFICATION_PREFERENCES : configures
    NOTIFICATIONS ||--o{ NOTIFICATION_DELIVERIES : delivered_via
    WORKSPACES ||--o{ PUSH_SUBSCRIPTIONS : scopes
    USERS ||--o{ PUSH_SUBSCRIPTIONS : subscribes
    WORKSPACES ||--o{ WEEKLY_SUMMARIES : scopes
```

## Imports & Exports

```mermaid
erDiagram
    USERS {
        int id PK
        uuid public_id UK
        string email UK
        string username UK
    }

    WORKSPACES {
        int id PK
        uuid public_id UK
        string name
    }

    IMPORT_BATCHES {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int user_id FK
        string module
        string status
        string filename
        string content_type
        int file_size_bytes
        string file_sha256
        string storage_backend
        string storage_key
        int total_rows
        int valid_rows
        int error_rows
        string commit_error
        json extra_json
        datetime started_at
        datetime validated_at
        datetime committed_at
        datetime created_at
        datetime updated_at
    }

    IMPORT_ERRORS {
        int id PK
        int import_batch_id FK
        int row_number
        string field_name
        string error_code
        string message
        string raw_value
    }

    IMPORT_PREVIEW_ROWS {
        int id PK
        int import_batch_id FK
        int row_number
        json payload_json
    }

    EXPORTS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int requested_by FK
        string format
        int schema_version
        json scope
        string status
        string storage_key
        bytes artifact_blob
        string artifact_mime_type
        string artifact_filename
        string error_message
        datetime created_at
        datetime completed_at
    }

    WORKSPACES ||--o{ IMPORT_BATCHES : scopes
    USERS ||--o{ IMPORT_BATCHES : uploads
    IMPORT_BATCHES ||--o{ IMPORT_ERRORS : has_errors
    IMPORT_BATCHES ||--o{ IMPORT_PREVIEW_ROWS : previews
    WORKSPACES ||--o{ EXPORTS : scopes
    USERS ||--o{ EXPORTS : requests
```

## Notes

- `workspace_id` is the tenant boundary for every business table.
- `public_id` is the external identifier exposed to API clients.
- `auth_sessions` stores refresh-token rotation hashes so login, refresh, and workspace selection can keep replay protection consistent.
- `spending_transactions` and `spending_budgets` enforce the `category_id + workspace_id` pairing with composite foreign keys in the database, even though Mermaid shows them as simple relationships.
- `workspace_finance_settings` is effectively one row per workspace; `user_finance_settings` stores per-user display/reporting overrides inside that workspace.
- `workspace_currencies` is the workspace-level allow-list for currencies.
- `fx_rates` stores currency pairs with both a base and quote currency reference.
- `capital_transfers` connects the spending and investing modules through accounts and currencies.
- `investing_holdings`, `investing_cash_balances`, and `investing_orders` enforce tenant-safe `account_id` relationships to the `accounts` table.
- `investing_orders` records each buy/sell trade against a brokerage account. Placing an order automatically writes a new `investing_cash_balances` row (`trigger_type="order"`) and replays the holding's FIFO lots (`investing_order_lots` / `investing_lot_consumptions`, spec-044) to recompute `avg_cost`; a transfer into an investing account writes a balance row with `trigger_type="transfer"`. `trigger_ref` points back to the order/transfer `public_id`.
- `investing_order_lots` rows are recomputed (deleted and recreated) on every holding replay. Exactly one of `buy_order_id`/`corporate_action_id` is set: a buy creates a lot; a bonus issue (spec-051) creates a zero-cost lot; a split/reverse split scales existing lots in place. `investing_lot_consumptions` is the audit trail of which lots each sell drew from.
- `investing_corporate_actions` (spec-051) are replayed alongside orders by `ex_date` and never touch `investing_cash_balances` — cash-neutral by construction.
- `notifications` fan out through `notification_deliveries` per channel; `push_subscriptions` (spec-052) stores Web Push endpoints as capability secrets (never logged, returned truncated) and is deactivated automatically on permanent push-service rejection.
- `workspace_finance_settings.default_spending_account_id` (spec-054) is the create-path fallback account for spending transactions; cleared automatically when that account is deactivated.
- `import_batches.extra_json` (spec-056) carries module-specific batch context, e.g. the CAMS CAS `target_account_id` and advisory preview signals.
- `import_batches` tracks the life of bulk data uploads for transaction and holdings modules.
- `exports` handles the user-driven data exports lifecycle.
- `reference_securities` (spec-083, migration 0053) is a **global** (no `workspace_id`) security-master table populated from the bundled `securities.json.gz` dataset (AMFI mutual funds, NSE equities/ETFs, Nasdaq/NYSE listings) and enriched on demand via the Yahoo Finance API fallback. It is the authoritative lookup for `ReferenceResolveService` when resolving ISIN/ticker/AMFI codes to a canonical security identity. The table carries conditional unique indexes: `isin` (where not null), `(ticker, exchange)` (where ticker not null), and `amfi_code` (where not null). There are no foreign keys from tenant tables into `reference_securities` — it is read-only reference data consulted during resolution and enrichment, never linked by FK.
- `investing_dividends` (spec-073) records dividend/interest/coupon income events. Each row credits `investing_cash_balances` once via the service layer — there is no offsetting debit, replacing the former workaround of a fake wallet→brokerage transfer. `holding_id` is an opportunistic link (null when the position has been exited); `income_type` is one of `dividend`/`interest`/`coupon`. A conditional unique index on `(workspace_id, account_id, external_ref)` prevents duplicate imports.
- `financial_kpis` (spec-077) stores user-defined KPI definitions (spend total, income total, net cash flow) scoped to a workspace with optional `category_id`, `category_group_id`, or `account_id` filters. Single-currency enforcement is checked at evaluation time (service layer), not as a DB constraint. Exactly one of `target_value`/`target_direction` is set (check constraint `ck_financial_kpis_target_pair`).
- `account_statements` and `statement_lines` (spec-078, wallet ledger reconciliation) are **metadata-only** — they never mutate any ledger table. A `statement_line` can be matched to at most one of `spending_transactions` or `capital_transfers` (check constraint `ck_statement_lines_exactly_one_match_target`). `account_statements.reconciled_through` is informational, not a processing lock. Duplicate statement-line detection uses the deterministic `external_ref` derived from `(account, date, amount, description, within-file index)`.
