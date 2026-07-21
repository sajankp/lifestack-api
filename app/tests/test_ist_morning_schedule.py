"""spec-089: deterministic IST-morning daily job schedule.

Replaces jitter with fixed UTC cron times so the schedule respects real data
dependencies -- the regression this spec exists to fix is bhavcopy_price_feed
(the preferred official NSE price source) racing investment_closing_prices
(Yahoo fallback) when both carried +-60min jitter (see a025a1a). Asserts exact
registered trigger fields (not ranges) and the full monotonic ordering across
the UTC-midnight boundary.
"""

import pytest

from app.config import settings
from app.core.scheduler import scheduler
from app.main import app, lifespan

# (job_id, hour_utc, minute_utc) per spec-089's schedule table.
FIXED_SCHEDULE = [
    ("export_cleanup", 21, 30),
    ("session_cleanup", 21, 45),
    ("import_preview_cleanup", 22, 0),
    ("fx_rate_ingestion", 22, 15),
    ("recurring_transactions", 22, 30),
    ("bhavcopy_price_feed", 22, 45),
    ("investment_closing_prices", 23, 0),
    ("net_worth_snapshot", 23, 15),
    ("dashboard_insights", 23, 30),
    ("morning_briefing", 23, 45),
    ("job_failure_digest", 0, 0),
]


def _cron_field(job_id: str, field_name: str) -> str:
    job = scheduler.get_job(job_id)
    assert job is not None, f"job {job_id!r} is not registered"
    for f in job.trigger.fields:
        if f.name == field_name:
            return str(f)
    raise AssertionError(f"cron field {field_name!r} not found on job {job_id!r}")


def _utc_minutes_since_2130(job_id: str) -> int:
    """Minutes elapsed since 21:30 UTC, wrapping the 00:00/00:15 jobs into the
    "next day" so ordering comparisons work across the UTC-midnight boundary
    the way real wall-clock time does."""
    hour = int(_cron_field(job_id, "hour"))
    minute = int(_cron_field(job_id, "minute"))
    total = hour * 60 + minute
    if hour < 12:  # early-UTC jobs (00:00, 00:15) are the next calendar day
        total += 24 * 60
    return total - (21 * 60 + 30)


@pytest.fixture
async def running_scheduler(override_database_url, monkeypatch):
    """Run the real app lifespan with the scheduler enabled so main.py's
    actual registration table -- not a hand-rolled stand-in -- is what gets
    asserted on."""
    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", True)
    async with lifespan(app):
        yield


@pytest.mark.asyncio
async def test_fixed_daily_jobs_registered_at_exact_utc_times_no_jitter(running_scheduler):
    """Every retimed job lands on its exact fixed hour/minute -- not a range --
    proving jitter is off for these jobs."""
    for job_id, hour, minute in FIXED_SCHEDULE:
        assert _cron_field(job_id, "hour") == str(hour), f"{job_id} hour mismatch"
        assert _cron_field(job_id, "minute") == str(minute), f"{job_id} minute mismatch"


@pytest.mark.asyncio
async def test_job_health_heartbeat_registered_monday_after_digest(running_scheduler):
    job = scheduler.get_job("job_health_heartbeat")
    assert job is not None
    assert _cron_field("job_health_heartbeat", "day_of_week") == "mon"
    assert _cron_field("job_health_heartbeat", "hour") == "0"
    assert _cron_field("job_health_heartbeat", "minute") == "15"


@pytest.mark.asyncio
async def test_bhavcopy_strictly_precedes_investment_closing_prices(running_scheduler):
    """The regression this spec fixes: bhavcopy (preferred official NSE close)
    must run before investment_closing_prices (Yahoo fallback), or INR
    holdings silently get priced from the fallback instead."""
    assert _utc_minutes_since_2130("bhavcopy_price_feed") < _utc_minutes_since_2130(
        "investment_closing_prices"
    )


@pytest.mark.asyncio
async def test_full_dependency_chain_is_monotonic_across_midnight(running_scheduler):
    """fx < closing < net_worth < insights < briefing < digest, in real
    wall-clock order -- even though digest's 00:00 UTC is numerically smaller
    than briefing's 23:45 UTC, it runs later in real time (next calendar day)."""
    chain = [
        "fx_rate_ingestion",
        "investment_closing_prices",
        "net_worth_snapshot",
        "dashboard_insights",
        "morning_briefing",
        "job_failure_digest",
    ]
    times = [_utc_minutes_since_2130(job_id) for job_id in chain]
    assert times == sorted(times), f"chain not monotonic: {list(zip(chain, times, strict=True))}"


@pytest.mark.asyncio
async def test_investment_closing_prices_no_earlier_than_us_close_floor(running_scheduler):
    """23:00 UTC is the binding floor: ~1h margin past the worst-case (EST)
    US market close settle, so valuations aren't computed on stale/missing
    data."""
    hour = int(_cron_field("investment_closing_prices", "hour"))
    minute = int(_cron_field("investment_closing_prices", "minute"))
    assert (hour, minute) >= (23, 0)


@pytest.mark.asyncio
async def test_interval_and_weekly_summary_jobs_unchanged(running_scheduler):
    """spec-089 retimes only the fixed daily-cron cluster -- interval jobs and
    the hourly cadence-gated weekly_summary cron are untouched."""
    assert scheduler.get_job("budget_guardrails") is not None
    assert scheduler.get_job("kpi_guardrails") is not None
    assert scheduler.get_job("push_delivery") is not None
    assert scheduler.get_job("email_delivery") is not None
    assert scheduler.get_job("todo_reminder") is not None
    assert scheduler.get_job("medication_reminder") is not None

    weekly_summary_job = scheduler.get_job("weekly_summary")
    assert weekly_summary_job is not None
    assert _cron_field("weekly_summary", "minute") == "30"
