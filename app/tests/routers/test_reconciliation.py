"""Integration tests for:
  - Transfer-inclusive spending ledger (GET /spending/accounts/{id}/ledger)
  - Account reconciliation endpoint (GET /finance/accounts/{id}/reconciliation)

RED phase: these tests are written BEFORE implementation and are expected to fail
until the feature is implemented per spec-040.
"""

import pytest
from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _register_and_login(client: AsyncClient, email: str, username: str) -> dict:
    register_res = await client.post(
        "/v1/auth/register",
        json={"email": email, "username": username, "password": "TestPass123!"},
    )
    assert register_res.status_code == 200
    login_res = await client.post(
        "/v1/auth/login",
        data={"username": username, "password": "TestPass123!"},
    )
    assert login_res.status_code == 200
    return dict(login_res.cookies)


async def _create_account(client, cookies, name, account_type="bank", currency="USD"):
    res = await client.post(
        "/v1/finance/accounts",
        json={"name": name, "account_type": account_type, "default_currency_code": currency},
        cookies=cookies,
    )
    assert res.status_code == 201, res.text
    return res.json()["public_id"]


async def _first_category(client, cookies):
    res = await client.get("/v1/spending/categories", cookies=cookies)
    assert res.status_code == 200
    return res.json()["items"][0]["public_id"]


async def _add_transaction(client, cookies, account_id, cat_id, amount, tx_type, occurred_at):
    r = await client.post(
        "/v1/spending/transactions",
        json={
            "amount": amount,
            "category_id": cat_id,
            "account_id": account_id,
            "type": tx_type,
            "occurred_at": occurred_at,
        },
        cookies=cookies,
    )
    assert r.status_code == 201, r.text
    return r.json()["public_id"]


async def _create_transfer(
    client, cookies, from_account_id, to_account_id, gross_amount, occurred_at
):
    r = await client.post(
        "/v1/finance/transfers",
        json={
            "from_module": "spending",
            "to_module": "spending",
            "from_account_id": from_account_id,
            "to_account_id": to_account_id,
            "from_currency_code": "USD",
            "to_currency_code": "USD",
            "gross_amount": gross_amount,
            "net_amount_received": gross_amount,
            "occurred_at": occurred_at,
        },
        cookies=cookies,
    )
    assert r.status_code == 201, r.text
    return r.json()["public_id"]


async def _add_cash_balance(client, cookies, account_id, balance, currency="USD"):
    r = await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_id,
            "balance": balance,
            "currency": currency,
            "as_of": "2026-06-20T00:00:00Z",
        },
        cookies=cookies,
    )
    assert r.status_code == 201, r.text
    return r.json()["public_id"]


# ---------------------------------------------------------------------------
# Ledger tests: entry_kind field + transfer rows included
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ledger_includes_transfer_out(client: AsyncClient):
    """A capital transfer (from this account) appears in the ledger as transfer_out
    and reduces the running balance."""
    cookies = await _register_and_login(client, "recon_xfer_out@example.com", "recon_xfer_out")
    bank_id = await _create_account(client, cookies, "Main Bank", "bank")
    brokerage_id = await _create_account(client, cookies, "Brokerage", "brokerage")
    cat_id = await _first_category(client, cookies)

    # Add income of 2000
    await _add_transaction(
        client, cookies, bank_id, cat_id, "2000.00", "income", "2026-06-01T10:00:00Z"
    )
    # Transfer 500 out to brokerage
    await _create_transfer(client, cookies, bank_id, brokerage_id, "500.00", "2026-06-05T10:00:00Z")

    res = await client.get(
        f"/v1/spending/accounts/{bank_id}/ledger", cookies=cookies, params={"limit": 50}
    )
    assert res.status_code == 200
    data = res.json()

    # Should have 2 entries (1 transaction + 1 transfer)
    assert data["total_entries"] == 2
    assert len(data["items"]) == 2

    # Desc order: transfer (Jun 5) first, then income (Jun 1)
    transfer_entry = data["items"][0]
    income_entry = data["items"][1]

    assert transfer_entry["entry_kind"] == "transfer_out"
    assert float(transfer_entry["amount"]) == 500.0
    # Running balance after transfer: 2000 - 500 = 1500
    assert float(transfer_entry["running_balance"]) == 1500.0

    assert income_entry["entry_kind"] == "transaction"
    # Running balance after income: 2000
    assert float(income_entry["running_balance"]) == 2000.0

    # Closing balance (balance after most recent entry = transfer)
    assert float(data["closing_balance"]) == 1500.0


