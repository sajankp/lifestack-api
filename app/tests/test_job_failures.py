from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.auth.models import User
from app.core.database import postgres
from app.core.job_failures import (
    JobFailure,
    record_job_failure,
    resolve_job_failures,
    scrub_error_message,
)
from app.platform.models import Workspace


@pytest.fixture(autouse=True)
async def seed_job_failure_test_data(override_database_url):
    async with postgres.async_session_maker() as session:
        session.add(
            User(
                id=1,
                email="owner@example.com",
                username="owner",
                hashed_password="hashed_password_here",
            )
        )
        for wid in (801, 802):
            session.add(Workspace(id=wid, name=f"Workspace {wid}"))
        await session.commit()


def test_scrub_error_message_redacts_email_digits_and_tokens():
    message = (
        "failed for user jane.doe@example.com account 12345678901 "
        "token abcdefghijklmnopqrstuvwxyz0123"
    )

    scrubbed = scrub_error_message(message)

    assert "jane.doe@example.com" not in scrubbed
    assert "12345678901" not in scrubbed
    assert "abcdefghijklmnopqrstuvwxyz0123" not in scrubbed
    assert "[REDACTED_EMAIL]" in scrubbed
    assert "[REDACTED_NUMBER]" in scrubbed
    assert "[REDACTED_TOKEN]" in scrubbed


@pytest.mark.asyncio
async def test_record_job_failure_writes_global_row(postgres_container, override_database_url):
    async with postgres.async_session_maker() as session:
        await record_job_failure(
            session,
            job_name="fx_rate_ingestion_job",
            workspace_id=None,
            exc=ValueError("rate feed unreachable for user jane@example.com"),
            attempts=3,
            first_failed_at=datetime.now(UTC),
        )

        rows = (
            (
                await session.execute(
                    select(JobFailure).where(JobFailure.job_name == "fx_rate_ingestion_job")
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 1
    row = rows[0]
    assert row.workspace_id is None
    assert row.error_type == "ValueError"
    assert "jane@example.com" not in row.error_message
    assert row.attempts == 3
    assert row.resolved_at is None
    assert row.notified_at is None


@pytest.mark.asyncio
async def test_record_job_failure_writes_workspace_scoped_row(
    postgres_container, override_database_url
):
    async with postgres.async_session_maker() as session:
        await record_job_failure(
            session,
            job_name="investment_closing_prices_job",
            workspace_id=801,
            exc=RuntimeError("boom"),
            attempts=1,
            first_failed_at=datetime.now(UTC),
        )

        rows = (
            (
                await session.execute(
                    select(JobFailure).where(JobFailure.job_name == "investment_closing_prices_job")
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 1
    assert rows[0].workspace_id == 801


@pytest.mark.asyncio
async def test_auto_resolve_only_touches_matching_open_rows(
    postgres_container, override_database_url
):
    async with postgres.async_session_maker() as session:
        async with session.begin():
            session.add(
                JobFailure(
                    job_name="investment_closing_prices_job",
                    workspace_id=801,
                    error_type="RuntimeError",
                    error_message="boom",
                    attempts=1,
                    first_failed_at=datetime.now(UTC),
                )
            )
            session.add(
                JobFailure(
                    job_name="investment_closing_prices_job",
                    workspace_id=802,
                    error_type="RuntimeError",
                    error_message="boom",
                    attempts=1,
                    first_failed_at=datetime.now(UTC),
                )
            )
            session.add(
                JobFailure(
                    job_name="fx_rate_ingestion_job",
                    workspace_id=None,
                    error_type="ValueError",
                    error_message="boom",
                    attempts=1,
                    first_failed_at=datetime.now(UTC),
                )
            )

        await resolve_job_failures(
            session, job_name="investment_closing_prices_job", workspace_id=801
        )

        rows = (await session.execute(select(JobFailure).order_by(JobFailure.id))).scalars().all()

    by_workspace = {(r.job_name, r.workspace_id): r.resolved_at for r in rows}
    assert by_workspace[("investment_closing_prices_job", 801)] is not None
    assert by_workspace[("investment_closing_prices_job", 802)] is None
    assert by_workspace[("fx_rate_ingestion_job", None)] is None


@pytest.mark.asyncio
async def test_auto_resolve_scopes_global_rows_by_null_workspace(
    postgres_container, override_database_url
):
    async with postgres.async_session_maker() as session:
        async with session.begin():
            session.add(
                JobFailure(
                    job_name="fx_rate_ingestion_job",
                    workspace_id=None,
                    error_type="ValueError",
                    error_message="boom",
                    attempts=1,
                    first_failed_at=datetime.now(UTC),
                )
            )

        await resolve_job_failures(session, job_name="fx_rate_ingestion_job", workspace_id=None)

        rows = (
            (
                await session.execute(
                    select(JobFailure).where(JobFailure.job_name == "fx_rate_ingestion_job")
                )
            )
            .scalars()
            .all()
        )

    assert rows[0].resolved_at is not None
