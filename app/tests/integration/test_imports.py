import io
import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.core.audit import AuditLog
from app.core.database import postgres
from app.imports.models import ImportBatch
from app.spending.models import SpendingTransaction


async def _register_and_login(client: AsyncClient, suffix: str):
    username = f"import_{suffix}"
    email = f"{username}@example.com"
    password = "Password123!"
    reg = await client.post(
        "/v1/auth/register", json={"email": email, "username": username, "password": password}
    )
    assert reg.status_code == 200
    login = await client.post("/v1/auth/login", data={"username": username, "password": password})
    assert login.status_code == 200
    return {"cookies": dict(login.cookies)}


@pytest.mark.asyncio
async def test_import_spending_transactions_fail_all_on_single_error(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])

    cats = (await client.get("/v1/spending/categories", cookies=creds["cookies"])).json()["items"]
    food = next(c for c in cats if c["name"] == "Food & Dining")

    csv_content = (
        "occurred_at,type,amount,category,description\n"
        f"{datetime.now(UTC).isoformat()},expense,10.00,{food['public_id']},valid row\n"
        f"{datetime.now(UTC).isoformat()},expense,-5.00,{food['public_id']},invalid amount\n"
    )

    files = {"file": ("tx.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
    data = {"module": "spending-transactions"}
    validate = await client.post("/v1/imports", data=data, files=files, cookies=creds["cookies"])
    assert validate.status_code == 200, validate.text
    payload = validate.json()
    assert payload["import_batch"]["status"] == "failed_validation"
    assert payload["import_batch"]["error_rows"] == 1
    assert payload["error_summary"]["total_errors"] == 1
    assert payload["error_summary"]["returned_errors"] == 1
    assert payload["error_summary"]["by_code"]["invalid_decimal"] == 1
    assert payload["error_summary"]["by_field"]["amount"] == 1

    import_id = payload["import_batch"]["public_id"]
    detail = await client.get(f"/v1/imports/{import_id}", cookies=creds["cookies"])
    assert detail.status_code == 200
    assert detail.json()["error_summary"]["total_errors"] == 1

    commit = await client.post(f"/v1/imports/{import_id}/commit", cookies=creds["cookies"])
    assert commit.status_code == 422

    tx_list = await client.get("/v1/spending/transactions", cookies=creds["cookies"])
    assert tx_list.status_code == 200
    assert tx_list.json()["total"] == 0


@pytest.mark.asyncio
async def test_import_spending_budgets_validates_and_commits(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])

    cats = (await client.get("/v1/spending/categories", cookies=creds["cookies"])).json()["items"]
    food = next(c for c in cats if c["name"] == "Food & Dining")
    transport = next(c for c in cats if c["name"] == "Transport")

    csv_content = (
        "month_start,category,amount\n"
        f"2026-06-01,{food['public_id']},500.00\n"
        f"2026-06-01,{transport['public_id']},200.00\n"
    )

    files = {"file": ("budgets.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
    data = {"module": "spending-budgets"}
    validate = await client.post("/v1/imports", data=data, files=files, cookies=creds["cookies"])
    assert validate.status_code == 200, validate.text
    body = validate.json()
    assert body["import_batch"]["status"] == "validated"
    assert body["import_batch"]["error_rows"] == 0
    import_id = body["import_batch"]["public_id"]

    commit = await client.post(f"/v1/imports/{import_id}/commit", cookies=creds["cookies"])
    assert commit.status_code == 200, commit.text
    assert commit.json()["inserted_rows"] == 2
    assert commit.json()["import_batch"]["status"] == "completed"
    assert commit.json()["auto_created_category_count"] == 0
    assert commit.json()["auto_created_categories"] == []

    budgets = await client.get("/v1/spending/budgets", cookies=creds["cookies"])
    assert budgets.status_code == 200
    assert budgets.json()["total"] == 2


@pytest.mark.asyncio
async def test_import_template_download_as_attachment(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    response = await client.get(
        "/v1/imports/templates/spending-transactions", cookies=creds["cookies"]
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="spending-transactions-template.csv"'
    )
    assert "occurred_at,type,amount,category,description" in response.text


@pytest.mark.asyncio
async def test_import_accepts_utf8_bom_csv(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])

    cats = (await client.get("/v1/spending/categories", cookies=creds["cookies"])).json()["items"]
    food = next(c for c in cats if c["name"] == "Food & Dining")

    csv_content = (
        "\ufeffoccurred_at,type,amount,category,description\n"
        f"{datetime.now(UTC).isoformat()},expense,10.00,{food['public_id']},valid row\n"
    )
    files = {"file": ("tx.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
    data = {"module": "spending-transactions"}
    validate = await client.post("/v1/imports", data=data, files=files, cookies=creds["cookies"])
    assert validate.status_code == 200, validate.text
    payload = validate.json()
    assert payload["import_batch"]["status"] == "validated"
    assert payload["error_summary"]["total_errors"] == 0


@pytest.mark.asyncio
async def test_import_spendee_csv_with_wallet_and_labels(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])

    categories = (await client.get("/v1/spending/categories", cookies=creds["cookies"])).json()[
        "items"
    ]
    other = next(c for c in categories if c["name"] == "Other")

    # Spendee-like export format.
    csv_content = (
        'Date,Wallet,Type,"Category name",Amount,Currency,Note,Labels,Author\n'
        '2026-02-17T00:50:58+00:00,Main Wallet,Expense,Other,-3700.00,INR,School fee,family,"Sajan"\n'
    )
    files = {"file": ("spendee.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
    data = {"module": "spending-transactions"}
    validate = await client.post("/v1/imports", data=data, files=files, cookies=creds["cookies"])
    assert validate.status_code == 200, validate.text
    payload = validate.json()
    assert payload["import_batch"]["status"] == "validated"
    assert payload["error_summary"]["total_errors"] == 0
    import_id = payload["import_batch"]["public_id"]

    commit = await client.post(f"/v1/imports/{import_id}/commit", cookies=creds["cookies"])
    assert commit.status_code == 200, commit.text
    assert commit.json()["inserted_rows"] == 1
    assert commit.json()["auto_created_category_count"] == 0
    assert commit.json()["auto_created_categories"] == []

    txs = await client.get("/v1/spending/transactions", cookies=creds["cookies"])
    assert txs.status_code == 200
    assert txs.json()["total"] == 1
    row = txs.json()["items"][0]
    assert row["category_id"] == other["public_id"]
    assert row["type"] == "expense"
    # Negative Spendee expense should be normalized to positive stored amount.
    assert row["amount"] == "3700.00"
    assert row["wallet_name"] == "Main Wallet"
    assert row["labels"] == "family"
    assert row["source_type"] == "imported"
    assert row["source_metadata"] == {
        "source_type": "imported",
        "source_ref": f"{import_id}:2",
        "origin": "bulk_import",
        "label": "Bulk import",
        "import_public_id": import_id,
        "import_module": "spending-transactions",
        "import_row_number": 2,
        "rollback_supported": True,
    }

    async with postgres.async_session_maker() as session:
        batch = (
            await session.execute(select(ImportBatch).where(ImportBatch.public_id == import_id))
        ).scalar_one()
        tx = (
            await session.execute(
                select(SpendingTransaction).where(
                    SpendingTransaction.public_id == uuid.UUID(row["public_id"])
                )
            )
        ).scalar_one()

    assert tx.source_type == "imported"
    assert tx.source_import_id == batch.id
    assert tx.source_ref == f"{import_id}:2"


@pytest.mark.asyncio
async def test_import_commit_reports_auto_created_categories(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])

    csv_content = (
        "occurred_at,type,amount,category,description\n"
        f"{datetime.now(UTC).isoformat()},expense,15.00,Road Trips,new category import\n"
    )

    files = {"file": ("tx.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
    validate = await client.post(
        "/v1/imports",
        data={"module": "spending-transactions"},
        files=files,
        cookies=creds["cookies"],
    )
    assert validate.status_code == 200, validate.text
    body = validate.json()
    assert body["import_batch"]["status"] == "validated"
    assert body["error_summary"]["total_errors"] == 0

    import_id = body["import_batch"]["public_id"]
    commit = await client.post(f"/v1/imports/{import_id}/commit", cookies=creds["cookies"])
    assert commit.status_code == 200, commit.text
    commit_body = commit.json()
    assert commit_body["inserted_rows"] == 1
    assert commit_body["auto_created_category_count"] == 1
    assert commit_body["auto_created_categories"] == ["Road Trips"]

    categories = await client.get("/v1/spending/categories", cookies=creds["cookies"])
    assert categories.status_code == 200
    assert any(c["name"] == "Road Trips" for c in categories.json()["items"])


@pytest.mark.asyncio
async def test_import_delete_lifecycle_validated_succeeds(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])

    csv_content = (
        "occurred_at,type,amount,category,description\n"
        f"{datetime.now(UTC).isoformat()},expense,15.00,Other,some description\n"
    )
    files = {"file": ("tx.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
    validate = await client.post(
        "/v1/imports",
        data={"module": "spending-transactions"},
        files=files,
        cookies=creds["cookies"],
    )
    assert validate.status_code == 200
    import_id = validate.json()["import_batch"]["public_id"]

    # Delete validated batch
    del_resp = await client.delete(f"/v1/imports/{import_id}", cookies=creds["cookies"])
    assert del_resp.status_code == 204

    # GET after delete -> 404
    get_resp = await client.get(f"/v1/imports/{import_id}", cookies=creds["cookies"])
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_import_delete_completed_spending_transactions_rolls_back_records(
    client: AsyncClient,
):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])

    csv_content = (
        "occurred_at,type,amount,category,description\n"
        f"{datetime.now(UTC).isoformat()},expense,15.00,Other,description\n"
    )
    files = {"file": ("tx.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
    validate = await client.post(
        "/v1/imports",
        data={"module": "spending-transactions"},
        files=files,
        cookies=creds["cookies"],
    )
    assert validate.status_code == 200
    import_id = validate.json()["import_batch"]["public_id"]

    # Commit batch to complete it
    commit_resp = await client.post(f"/v1/imports/{import_id}/commit", cookies=creds["cookies"])
    assert commit_resp.status_code == 200

    txs_after_commit = await client.get("/v1/spending/transactions", cookies=creds["cookies"])
    assert txs_after_commit.status_code == 200
    assert txs_after_commit.json()["total"] == 1
    assert txs_after_commit.json()["items"][0]["source_type"] == "imported"

    # Deleting the completed batch should roll back imported spending records.
    del_resp = await client.delete(f"/v1/imports/{import_id}", cookies=creds["cookies"])
    assert del_resp.status_code == 204

    get_resp = await client.get(f"/v1/imports/{import_id}", cookies=creds["cookies"])
    assert get_resp.status_code == 404

    txs_after_delete = await client.get("/v1/spending/transactions", cookies=creds["cookies"])
    assert txs_after_delete.status_code == 200
    assert txs_after_delete.json()["total"] == 0

    async with postgres.async_session_maker() as session:
        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "import_rolled_back",
                    AuditLog.entity_type == "import_batch",
                )
            )
        ).scalar_one()

    assert audit.details["entity_public_id"] == import_id
    assert audit.details["before"]["status"] == "completed"
    assert audit.details["deleted_records"] == 1
