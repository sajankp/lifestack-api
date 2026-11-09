"""REST integration tests: LRS remittance summary + stored Rule 115 FX rate reads.

Card t_38616759 acceptance:
1. GET /finance/lrs/summary -- FY total vs USD 250k cap, cap remaining, TCS status
2. GET /finance/fx-rates/{date}?currency= -- stored rate or 404
3. GET /finance/fx-rates/missing -- dates with no stored rate
4. Rates written by ``set_fx_rate`` (WorkspaceTaxProfile.fx_overrides) are queryable
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.auth.models import User
from app.core.database import postgres
from app.finance.lrs_service import IST
from app.finance.models import FxRate, WorkspaceTaxProfile
from app.investing.tax.fy import current_fy_label
from app.platform.models import WorkspaceMembership

_INR_USD_RATE = "0.0117647059"  # USD per INR (~INR 85 per USD)


async def _register_and_login(client: AsyncClient, *, email: str, username: str) -> None:
    register_res = await client.post(
        "/v1/auth/register",
        json={"email": email, "username": username, "password": "TestPass123!"},
    )
    assert register_res.status_code == 200, register_res.text
    login_res = await client.post(
        "/v1/auth/login",
        data={"username": username, "password": "TestPass123!"},
    )
    assert login_res.status_code == 200, login_res.text


async def _workspace_id_for(email: str) -> int:
    async with postgres.async_session_maker() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        membership = (
            await session.execute(
                select(WorkspaceMembership).where(WorkspaceMembership.user_id == user.id)
            )
        ).scalar_one()
        return membership.workspace_id


async def _create_account(client: AsyncClient, name: str, account_type: str, currency: str) -> str:
    res = await client.post(
        "/v1/finance/accounts",
        json={"name": name, "account_type": account_type, "default_currency_code": currency},
    )
    assert res.status_code == 201, res.text
    return res.json()["public_id"]


async def _transfer(
    client: AsyncClient,
    from_account: str,
    to_account: str,
    from_ccy: str,
    to_ccy: str,
    gross: str,
    rate: str | None,
    occurred_at: str,
    *,
    from_module: str = "spending",
    to_module: str = "investing",
) -> None:
    # Bank convention: net = gross * rate (no fees); same-currency transfers use rate 1.
    net = (Decimal(gross) * Decimal(rate)).quantize(Decimal("0.01")) if rate else Decimal(gross)
    res = await client.post(
        "/v1/finance/transfers",
        json={
            "from_module": from_module,
            "to_module": to_module,
            "from_account_id": from_account,
            "to_account_id": to_account,
            "from_currency_code": from_ccy,
            "to_currency_code": to_ccy,
            "gross_amount": gross,
            "fx_rate_used": rate,
            "net_amount_received": str(net),
            "occurred_at": occurred_at,
        },
    )
    assert res.status_code == 201, res.text


async def _setup_accounts(client: AsyncClient) -> tuple[str, str]:
    inr_bank = await _create_account(client, "INR Bank", "bank", "INR")
    usd_brokerage = await _create_account(client, "USD Brokerage", "brokerage", "USD")
    return inr_bank, usd_brokerage


async def _lrs(client: AsyncClient, fy: str | None = "2026-27"):
    params = {"fy": fy} if fy else {}
    return await client.get("/v1/finance/lrs/summary", params=params)


# ----------------------------------------------------------------- LRS summary


@pytest.mark.asyncio
async def test_lrs_summary_empty_workspace(client: AsyncClient):
    await _register_and_login(client, email="lrs-empty@example.com", username="lrs-empty")
    res = await _lrs(client)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["fy_label"] == "2026-27"
    assert body["fy_start"] == "2026-04-01"
    assert body["fy_end"] == "2027-03-31"
    assert body["transfer_count"] == 0
    assert body["total_usd"] == "0.00"
    assert body["cap_usd"] == "250000.00"
    assert body["cap_remaining_usd"] == "250000.00"
    assert body["cap_exceeded"] is False
    assert body["pct_of_cap"] == "0.00"
    assert body["total_inr"] == "0.00"
    assert body["tcs_applicable"] is False
    assert body["tcs_amount_inr"] == "0.00"
    assert body["currency"] == "USD"
    assert body["unconverted_transfer_ids"] == []
    assert isinstance(body["assumptions"], list)


@pytest.mark.asyncio
async def test_lrs_summary_defaults_to_current_fy(client: AsyncClient):
    await _register_and_login(client, email="lrs-dflt@example.com", username="lrs-dflt")
    res = await _lrs(client, fy=None)
    assert res.status_code == 200, res.text
    assert res.json()["fy_label"] == current_fy_label(datetime.now(IST))


@pytest.mark.asyncio
async def test_lrs_summary_counts_only_in_fy_outward_foreign_remittances(client: AsyncClient):
    await _register_and_login(client, email="lrs-basic@example.com", username="lrs-basic")
    inr_bank, usd_brokerage = await _setup_accounts(client)
    inr_brokerage = await _create_account(client, "Zerodha", "brokerage", "INR")

    # Two outward LRS remittances in FY 2026-27 (INR -> USD).
    await _transfer(
        client, inr_bank, usd_brokerage, "INR", "USD",
        "170000.00", _INR_USD_RATE, "2026-08-15T10:00:00+00:00",
    )  # fmt: skip
    await _transfer(
        client, inr_bank, usd_brokerage, "INR", "USD",
        "85000.00", _INR_USD_RATE, "2026-09-10T10:00:00+00:00",
    )  # fmt: skip
    # Repatriation (investing -> spending) is not outward.
    await _transfer(
        client, usd_brokerage, inr_bank, "USD", "INR",
        "500.00", "85.00", "2026-10-01T10:00:00+00:00",
        from_module="investing", to_module="spending",
    )  # fmt: skip
    # Domestic INR brokerage funding is not an LRS remittance.
    await _transfer(
        client, inr_bank, inr_brokerage, "INR", "INR",
        "900000.00", None, "2026-10-02T10:00:00+00:00",
    )  # fmt: skip
    # A remittance in a different FY must not count.
    await _transfer(
        client, inr_bank, usd_brokerage, "INR", "USD",
        "500000.00", _INR_USD_RATE, "2025-08-15T10:00:00+00:00",
    )  # fmt: skip

    body = (await _lrs(client)).json()
    assert body["transfer_count"] == 2
    assert body["total_usd"] == "3000.00"
    assert body["cap_remaining_usd"] == "247000.00"
    assert body["pct_of_cap"] == "1.20"
    assert body["total_inr"] == "255000.00"
    assert body["tcs_applicable"] is False
    assert body["tcs_amount_inr"] == "0.00"

    # The other FY has its own, separate total.
    prev = (await _lrs(client, "2025-26")).json()
    assert prev["transfer_count"] == 1
    assert prev["total_inr"] == "500000.00"


@pytest.mark.asyncio
async def test_lrs_summary_fy_boundary_uses_ist(client: AsyncClient):
    await _register_and_login(client, email="lrs-ist@example.com", username="lrs-ist")
    inr_bank, usd_brokerage = await _setup_accounts(client)
    # 2027-03-31T19:00Z == 2027-04-01 00:30 IST -> FY 2027-28 (not 2026-27).
    await _transfer(
        client, inr_bank, usd_brokerage, "INR", "USD",
        "85000.00", _INR_USD_RATE, "2027-03-31T19:00:00+00:00",
    )  # fmt: skip
    # 2026-03-31T18:00Z == 2026-03-31 23:30 IST -> FY 2025-26 (not 2026-27).
    await _transfer(
        client, inr_bank, usd_brokerage, "INR", "USD",
        "17000.00", _INR_USD_RATE, "2026-03-31T18:00:00+00:00",
    )  # fmt: skip

    assert (await _lrs(client, "2026-27")).json()["transfer_count"] == 0
    next_fy = (await _lrs(client, "2027-28")).json()
    assert next_fy["transfer_count"] == 1
    assert next_fy["total_inr"] == "85000.00"
    assert (await _lrs(client, "2025-26")).json()["total_inr"] == "17000.00"


@pytest.mark.asyncio
async def test_lrs_summary_tcs_threshold_is_10_lakh_from_fy_2025_26(client: AsyncClient):
    await _register_and_login(client, email="lrs-tcs@example.com", username="lrs-tcs")
    inr_bank, usd_brokerage = await _setup_accounts(client)
    await _transfer(
        client, inr_bank, usd_brokerage, "INR", "USD",
        "800000.00", _INR_USD_RATE, "2026-10-01T10:00:00+00:00",
    )  # fmt: skip
    # Same INR 8 lakh in FY 2024-25 (INR 7 lakh threshold).
    await _transfer(
        client, inr_bank, usd_brokerage, "INR", "USD",
        "800000.00", _INR_USD_RATE, "2024-10-01T10:00:00+00:00",
    )  # fmt: skip

    fy2627 = (await _lrs(client, "2026-27")).json()
    assert fy2627["total_inr"] == "800000.00"
    assert fy2627["tcs_threshold_inr"] == "1000000.00"
    assert fy2627["tcs_rate_pct"] == "20.00"
    assert fy2627["tcs_applicable"] is False
    assert fy2627["tcs_amount_inr"] == "0.00"

    fy2425 = (await _lrs(client, "2024-25")).json()
    assert fy2425["tcs_threshold_inr"] == "700000.00"
    assert fy2425["tcs_applicable"] is True
    assert fy2425["tcs_amount_inr"] == "20000.00"  # 20% of (800000 - 700000)


@pytest.mark.asyncio
async def test_lrs_summary_tcs_applies_above_threshold_and_not_at_it(client: AsyncClient):
    await _register_and_login(client, email="lrs-tcs2@example.com", username="lrs-tcs2")
    inr_bank, usd_brokerage = await _setup_accounts(client)
    await _transfer(
        client, inr_bank, usd_brokerage, "INR", "USD",
        "1000000.00", _INR_USD_RATE, "2026-06-01T10:00:00+00:00",
    )  # fmt: skip
    exactly = (await _lrs(client)).json()
    assert exactly["total_inr"] == "1000000.00"
    assert exactly["tcs_applicable"] is False  # TCS only on the excess; none at the threshold
    assert exactly["tcs_amount_inr"] == "0.00"

    await _transfer(
        client, inr_bank, usd_brokerage, "INR", "USD",
        "200000.00", _INR_USD_RATE, "2026-07-01T10:00:00+00:00",
    )  # fmt: skip
    above = (await _lrs(client)).json()
    assert above["total_inr"] == "1200000.00"
    assert above["tcs_applicable"] is True
    assert above["tcs_amount_inr"] == "40000.00"


@pytest.mark.asyncio
async def test_lrs_summary_cap_exceeded(client: AsyncClient):
    await _register_and_login(client, email="lrs-cap@example.com", username="lrs-cap")
    inr_bank, usd_brokerage = await _setup_accounts(client)
    # INR 2.55 crore ~= USD 3,000,000 -> over the USD 250k cap.
    await _transfer(
        client, inr_bank, usd_brokerage, "INR", "USD",
        "25500000.00", _INR_USD_RATE, "2026-06-01T10:00:00+00:00",
    )  # fmt: skip
    body = (await _lrs(client)).json()
    assert body["total_usd"] == "300000.00"
    assert body["cap_exceeded"] is True
    assert body["cap_remaining_usd"] == "0.00"
    assert body["pct_of_cap"] == "120.00"


@pytest.mark.asyncio
async def test_lrs_summary_non_usd_destination_uses_stored_rate_or_flags_unconverted(
    client: AsyncClient,
):
    await _register_and_login(client, email="lrs-gbp@example.com", username="lrs-gbp")
    inr_bank = await _create_account(client, "INR Bank", "bank", "INR")
    gbp_brokerage = await _create_account(client, "GBP Brokerage", "brokerage", "GBP")
    # INR 100,000 at 0.0095 GBP/INR -> GBP 950.00
    await _transfer(
        client, inr_bank, gbp_brokerage, "INR", "GBP",
        "100000.00", "0.0095", "2026-08-15T10:00:00+00:00",
    )  # fmt: skip

    unconverted = (await _lrs(client)).json()
    assert unconverted["transfer_count"] == 1
    assert unconverted["total_inr"] == "100000.00"  # INR side is known regardless
    assert unconverted["total_usd"] == "0.00"
    assert len(unconverted["unconverted_transfer_ids"]) == 1

    async with postgres.async_session_maker() as session:
        session.add(
            FxRate(
                workspace_id=None,
                base_currency_code="GBP",
                quote_currency_code="USD",
                rate=Decimal("1.25"),
                as_of=datetime(2026, 8, 15, 6, 0, tzinfo=UTC),
                fetched_at=datetime(2026, 8, 15, 6, 0, tzinfo=UTC),
                source="test",
            )
        )
        await session.commit()

    converted = (await _lrs(client)).json()
    assert converted["total_usd"] == "1187.50"  # 950 GBP * 1.25
    assert converted["unconverted_transfer_ids"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fy", ["not-a-fy", "2026-28", "1999-00", "2026", "9999-00"])
async def test_lrs_summary_invalid_fy_is_422(client: AsyncClient, fy: str):
    await _register_and_login(client, email="lrs-fy@example.com", username="lrs-fy")
    res = await _lrs(client, fy)
    assert res.status_code == 422, res.text


@pytest.mark.asyncio
async def test_lrs_summary_is_workspace_scoped_and_requires_auth(client: AsyncClient):
    await _register_and_login(client, email="lrs-a@example.com", username="lrs-a")
    inr_bank, usd_brokerage = await _setup_accounts(client)
    await _transfer(
        client, inr_bank, usd_brokerage, "INR", "USD",
        "85000.00", _INR_USD_RATE, "2026-08-15T10:00:00+00:00",
    )  # fmt: skip
    assert (await _lrs(client)).json()["transfer_count"] == 1

    await _register_and_login(client, email="lrs-b@example.com", username="lrs-b")
    other = (await _lrs(client)).json()
    assert other["transfer_count"] == 0
    assert other["total_usd"] == "0.00"

    client.cookies.clear()
    assert (await _lrs(client)).status_code == 401


# -------------------------------------------------------------------- FX rates


async def _seed_overrides(email: str, overrides: dict[str, str]) -> None:
    workspace_id = await _workspace_id_for(email)
    async with postgres.async_session_maker() as session:
        session.add(WorkspaceTaxProfile(workspace_id=workspace_id, fx_overrides=overrides))
        await session.commit()


@pytest.mark.asyncio
async def test_fx_rate_read_returns_404_when_missing_and_does_not_create_profile(
    client: AsyncClient,
):
    email = "fx-read@example.com"
    await _register_and_login(client, email=email, username="fx-read")
    res = await client.get("/v1/finance/fx-rates/2026-01-31", params={"currency": "USD"})
    assert res.status_code == 404, res.text

    workspace_id = await _workspace_id_for(email)
    async with postgres.async_session_maker() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(WorkspaceTaxProfile)
                .where(WorkspaceTaxProfile.workspace_id == workspace_id)
            )
        ).scalar_one()
    assert count == 0  # a read must not insert a profile row


@pytest.mark.asyncio
async def test_fx_rate_read_and_missing_audit(client: AsyncClient):
    email = "fx-audit@example.com"
    await _register_and_login(client, email=email, username="fx-audit")
    # Mirrors what the set_fx_rate MCP tool writes.
    await _seed_overrides(email, {"2026-01-31:USD": "86.50", "2026-02-28:USD": "87.25"})

    res = await client.get("/v1/finance/fx-rates/2026-01-31", params={"currency": "USD"})
    assert res.status_code == 200, res.text
    assert res.json() == {
        "date": "2026-01-31",
        "currency": "USD",
        "rate": "86.50",
        "source": "user_provided",
    }
    # Currency is case-insensitive.
    res = await client.get("/v1/finance/fx-rates/2026-02-28", params={"currency": "usd"})
    assert res.status_code == 200 and res.json()["rate"] == "87.25"

    # No rate for that date, or for that currency -> 404.
    res = await client.get("/v1/finance/fx-rates/2026-02-15", params={"currency": "USD"})
    assert res.status_code == 404, res.text
    res = await client.get("/v1/finance/fx-rates/2026-01-31", params={"currency": "GBP"})
    assert res.status_code == 404, res.text

    res = await client.get(
        "/v1/finance/fx-rates/missing",
        params={"from_date": "2026-01-31", "to_date": "2026-02-02", "currency": "USD"},
    )
    assert res.status_code == 200, res.text
    assert res.json() == {
        "from_date": "2026-01-31",
        "to_date": "2026-02-02",
        "currency": "USD",
        "missing": ["2026-02-01", "2026-02-02"],
        "missing_count": 2,
    }

    # A fully covered single-day range has nothing missing.
    res = await client.get(
        "/v1/finance/fx-rates/missing",
        params={"from_date": "2026-01-31", "to_date": "2026-01-31", "currency": "USD"},
    )
    assert res.json()["missing"] == [] and res.json()["missing_count"] == 0

    # A currency with no stored rates is missing every day.
    res = await client.get(
        "/v1/finance/fx-rates/missing",
        params={"from_date": "2026-01-31", "to_date": "2026-02-01", "currency": "GBP"},
    )
    assert res.json()["missing"] == ["2026-01-31", "2026-02-01"]


@pytest.mark.asyncio
async def test_fx_rates_validation_errors_are_422(client: AsyncClient):
    await _register_and_login(client, email="fx-422@example.com", username="fx-422")
    get = client.get
    assert (
        await get("/v1/finance/fx-rates/2026-13-45", params={"currency": "USD"})
    ).status_code == 422
    assert (
        await get("/v1/finance/fx-rates/2026-01-31", params={"currency": "US"})
    ).status_code == 422
    assert (await get("/v1/finance/fx-rates/2026-01-31")).status_code == 422  # currency required

    missing = "/v1/finance/fx-rates/missing"
    ok = {"from_date": "2026-01-01", "to_date": "2026-01-02", "currency": "USD"}
    assert (await get(missing, params=ok)).status_code == 200
    assert (await get(missing, params={**ok, "currency": "U$D"})).status_code == 422
    assert (await get(missing, params={**ok, "from_date": "2026-01-03"})).status_code == 422
    assert (await get(missing, params={**ok, "from_date": "bad"})).status_code == 422
    # 366 days inclusive is allowed (a leap FY); 367 is not.
    assert (
        await get(missing, params={**ok, "from_date": "2027-04-01", "to_date": "2028-03-31"})
    ).status_code == 200
    assert (
        await get(missing, params={**ok, "from_date": "2027-04-01", "to_date": "2028-04-01"})
    ).status_code == 422


@pytest.mark.asyncio
async def test_fx_rates_are_workspace_scoped_and_require_auth(client: AsyncClient):
    await _register_and_login(client, email="fx-a@example.com", username="fx-a")
    await _seed_overrides("fx-a@example.com", {"2026-01-31:USD": "86.50"})
    assert (
        await client.get("/v1/finance/fx-rates/2026-01-31", params={"currency": "USD"})
    ).status_code == 200

    await _register_and_login(client, email="fx-b@example.com", username="fx-b")
    res = await client.get("/v1/finance/fx-rates/2026-01-31", params={"currency": "USD"})
    assert res.status_code == 404, res.text

    client.cookies.clear()
    res = await client.get("/v1/finance/fx-rates/2026-01-31", params={"currency": "USD"})
    assert res.status_code == 401
    res = await client.get(
        "/v1/finance/fx-rates/missing",
        params={"from_date": "2026-01-01", "to_date": "2026-01-02", "currency": "USD"},
    )
    assert res.status_code == 401
