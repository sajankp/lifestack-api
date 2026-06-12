import sys
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from app.cli.run import main


@pytest.mark.asyncio
async def test_cli_runner_successful_execution():
    # Mock budget_guardrails_job
    mock_job = AsyncMock()
    with (
        patch("app.cli.run.JOBS", {"budget_guardrails": mock_job}),
        patch.object(sys, "argv", ["run.py", "budget_guardrails", "--workspace-id", "123"]),
    ):
        await main()
        mock_job.assert_called_once_with(workspace_id=123)


@pytest.mark.asyncio
async def test_cli_runner_weekly_summary_with_week_start():
    mock_job = AsyncMock()
    with (
        patch("app.cli.run.JOBS", {"weekly_summary": mock_job}),
        patch.object(
            sys,
            "argv",
            ["run.py", "weekly_summary", "--workspace-id", "456", "--week-start", "2026-06-01"],
        ),
    ):
        await main()
        mock_job.assert_called_once_with(workspace_id=456, week_start=date(2026, 6, 1))


@pytest.mark.asyncio
async def test_cli_runner_invalid_week_start_format():
    mock_job = AsyncMock()
    with (
        patch("app.cli.run.JOBS", {"weekly_summary": mock_job}),
        patch.object(
            sys,
            "argv",
            ["run.py", "weekly_summary", "--workspace-id", "456", "--week-start", "invalid-date"],
        ),
    ):
        with pytest.raises(SystemExit) as exc_info:
            await main()
        assert exc_info.value.code == 1
        mock_job.assert_not_called()


@pytest.mark.asyncio
async def test_cli_runner_week_start_not_supported_on_other_jobs():
    mock_job = AsyncMock()
    with (
        patch("app.cli.run.JOBS", {"budget_guardrails": mock_job}),
        patch.object(
            sys,
            "argv",
            ["run.py", "budget_guardrails", "--workspace-id", "456", "--week-start", "2026-06-01"],
        ),
    ):
        with pytest.raises(SystemExit) as exc_info:
            await main()
        assert exc_info.value.code == 1
        mock_job.assert_not_called()


@pytest.mark.asyncio
async def test_cli_runner_workspace_id_not_supported_on_global_jobs():
    mock_job = AsyncMock()
    with (
        patch("app.cli.run.JOBS", {"fx_rate_ingestion": mock_job}),
        patch.object(
            sys,
            "argv",
            ["run.py", "fx_rate_ingestion", "--workspace-id", "456"],
        ),
    ):
        with pytest.raises(SystemExit) as exc_info:
            await main()
        assert exc_info.value.code == 1
        mock_job.assert_not_called()
