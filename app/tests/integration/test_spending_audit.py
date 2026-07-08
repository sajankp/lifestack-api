"""
E2E audit logging test for the Spending module (categories, transactions, budgets).

Co-verification principle:
  Each assertion block opens ONE session and verifies BOTH the entity's DB state
  AND the audit log fields in the same pass. This proves the audit log is a
  faithful mirror of reality — not just that a log entry was written, but that
  what it records matches what is actually stored.

Full stack path: HTTP API → router → {Category,Transaction,Budget}Service
                           → Repository + AuditLogger → DB.
"""

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy import select

from app.core.audit import AuditLog
from app.core.database import postgres
from app.spending.models import SpendingBudget, SpendingCategory, SpendingTransaction


@pytest.mark.asyncio
async def test_spending_audit_logging_e2e(client: AsyncClient):
    # --- Setup: register and authenticate a real user ---
    user_data = {
        "email": "spending_audit_e2e@example.com",
        "username": "spend_audit",
        "password": "TestPass123!",
    }
    reg_res = await client.post("/v1/auth/register", json=user_data)
    assert reg_res.status_code == 200

    login_res = await client.post(
        "/v1/auth/login",
        data={"username": user_data["username"], "password": "TestPass123!"},
    )
    assert login_res.status_code == 200
    cookies = dict(login_res.cookies)

    # =========================================================================
    # Category: create → update → delete
    # =========================================================================

    # --- Create category ---
    cat_payload = {"name": "Audit Gym", "color": "#00FF00", "icon": "🏋️"}
    create_cat_res = await client.post("/v1/spending/categories", json=cat_payload, cookies=cookies)
    assert create_cat_res.status_code == 201
    api_cat = create_cat_res.json()
    cat_uuid = api_cat["public_id"]

    async with postgres.async_session_maker() as session:
        db_cat = (
            await session.execute(
                select(SpendingCategory).where(SpendingCategory.public_id == cat_uuid)
            )
        ).scalar_one()

        # Entity in DB matches what API returned
        assert db_cat.name == "Audit Gym"
        assert db_cat.color == "#00FF00"
        assert api_cat["name"] == db_cat.name
        assert api_cat["color"] == db_cat.color

        # Audit log faithfully mirrors DB entity
        audit = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.entity_id == db_cat.id)
                .where(AuditLog.entity_type == "spending_category")
            )
        ).scalar_one()
        assert audit.action == "create"
        assert audit.module == "spending"
        assert audit.entity_id == db_cat.id
        assert audit.details["entity_public_id"] == str(db_cat.public_id)
        assert audit.details["before"] is None
        assert audit.details["after"]["name"] == db_cat.name  # mirrors DB, not request payload
        assert audit.details["after"]["color"] == db_cat.color

        cat_id = db_cat.id

    # --- Update category ---
    update_cat_res = await client.patch(
        f"/v1/spending/categories/{cat_uuid}", json={"name": "Audit Fitness"}, cookies=cookies
    )
    assert update_cat_res.status_code == 200
    api_updated_cat = update_cat_res.json()

    async with postgres.async_session_maker() as session:
        db_cat = (
            await session.execute(select(SpendingCategory).where(SpendingCategory.id == cat_id))
        ).scalar_one()

        # DB entity reflects the update
        assert db_cat.name == "Audit Fitness"
        assert api_updated_cat["name"] == db_cat.name

        # Two audit logs now; update log captures the delta
        logs = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_id == cat_id)
                    .where(AuditLog.entity_type == "spending_category")
                    .order_by(AuditLog.timestamp.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(logs) == 2

        update_log = logs[1]
        assert update_log.action == "update"
        assert update_log.details["before"]["name"] == "Audit Gym"  # old DB value
        assert update_log.details["after"]["name"] == db_cat.name  # current DB value
        assert "name" in update_log.details["changed_fields"]

    # =========================================================================
    # Transaction: create → update → delete
    # =========================================================================

    account_res = await client.post(
        "/v1/finance/accounts",
        json={"name": "Wallet", "account_type": "wallet", "default_currency_code": "USD"},
        cookies=cookies,
    )
    assert account_res.status_code == 201, account_res.text
    account_id = account_res.json()["public_id"]

    tx_payload = {
        "category_id": cat_uuid,
        "account_id": account_id,
        "amount": "120.50",
        "type": "expense",
        "occurred_at": datetime.now(UTC).isoformat(),
        "description": "Gym membership",
    }
    create_tx_res = await client.post("/v1/spending/transactions", json=tx_payload, cookies=cookies)
    assert create_tx_res.status_code == 201
    api_tx = create_tx_res.json()
    tx_uuid = api_tx["public_id"]

    async with postgres.async_session_maker() as session:
        db_tx = (
            await session.execute(
                select(SpendingTransaction).where(SpendingTransaction.public_id == tx_uuid)
            )
        ).scalar_one()

        # Entity in DB matches API response
        assert str(db_tx.amount) == "120.50"
        assert db_tx.description == "Gym membership"

        # Audit log mirrors DB
        audit = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.entity_id == db_tx.id)
                .where(AuditLog.entity_type == "spending_transaction")
            )
        ).scalar_one()
        assert audit.action == "create"
        assert audit.module == "spending"
        assert audit.details["before"] is None
        assert audit.details["after"]["amount"] == str(db_tx.amount)  # mirrors DB
        assert audit.details["after"]["description"] == db_tx.description

        tx_id = db_tx.id

    # --- Update transaction ---
    update_tx_res = await client.patch(
        f"/v1/spending/transactions/{tx_uuid}", json={"amount": "130.00"}, cookies=cookies
    )
    assert update_tx_res.status_code == 200

    async with postgres.async_session_maker() as session:
        db_tx = (
            await session.execute(
                select(SpendingTransaction).where(SpendingTransaction.id == tx_id)
            )
        ).scalar_one()

        # DB reflects the update
        assert str(db_tx.amount) == "130.00"

        # Two audit logs; update captures delta
        logs = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_id == tx_id)
                    .where(AuditLog.entity_type == "spending_transaction")
                    .order_by(AuditLog.timestamp.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(logs) == 2

        update_log = logs[1]
        assert update_log.action == "update"
        assert update_log.details["before"]["amount"] == "120.50"
        assert update_log.details["after"]["amount"] == str(db_tx.amount)  # mirrors DB
        assert "amount" in update_log.details["changed_fields"]

    # --- Delete transaction ---
    # Capture the current title/amount before deletion for snapshot verification
    async with postgres.async_session_maker() as session:
        db_tx = (
            await session.execute(
                select(SpendingTransaction).where(SpendingTransaction.id == tx_id)
            )
        ).scalar_one()
        amount_before_delete = str(db_tx.amount)

    del_tx_res = await client.delete(f"/v1/spending/transactions/{tx_uuid}", cookies=cookies)
    assert del_tx_res.status_code == 204

    async with postgres.async_session_maker() as session:
        # Entity is gone from DB
        gone_tx = (
            await session.execute(
                select(SpendingTransaction).where(SpendingTransaction.id == tx_id)
            )
        ).scalar()
        assert gone_tx is None

        # Audit log has the delete entry with correct before-snapshot
        logs = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_id == tx_id)
                    .where(AuditLog.entity_type == "spending_transaction")
                    .order_by(AuditLog.timestamp.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(logs) == 3
        delete_log = logs[2]
        assert delete_log.action == "delete"
        assert delete_log.details["before"]["amount"] == amount_before_delete  # last known DB value
        assert delete_log.details["after"] is None

    # =========================================================================
    # Budget: create → update
    # =========================================================================

    budget_payload = {
        "category_id": cat_uuid,
        "amount": "150.00",
        "start_month": "2026-06-01",
    }
    create_budget_res = await client.post(
        "/v1/spending/budgets", json=budget_payload, cookies=cookies
    )
    assert create_budget_res.status_code == 201
    api_budget = create_budget_res.json()
    budget_uuid = api_budget["public_id"]

    async with postgres.async_session_maker() as session:
        db_budget = (
            await session.execute(
                select(SpendingBudget).where(SpendingBudget.public_id == budget_uuid)
            )
        ).scalar_one()

        # Entity in DB matches API response
        assert str(db_budget.amount) == "150.00"

        # Audit log mirrors DB
        audit = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.entity_id == db_budget.id)
                .where(AuditLog.entity_type == "spending_budget")
            )
        ).scalar_one()
        assert audit.action == "create"
        assert audit.module == "spending"
        assert audit.details["before"] is None
        assert audit.details["after"]["amount"] == str(db_budget.amount)  # mirrors DB

        budget_id = db_budget.id

    # --- Update budget ---
    update_budget_res = await client.patch(
        f"/v1/spending/budgets/{budget_uuid}", json={"amount": "180.00"}, cookies=cookies
    )
    assert update_budget_res.status_code == 200

    async with postgres.async_session_maker() as session:
        db_budget = (
            await session.execute(select(SpendingBudget).where(SpendingBudget.id == budget_id))
        ).scalar_one()

        # DB reflects update
        assert str(db_budget.amount) == "180.00"

        # Two audit logs; update captures delta
        logs = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_id == budget_id)
                    .where(AuditLog.entity_type == "spending_budget")
                    .order_by(AuditLog.timestamp.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(logs) == 2
        update_log = logs[1]
        assert update_log.action == "update"
        assert update_log.details["before"]["amount"] == "150.00"
        assert update_log.details["after"]["amount"] == str(db_budget.amount)  # mirrors DB
        assert "amount" in update_log.details["changed_fields"]

    # =========================================================================
    # Category delete — requires budget deleted first (FK constraint)
    # =========================================================================
    async with postgres.async_session_maker() as session:
        db_cat = (
            await session.execute(select(SpendingCategory).where(SpendingCategory.id == cat_id))
        ).scalar_one()
        name_before_delete = db_cat.name

    # Remove budget directly (FK constraint prevents category delete otherwise)
    async with postgres.async_session_maker() as session:
        await session.execute(sa.delete(SpendingBudget).where(SpendingBudget.id == budget_id))
        await session.commit()

    del_cat_res = await client.delete(f"/v1/spending/categories/{cat_uuid}", cookies=cookies)
    assert del_cat_res.status_code == 204

    async with postgres.async_session_maker() as session:
        # Entity is gone from DB
        gone_cat = (
            await session.execute(select(SpendingCategory).where(SpendingCategory.id == cat_id))
        ).scalar()
        assert gone_cat is None

        # Audit log captures delete with correct before-snapshot
        logs = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_id == cat_id)
                    .where(AuditLog.entity_type == "spending_category")
                    .order_by(AuditLog.timestamp.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(logs) == 3
        delete_log = logs[2]
        assert delete_log.action == "delete"
        assert delete_log.details["before"]["name"] == name_before_delete  # last known DB value
        assert delete_log.details["after"] is None
