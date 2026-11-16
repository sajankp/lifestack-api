"""MeasurementService against real Postgres (spec-110)."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.auth.models import User
from app.core.audit import AuditLog, AuditLogger
from app.core.database import postgres
from app.core.exceptions import NotFoundError
from app.health.measurements.models import HealthMeasurement, HealthMetric, HealthRecord
from app.health.measurements.schemas import MetricDefinition
from app.health.measurements.service import HealthMeasurementError, MeasurementService
from app.platform.models import Workspace

WS = 9101
OTHER_WS = 9102
USER = 9101


async def _seed(session, timezone: str = "Asia/Kolkata") -> MeasurementService:
    session.add_all([
        User(
            id=USER,
            email="meas@example.com",
            username="meas_user",
            hashed_password="x",
            timezone=timezone,
        ),
        Workspace(id=WS, name="Meas"),
        Workspace(id=OTHER_WS, name="Other"),
    ])
    await session.flush()
    return MeasurementService.from_session(session)


async def _count(session, model, **where) -> int:
    stmt = select(func.count()).select_from(model)
    for col, val in where.items():
        stmt = stmt.where(getattr(model, col) == val)
    return int((await session.execute(stmt)).scalar_one())


def _lipid_defs() -> list[MetricDefinition]:
    return [
        MetricDefinition(
            name="LDL Cholesterol",
            code="ldl_cholesterol",
            category="lipid",
            canonical_unit="mg/dL",
            unit_conversions={"mmol/L": {"factor": "38.67", "offset": "0"}},
            aliases=["LDL-C", "LDL"],
        ),
        MetricDefinition(name="HbA1c", code="hba1c", category="blood", canonical_unit="%"),
        MetricDefinition(name="Urine protein", code="urine_protein", value_type="text"),
    ]


async def _with_metrics(service: MeasurementService) -> None:
    for definition in _lipid_defs():
        await service.define_metric(user_id=USER, workspace_id=WS, definition=definition)


def _write(service: MeasurementService, items, record=None, **kw):
    return service.record_results(
        user_id=USER,
        workspace_id=WS,
        record=record
        or {
            "kind": "lab_report",
            "title": "Panel",
            "recorded_on": "2026-11-10",
            "source_ref": "r1",
        },
        items=items,
        dry_run=kw.pop("dry_run", False),
        **kw,
    )


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_builtin_body_weight_is_created_once(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        first = await service.ensure_builtin_metrics(WS)
        second = await service.ensure_builtin_metrics(WS)
        assert first.id == second.id
        assert (first.code, first.canonical_unit, first.value_type) == (
            "body_weight",
            "kg",
            "numeric",
        )
        assert await _count(session, HealthMetric, workspace_id=WS) == 1
        # Another workspace gets its own.
        other = await service.ensure_builtin_metrics(OTHER_WS)
        assert other.id != first.id


@pytest.mark.asyncio
async def test_reads_never_create_the_builtin_metric(override_database_url):
    """A GET or a background summary job must not write: only writes create ``body_weight``."""
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        assert await service.list_metrics(WS) == []
        assert await service.list_weight(WS) == ([], 0)
        assert (
            await service.weight_between(WS, datetime(2026, 1, 1, tzinfo=UTC), datetime.now(UTC))
            == []
        )
        window, stats = await service.weight_trend(WS, 30, user_id=USER)
        assert window == [] and stats.count == 0 and stats.latest is None
        with pytest.raises(NotFoundError):
            await service.get_series(WS, "body_weight")
        with pytest.raises(NotFoundError):
            await service.delete_weight(user_id=USER, workspace_id=WS, public_id=uuid.uuid4())
        assert await _count(session, HealthMetric) == 0


@pytest.mark.asyncio
async def test_define_metric_create_update_and_alias_collision(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        await _with_metrics(service)

        metric, outcome = await service.define_metric(
            user_id=USER,
            workspace_id=WS,
            definition=MetricDefinition(
                name="LDL Cholesterol",
                code="ldl_cholesterol",
                category="lipid",
                canonical_unit="mg/dL",
                aliases=["LDL-C", "LDL", "Bad cholesterol"],
                unit_conversions={"mmol/L": {"factor": "38.67", "offset": "0"}},
            ),
        )
        assert outcome == "updated"
        assert "Bad cholesterol" in metric.aliases

        with pytest.raises(
            HealthMeasurementError, match="already resolves to metric 'ldl_cholesterol'"
        ):
            await service.define_metric(
                user_id=USER,
                workspace_id=WS,
                definition=MetricDefinition(
                    name="HDL", code="hdl", canonical_unit="mg/dL", aliases=["ldl-c"]
                ),
            )


@pytest.mark.asyncio
async def test_define_metric_rejects_unit_or_type_change_once_it_has_data(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        await _with_metrics(service)
        out = await _write(service, [{"metric": "hba1c", "value": "5.6", "unit": "%"}])
        assert out["status"] == "success"

        with pytest.raises(HealthMeasurementError, match="canonical unit"):
            await service.define_metric(
                user_id=USER,
                workspace_id=WS,
                definition=MetricDefinition(
                    name="HbA1c", code="hba1c", category="blood", canonical_unit="mmol/mol"
                ),
            )
        with pytest.raises(HealthMeasurementError, match="value_type"):
            await service.define_metric(
                user_id=USER,
                workspace_id=WS,
                definition=MetricDefinition(
                    name="HbA1c", code="hba1c", category="blood", value_type="text"
                ),
            )


@pytest.mark.asyncio
async def test_define_metric_cannot_drop_a_unit_that_measurements_use(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        await _with_metrics(service)
        out = await _write(service, [{"metric": "ldl", "value": "2.5", "unit": "mmol/L"}])
        assert out["status"] == "success"
        with pytest.raises(HealthMeasurementError, match="Cannot drop unit"):
            await service.define_metric(
                user_id=USER,
                workspace_id=WS,
                definition=MetricDefinition(
                    name="LDL Cholesterol",
                    code="ldl_cholesterol",
                    category="lipid",
                    canonical_unit="mg/dL",
                    aliases=["LDL"],
                ),
            )


# --------------------------------------------------------------------------
# Bulk write
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        await _with_metrics(service)
        out = await _write(
            service, [{"metric": "HbA1c", "value": "5.6", "unit": "%"}], dry_run=True
        )
        assert out["status"] == "success"
        assert out["dry_run"] is True
        assert out["items"][0]["status"] == "would_create"
        assert out["counts"]["create"] == 1
        assert await _count(session, HealthMeasurement) == 0
        assert await _count(session, HealthRecord) == 0


@pytest.mark.asyncio
async def test_bulk_write_creates_one_record_with_note_and_provenance(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        await _with_metrics(service)
        out = await _write(
            service,
            [
                {
                    "metric": "LDL-C",
                    "value": "2.5",
                    "unit": "mmol/L",
                    "ref_high": "3.4",
                    "note": "fasting",
                },
                {"metric": "hba1c", "value": "<5.0", "unit": "%"},
                {"metric": "Urine protein", "value": "Negative"},
            ],
            record={
                "kind": "lab_report",
                "title": "Annual panel",
                "recorded_on": "2026-11-10",
                "provider": "Metropolis",
                "note": "Extracted from PDF report 12345 on 2026-11-11",
                "source_ref": "metropolis-12345",
            },
            audit_logger=AuditLogger(session),
        )
        assert out["status"] == "success"
        assert out["record"]["created"] is True
        assert [i["status"] for i in out["items"]] == ["created"] * 3

        record = (await session.execute(select(HealthRecord))).scalar_one()
        assert record.note == "Extracted from PDF report 12345 on 2026-11-11"
        assert record.provider == "Metropolis"
        assert record.source_type == "mcp"
        rows = (await session.execute(select(HealthMeasurement))).scalars().all()
        assert len(rows) == 3
        assert {r.record_id for r in rows} == {record.id}
        assert {r.source_type for r in rows} == {"mcp"}
        assert all(r.measured_at is None for r in rows)  # date-only: no faked time
        ldl = next(r for r in rows if r.unit == "mmol/L")
        assert (ldl.value_num, ldl.ref_high, ldl.note) == (
            Decimal("2.5"),
            Decimal("3.4"),
            "fasting",
        )
        thresh = next(r for r in rows if r.comparator == "<")
        assert thresh.value_num == Decimal("5.0")
        text = next(r for r in rows if r.value_text)
        assert text.value_text == "Negative" and text.value_num is None

        audits = (await session.execute(select(AuditLog))).scalars().all()
        assert len(audits) == 1  # one audit row per call, not per measurement
        assert "Extracted from PDF" not in str(audits[0].details)
        assert "fasting" not in str(audits[0].details)


@pytest.mark.asyncio
async def test_resending_a_report_is_a_noop(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        await _with_metrics(service)
        items = [
            {"metric": "hba1c", "value": "5.6", "unit": "%"},
            {"metric": "ldl", "value": "100", "unit": "mg/dL"},
        ]
        first = await _write(service, items)
        assert first["counts"]["create"] == 2
        second = await _write(service, items)
        assert second["status"] == "success"
        assert second["record"] == {
            "public_id": first["record"]["public_id"],
            "created": False,
            "reused": True,
        }
        assert [i["status"] for i in second["items"]] == ["duplicate", "duplicate"]
        assert {i["match_reason"] for i in second["items"]} == {"record_source_ref"}
        assert await _count(session, HealthMeasurement) == 2
        assert await _count(session, HealthRecord) == 1


@pytest.mark.asyncio
async def test_unknown_metric_is_not_written_and_suggests_names(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        await _with_metrics(service)
        out = await _write(service, [{"metric": "HbA1", "value": "5.6", "unit": "%"}])
        assert out["items"][0]["status"] == "unknown_metric"
        assert "HbA1c" in out["items"][0]["suggestions"]
        assert out["counts"]["unknown_metric"] == 1
        assert await _count(session, HealthMeasurement) == 0
        assert await _count(session, HealthRecord) == 0  # no empty record either


@pytest.mark.asyncio
async def test_create_missing_metrics_previews_then_creates_one_metric(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        items = [
            {"metric": "Vitamin D", "value": "28", "unit": "ng/mL", "measured_on": "2026-11-01"},
            {"metric": "vitamin d", "value": "31", "unit": "ng/mL", "measured_on": "2026-11-09"},
        ]
        preview = await _write(service, items, dry_run=True, create_missing_metrics=True)
        assert preview["counts"]["would_create_metric"] == 1
        assert preview["counts"]["create"] == 2
        assert await _count(session, HealthMetric, code="vitamin_d") == 0

        done = await _write(service, items, create_missing_metrics=True)
        assert done["status"] == "success"
        metric = (
            await session.execute(select(HealthMetric).where(HealthMetric.code == "vitamin_d"))
        ).scalar_one()
        assert (metric.canonical_unit, metric.value_type, metric.category) == (
            "ng/mL",
            "numeric",
            "lab_report",
        )
        assert await _count(session, HealthMeasurement) == 2


@pytest.mark.asyncio
async def test_unit_rules_never_guess(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        await _with_metrics(service)
        out = await _write(
            service,
            [
                {"metric": "ldl", "value": "100"},  # unit missing
                {"metric": "ldl", "value": "100", "unit": "g/L"},  # unregistered
                {"metric": "ldl", "value": "100", "unit": "MG/DL"},  # case-insensitive ok
            ],
        )
        statuses = [i["status"] for i in out["items"]]
        assert statuses == ["invalid", "invalid", "created"]
        assert "unit is required" in out["items"][0]["errors"][0]
        assert "Accepted units: mg/dL, mmol/L" in out["items"][1]["errors"][0]
        assert await _count(session, HealthMeasurement) == 1


@pytest.mark.asyncio
async def test_invalid_items_dates_and_numbers(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        await _with_metrics(service)
        tomorrow2 = (datetime.now(UTC).date() + timedelta(days=3)).isoformat()
        out = await _write(
            service,
            [
                {"metric": "hba1c", "value": "Negative", "unit": "%"},
                {"metric": "hba1c", "value": "5.6", "unit": "%", "measured_on": tomorrow2},
                {"metric": "hba1c", "value": "5.6", "unit": "%", "measured_on": "not-a-date"},
                {
                    "metric": "hba1c",
                    "value": "5.6",
                    "unit": "%",
                    "measured_on": "2026-11-10",
                    "measured_at": "2026-11-12T10:00:00+05:30",
                },
                {"metric": "hba1c", "value": "5.6", "unit": "%", "ref_low": "9", "ref_high": "4"},
                {"metric": "urine_protein", "value": "Trace", "ref_low": "1"},
                {"value": "5"},
                "not-an-object",
                {"metric": "hba1c", "value": "5.6", "unit": "%", "note": "n" * 501},
            ],
        )
        assert [i["status"] for i in out["items"]] == ["invalid"] * 9
        assert out["counts"]["invalid"] == 9
        assert await _count(session, HealthMeasurement) == 0


@pytest.mark.asyncio
async def test_atomic_rejects_the_whole_batch(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        await _with_metrics(service)
        out = await _write(
            service,
            [
                {"metric": "hba1c", "value": "5.6", "unit": "%"},
                {"metric": "hba1c", "value": "oops", "unit": "%"},
            ],
            atomic=True,
        )
        assert out["status"] == "error"
        assert "Nothing was written" in out["message"]
        assert await _count(session, HealthMeasurement) == 0


@pytest.mark.asyncio
async def test_partial_success_writes_the_valid_items(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        await _with_metrics(service)
        out = await _write(
            service,
            [
                {"metric": "hba1c", "value": "5.6", "unit": "%"},
                {"metric": "hba1c", "value": "oops", "unit": "%"},
            ],
        )
        assert out["status"] == "success"
        assert [i["status"] for i in out["items"]] == ["created", "invalid"]
        assert await _count(session, HealthMeasurement) == 1


@pytest.mark.asyncio
async def test_duplicates_are_multiplicity_aware_across_records(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        await _with_metrics(service)
        item = {"metric": "hba1c", "value": "5.6", "unit": "%", "measured_on": "2026-11-10"}
        await _write(service, [item], record={"title": "First", "source_ref": "a"})

        # One identical reading already exists: of two identical items, one is a duplicate.
        out = await _write(
            service,
            [item, item],
            record={"title": "Second", "source_ref": "b"},
            skip_duplicates=True,
        )
        assert [i["status"] for i in out["items"]] == ["duplicate", "created"]
        assert await _count(session, HealthMeasurement) == 2

        # Without skip_duplicates the match is surfaced, never silently skipped or written.
        out2 = await _write(service, [item], record={"title": "Third", "source_ref": "c"})
        assert out2["items"][0]["status"] == "possible_duplicate"
        assert out2["items"][0]["existing_id"]
        assert out2["counts"]["possible_duplicate"] == 1
        assert await _count(session, HealthMeasurement) == 2
        assert await _count(session, HealthRecord) == 2  # the third wrote nothing, so no record


@pytest.mark.asyncio
async def test_item_source_ref_returns_the_existing_row(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        await _with_metrics(service)
        first = await _write(
            service,
            [{"metric": "hba1c", "value": "5.6", "unit": "%", "source_ref": "line-7"}],
            record={"title": "A", "source_ref": "A"},
        )
        created_id = first["items"][0]["public_id"]
        again = await _write(
            service,
            [
                {"metric": "hba1c", "value": "7.1", "unit": "%", "source_ref": "line-7"},
                {"metric": "hba1c", "value": "5.0", "unit": "%", "source_ref": "line-7"},
            ],
            record={"title": "B", "source_ref": "B"},
        )
        assert again["items"][0]["status"] == "duplicate"
        assert again["items"][0]["match_reason"] == "source_ref"
        assert again["items"][0]["existing_id"] == created_id
        assert again["items"][1]["status"] == "duplicate"  # same ref earlier in the batch
        value = (await session.execute(select(HealthMeasurement.value_num))).scalar_one()
        assert value == Decimal("5.6")  # a different value on a known ref never overwrites


@pytest.mark.asyncio
async def test_batch_size_limits(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        await _with_metrics(service)
        empty = await _write(service, [])
        assert empty["status"] == "error"
        big = await _write(service, [{"metric": "hba1c", "value": "5", "unit": "%"}] * 101)
        assert big["status"] == "error" and "At most 100" in big["message"]
        bad_header = await _write(
            service, [{"metric": "hba1c", "value": "5", "unit": "%"}], record={"kind": "x"}
        )
        assert bad_header["status"] == "error" and "Invalid record header" in bad_header["message"]


@pytest.mark.asyncio
async def test_measured_at_derives_the_local_day(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session, timezone="Asia/Kolkata")
        await service.ensure_builtin_metrics(WS)
        # 20:00 UTC on the 10th is 01:30 IST on the 11th.
        out = await _write(
            service,
            [
                {
                    "metric": "weight",
                    "value": "72.4",
                    "unit": "kg",
                    "measured_at": "2026-11-10T20:00:00Z",
                }
            ],
            record={"title": "Scale", "kind": "import"},
        )
        assert out["items"][0]["status"] == "created"
        row = (await session.execute(select(HealthMeasurement))).scalar_one()
        assert row.measured_on == date(2026, 11, 11)
        assert row.measured_at == datetime(2026, 11, 10, 20, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Series and records
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_series_converts_flags_and_excludes_thresholds_from_stats(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        await _with_metrics(service)
        today = datetime.now(UTC).date()
        await _write(
            service,
            [
                {
                    "metric": "ldl",
                    "value": "100",
                    "unit": "mg/dL",
                    "measured_on": (today - timedelta(days=40)).isoformat(),
                    "ref_low": "50",
                    "ref_high": "130",
                },
                {
                    "metric": "ldl",
                    "value": "2.0",
                    "unit": "mmol/L",
                    "measured_on": (today - timedelta(days=10)).isoformat(),
                    "ref_high": "3.4",
                },
                {
                    "metric": "ldl",
                    "value": "4.0",
                    "unit": "mmol/L",
                    "measured_on": (today - timedelta(days=2)).isoformat(),
                    "ref_high": "3.4",
                },
                {
                    "metric": "ldl",
                    "value": "<20",
                    "unit": "mg/dL",
                    "measured_on": today.isoformat(),
                },
            ],
        )
        series = await service.get_series(WS, "LDL", user_id=USER)
        assert series.total == 4
        newest = series.points[0]
        assert (newest.comparator, newest.in_range) == ("<", None)  # threshold: never flagged
        by_value = {(p.value, p.unit): p for p in series.points if p.comparator is None}
        high = by_value[(Decimal("4.000000"), "mmol/L")]
        assert high.in_range == "high" and high.converted is True
        assert high.canonical_value == Decimal("154.680000")
        assert by_value[(Decimal("100.000000"), "mg/dL")].converted is False
        assert by_value[(Decimal("2.000000"), "mmol/L")].in_range == "normal"

        stats = series.stats
        assert stats.count == 4
        assert stats.latest == Decimal("154.680000")  # the threshold row is not "latest"
        assert (stats.min, stats.max) == (Decimal("77.340000"), Decimal("154.680000"))
        assert stats.delta_7d == Decimal("77.340000")  # 154.68 - (2.0 mmol/L = 77.34)
        assert stats.delta_30d == Decimal("54.680000")  # 154.68 - 100


@pytest.mark.asyncio
async def test_series_filters_pages_and_unknown_metric(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        await _with_metrics(service)
        await _write(
            service,
            [
                {"metric": "hba1c", "value": f"5.{d}", "unit": "%", "measured_on": f"2026-10-0{d}"}
                for d in range(1, 6)
            ],
        )
        page = await service.get_series(WS, "hba1c", limit=2, offset=0)
        assert [p.measured_on.day for p in page.points] == [5, 4]
        assert page.has_more is True and page.total == 5
        ranged = await service.get_series(
            WS, "hba1c", from_on=date(2026, 10, 2), to_on=date(2026, 10, 3)
        )
        assert [p.measured_on.day for p in ranged.points] == [3, 2]
        with pytest.raises(NotFoundError):
            await service.get_series(WS, "nope")


@pytest.mark.asyncio
async def test_workspaces_are_isolated(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        await _with_metrics(service)
        await _write(service, [{"metric": "hba1c", "value": "5.6", "unit": "%"}])
        with pytest.raises(NotFoundError):
            await service.get_series(OTHER_WS, "hba1c")
        records, total = await service.list_records(OTHER_WS)
        assert (records, total) == ([], 0)
        mine, mine_total = await service.list_records(WS)
        assert mine_total == 1 and mine[0].measurement_count == 1
        detail = await service.get_record(WS, mine[0].public_id)
        assert detail.measurements[0].metric_code == "hba1c"
        with pytest.raises(NotFoundError):
            await service.get_record(OTHER_WS, mine[0].public_id)


@pytest.mark.asyncio
async def test_delete_record_previews_then_cascades(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        await _with_metrics(service)
        await _write(
            service,
            [
                {"metric": "hba1c", "value": "5.6", "unit": "%"},
                {"metric": "ldl", "value": "90", "unit": "mg/dL"},
            ],
        )
        record = (await session.execute(select(HealthRecord))).scalar_one()
        preview = await service.delete_record(
            user_id=USER, workspace_id=WS, public_id=record.public_id, confirmed=False
        )
        assert preview["status"] == "preview" and preview["measurement_count"] == 2
        assert await _count(session, HealthMeasurement) == 2

        done = await service.delete_record(
            user_id=USER,
            workspace_id=WS,
            public_id=record.public_id,
            confirmed=True,
            audit_logger=AuditLogger(session),
        )
        assert done == {"status": "success", "deleted": True, "measurement_count": 2}
        session.expire_all()
        assert await _count(session, HealthMeasurement) == 0
        assert await _count(session, HealthRecord) == 0
        # The metric (catalog) survives, and a metric with data cannot be deleted by accident.
        assert await _count(session, HealthMetric, code="hba1c") == 1


@pytest.mark.asyncio
async def test_delete_measurement_and_unknown_id(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        await _with_metrics(service)
        out = await _write(service, [{"metric": "hba1c", "value": "5.6", "unit": "%"}])
        public_id = uuid.UUID(out["items"][0]["public_id"])
        await service.delete_measurement(
            user_id=USER, workspace_id=WS, public_id=public_id, audit_logger=AuditLogger(session)
        )
        assert await _count(session, HealthMeasurement) == 0
        with pytest.raises(NotFoundError):
            await service.delete_measurement(user_id=USER, workspace_id=WS, public_id=public_id)


# --------------------------------------------------------------------------
# Weight on the metric store
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weight_log_list_trend_and_delete(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        now = datetime.now(UTC)
        older = await service.log_weight(
            user_id=USER,
            workspace_id=WS,
            weight_kg=Decimal("80.00"),
            measured_at=now - timedelta(days=10),
            note=None,
        )
        newest = await service.log_weight(
            user_id=USER,
            workspace_id=WS,
            weight_kg=Decimal("79.00"),
            measured_at=now,
            note="morning",
            audit_logger=AuditLogger(session),
        )
        assert newest.source_type == "manual" and newest.unit == "kg"
        rows, total = await service.list_weight(WS)
        assert total == 2 and [r.public_id for r in rows] == [newest.public_id, older.public_id]

        window, stats = await service.weight_trend(WS, 30, user_id=USER)
        assert len(window) == 2
        assert stats.latest == Decimal("79.000000")
        assert (stats.min, stats.max) == (Decimal("79.000000"), Decimal("80.000000"))
        assert stats.delta_7d == Decimal("-1.000000")
        assert stats.delta_30d is None  # nothing 30 days back

        # A non-weight measurement id is not a weight entry.
        await _with_metrics(service)
        other = await _write(service, [{"metric": "hba1c", "value": "5.6", "unit": "%"}])
        with pytest.raises(NotFoundError):
            await service.delete_weight(
                user_id=USER, workspace_id=WS, public_id=uuid.UUID(other["items"][0]["public_id"])
            )
        await service.delete_weight(user_id=USER, workspace_id=WS, public_id=newest.public_id)
        _, total_after = await service.list_weight(WS)
        assert total_after == 1


@pytest.mark.asyncio
async def test_weight_delta_looks_past_the_window(override_database_url):
    """A 7-day window must not blank a 30-day delta: the earlier reading is searched in history."""
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        now = datetime.now(UTC)
        for days, kg in ((45, "85.00"), (3, "80.00")):
            await service.log_weight(
                user_id=USER,
                workspace_id=WS,
                weight_kg=Decimal(kg),
                measured_at=now - timedelta(days=days),
                note=None,
            )
        window, stats = await service.weight_trend(WS, 7, user_id=USER)
        assert len(window) == 1
        assert stats.delta_30d == Decimal("-5.000000")


@pytest.mark.asyncio
async def test_weight_rejects_non_positive(override_database_url):
    async with postgres.async_session_maker() as session:
        service = await _seed(session)
        with pytest.raises(HealthMeasurementError):
            await service.log_weight(
                user_id=USER,
                workspace_id=WS,
                weight_kg=Decimal("0"),
                measured_at=datetime.now(UTC),
                note=None,
            )
