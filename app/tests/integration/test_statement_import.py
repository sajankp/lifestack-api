"""Integration tests for the finance-account-statement import module
(spec-078): importing a bank statement CSV against a ledger-managed
account, re-importing the same file, and rejecting brokerage accounts.

The statement line-matching feature (the reconciliation view and the
match/unmatch endpoints) was removed -- it had no UI and was never used.
"""

import csv
import io

import pytest
from httpx import AsyncClient


def _rows_to_csv(headers: list[str], rows: list[dict]) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=headers)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return out.getvalue().encode("utf-8")


STATEMENT_HEADERS = ["date", "description", "debit", "credit", "balance"]


async def _register_and_login(client: AsyncClient, email: str, username: str) -> None:
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


async def _create_account(client: AsyncClient, name: str, account_type: str = "bank") -> str:
    res = await client.post(
        "/v1/finance/accounts",
        json={"name": name, "account_type": account_type, "default_currency_code": "USD"},
    )
    assert res.status_code == 201, res.text
    return res.json()["public_id"]


async def _first_category(client: AsyncClient) -> str:
    res = await client.get("/v1/spending/categories")
    assert res.status_code == 200
    return res.json()["items"][0]["public_id"]


async def _add_transaction(
    client: AsyncClient, account_id: str, cat_id: str, amount: str, tx_type: str, occurred_at: str
) -> str:
    r = await client.post(
        "/v1/spending/transactions",
        json={
            "amount": amount,
            "category_id": cat_id,
            "account_id": account_id,
            "type": tx_type,
            "occurred_at": occurred_at,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["public_id"]


async def _upload_statement(
    client: AsyncClient, account_id: str, rows: list[dict], date_format: str = "yyyy-MM-dd"
) -> dict:
    csv_bytes = _rows_to_csv(STATEMENT_HEADERS, rows)
    res = await client.post(
        "/v1/imports",
        data={
            "module": "finance-account-statement",
            "target_account_id": account_id,
            "date_format": date_format,
        },
        files={"file": ("statement.csv", csv_bytes, "text/csv")},
    )
    assert res.status_code in {200, 202}, res.text
    return res.json()


async def _commit_batch(client: AsyncClient, batch_public_id: str) -> dict:
    res = await client.post(f"/v1/imports/{batch_public_id}/commit")
    assert res.status_code in {200, 202}, res.text
    return res.json()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_statement_reimport_is_idempotent(client: AsyncClient):
    await _register_and_login(client, "wallet-idem@example.com", "wallet-idem")
    account_id = await _create_account(client, "checking")

    rows = [
        {
            "date": "2026-02-01",
            "description": "Coffee shop",
            "debit": "5.00",
            "credit": "",
            "balance": "995.00",
        },
        # Two identical lines same day/amount/description — must dedupe
        # deterministically via the per-line duplicate index.
        {
            "date": "2026-02-01",
            "description": "Coffee shop",
            "debit": "5.00",
            "credit": "",
            "balance": "990.00",
        },
    ]

    body1 = await _upload_statement(client, account_id, rows)
    commit1 = await _commit_batch(client, body1["import_batch"]["public_id"])
    assert commit1["inserted_rows"] == 2

    # Re-upload the identical file (overlapping statement) — nothing new inserted.
    body2 = await _upload_statement(client, account_id, rows)
    commit2 = await _commit_batch(client, body2["import_batch"]["public_id"])
    assert commit2["inserted_rows"] == 0

    statements_res = await client.get(f"/v1/finance/accounts/{account_id}/statements")
    assert len(statements_res.json()) == 2  # two statement headers, one set of lines


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_statement_import_rejects_brokerage_account(client: AsyncClient):
    await _register_and_login(client, "wallet-brk@example.com", "wallet-brk")
    account_id = await _create_account(client, "invest", account_type="brokerage")

    res = await client.post(
        "/v1/imports",
        data={
            "module": "finance-account-statement",
            "target_account_id": account_id,
            "date_format": "yyyy-MM-dd",
        },
        files={
            "file": (
                "statement.csv",
                _rows_to_csv(
                    STATEMENT_HEADERS,
                    [
                        {
                            "date": "2026-01-05",
                            "description": "x",
                            "debit": "1.00",
                            "credit": "",
                            "balance": "1.00",
                        }
                    ],
                ),
                "text/csv",
            )
        },
    )
    assert res.status_code == 422, res.text
