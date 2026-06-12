import io
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.config import settings
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
async def test_import_rejects_oversized_multipart_before_parsing(client: AsyncClient):
    oversized_file = io.BytesIO(b"x" * (settings.MAX_MULTIPART_BODY_BYTES + 1))
    files = {"file": ("too-large.csv", oversized_file, "text/csv")}

    response = await client.post(
        "/v1/imports",
        data={"module": "spending-transactions"},
        files=files,
    )

    assert response.status_code == 413
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    body = response.json()
    assert body["type"] == "https://lifestack.app/errors/request-too-large"
    assert body["title"] == "Request Too Large"


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
async def test_import_local_storage_key_uses_generated_object_name(
    client: AsyncClient, monkeypatch, tmp_path
):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])

    monkeypatch.setattr(settings, "IMPORT_STORAGE_BACKEND", "local")
    monkeypatch.setattr(settings, "IMPORT_LOCAL_PATH", str(tmp_path / "imports"))

    cats = (await client.get("/v1/spending/categories", cookies=creds["cookies"])).json()["items"]
    food = next(c for c in cats if c["name"] == "Food & Dining")

    csv_content = (
        "occurred_at,type,amount,category,description\n"
        f"{datetime.now(UTC).isoformat()},expense,10.00,{food['public_id']},valid row\n"
    )
    files = {
        "file": ("../../bank-statement.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")
    }
    validate = await client.post(
        "/v1/imports",
        data={"module": "spending-transactions"},
        files=files,
        cookies=creds["cookies"],
    )

    assert validate.status_code == 200, validate.text
    batch = validate.json()["import_batch"]
    assert batch["filename"] == "bank-statement.csv"
    assert batch["storage_backend"] == "local"
    stored_path = Path(batch["storage_key"])
    assert stored_path.name == "source.csv"
    assert stored_path.parent.name == batch["public_id"]
    assert "bank-statement.csv" not in batch["storage_key"]
    assert stored_path.is_file()


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


