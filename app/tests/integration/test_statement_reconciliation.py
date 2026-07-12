"""Integration tests for spec-078 (wallet ledger reconciliation / statement
matching): the finance-account-statement import module, the deterministic
match engine, the reconciliation view, and the breaking-edit-clears-match
behavior.
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
async def test_statement_import_and_reconciliation_flow(client: AsyncClient):
    await _register_and_login(client, "wallet-recon@example.com", "wallet-recon")
    account_id = await _create_account(client, "checking")
    cat_id = await _first_category(client)

    # A matching transaction already in the ledger.
    await _add_transaction(client, account_id, cat_id, "45.20", "expense", "2026-01-05T00:00:00Z")

    rows = [
        {
            "date": "2026-01-05",
            "description": "Grocery store",
            "debit": "45.20",
            "credit": "",
            "balance": "954.80",
        },
        {
            "date": "2026-01-08",
            "description": "Paycheck",
            "debit": "",
            "credit": "2000.00",
            "balance": "2954.80",
        },
    ]
    validate_body = await _upload_statement(client, account_id, rows)
    assert not validate_body["errors"], validate_body["errors"]
    batch = validate_body["import_batch"]
    commit_body = await _commit_batch(client, batch["public_id"])
    assert commit_body["inserted_rows"] == 2

    statements_res = await client.get(f"/v1/finance/accounts/{account_id}/statements")
    assert statements_res.status_code == 200
    statements = statements_res.json()
    assert len(statements) == 1
    statement = statements[0]
    assert statement["period_start"] == "2026-01-05"
    assert statement["period_end"] == "2026-01-08"
    assert statement["closing_balance"] == "2954.80"
    assert statement["reconciled_through"] is None

    recon_res = await client.get(
        f"/v1/finance/accounts/{account_id}/statements/{statement['public_id']}/reconciliation"
    )
    assert recon_res.status_code == 200
    recon = recon_res.json()
    assert len(recon["matched_lines"]) == 0
    assert len(recon["unmatched_lines"]) == 2

    grocery_entry = next(
        e for e in recon["unmatched_lines"] if e["line"]["description"] == "Grocery store"
    )
    assert len(grocery_entry["candidates"]) == 1
    candidate = grocery_entry["candidates"][0]
    assert candidate["kind"] == "transaction"
    assert candidate["amount"] == "-45.20"

    paycheck_entry = next(
        e for e in recon["unmatched_lines"] if e["line"]["description"] == "Paycheck"
    )
    assert paycheck_entry["candidates"] == []  # no matching ledger row exists yet

    # Confirm the grocery match.
    match_res = await client.post(
        f"/v1/finance/accounts/{account_id}/statements/{statement['public_id']}"
        f"/lines/{grocery_entry['line']['public_id']}/match",
        json={"transaction_id": candidate["id"]},
    )
    assert match_res.status_code == 200, match_res.text
    matched_line = match_res.json()
    assert matched_line["matched_transaction_id"] == candidate["id"]

    # Statement is not fully reconciled yet — paycheck line still unmatched.
    statements_res = await client.get(f"/v1/finance/accounts/{account_id}/statements")
    assert statements_res.json()[0]["reconciled_through"] is None

    # Match the paycheck line to a freshly-added transaction, completing reconciliation.
    paycheck_tx_id = await _add_transaction(
        client, account_id, cat_id, "2000.00", "income", "2026-01-08T00:00:00Z"
    )
    recon_res = await client.get(
        f"/v1/finance/accounts/{account_id}/statements/{statement['public_id']}/reconciliation"
    )
    unmatched = recon_res.json()["unmatched_lines"]
    assert len(unmatched) == 1
    paycheck_line_id = unmatched[0]["line"]["public_id"]
    match_res = await client.post(
        f"/v1/finance/accounts/{account_id}/statements/{statement['public_id']}"
        f"/lines/{paycheck_line_id}/match",
        json={"transaction_id": paycheck_tx_id},
    )
    assert match_res.status_code == 200, match_res.text

    statements_res = await client.get(f"/v1/finance/accounts/{account_id}/statements")
    assert statements_res.json()[0]["reconciled_through"] == "2026-01-08"


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
async def test_breaking_edit_clears_match_and_unreconciles(client: AsyncClient):
    await _register_and_login(client, "wallet-break@example.com", "wallet-break")
    account_id = await _create_account(client, "checking")
    cat_id = await _first_category(client)

    tx_id = await _add_transaction(
        client, account_id, cat_id, "20.00", "expense", "2026-03-01T00:00:00Z"
    )
    rows = [
        {
            "date": "2026-03-01",
            "description": "Lunch",
            "debit": "20.00",
            "credit": "",
            "balance": "980.00",
        }
    ]
    body = await _upload_statement(client, account_id, rows)
    commit_body = await _commit_batch(client, body["import_batch"]["public_id"])
    assert commit_body["inserted_rows"] == 1

    statements = (await client.get(f"/v1/finance/accounts/{account_id}/statements")).json()
    statement = statements[0]
    recon = (
        await client.get(
            f"/v1/finance/accounts/{account_id}/statements/{statement['public_id']}/reconciliation"
        )
    ).json()
    line_id = recon["unmatched_lines"][0]["line"]["public_id"]

    match_res = await client.post(
        f"/v1/finance/accounts/{account_id}/statements/{statement['public_id']}"
        f"/lines/{line_id}/match",
        json={"transaction_id": tx_id},
    )
    assert match_res.status_code == 200

    statements = (await client.get(f"/v1/finance/accounts/{account_id}/statements")).json()
    assert statements[0]["reconciled_through"] == "2026-03-01"

    # Breaking edit: change the matched transaction's amount.
    update_res = await client.patch(
        f"/v1/spending/transactions/{tx_id}",
        json={"amount": "25.00"},
    )
    assert update_res.status_code == 200, update_res.text

    recon = (
        await client.get(
            f"/v1/finance/accounts/{account_id}/statements/{statement['public_id']}/reconciliation"
        )
    ).json()
    assert len(recon["matched_lines"]) == 0
    assert len(recon["unmatched_lines"]) == 1

    statements = (await client.get(f"/v1/finance/accounts/{account_id}/statements")).json()
    assert statements[0]["reconciled_through"] is None


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
