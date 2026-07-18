import io
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.auth.models import User
from app.config import settings
from app.core.audit import AuditLog
from app.core.database import postgres
from app.finance.models import NetWorthSnapshot
from app.imports.models import ImportBatch
from app.investing.models import (
    Company,
    Instrument,
    InstrumentConstituent,
    PortfolioSnapshot,
    ReferenceSecurity,
)
from app.platform.models import WorkspaceMembership
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


async def _create_account(client: AsyncClient, creds: dict, name: str = "Wallet") -> str:
    response = await client.post(
        "/v1/finance/accounts",
        json={"name": name, "account_type": "wallet", "default_currency_code": "USD"},
        cookies=creds["cookies"],
    )
    assert response.status_code == 201, response.text
    return response.json()["public_id"]


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
    account_id = await _create_account(client, creds)

    csv_content = (
        "occurred_at,type,amount,category,description\n"
        f"{datetime.now(UTC).isoformat()},expense,10.00,{food['public_id']},valid row\n"
        f"{datetime.now(UTC).isoformat()},expense,-5.00,{food['public_id']},invalid amount\n"
    )

    files = {"file": ("tx.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
    data = {"module": "spending-transactions", "target_account_id": account_id}
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
    assert "occurred_at,type,amount,category,description,account_name" in response.text


@pytest.mark.asyncio
async def test_investing_constituents_template_is_self_documenting_and_roundtrips(
    client: AsyncClient,
):
    """spec-083 §8a.1: the downloaded constituent CSV template carries a
    per-type identifier-rule header comment plus filled example rows, and
    re-uploading it unedited must still parse (the '#' comment lines must
    not be mistaken for the header row).
    """
    suffix = uuid.uuid4().hex[:8]
    creds = await _register_and_login(client, suffix)

    template = await client.get(
        "/v1/imports/templates/investing-constituents", cookies=creds["cookies"]
    )
    assert template.status_code == 200
    assert template.text.startswith("# Required identifier by security type/market:")
    assert "company_isin,company_ticker,company_exchange" in template.text
    assert "HIEU.L" in template.text  # London-suffixed ETF example
    assert "INF209K01165" in template.text  # India-MF ISIN example

    async with postgres.async_session_maker() as session:
        user = (
            await session.execute(select(User).where(User.username == f"import_{suffix}"))
        ).scalar_one()
        membership = (
            await session.execute(
                select(WorkspaceMembership).where(WorkspaceMembership.user_id == user.id)
            )
        ).scalar_one()
        session.add(
            Instrument(
                workspace_id=membership.workspace_id,
                symbol="UMMA",
                name="Wahed Shariah ETF",
                instrument_type="etf",
                is_active=True,
            )
        )
        await session.commit()

    files = {"file": ("constituents.csv", io.BytesIO(template.text.encode("utf-8")), "text/csv")}
    validate = await client.post(
        "/v1/imports",
        data={"module": "investing-constituents"},
        files=files,
        cookies=creds["cookies"],
    )
    assert validate.status_code == 200
    body = validate.json()
    # The comment lines were correctly skipped (not treated as the header
    # row / a data row) — only the 3 example rows became preview rows.
    assert body["import_batch"]["total_rows"] == 3
    assert not any(err["field_name"] == "company_ticker" for err in body["errors"])


@pytest.mark.asyncio
async def test_import_accepts_utf8_bom_csv(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])

    cats = (await client.get("/v1/spending/categories", cookies=creds["cookies"])).json()["items"]
    food = next(c for c in cats if c["name"] == "Food & Dining")
    account_id = await _create_account(client, creds)

    csv_content = (
        "\ufeffoccurred_at,type,amount,category,description\n"
        f"{datetime.now(UTC).isoformat()},expense,10.00,{food['public_id']},valid row\n"
    )
    files = {"file": ("tx.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
    data = {"module": "spending-transactions", "target_account_id": account_id}
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

    account_res = await client.post(
        "/v1/finance/accounts",
        json={"name": "Main Wallet", "account_type": "wallet", "default_currency_code": "INR"},
        cookies=creds["cookies"],
    )
    assert account_res.status_code == 201, account_res.text
    account_id = account_res.json()["public_id"]

    categories = (await client.get("/v1/spending/categories", cookies=creds["cookies"])).json()[
        "items"
    ]
    other = next(c for c in categories if c["name"] == "Other")

    # Spendee-like export format.
    csv_content = (
        'Date,Wallet,Type,"Category name",Amount,Currency,Note,Labels\n'
        "2026-02-17T00:50:58+00:00,Main Wallet,Expense,Other,-3700.00,INR,School fee,family\n"
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
    assert row["account_id"] == account_id
    assert row["wallet_name"] is None
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
async def test_import_spendee_csv_fails_when_wallet_does_not_match_account(
    client: AsyncClient,
):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])

    csv_content = (
        'Date,Wallet,Type,"Category name",Amount,Currency,Note,Labels\n'
        "2026-02-17T00:50:58+00:00,Missing Wallet,Expense,Other,-3700.00,INR,School fee,family\n"
    )
    files = {"file": ("spendee.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
    validate = await client.post(
        "/v1/imports",
        data={"module": "spending-transactions"},
        files=files,
        cookies=creds["cookies"],
    )

    assert validate.status_code == 200, validate.text
    payload = validate.json()
    assert payload["import_batch"]["status"] == "failed_validation"
    assert payload["error_summary"]["by_field"]["account_name"] == 1
    assert payload["errors"][0]["error_code"] == "not_found"


@pytest.mark.asyncio
async def test_import_commit_reports_auto_created_categories(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    account_id = await _create_account(client, creds)

    csv_content = (
        "occurred_at,type,amount,category,description\n"
        f"{datetime.now(UTC).isoformat()},expense,15.00,Road Trips,new category import\n"
    )

    files = {"file": ("tx.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
    validate = await client.post(
        "/v1/imports",
        data={"module": "spending-transactions", "target_account_id": account_id},
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
    account_id = await _create_account(client, creds)

    csv_content = (
        "occurred_at,type,amount,category,description\n"
        f"{datetime.now(UTC).isoformat()},expense,15.00,Other,description\n"
    )
    files = {"file": ("tx.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
    validate = await client.post(
        "/v1/imports",
        data={"module": "spending-transactions", "target_account_id": account_id},
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
async def test_import_revert_invalidates_current_day_snapshots(client: AsyncClient):
    """spec-086 Layer 1: reverting a completed import that changed balances
    must invalidate today's NetWorthSnapshot and PortfolioSnapshot, matching
    what every interactive mutation endpoint already does
    (snapshot_repo.delete_for_date(today)). Otherwise a snapshot captured
    while the imported (now-reverted) data was live is served stale by the
    history/weekly-summary read paths, which read snapshots directly without
    recomputing -- the api#183 item-2 root cause for the same-day case."""
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])

    broker = await client.post(
        "/v1/finance/accounts",
        json={"name": "BROKER", "account_type": "brokerage", "default_currency_code": "INR"},
        cookies=creds["cookies"],
    )
    assert broker.status_code == 201
    settings_resp = await client.patch(
        "/v1/finance/settings", json={"reporting_currency_code": "INR"}, cookies=creds["cookies"]
    )
    assert settings_resp.status_code == 200, settings_resp.text

    # Seed cash then commit an investing-orders import that creates a holding.
    cash_resp = await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": broker.json()["public_id"],
            "balance": "100000.00",
            "currency": "INR",
            "as_of": datetime.now(UTC).isoformat(),
        },
        cookies=creds["cookies"],
    )
    assert cash_resp.status_code == 201, cash_resp.text

    files = {
        "file": (
            "orders.csv",
            io.BytesIO(
                b"order_type,symbol,instrument_type,instrument_name,account_name,"
                b"quantity,price_per_unit,currency,brokerage_fee,tax_amount,other_fees,"
                b"occurred_at,exchange_name,notes\n"
                b"buy,TESTFUND,mutual_fund,Test Fund,BROKER,900,10,INR,0,0,0,"
                b"2023-02-01T00:00:00+00:00,,\n"
            ),
            "text/csv",
        )
    }
    r = await client.post(
        "/v1/imports", data={"module": "investing-orders"}, files=files, cookies=creds["cookies"]
    )
    assert r.status_code == 200, r.text
    import_id = r.json()["import_batch"]["public_id"]
    commit = await client.post(f"/v1/imports/{import_id}/commit", cookies=creds["cookies"])
    assert commit.status_code in (200, 202), commit.text

    # Materialize today's snapshots via the opportunistic recompute-on-read paths.
    assert (await client.get("/v1/finance/net-worth", cookies=creds["cookies"])).status_code == 200
    assert (
        await client.get("/v1/investing/performance/summary", cookies=creds["cookies"])
    ).status_code == 200

    today = datetime.now(UTC).date()
    async with postgres.async_session_maker() as session:
        nw = (
            (
                await session.execute(
                    select(NetWorthSnapshot).where(NetWorthSnapshot.snapshot_date == today)
                )
            )
            .scalars()
            .all()
        )
        pf = (
            (
                await session.execute(
                    select(PortfolioSnapshot).where(PortfolioSnapshot.snapshot_date == today)
                )
            )
            .scalars()
            .all()
        )
        assert len(nw) == 1, "expected today's net-worth snapshot to exist after read"
        assert len(pf) == 1, "expected today's portfolio snapshot to exist after read"

    # Revert the import.
    del_resp = await client.delete(f"/v1/imports/{import_id}", cookies=creds["cookies"])
    assert del_resp.status_code == 204

    async with postgres.async_session_maker() as session:
        nw_after = (
            (
                await session.execute(
                    select(NetWorthSnapshot).where(NetWorthSnapshot.snapshot_date == today)
                )
            )
            .scalars()
            .all()
        )
        pf_after = (
            (
                await session.execute(
                    select(PortfolioSnapshot).where(PortfolioSnapshot.snapshot_date == today)
                )
            )
            .scalars()
            .all()
        )
    assert nw_after == [], "today's net-worth snapshot should be invalidated on import revert"
    assert pf_after == [], "today's portfolio snapshot should be invalidated on import revert"


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


@pytest.mark.asyncio
async def test_import_investing_constituents_rejects_name_only_rows(client: AsyncClient):
    """spec-083 §6 mandate: a constituent row with a company_name but no
    company_ticker/company_isin must fail validation — this is exactly the
    name-only path that fragmented "Apple Inc" / "Apple Inc." pre-spec-083.
    """
    suffix = uuid.uuid4().hex[:8]
    creds = await _register_and_login(client, suffix)

    async with postgres.async_session_maker() as session:
        user = (
            await session.execute(select(User).where(User.username == f"import_{suffix}"))
        ).scalar_one()
        membership = (
            await session.execute(
                select(WorkspaceMembership).where(WorkspaceMembership.user_id == user.id)
            )
        ).scalar_one()
        workspace_id = membership.workspace_id
        session.add(
            Instrument(
                workspace_id=workspace_id,
                symbol="UMMA",
                name="Wahed Shariah ETF",
                instrument_type="etf",
                is_active=True,
            )
        )
        await session.commit()

    csv_name_only = (
        "instrument_symbol,company_name,company_ticker,weight,as_of_date\n"
        "UMMA,Apple Inc,,0.50,2026-06-14\n"
        "UMMA,Microsoft Corp,MSFT,0.50,2026-06-14\n"
    )
    files = {"file": ("constituents.csv", io.BytesIO(csv_name_only.encode("utf-8")), "text/csv")}
    validate = await client.post(
        "/v1/imports",
        data={"module": "investing-constituents"},
        files=files,
        cookies=creds["cookies"],
    )
    assert validate.status_code == 200
    body = validate.json()
    assert body["import_batch"]["status"] == "failed_validation"
    assert body["error_summary"]["by_code"]["identifier_required"] == 1
    assert any("company_ticker or company_isin" in err["message"] for err in body["errors"])


@pytest.mark.asyncio
async def test_import_investing_constituents_preview_reports_identifier_status(
    client: AsyncClient,
):
    """spec-083 §5.5: each constituent preview row is classified against
    `reference_securities` — resolved / unresolved / ambiguous — so the
    import-preview UI can flag rows before commit, not just reject the
    completely-blank-identifier case.
    """
    suffix = uuid.uuid4().hex[:8]
    creds = await _register_and_login(client, suffix)

    async with postgres.async_session_maker() as session:
        user = (
            await session.execute(select(User).where(User.username == f"import_{suffix}"))
        ).scalar_one()
        membership = (
            await session.execute(
                select(WorkspaceMembership).where(WorkspaceMembership.user_id == user.id)
            )
        ).scalar_one()
        workspace_id = membership.workspace_id
        session.add(
            Instrument(
                workspace_id=workspace_id,
                symbol="UMMA",
                name="Wahed Shariah ETF",
                instrument_type="etf",
                is_active=True,
            )
        )
        session.add(
            ReferenceSecurity(
                isin="US0378331005",
                ticker="AAPL",
                exchange="XNAS",
                security_type="stock",
                name="Apple Inc",
                source="test",
                fetched_at=datetime.now(UTC),
            )
        )
        session.add(
            ReferenceSecurity(
                ticker="TATASTEEL",
                exchange="XNSE",
                security_type="stock",
                name="Tata Steel Ltd (India)",
                source="test",
                fetched_at=datetime.now(UTC),
            )
        )
        session.add(
            ReferenceSecurity(
                ticker="TATASTEEL",
                exchange="XLON",
                security_type="stock",
                name="Tata Steel Ltd (London)",
                source="test",
                fetched_at=datetime.now(UTC),
            )
        )
        await session.commit()

    csv_rows = (
        "instrument_symbol,company_name,company_isin,company_ticker,company_exchange,weight,as_of_date\n"
        "UMMA,Apple Inc,,AAPL,,0.40,2026-06-14\n"
        "UMMA,Unknown Co,,UNKNOWNCO,,0.30,2026-06-14\n"
        "UMMA,Tata Steel,,TATASTEEL,,0.30,2026-06-14\n"
    )
    files = {"file": ("constituents.csv", io.BytesIO(csv_rows.encode("utf-8")), "text/csv")}
    validate = await client.post(
        "/v1/imports",
        data={"module": "investing-constituents"},
        files=files,
        cookies=creds["cookies"],
    )
    assert validate.status_code == 200
    body = validate.json()
    statuses = {
        row["payload_json"]["company_ticker"]: row["payload_json"]["identifier_status"]
        for row in body["preview_rows"]
    }
    assert statuses["AAPL"] == "resolved"
    assert statuses["UNKNOWNCO"] == "unresolved"
    assert statuses["TATASTEEL"] == "ambiguous"


@pytest.mark.asyncio
async def test_import_investing_constituents_lifecycle(client: AsyncClient):
    suffix = uuid.uuid4().hex[:8]
    creds = await _register_and_login(client, suffix)

    # 1. Look up user and workspace to insert test instruments
    async with postgres.async_session_maker() as session:
        user = (
            await session.execute(select(User).where(User.username == f"import_{suffix}"))
        ).scalar_one()
        membership = (
            await session.execute(
                select(WorkspaceMembership).where(WorkspaceMembership.user_id == user.id)
            )
        ).scalar_one()
        workspace_id = membership.workspace_id

        # Insert active ETF
        etf = Instrument(
            workspace_id=workspace_id,
            symbol="UMMA",
            name="Wahed Shariah ETF",
            instrument_type="etf",
            is_active=True,
        )
        # Insert inactive ETF
        etf_inactive = Instrument(
            workspace_id=workspace_id,
            symbol="INACTIVE_ETF",
            name="Inactive ETF",
            instrument_type="etf",
            is_active=False,
        )
        # Insert stock
        stock = Instrument(
            workspace_id=workspace_id,
            symbol="AAPL",
            name="Apple Inc",
            instrument_type="stock",
            is_active=True,
        )
        session.add_all([etf, etf_inactive, stock])
        await session.commit()

    # Test A: Rejection of invalid instrument symbols / types / inactive
    csv_invalid_instrument = (
        "instrument_symbol,company_name,company_ticker,weight,as_of_date\n"
        "UMMA,Apple Inc,AAPL,0.50,2026-06-14\n"
        "NONEXISTENT,Microsoft Corp,MSFT,0.50,2026-06-14\n"
    )
    files = {
        "file": ("constituents.csv", io.BytesIO(csv_invalid_instrument.encode("utf-8")), "text/csv")
    }
    validate = await client.post(
        "/v1/imports",
        data={"module": "investing-constituents"},
        files=files,
        cookies=creds["cookies"],
    )
    assert validate.status_code == 200
    body = validate.json()
    assert body["import_batch"]["status"] == "failed_validation"
    assert body["error_summary"]["by_field"]["instrument_symbol"] == 1
    assert any(
        "must resolve to an active ETF/Mutual Fund" in err["message"] for err in body["errors"]
    )

    # Test B: Rejection of stock
    csv_stock = (
        "instrument_symbol,company_name,company_ticker,weight,as_of_date\n"
        "AAPL,Apple Inc,AAPL,1.00,2026-06-14\n"
    )
    files = {"file": ("constituents.csv", io.BytesIO(csv_stock.encode("utf-8")), "text/csv")}
    validate = await client.post(
        "/v1/imports",
        data={"module": "investing-constituents"},
        files=files,
        cookies=creds["cookies"],
    )
    assert validate.status_code == 200
    body = validate.json()
    assert body["import_batch"]["status"] == "failed_validation"
    assert body["error_summary"]["by_field"]["instrument_symbol"] == 1

    # Test C: Rejection of inactive ETF
    csv_inactive = (
        "instrument_symbol,company_name,company_ticker,weight,as_of_date\n"
        "INACTIVE_ETF,Apple Inc,AAPL,1.00,2026-06-14\n"
    )
    files = {"file": ("constituents.csv", io.BytesIO(csv_inactive.encode("utf-8")), "text/csv")}
    validate = await client.post(
        "/v1/imports",
        data={"module": "investing-constituents"},
        files=files,
        cookies=creds["cookies"],
    )
    assert validate.status_code == 200
    body = validate.json()
    assert body["import_batch"]["status"] == "failed_validation"
    assert body["error_summary"]["by_field"]["instrument_symbol"] == 1

    # Test D: Rejection of invalid weight sums (e.g. 0.80 instead of ~1.00)
    csv_invalid_weight = (
        "instrument_symbol,company_name,company_ticker,weight,as_of_date\n"
        "UMMA,Apple Inc,AAPL,0.40,2026-06-14\n"
        "UMMA,Microsoft Corp,MSFT,0.40,2026-06-14\n"
    )
    files = {
        "file": ("constituents.csv", io.BytesIO(csv_invalid_weight.encode("utf-8")), "text/csv")
    }
    validate = await client.post(
        "/v1/imports",
        data={"module": "investing-constituents"},
        files=files,
        cookies=creds["cookies"],
    )
    assert validate.status_code == 200
    body = validate.json()
    assert body["import_batch"]["status"] == "failed_validation"
    assert body["error_summary"]["by_code"]["invalid_weight_sum"] == 1

    # Test E: Successful validation and commit of valid weights (sum = 1.00)
    csv_valid = (
        "instrument_symbol,company_name,company_ticker,weight,as_of_date\n"
        "UMMA,Apple Inc,AAPL,0.60,2026-06-14\n"
        "UMMA,Microsoft Corp,MSFT,0.40,2026-06-14\n"
    )
    files = {"file": ("constituents.csv", io.BytesIO(csv_valid.encode("utf-8")), "text/csv")}
    validate = await client.post(
        "/v1/imports",
        data={"module": "investing-constituents"},
        files=files,
        cookies=creds["cookies"],
    )
    assert validate.status_code == 200
    body = validate.json()
    assert body["import_batch"]["status"] == "validated"
    assert body["error_summary"]["total_errors"] == 0
    import_id = body["import_batch"]["public_id"]

    # Commit the import
    commit = await client.post(f"/v1/imports/{import_id}/commit", cookies=creds["cookies"])
    assert commit.status_code == 200
    assert commit.json()["inserted_rows"] == 2
    assert commit.json()["import_batch"]["status"] == "completed"

    # Verify that DB contains the constituents and companies are created
    async with postgres.async_session_maker() as session:
        # Check companies are created
        company_aapl = (
            await session.execute(
                select(Company).where(
                    Company.workspace_id == workspace_id, Company.name == "Apple Inc"
                )
            )
        ).scalar_one()
        assert company_aapl.ticker == "AAPL"

        company_msft = (
            await session.execute(
                select(Company).where(
                    Company.workspace_id == workspace_id, Company.name == "Microsoft Corp"
                )
            )
        ).scalar_one()
        assert company_msft.ticker == "MSFT"

        # Check constituents
        consts = (
            (
                await session.execute(
                    select(InstrumentConstituent).where(
                        InstrumentConstituent.instrument_id == etf.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(consts) == 2
        weights = {c.constituent_company_id: c.weight for c in consts}
        assert weights[company_aapl.id] == Decimal("0.60000000")
        assert weights[company_msft.id] == Decimal("0.40000000")
        for c in consts:
            assert c.source == "csv_import"

    # Test F: Overwrite behavior (subsequent import of different constituents on same date replaces existing ones)
    csv_overwrite = (
        "instrument_symbol,company_name,company_ticker,weight,as_of_date\n"
        "UMMA,NVIDIA Corp,NVDA,1.00,2026-06-14\n"
    )
    files = {"file": ("constituents.csv", io.BytesIO(csv_overwrite.encode("utf-8")), "text/csv")}
    validate = await client.post(
        "/v1/imports",
        data={"module": "investing-constituents"},
        files=files,
        cookies=creds["cookies"],
    )
    assert validate.status_code == 200
    body = validate.json()
    assert body["import_batch"]["status"] == "validated"
    import_id_2 = body["import_batch"]["public_id"]

    commit_2 = await client.post(f"/v1/imports/{import_id_2}/commit", cookies=creds["cookies"])
    assert commit_2.status_code == 200
    assert commit_2.json()["inserted_rows"] == 1

    # Verify that NVIDIA has replaced Apple Inc and Microsoft Corp
    async with postgres.async_session_maker() as session:
        # Check Nvidia company was created
        company_nvda = (
            await session.execute(
                select(Company).where(
                    Company.workspace_id == workspace_id, Company.name == "NVIDIA Corp"
                )
            )
        ).scalar_one()
        assert company_nvda.ticker == "NVDA"

        # Check constituents: only NVDA should exist for UMMA on 2026-06-14
        consts = (
            (
                await session.execute(
                    select(InstrumentConstituent).where(
                        InstrumentConstituent.instrument_id == etf.id,
                        InstrumentConstituent.as_of_date
                        == datetime.strptime("2026-06-14", "%Y-%m-%d").date(),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(consts) == 1
        assert consts[0].constituent_company_id == company_nvda.id
        assert consts[0].weight == Decimal("1.00000000")


@pytest.mark.asyncio
async def test_import_spendee_csv_with_author_column(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])

    # Create the "Main Wallet" account in the workspace
    acct_resp = await client.post(
        "/v1/finance/accounts",
        json={"name": "Main Wallet", "account_type": "bank", "default_currency_code": "USD"},
        cookies=creds["cookies"],
    )
    assert acct_resp.status_code == 201
    account_public_id = acct_resp.json()["public_id"]

    # Spendee export format WITH Author column
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

    txs = await client.get("/v1/spending/transactions", cookies=creds["cookies"])
    assert txs.status_code == 200
    assert txs.json()["total"] == 1
    row = txs.json()["items"][0]
    assert row["account_id"] == account_public_id
    assert row["amount"] == "3700.00"
    assert row["type"] == "expense"


@pytest.mark.asyncio
async def test_import_capital_transfers_validates_and_commits(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])

    # Create from_account and to_account
    from_acct = await client.post(
        "/v1/finance/accounts",
        json={"name": "ICICI", "account_type": "bank", "default_currency_code": "INR"},
        cookies=creds["cookies"],
    )
    assert from_acct.status_code == 201

    to_acct = await client.post(
        "/v1/finance/accounts",
        json={"name": "GROWW", "account_type": "brokerage", "default_currency_code": "INR"},
        cookies=creds["cookies"],
    )
    assert to_acct.status_code == 201

    # Enable INR currency for workspace
    # Since currency bootstrap automatically enables defaults, and INR was set, it should be active.

    csv_content = (
        "occurred_at,from_account,to_account,from_currency,to_currency,gross_amount,net_amount_received,notes\n"
        "2026-02-17T00:50:58+00:00,ICICI,GROWW,INR,INR,50000.00,50000.00,SIP Investment\n"
    )
    files = {"file": ("transfers.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
    data = {"module": "finance-transfers"}
    validate = await client.post("/v1/imports", data=data, files=files, cookies=creds["cookies"])
    assert validate.status_code == 200, validate.text
    payload = validate.json()
    assert payload["import_batch"]["status"] == "validated"
    assert payload["error_summary"]["total_errors"] == 0
    import_id = payload["import_batch"]["public_id"]

    commit = await client.post(f"/v1/imports/{import_id}/commit", cookies=creds["cookies"])
    assert commit.status_code == 200, commit.text

    # Verify transfers list contains the imported transfer
    transfers_resp = await client.get("/v1/finance/transfers", cookies=creds["cookies"])
    assert transfers_resp.status_code == 200
    transfers = transfers_resp.json()["items"]
    assert len(transfers) == 1
    assert transfers[0]["from_account_name"] == "ICICI"
    assert transfers[0]["to_account_name"] == "GROWW"
    assert transfers[0]["gross_amount"] == "50000.00"
    assert transfers[0]["notes"] == "SIP Investment"

    # Roll back
    rollback_resp = await client.delete(f"/v1/imports/{import_id}", cookies=creds["cookies"])
    assert rollback_resp.status_code == 204

    # Verify transfer was deleted
    transfers_resp2 = await client.get("/v1/finance/transfers", cookies=creds["cookies"])
    assert transfers_resp2.status_code == 200
    assert len(transfers_resp2.json()["items"]) == 0


@pytest.mark.asyncio
async def test_historical_transfer_import_after_orders_updates_cash_balance(
    client: AsyncClient,
):
    """
    Regression test: a historical transfer (occurred_at in the past) committed
    AFTER investing orders must still be visible as the latest cash balance.

    The bug: get_latest_for_account_currency ordered by as_of DESC.  Orders always
    write balances with as_of=now(), so a historical transfer's balance record
    (as_of=past) was permanently shadowed — the next order saw the stale balance.
    Fix: order by created_at DESC so the most recently written record wins.
    """
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])

    # Create accounts
    bank = await client.post(
        "/v1/finance/accounts",
        json={"name": "BANK", "account_type": "bank", "default_currency_code": "INR"},
        cookies=creds["cookies"],
    )
    assert bank.status_code == 201

    broker = await client.post(
        "/v1/finance/accounts",
        json={"name": "BROKER", "account_type": "brokerage", "default_currency_code": "INR"},
        cookies=creds["cookies"],
    )
    assert broker.status_code == 201

    async def _commit_import(csv: str, module: str) -> str:
        files = {"file": ("data.csv", io.BytesIO(csv.encode()), "text/csv")}
        r = await client.post(
            "/v1/imports", data={"module": module}, files=files, cookies=creds["cookies"]
        )
        assert r.status_code == 200, r.text
        assert r.json()["import_batch"]["status"] == "validated"
        batch_id = r.json()["import_batch"]["public_id"]
        commit = await client.post(f"/v1/imports/{batch_id}/commit", cookies=creds["cookies"])
        assert commit.status_code in (200, 202), commit.text
        return batch_id

    # Step 1: seed transfer at a historical date — credits 10000 INR to BROKER
    await _commit_import(
        "occurred_at,from_account,to_account,from_currency,to_currency,"
        "gross_amount,net_amount_received,notes,from_module,to_module\n"
        "2023-01-01T00:00:00+00:00,BANK,BROKER,INR,INR,10000,10000,,spending,investing\n",
        "finance-transfers",
    )

    # Step 2: place an order that consumes 9000 INR — balance should become 1000
    # (balance record as_of=now() overwrites the transfer's as_of=2023-01-01 in the old bug)
    await _commit_import(
        "order_type,symbol,instrument_type,instrument_name,account_name,"
        "quantity,price_per_unit,currency,brokerage_fee,tax_amount,other_fees,"
        "occurred_at,exchange_name,notes\n"
        "buy,TESTFUND,mutual_fund,Test Fund,BROKER,900,10,INR,0,0,0,"
        "2023-02-01T00:00:00+00:00,,\n",
        "investing-orders",
    )

    # Step 3: import ANOTHER historical transfer (occurred_at older than the order's as_of)
    # This is the scenario that was broken: the new balance must be 1000 + 5000 = 6000
    await _commit_import(
        "occurred_at,from_account,to_account,from_currency,to_currency,"
        "gross_amount,net_amount_received,notes,from_module,to_module\n"
        "2022-06-01T00:00:00+00:00,BANK,BROKER,INR,INR,5000,5000,,spending,investing\n",
        "finance-transfers",
    )

    # Step 4: place a 5500 INR order — must succeed (6000 available, NOT the stale 1000)
    files = {
        "file": (
            "data.csv",
            io.BytesIO(
                b"order_type,symbol,instrument_type,instrument_name,account_name,"
                b"quantity,price_per_unit,currency,brokerage_fee,tax_amount,other_fees,"
                b"occurred_at,exchange_name,notes\n"
                b"buy,TESTFUND2,mutual_fund,Test Fund 2,BROKER,550,10,INR,0,0,0,"
                b"2023-03-01T00:00:00+00:00,,\n"
            ),
            "text/csv",
        )
    }
    r = await client.post(
        "/v1/imports", data={"module": "investing-orders"}, files=files, cookies=creds["cookies"]
    )
    assert r.status_code == 200, r.text
    batch_id = r.json()["import_batch"]["public_id"]
    commit = await client.post(f"/v1/imports/{batch_id}/commit", cookies=creds["cookies"])
    assert commit.status_code in (200, 202), commit.text
    result = commit.json()
    # Must not fail with insufficient balance
    assert result["import_batch"]["status"] in ("completed", "committing"), (
        f"Expected completed/committing, got: {result['import_batch']['status']} — "
        f"error: {result['import_batch'].get('commit_error')}"
    )
