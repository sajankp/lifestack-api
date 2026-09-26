import pytest
from httpx import AsyncClient

from app.tests.integration.test_finance import _register_and_login


@pytest.mark.asyncio
async def test_dividend_credited_to_bank_account(client: AsyncClient):
    await _register_and_login(
        client,
        email="div-bank@example.com",
        username="div-bank",
        password="TestPass123!",
    )

    # 1. Create a brokerage account (Groww) and a bank account (ICICI)
    brokerage_res = await client.post(
        "/v1/finance/accounts",
        json={
            "name": "Groww Brokerage",
            "account_type": "brokerage",
            "default_currency_code": "INR",
        },
    )
    assert brokerage_res.status_code == 201
    brokerage = brokerage_res.json()

    bank_res = await client.post(
        "/v1/finance/accounts",
        json={
            "name": "ICICI Bank",
            "account_type": "bank",
            "default_currency_code": "INR",
        },
    )
    assert bank_res.status_code == 201
    bank = bank_res.json()

    # 2. Post a dividend on Groww, but credit ICICI Bank
    div_res = await client.post(
        "/v1/investing/dividends",
        json={
            "account_id": brokerage["public_id"],
            "credit_account_id": bank["public_id"],
            "symbol": "TATSILV",
            "income_type": "dividend",
            "gross_amount": "500.00",
            "tax_withheld": "50.00",
            "currency": "INR",
            "pay_date": "2026-07-15",
            "notes": "Q1 Tata Silver dividend",
        },
    )
    assert div_res.status_code == 201, div_res.text
    div_data = div_res.json()
    assert div_data["account_id"] == brokerage["public_id"]
    assert div_data["account_name"] == "Groww Brokerage"
    assert div_data["credit_account_id"] == bank["public_id"]
    assert div_data["credit_account_name"] == "ICICI Bank"
    assert div_data["net_amount"] == "450.00"

    # 3. Verify a spending transaction was created on ICICI Bank
    tx_res = await client.get(f"/v1/spending/transactions?account_id={bank['public_id']}")
    assert tx_res.status_code == 200
    txs = tx_res.json()["items"]
    assert len(txs) == 1
    assert txs[0]["type"] == "income"
    assert txs[0]["amount"] == "450.00"
    assert "TATSILV" in txs[0]["description"]

    # 4. Verify no cash snapshot was added to Groww Brokerage
    cash_res = await client.get(f"/v1/investing/cash-balances?account_id={brokerage['public_id']}")
    assert cash_res.status_code == 200
    assert len(cash_res.json()["items"]) == 0

    # 5. Delete dividend and verify linked spending transaction is removed
    del_res = await client.delete(f"/v1/investing/dividends/{div_data['public_id']}")
    assert del_res.status_code == 204

    tx_res2 = await client.get(f"/v1/spending/transactions?account_id={bank['public_id']}")
    assert tx_res2.status_code == 200
    assert len(tx_res2.json()["items"]) == 0


@pytest.mark.asyncio
async def test_dividend_credit_currency_mismatch(client: AsyncClient):
    await _register_and_login(
        client,
        email="div-mismatch@example.com",
        username="div-mismatch",
        password="TestPass123!",
    )

    brokerage_res = await client.post(
        "/v1/finance/accounts",
        json={
            "name": "Groww",
            "account_type": "brokerage",
            "default_currency_code": "INR",
        },
    )
    assert brokerage_res.status_code == 201

    usd_bank = await client.post(
        "/v1/finance/accounts",
        json={
            "name": "US Bank",
            "account_type": "bank",
            "default_currency_code": "USD",
        },
    )
    assert usd_bank.status_code == 201

    # Try to credit USD bank with INR dividend
    div_res = await client.post(
        "/v1/investing/dividends",
        json={
            "account_id": brokerage_res.json()["public_id"],
            "credit_account_id": usd_bank.json()["public_id"],
            "symbol": "TATSILV",
            "income_type": "dividend",
            "gross_amount": "500.00",
            "tax_withheld": "50.00",
            "currency": "INR",
            "pay_date": "2026-07-15",
        },
    )
    assert div_res.status_code == 422


