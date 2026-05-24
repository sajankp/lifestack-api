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
        datetime last_seen_at
        datetime created_at
    }

    USERS ||--o{ AUTH_SESSIONS : authenticates
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
        string title
        string description
        datetime due_date
        string priority
        boolean completed
        string system_key
        datetime created_at
        datetime updated_at
    }

    USERS ||--o{ TODOS : owns
    WORKSPACES ||--o{ TODOS : scopes
```

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

    SPENDING_CATEGORIES {
        int id PK
        uuid public_id UK
        int workspace_id FK
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
        decimal amount
        string type
        datetime occurred_at
        string description
        datetime created_at
        datetime updated_at
    }

    SPENDING_BUDGETS {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int category_id FK
        decimal amount
        date month_start
        datetime created_at
        datetime updated_at
    }

    WORKSPACES ||--o{ SPENDING_CATEGORIES : scopes
    WORKSPACES ||--o{ SPENDING_TRANSACTIONS : scopes
    USERS ||--o{ SPENDING_TRANSACTIONS : records
    SPENDING_CATEGORIES ||--o{ SPENDING_TRANSACTIONS : categorizes
    WORKSPACES ||--o{ SPENDING_BUDGETS : scopes
    SPENDING_CATEGORIES ||--o{ SPENDING_BUDGETS : budgets
```

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
        datetime created_at
        datetime updated_at
    }

    WORKSPACES ||--o{ WORKSPACE_CURRENCIES : allows
    CURRENCIES ||--o{ WORKSPACE_CURRENCIES : listed_in
    WORKSPACES ||--o{ ACCOUNTS : owns
    CURRENCIES ||--o{ ACCOUNTS : default_currency
    WORKSPACES ||--o| WORKSPACE_FINANCE_SETTINGS : config
    CURRENCIES ||--o| WORKSPACE_FINANCE_SETTINGS : reporting_currency
    CURRENCIES ||--o{ FX_RATES : base_currency
    CURRENCIES ||--o{ FX_RATES : quote_currency
    WORKSPACES ||--o{ CAPITAL_TRANSFERS : scopes
    USERS ||--o{ CAPITAL_TRANSFERS : initiates
    ACCOUNTS ||--o{ CAPITAL_TRANSFERS : from_account
    ACCOUNTS ||--o{ CAPITAL_TRANSFERS : to_account
    CURRENCIES ||--o{ CAPITAL_TRANSFERS : from_currency
    CURRENCIES ||--o{ CAPITAL_TRANSFERS : to_currency
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
        string account_name
        decimal quantity
        decimal avg_cost
        string currency
        datetime created_at
        datetime updated_at
    }

    INVESTING_CASH_BALANCES {
        int id PK
        uuid public_id UK
        int workspace_id FK
        int user_id FK
        string account_name
        decimal balance
        string currency
        datetime as_of
        datetime created_at
        datetime updated_at
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
```

## Notes

- `workspace_id` is the tenant boundary for every business table.
- `public_id` is the external identifier exposed to API clients.
- `spending_transactions` and `spending_budgets` enforce the `category_id + workspace_id` pairing with composite foreign keys in the database, even though Mermaid shows them as simple relationships.
- `workspace_finance_settings` is effectively one row per workspace.
- `workspace_currencies` is the workspace-level allow-list for currencies.
- `fx_rates` stores currency pairs with both a base and quote currency reference.
- `capital_transfers` connects the spending and investing modules through accounts and currencies.
