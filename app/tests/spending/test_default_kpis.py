"""Default KPI seeding (KpiService.provision_default_kpis).

The custom-KPI surface is empty on arrival: the dashboard "Custom KPIs"
card returns null when a workspace has no KPIs, and /spending/kpis has
nothing to show. These tests pin the seeding contract:

- seeds exactly the three spec-077 metric types
- seeds no targets (a target is a personal commitment; a wrong default
  would fire a spurious breach todo)
- is a no-op when the workspace already has KPIs (so deleting the
  defaults does not bring them back)
- is a no-op when the workspace has no active account, or spans several
  currencies (a workspace-level KPI would be invalid in v1)
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.spending.models import KpiMetricType, KpiWindow
from app.spending.service import DEFAULT_KPIS, KpiService


def _account(name: str, currency: str, active: bool = True):
    return SimpleNamespace(
        id=1,
        name=name,
        default_currency_code=currency,
        is_active=active,
        is_active_currency=currency,
    )


def _service(accounts, existing_kpis=()):
    repo = MagicMock()
    repo.get_all = AsyncMock(return_value=(list(existing_kpis), len(existing_kpis)))
    repo.create = AsyncMock(side_effect=lambda k: k)

    account_repo = MagicMock()
    account_repo.list_workspace_accounts = AsyncMock(return_value=(accounts, len(accounts)))

    return KpiService(
        kpi_repo=repo,
        category_repo=MagicMock(),
        group_repo=MagicMock(),
        account_repo=account_repo,
        transaction_repo=MagicMock(),
    )


@pytest.mark.asyncio
async def test_seeds_the_three_default_kpis() -> None:
    service = _service([_account("Bank", "USD")])

    created = await service.provision_default_kpis(1)

    assert created == 3
    assert service.kpi_repo.create.await_count == 3

    created_kpis = [c.args[0] for c in service.kpi_repo.create.await_args_list]
    assert {k.metric_type for k in created_kpis} == {
        KpiMetricType.spend_total,
        KpiMetricType.income_total,
        KpiMetricType.net_cash_flow,
    }
    assert all(k.evaluation_window == KpiWindow.calendar_month for k in created_kpis)
    assert all(k.workspace_id == 1 for k in created_kpis)
    # Currency pinned to the workspace's single active currency.
    assert all(k.currency_code == "USD" for k in created_kpis)


@pytest.mark.asyncio
async def test_seeds_no_targets() -> None:
    """A seeded target would fire a breach todo on day one."""
    service = _service([_account("Bank", "USD")])

    await service.provision_default_kpis(1)

    created_kpis = [c.args[0] for c in service.kpi_repo.create.await_args_list]
    assert all(k.target_value is None for k in created_kpis)
    assert all(k.target_direction is None for k in created_kpis)


@pytest.mark.asyncio
async def test_is_a_noop_when_the_workspace_already_has_kpis() -> None:
    service = _service([_account("Bank", "USD")], existing_kpis=[SimpleNamespace(id=1)])

    created = await service.provision_default_kpis(1)

    assert created == 0
    service.kpi_repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_is_a_noop_without_an_active_account() -> None:
    """A KPI cannot be evaluated over nothing."""
    service = _service([])

    created = await service.provision_default_kpis(1)

    assert created == 0
    service.kpi_repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_is_a_noop_for_a_mixed_currency_workspace() -> None:
    """A workspace-level KPI spanning several currencies is invalid in v1."""
    service = _service([_account("Bank", "USD"), _account("Wallet", "INR")])

    created = await service.provision_default_kpis(1)

    assert created == 0
    service.kpi_repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_is_a_noop_when_all_accounts_are_inactive() -> None:
    service = _service([_account("Bank", "USD", active=False)])

    created = await service.provision_default_kpis(1)

    assert created == 0
    service.kpi_repo.create.assert_not_called()


def test_default_kpis_use_only_supported_metric_types() -> None:
    """Guard against a default that the v1 evaluator cannot compute."""
    supported = {KpiMetricType.spend_total, KpiMetricType.income_total, KpiMetricType.net_cash_flow}
    assert {spec["metric_type"] for spec in DEFAULT_KPIS} <= supported
    assert len(DEFAULT_KPIS) == 3
    assert all("target_value" not in spec for spec in DEFAULT_KPIS)
    assert all(isinstance(spec["name"], str) and spec["name"] for spec in DEFAULT_KPIS)