@pytest.mark.asyncio
async def test_unified_activity_feed_endpoint(client: AsyncClient):
    await _register_and_login(
        client,
        email="unified-feed@example.com",
        username="unified-feed",
        password="TestPass123!",
    )

    # Accounts
    bank_res = await client.post(
        "/v1/finance/accounts",
        json={"name": "ICICI", "account_type": "bank", "default_currency_code": "INR"},
    )
    assert bank_res.status_code == 201
    bank = bank_res.json()

    brokerage_res = await client.post(
        "/v1/finance/accounts",
        json={"name": "Groww", "account_type": "brokerage", "default_currency_code": "INR"},
    )
    assert brokerage_res.status_code == 201
    brokerage = brokerage_res.json()

    # Category for spend (use seeded category)
    cat_list_res = await client.get("/v1/spending/categories")
    assert cat_list_res.status_code == 200
    cat = cat_list_res.json()["items"][0]

    # 1. Spend transaction
    spend_res = await client.post(
        "/v1/spending/transactions",
        json={
            "category_id": cat["public_id"],
            "account_id": bank["public_id"],
            "amount": "250.00",
            "type": "expense",
            "occurred_at": "2026-07-01T12:00:00Z",
            "description": "Lunch at cafe",
        },
    )
    assert spend_res.status_code == 201

    # 2. Transfer from ICICI to Groww
    transfer_res = await client.post(
        "/v1/finance/transfers",
        json={
            "from_account_id": bank["public_id"],
            "to_account_id": brokerage["public_id"],
            "from_currency_code": "INR",
            "to_currency_code": "INR",
            "gross_amount": "5000.00",
            "net_amount_received": "5000.00",
            "occurred_at": "2026-07-02T10:00:00Z",
            "notes": "Top up investment",
        },
    )
    assert transfer_res.status_code == 201, transfer_res.text

    # 3. Investing order (buy TATSILV)
    order_res = await client.post(
        "/v1/investing/orders",
        json={
            "account_id": brokerage["public_id"],
            "symbol": "TATSILV",
            "order_type": "buy",
            "quantity": "100.00000000",
            "price_per_unit": "20.000000",
            "gross_amount": "2000.00",
            "brokerage_fee": "10.00",
            "tax_amount": "5.00",
            "other_fees": "0.00",
            "net_amount": "2015.00",
            "currency": "INR",
            "occurred_at": "2026-07-03T11:00:00Z",
        },
    )
    assert order_res.status_code == 201

    # 4. Dividend
    div_res = await client.post(
        "/v1/investing/dividends",
        json={
            "account_id": brokerage["public_id"],
            "symbol": "TATSILV",
            "income_type": "dividend",
            "gross_amount": "100.00",
            "tax_withheld": "10.00",
            "currency": "INR",
            "pay_date": "2026-07-04",
        },
    )
    assert div_res.status_code == 201

    # Fetch unified feed
    feed_res = await client.get("/v1/finance/activity-feed")
    assert feed_res.status_code == 200, feed_res.text
    data = feed_res.json()
    assert data["total"] >= 5  # spend(1), transfer(2: out+in), order(1), dividend(1) = 5 items
    items = data["items"]

    types_found = {item["event_type"] for item in items}
    assert "spend" in types_found
    assert "transfer" in types_found
    assert "order" in types_found
    assert "dividend" in types_found

    # Test filtering by event_types
    spend_only_res = await client.get("/v1/finance/activity-feed?event_types=spend")
    assert spend_only_res.status_code == 200
    spend_items = spend_only_res.json()["items"]
    assert all(it["event_type"] == "spend" for it in spend_items)

    # Test filtering by account_id (bank only)
    bank_feed = await client.get(f"/v1/finance/activity-feed?account_id={bank['public_id']}")
    assert bank_feed.status_code == 200
    bank_items = bank_feed.json()["items"]
    # All items must belong to bank
    assert all(it["account_id"] == bank["public_id"] for it in bank_items)
