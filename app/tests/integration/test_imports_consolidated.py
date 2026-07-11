import csv
import io
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.tests.integration.test_investing import _register_and_login


def _rows_to_csv(headers: list[str], rows: list[dict]) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=headers)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return out.getvalue().encode("utf-8")


async def _run_framework_import(
    client: AsyncClient, module: str, headers: list[str], rows: list[dict]
) -> dict:
    csv_bytes = _rows_to_csv(headers, rows)
    res = await client.post(
        "/v1/imports",
        data={"module": module},
        files={"file": ("import.csv", csv_bytes, "text/csv")},
    )
    assert res.status_code in {200, 202}, res.text
    body = res.json()
    batch = body["import_batch"]

    if body["errors"]:
        return body

    commit_res = await client.post(f"/v1/imports/{batch['public_id']}/commit")
    assert commit_res.status_code in {200, 202}, commit_res.text
    return commit_res.json()


@pytest.mark.asyncio
async def test_dividend_import_framework_flow(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="div-framework@example.com",
        username="div-framework",
        password="TestPass123!",
    )
    broker_id = account_map["brokerage"]

    # We need the account name to do name -> Account lookup
    # Let's get the brokerage account name
    acc_res = await client.get("/v1/finance/accounts")
    brokerage_account_name = next(
        a["name"] for a in acc_res.json()["items"] if a["public_id"] == broker_id
    )

    headers = [
        "account",
        "symbol",
        "income_type",
        "gross",
        "tax",
        "currency",
        "pay_date",
        "external_ref",
        "notes",
    ]

    # 1. Successful upload & commit
    row = {
        "account": brokerage_account_name,
        "symbol": "NVDA",
        "income_type": "dividend",
        "gross": "100.00",
        "tax": "10.00",
        "currency": "USD",
        "pay_date": "2026-06-15",
        "external_ref": "ref-framework-1",
        "notes": "Framework dividend test",
    }
    result = await _run_framework_import(client, "investing-dividends", headers, [row])
    assert "errors" not in result or not result["errors"]
    assert result["inserted_rows"] == 1
    assert result["import_batch"]["extra_json"]["imported"] == 1

    # 2. Idempotent re-upload is skipped/updated
    row_dup = {**row}
    result_dup = await _run_framework_import(client, "investing-dividends", headers, [row_dup])
    assert result_dup["inserted_rows"] == 1
    assert result_dup["import_batch"]["extra_json"]["updated"] == 1

    # 3. Validation failure: tax >= gross
    bad_row = {**row, "tax": "150.00", "external_ref": "ref-framework-bad"}
    validate_res = await client.post(
        "/v1/imports",
        data={"module": "investing-dividends"},
        files={"file": ("import.csv", _rows_to_csv(headers, [bad_row]), "text/csv")},
    )
    assert validate_res.status_code == 200
    assert len(validate_res.json()["errors"]) == 1
    assert validate_res.json()["errors"][0]["error_code"] == "tax_ge_gross"


@pytest.mark.asyncio
async def test_fx_rates_import_framework_flow(client: AsyncClient):
    await _register_and_login(
        client,
        email="fx-framework@example.com",
        username="fx-framework",
        password="TestPass123!",
    )
    headers = ["base_currency_code", "quote_currency_code", "rate", "as_of_date"]

    row = {
        "base_currency_code": "USD",
        "quote_currency_code": "INR",
        "rate": "70.0000000000",
        "as_of_date": "2020-01-01",
    }
    result = await _run_framework_import(client, "finance-fx-rates", headers, [row])
    assert "errors" not in result
    assert result["inserted_rows"] == 1
    assert result["import_batch"]["extra_json"]["imported"] == 1

    # Exact re-upload is a no-op / skipped
    result_dup = await _run_framework_import(client, "finance-fx-rates", headers, [row])
    assert result_dup["import_batch"]["extra_json"]["skipped"] == 1


@pytest.mark.asyncio
async def test_net_worth_history_import_framework_flow(client: AsyncClient):
    await _register_and_login(
        client,
        email="nw-framework@example.com",
        username="nw-framework",
        password="TestPass123!",
    )
    headers = [
        "date",
        "reporting_currency",
        "total_net_worth",
        "holdings_value",
        "investing_cash",
        "spending_cash",
    ]
    past_date = (datetime.now(UTC).date() - timedelta(days=400)).isoformat()

    row = {
        "date": past_date,
        "reporting_currency": "USD",
        "total_net_worth": "10000.00",
        "holdings_value": "5000.00",
        "investing_cash": "3000.00",
        "spending_cash": "2000.00",
    }
    result = await _run_framework_import(client, "finance-net-worth-history", headers, [row])
    assert "errors" not in result
    assert result["inserted_rows"] == 1
    assert result["import_batch"]["extra_json"]["imported"] == 1


@pytest.mark.asyncio
async def test_retired_endpoints_return_404_405(client: AsyncClient):
    await _register_and_login(
        client,
        email="retired-check@example.com",
        username="retired-check",
        password="TestPass123!",
    )

    res1 = await client.post("/v1/investing/dividends/bulk", json={"rows": []})
    assert res1.status_code in {404, 405}

    res2 = await client.post("/v1/finance/fx/history/import", json={"rows": []})
    assert res2.status_code in {404, 405}

    res3 = await client.post("/v1/finance/net-worth/history/import", json={"rows": []})
    assert res3.status_code in {404, 405}