@pytest.mark.asyncio
async def test_ledger_includes_transfer_in(client: AsyncClient):
    """A capital transfer (to this account) appears as transfer_in and increases the balance."""
    cookies = await _register_and_login(client, "recon_xfer_in@example.com", "recon_xfer_in")
    wallet_id = await _create_account(client, cookies, "Digital Wallet", "wallet")
    bank_id = await _create_account(client, cookies, "Savings Bank", "bank")

    # Transfer 1000 into wallet from bank
    await _create_transfer(client, cookies, bank_id, wallet_id, "1000.00", "2026-06-10T10:00:00Z")

    res = await client.get(
        f"/v1/spending/accounts/{wallet_id}/ledger", cookies=cookies, params={"limit": 50}
    )
    assert res.status_code == 200
    data = res.json()

    assert data["total_entries"] == 1
    entry = data["items"][0]
    assert entry["entry_kind"] == "transfer_in"
    assert float(entry["amount"]) == 1000.0
    assert float(entry["running_balance"]) == 1000.0
    assert float(data["closing_balance"]) == 1000.0


@pytest.mark.asyncio
async def test_ledger_mixed_entries_running_balance(client: AsyncClient):
    """Running balance is correct across a mix of income, expense, and transfer entries."""
    cookies = await _register_and_login(client, "recon_mixed@example.com", "recon_mixed")
    bank_id = await _create_account(client, cookies, "Mixed Bank", "bank")
    other_id = await _create_account(client, cookies, "Other Account", "wallet")
    cat_id = await _first_category(client, cookies)

    # Sequence:
    # Jun 1: income 3000  → bal 3000
    # Jun 3: expense 500  → bal 2500
    # Jun 5: transfer out 800 → bal 1700
    # Jun 7: income 200   → bal 1900
    await _add_transaction(
        client, cookies, bank_id, cat_id, "3000.00", "income", "2026-06-01T10:00:00Z"
    )
    await _add_transaction(
        client, cookies, bank_id, cat_id, "500.00", "expense", "2026-06-03T10:00:00Z"
    )
    await _create_transfer(client, cookies, bank_id, other_id, "800.00", "2026-06-05T10:00:00Z")
    await _add_transaction(
        client, cookies, bank_id, cat_id, "200.00", "income", "2026-06-07T10:00:00Z"
    )

    res = await client.get(
        f"/v1/spending/accounts/{bank_id}/ledger", cookies=cookies, params={"limit": 50}
    )
    assert res.status_code == 200
    data = res.json()

    assert data["total_entries"] == 4
    assert len(data["items"]) == 4

    # Desc order: Jun 7, Jun 5, Jun 3, Jun 1
    balances = [float(item["running_balance"]) for item in data["items"]]
    assert balances == [1900.0, 1700.0, 2500.0, 3000.0]

    kinds = [item["entry_kind"] for item in data["items"]]
    assert kinds == ["transaction", "transfer_out", "transaction", "transaction"]

    assert float(data["closing_balance"]) == 1900.0


@pytest.mark.asyncio
async def test_ledger_transaction_entries_have_entry_kind(client: AsyncClient):
    """All spending transaction rows have entry_kind == 'transaction'."""
    cookies = await _register_and_login(client, "recon_txkind@example.com", "recon_txkind")
    acc_id = await _create_account(client, cookies, "Kind Test Wallet", "wallet")
    cat_id = await _first_category(client, cookies)

    await _add_transaction(
        client, cookies, acc_id, cat_id, "100.00", "income", "2026-06-01T10:00:00Z"
    )

    res = await client.get(f"/v1/spending/accounts/{acc_id}/ledger", cookies=cookies)
    assert res.status_code == 200
    items = res.json()["items"]
    assert items[0]["entry_kind"] == "transaction"