@pytest.mark.asyncio
async def test_import_delete_completed_spending_budgets_rolls_back_records(
    client: AsyncClient,
):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])

    cats = (await client.get("/v1/spending/categories", cookies=creds["cookies"])).json()["items"]
    food = next(c for c in cats if c["name"] == "Food & Dining")

    csv_content = f"month_start,category,amount\n2026-06-01,{food['public_id']},500.00\n"
    files = {"file": ("budgets.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
    validate = await client.post(
        "/v1/imports",
        data={"module": "spending-budgets"},
        files=files,
        cookies=creds["cookies"],
    )
    assert validate.status_code == 200
    import_id = validate.json()["import_batch"]["public_id"]

    commit_resp = await client.post(f"/v1/imports/{import_id}/commit", cookies=creds["cookies"])
    assert commit_resp.status_code == 200

    budgets_after_commit = await client.get("/v1/spending/budgets", cookies=creds["cookies"])
    assert budgets_after_commit.status_code == 200
    assert budgets_after_commit.json()["total"] == 1

    del_resp = await client.delete(f"/v1/imports/{import_id}", cookies=creds["cookies"])
    assert del_resp.status_code == 204

    budgets_after_delete = await client.get("/v1/spending/budgets", cookies=creds["cookies"])
    assert budgets_after_delete.status_code == 200
    assert budgets_after_delete.json()["total"] == 0


@pytest.mark.asyncio
async def test_import_delete_completed_investing_holdings_rolls_back_records(
    client: AsyncClient,
):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])

    acct_resp = await client.post(
        "/v1/finance/accounts",
        json={"name": "brokerage", "account_type": "brokerage", "default_currency_code": "USD"},
        cookies=creds["cookies"],
    )
    assert acct_resp.status_code == 201

    csv_content = (
        "symbol,account_name,quantity,avg_cost,currency\nAAPL,brokerage,10.00,150.00,USD\n"
    )
    files = {"file": ("holdings.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
    validate = await client.post(
        "/v1/imports",
        data={"module": "investing-holdings"},
        files=files,
        cookies=creds["cookies"],
    )
    assert validate.status_code == 200
    import_id = validate.json()["import_batch"]["public_id"]

    commit_resp = await client.post(f"/v1/imports/{import_id}/commit", cookies=creds["cookies"])
    assert commit_resp.status_code == 200

    holdings_after_commit = await client.get("/v1/investing/holdings", cookies=creds["cookies"])
    assert holdings_after_commit.status_code == 200
    assert len(holdings_after_commit.json()["items"]) == 1

    del_resp = await client.delete(f"/v1/imports/{import_id}", cookies=creds["cookies"])
    assert del_resp.status_code == 204

    holdings_after_delete = await client.get("/v1/investing/holdings", cookies=creds["cookies"])
    assert holdings_after_delete.status_code == 200
    assert len(holdings_after_delete.json()["items"]) == 0


@pytest.mark.asyncio
async def test_import_investing_holdings_upserts_on_duplicate(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])

    acct_resp = await client.post(
        "/v1/finance/accounts",
        json={"name": "brokerage", "account_type": "brokerage", "default_currency_code": "USD"},
        cookies=creds["cookies"],
    )
    assert acct_resp.status_code == 201

    # 1. Add a holding manually or via first import
    csv_content1 = (
        "symbol,account_name,quantity,avg_cost,currency\nAAPL,brokerage,10.00,150.00,USD\n"
    )
    files1 = {"file": ("holdings1.csv", io.BytesIO(csv_content1.encode("utf-8")), "text/csv")}
    validate1 = await client.post(
        "/v1/imports",
        data={"module": "investing-holdings"},
        files=files1,
        cookies=creds["cookies"],
    )
    assert validate1.status_code == 200
    import_id1 = validate1.json()["import_batch"]["public_id"]
    commit_resp1 = await client.post(f"/v1/imports/{import_id1}/commit", cookies=creds["cookies"])
    assert commit_resp1.status_code == 200

    # Verify first import holding quantity is 10.00
    holdings = await client.get("/v1/investing/holdings", cookies=creds["cookies"])
    assert holdings.status_code == 200
    items = holdings.json()["items"]
    assert len(items) == 1
    assert items[0]["symbol"] == "AAPL"
    assert items[0]["quantity"] == "10.00000000"
    assert items[0]["avg_cost"] == "150.00"

    # 2. Upload and commit import with same holding symbol/account but different quantity/avg_cost
    csv_content2 = (
        "symbol,account_name,quantity,avg_cost,currency\nAAPL,brokerage,15.50,165.00,USD\n"
    )
    files2 = {"file": ("holdings2.csv", io.BytesIO(csv_content2.encode("utf-8")), "text/csv")}
    validate2 = await client.post(
        "/v1/imports",
        data={"module": "investing-holdings"},
        files=files2,
        cookies=creds["cookies"],
    )
    assert validate2.status_code == 200
    import_id2 = validate2.json()["import_batch"]["public_id"]
    commit_resp2 = await client.post(f"/v1/imports/{import_id2}/commit", cookies=creds["cookies"])
    assert commit_resp2.status_code == 200

    # Verify that duplicate holding is updated/upserted and not duplicated
    holdings_after = await client.get("/v1/investing/holdings", cookies=creds["cookies"])
    assert holdings_after.status_code == 200
    items_after = holdings_after.json()["items"]
    assert len(items_after) == 1
    assert items_after[0]["symbol"] == "AAPL"
    assert items_after[0]["quantity"] == "15.50000000"
    assert items_after[0]["avg_cost"] == "165.00"


@pytest.mark.asyncio
async def test_import_spending_budgets_upserts_on_duplicate(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])

    cats = (await client.get("/v1/spending/categories", cookies=creds["cookies"])).json()["items"]
    food = next(c for c in cats if c["name"] == "Food & Dining")

    # 1. Import budget first time
    csv_content1 = f"month_start,category,amount\n2026-06-01,{food['public_id']},500.00\n"
    files1 = {"file": ("budgets1.csv", io.BytesIO(csv_content1.encode("utf-8")), "text/csv")}
    validate1 = await client.post(
        "/v1/imports",
        data={"module": "spending-budgets"},
        files=files1,
        cookies=creds["cookies"],
    )
    assert validate1.status_code == 200
    import_id1 = validate1.json()["import_batch"]["public_id"]
    commit_resp1 = await client.post(f"/v1/imports/{import_id1}/commit", cookies=creds["cookies"])
    assert commit_resp1.status_code == 200

    # 2. Import budget second time for the same month and category but different amount
    csv_content2 = f"month_start,category,amount\n2026-06-01,{food['public_id']},750.00\n"
    files2 = {"file": ("budgets2.csv", io.BytesIO(csv_content2.encode("utf-8")), "text/csv")}
    validate2 = await client.post(
        "/v1/imports",
        data={"module": "spending-budgets"},
        files=files2,
        cookies=creds["cookies"],
    )
    assert validate2.status_code == 200
    import_id2 = validate2.json()["import_batch"]["public_id"]
    commit_resp2 = await client.post(f"/v1/imports/{import_id2}/commit", cookies=creds["cookies"])
    assert commit_resp2.status_code == 200

    # Verify that the budget is updated/upserted and not duplicated or failing
    budgets = await client.get("/v1/spending/budgets", cookies=creds["cookies"])
    assert budgets.status_code == 200
    items = budgets.json()["items"]
    assert len(items) == 1
    assert items[0]["amount"] == "750.00"


@pytest.mark.asyncio
async def test_import_workspace_isolation(client: AsyncClient):
    creds_a = await _register_and_login(client, "ws_a")
    creds_b = await _register_and_login(client, "ws_b")

    # User A creates an import batch
    csv_content = (
        "occurred_at,type,amount,category,description\n"
        f"{datetime.now(UTC).isoformat()},expense,15.00,Other,some description\n"
    )
    files = {"file": ("tx.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
    validate = await client.post(
        "/v1/imports",
        data={"module": "spending-transactions"},
        files=files,
        cookies=creds_a["cookies"],
    )
    assert validate.status_code == 200
    import_id = validate.json()["import_batch"]["public_id"]

    # User B (different workspace) attempts to fetch User A's import batch -> 404
    get_resp = await client.get(f"/v1/imports/{import_id}", cookies=creds_b["cookies"])
    assert get_resp.status_code == 404

    # User B attempts to commit User A's import batch -> 404
    commit_resp = await client.post(f"/v1/imports/{import_id}/commit", cookies=creds_b["cookies"])
    assert commit_resp.status_code == 404

    # User B attempts to delete User A's import batch -> 404
    del_resp = await client.delete(f"/v1/imports/{import_id}", cookies=creds_b["cookies"])
    assert del_resp.status_code == 404

    # User B lists imports -> User A's import batch should not be present
    list_resp = await client.get("/v1/imports", cookies=creds_b["cookies"])
    assert list_resp.status_code == 200
    batch_ids = [item["public_id"] for item in list_resp.json()["items"]]
    assert import_id not in batch_ids