@pytest.mark.asyncio
async def test_ledger_uses_total_entries_field(client: AsyncClient):
    """Response uses 'total_entries' (not 'total_transactions') to count all entries."""
    cookies = await _register_and_login(client, "recon_totentries@example.com", "recon_totentries")
    acc_id = await _create_account(client, cookies, "Entries Test", "bank")
    other_id = await _create_account(client, cookies, "Other", "wallet")
    cat_id = await _first_category(client, cookies)

    await _add_transaction(
        client, cookies, acc_id, cat_id, "500.00", "income", "2026-06-01T10:00:00Z"
    )
    await _create_transfer(client, cookies, acc_id, other_id, "200.00", "2026-06-02T10:00:00Z")

    res = await client.get(f"/v1/spending/accounts/{acc_id}/ledger", cookies=cookies)
    assert res.status_code == 200
    data = res.json()
    assert "total_entries" in data
    assert data["total_entries"] == 2


@pytest.mark.asyncio
async def test_ledger_pagination_with_transfers(client: AsyncClient):
    """Pagination works correctly across mixed transaction + transfer entries."""
    cookies = await _register_and_login(client, "recon_page_mix@example.com", "recon_page_mix")
    acc_id = await _create_account(client, cookies, "Paged Mixed", "bank")
    other_id = await _create_account(client, cookies, "Other Paged", "wallet")
    cat_id = await _first_category(client, cookies)

    for i in range(3):
        await _add_transaction(
            client,
            cookies,
            acc_id,
            cat_id,
            f"{(i + 1) * 100}.00",
            "income",
            f"2026-06-{i + 1:02d}T10:00:00Z",
        )
    for i in range(2):
        await _create_transfer(
            client, cookies, acc_id, other_id, "50.00", f"2026-06-{i + 4:02d}T10:00:00Z"
        )

    # 5 total entries; paginate in pages of 2
    page1 = (
        await client.get(
            f"/v1/spending/accounts/{acc_id}/ledger",
            cookies=cookies,
            params={"limit": 2, "offset": 0},
        )
    ).json()
    page2 = (
        await client.get(
            f"/v1/spending/accounts/{acc_id}/ledger",
            cookies=cookies,
            params={"limit": 2, "offset": 2},
        )
    ).json()
    page3 = (
        await client.get(
            f"/v1/spending/accounts/{acc_id}/ledger",
            cookies=cookies,
            params={"limit": 2, "offset": 4},
        )
    ).json()

    assert page1["total_entries"] == 5
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 2
    assert len(page3["items"]) == 1

    all_ids = (
        [e["public_id"] for e in page1["items"]]
        + [e["public_id"] for e in page2["items"]]
        + [e["public_id"] for e in page3["items"]]
    )
    assert len(all_ids) == len(set(all_ids)), "Duplicate entries across pages"


# ---------------------------------------------------------------------------
# Account balance: transfer-inclusive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_account_balance_includes_transfers(client: AsyncClient):
    """GET /finance/accounts/{id}/balance spending_balance includes capital transfer contributions."""
    cookies = await _register_and_login(client, "recon_bal_xfer@example.com", "recon_bal_xfer")
    bank_id = await _create_account(client, cookies, "Balance Bank", "bank")
    brok_id = await _create_account(client, cookies, "Brokerage Bal", "brokerage")
    cat_id = await _first_category(client, cookies)

    # Income 2000, then transfer out 500
    await _add_transaction(
        client, cookies, bank_id, cat_id, "2000.00", "income", "2026-06-01T10:00:00Z"
    )
    await _create_transfer(client, cookies, bank_id, brok_id, "500.00", "2026-06-05T10:00:00Z")

    res = await client.get(f"/v1/finance/accounts/{bank_id}/balance", cookies=cookies)
    assert res.status_code == 200
    data = res.json()
    # Balance should be 2000 - 500 = 1500 (transfer counts against it)
    assert float(data["spending_balance"]) == 1500.0


# ---------------------------------------------------------------------------
# Reconciliation endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconciliation_happy_path(client: AsyncClient):
    """Reconciliation returns projected_balance, snapshot_balance, and discrepancy."""
    cookies = await _register_and_login(client, "recon_happy@example.com", "recon_happy")
    bank_id = await _create_account(client, cookies, "Recon Bank", "bank")
    other_id = await _create_account(client, cookies, "Other Recon", "wallet")
    cat_id = await _first_category(client, cookies)

    # Projected: income 3000 - expense 500 - transfer_out 200 = 2300
    await _add_transaction(
        client, cookies, bank_id, cat_id, "3000.00", "income", "2026-06-01T10:00:00Z"
    )
    await _add_transaction(
        client, cookies, bank_id, cat_id, "500.00", "expense", "2026-06-02T10:00:00Z"
    )
    await _create_transfer(client, cookies, bank_id, other_id, "200.00", "2026-06-03T10:00:00Z")

    # Snapshot balance: 2350 (slight discrepancy of +50)
    await _add_cash_balance(client, cookies, bank_id, "2350.00")

    res = await client.get(f"/v1/finance/accounts/{bank_id}/reconciliation", cookies=cookies)
    assert res.status_code == 200
    data = res.json()

    assert data["account_public_id"] == bank_id
    assert float(data["projected_balance"]) == 2300.0
    assert float(data["snapshot_balance"]) == 2350.0
    # discrepancy = projected - snapshot = 2300 - 2350 = -50
    assert float(data["discrepancy"]) == -50.0
    assert data["snapshot_as_of"] is not None
    assert data["transaction_count"] == 2
    assert data["transfer_count"] == 1


@pytest.mark.asyncio
async def test_reconciliation_no_snapshot(client: AsyncClient):
    """Reconciliation returns null snapshot fields when no cash balance exists."""
    cookies = await _register_and_login(client, "recon_nosnap@example.com", "recon_nosnap")
    bank_id = await _create_account(client, cookies, "No Snapshot Bank", "bank")
    cat_id = await _first_category(client, cookies)

    await _add_transaction(
        client, cookies, bank_id, cat_id, "1000.00", "income", "2026-06-01T10:00:00Z"
    )

    res = await client.get(f"/v1/finance/accounts/{bank_id}/reconciliation", cookies=cookies)
    assert res.status_code == 200
    data = res.json()

    assert float(data["projected_balance"]) == 1000.0
    assert data["snapshot_balance"] is None
    assert data["snapshot_as_of"] is None
    assert data["discrepancy"] is None


@pytest.mark.asyncio
async def test_reconciliation_empty_account(client: AsyncClient):
    """Reconciliation for an account with no transactions or transfers returns zero projected balance."""
    cookies = await _register_and_login(client, "recon_empty@example.com", "recon_empty")
    bank_id = await _create_account(client, cookies, "Empty Recon Bank", "bank")

    res = await client.get(f"/v1/finance/accounts/{bank_id}/reconciliation", cookies=cookies)
    assert res.status_code == 200
    data = res.json()

    assert float(data["projected_balance"]) == 0.0
    assert data["snapshot_balance"] is None
    assert data["discrepancy"] is None
    assert data["transaction_count"] == 0
    assert data["transfer_count"] == 0


@pytest.mark.asyncio
async def test_reconciliation_workspace_isolation(client: AsyncClient):
    """Cannot access reconciliation for another workspace's account."""
    cookies_1 = await _register_and_login(client, "recon_iso1@example.com", "recon_iso1")
    bank_id = await _create_account(client, cookies_1, "Private Bank", "bank")

    cookies_2 = await _register_and_login(client, "recon_iso2@example.com", "recon_iso2")
    res = await client.get(f"/v1/finance/accounts/{bank_id}/reconciliation", cookies=cookies_2)
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_reconciliation_transfers_from_brokerage_to_bank(client: AsyncClient):
    """transfer_in from another account shows in reconciliation transfer_count."""
    cookies = await _register_and_login(client, "recon_inflow@example.com", "recon_inflow")
    bank_id = await _create_account(client, cookies, "Inflow Bank", "bank")
    brok_id = await _create_account(client, cookies, "Inflow Brokerage", "brokerage")

    # 1500 transferred into bank from brokerage
    await _create_transfer(client, cookies, brok_id, bank_id, "1500.00", "2026-06-01T10:00:00Z")

    res = await client.get(f"/v1/finance/accounts/{bank_id}/reconciliation", cookies=cookies)
    assert res.status_code == 200
    data = res.json()

    assert float(data["projected_balance"]) == 1500.0
    assert data["transaction_count"] == 0
    assert data["transfer_count"] == 1
