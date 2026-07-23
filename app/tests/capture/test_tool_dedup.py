"""spec-090: tool-call idempotency guard for resumed voice-capture sessions.

The ledger records successful write-tool executions and suppresses replays —
Gemini re-emitting already-executed calls when a dropped session is resumed
(`?resume=<handle>`). Scenario fixtures under
``docs/specs/spec-090-replay-scenarios/`` are scrubbed reconstructions of the
real production incidents (2026-07-22 addendum) and are replayed verbatim here.
"""

import json
from datetime import datetime
from pathlib import Path

from app.capture.tool_dedup import WRITE_TOOLS, CaptureToolDedupLedger

SCENARIO_DIR = Path(__file__).parents[2].parent / "docs" / "specs" / "spec-090-replay-scenarios"

WS = 1
USER = 1


def _record_spend(ledger, args, now, result=None):
    ledger.record(
        workspace_id=WS,
        user_id=USER,
        tool="log_spending_transaction",
        args=args,
        result=result or {"status": "success", "entity_public_id": "txn-1"},
        user_timezone="UTC",
        now=now,
    )


def _check_spend(ledger, args, now):
    return ledger.check_replay(
        workspace_id=WS,
        user_id=USER,
        tool="log_spending_transaction",
        args=args,
        user_timezone="UTC",
        now=now,
    )


def test_write_tools_cover_all_mutating_capture_tools():
    assert (
        frozenset({
            "create_todo_task",
            "create_recurring_todo",
            "log_spending_transaction",
            "log_weight",
            "log_medication_event",
            "update_todo",
            "delete_todo",
        })
        == WRITE_TOOLS
    )


def test_exact_replay_within_window_is_suppressed_with_original_result():
    ledger = CaptureToolDedupLedger(window_seconds=2700)
    args = {"amount": "40", "category_name": "food", "description": "coffee"}
    _record_spend(ledger, args, now=1000.0, result={"status": "success", "entity_public_id": "t1"})

    original = _check_spend(ledger, args, now=1047.0)

    assert original == {"status": "success", "entity_public_id": "t1"}


def test_replay_outside_window_is_not_suppressed():
    ledger = CaptureToolDedupLedger(window_seconds=120)
    args = {"amount": "40", "category_name": "food", "description": "coffee"}
    _record_spend(ledger, args, now=1000.0)

    assert _check_spend(ledger, args, now=1121.0) is None


def test_drifted_replay_still_matches_fuzzy_key():
    """The b1a3ad51 incident shape: replay rewrote the description, added
    account_name, and recategorized — but tool + amount + occurred_at
    survived. The fuzzy key must catch it."""
    ledger = CaptureToolDedupLedger(window_seconds=2700)
    _record_spend(
        ledger,
        {
            "amount": "65",
            "category_name": "transport",
            "description": "taxi",
            "occurred_at": "2026-01-09",
        },
        now=1000.0,
    )

    drifted = {
        "amount": "65",
        "category_name": "entertainment",
        "description": "Cab ride",
        "account_name": "HDFC Card",
        "occurred_at": "2026-01-09",
    }
    assert _check_spend(ledger, drifted, now=1000.0 + 37 * 60) is not None


def test_different_amount_is_not_a_replay():
    ledger = CaptureToolDedupLedger(window_seconds=2700)
    _record_spend(
        ledger, {"amount": "65", "category_name": "transport", "description": "taxi"}, now=1000.0
    )

    assert (
        _check_spend(
            ledger,
            {"amount": "60", "category_name": "transport", "description": "taxi"},
            now=1010.0,
        )
        is None
    )


def test_amount_formatting_differences_still_match():
    ledger = CaptureToolDedupLedger(window_seconds=2700)
    _record_spend(
        ledger, {"amount": "90", "category_name": "transport", "description": "metro"}, now=1000.0
    )

    assert (
        _check_spend(
            ledger,
            {"amount": "90.00", "category_name": "transport", "description": "metro"},
            now=1010.0,
        )
        is not None
    )


def test_missing_occurred_at_normalizes_to_today_in_user_timezone():
    """The f87ebe07 incident shape: original call omitted occurred_at (tool
    defaults to today), the +8 min replay spelled today out explicitly.
    Both must land on the same key."""
    ledger = CaptureToolDedupLedger(window_seconds=2700)
    now = datetime.fromisoformat("2026-01-10T12:00:00+00:00").timestamp()
    _record_spend(
        ledger, {"amount": "30", "category_name": "food", "description": "snacks"}, now=now
    )

    explicit = {
        "amount": "30",
        "category_name": "food",
        "description": "snacks",
        "occurred_at": "2026-01-10",
    }
    assert _check_spend(ledger, explicit, now=now + 480) is not None


def test_multiplicity_each_execution_absorbs_exactly_one_suppression():
    """Two legit identical originals (the two same-price transit fares) →
    a replay of both is fully suppressed, but a third identical call is not."""
    ledger = CaptureToolDedupLedger(window_seconds=2700)
    args = {"amount": "30", "category_name": "transport", "description": "bus ticket"}
    _record_spend(ledger, args, now=1000.0, result={"status": "success", "entity_public_id": "t1"})
    _record_spend(ledger, args, now=1001.0, result={"status": "success", "entity_public_id": "t2"})

    assert _check_spend(ledger, args, now=1050.0) is not None
    assert _check_spend(ledger, args, now=1051.0) is not None
    assert _check_spend(ledger, args, now=1052.0) is None


def test_non_spending_write_tools_use_exact_args_key():
    ledger = CaptureToolDedupLedger(window_seconds=2700)
    ledger.record(
        workspace_id=WS,
        user_id=USER,
        tool="log_weight",
        args={"weight_kg": "72.4"},
        result={"status": "success"},
        user_timezone="UTC",
        now=1000.0,
    )

    same = ledger.check_replay(
        workspace_id=WS,
        user_id=USER,
        tool="log_weight",
        args={"weight_kg": "72.4"},
        user_timezone="UTC",
        now=1010.0,
    )
    different = ledger.check_replay(
        workspace_id=WS,
        user_id=USER,
        tool="log_weight",
        args={"weight_kg": "80.0"},
        user_timezone="UTC",
        now=1011.0,
    )
    assert same is not None
    assert different is None


def test_workspace_isolation():
    ledger = CaptureToolDedupLedger(window_seconds=2700)
    args = {"amount": "40", "category_name": "food", "description": "coffee"}
    _record_spend(ledger, args, now=1000.0)

    other_ws = ledger.check_replay(
        workspace_id=2,
        user_id=USER,
        tool="log_spending_transaction",
        args=args,
        user_timezone="UTC",
        now=1010.0,
    )
    assert other_ws is None


def test_expired_entries_are_pruned():
    ledger = CaptureToolDedupLedger(window_seconds=60)
    _record_spend(ledger, {"amount": "1", "category_name": "food", "description": "a"}, now=1000.0)
    _record_spend(ledger, {"amount": "2", "category_name": "food", "description": "b"}, now=2000.0)

    # First key expired; recording/checking later must not keep it alive.
    assert (
        _check_spend(
            ledger, {"amount": "1", "category_name": "food", "description": "a"}, now=2000.0
        )
        is None
    )
    assert ledger.size() == 1


# ── scenario fixtures: replayed verbatim against the ledger ──────────────────


def _run_scenario(name: str, window_seconds: int = 2700) -> tuple[list[dict], list[dict]]:
    """Drive a scrubbed scenario file through the guard rule exactly as the
    agent applies it: a tool call is replay-suspect iff its session is not the
    file's first session (a reconnect) and no user activity has been seen on
    that session yet (`user_transcript` is the fixture's proxy for client
    input). Returns (executed, suppressed) tool-call entries."""
    ledger = CaptureToolDedupLedger(window_seconds=window_seconds)
    entries = [
        json.loads(line) for line in (SCENARIO_DIR / name).read_text().splitlines() if line.strip()
    ]
    executed: list[dict] = []
    suppressed: list[dict] = []
    first_session = entries[0]["session_id"]
    sessions_with_user_input: set[str] = set()

    for entry in entries:
        sid = entry["session_id"]
        if entry["kind"] == "user_transcript":
            sessions_with_user_input.add(sid)
            continue
        if entry["kind"] != "tool_call":
            continue
        now = datetime.fromisoformat(entry["timestamp"]).timestamp()
        replay_suspect = sid != first_session and sid not in sessions_with_user_input
        if replay_suspect and ledger.check_replay(
            workspace_id=entry["workspace_id"],
            user_id=entry["user_id"],
            tool=entry["tool"],
            args=entry["args"],
            user_timezone="UTC",
            now=now,
        ):
            suppressed.append(entry)
            continue
        executed.append(entry)
        ledger.record(
            workspace_id=entry["workspace_id"],
            user_id=entry["user_id"],
            tool=entry["tool"],
            args=entry["args"],
            result={"status": "success"},
            user_timezone="UTC",
            now=now,
        )
    return executed, suppressed


def test_scenario_01_exact_replay_burst_fully_suppressed():
    executed, suppressed = _run_scenario("scenario-01-exact-replay-burst.jsonl")
    assert len(executed) == 5
    assert len(suppressed) == 5


def test_scenario_02_drifted_replay_suppressed_via_fuzzy_key():
    executed, suppressed = _run_scenario("scenario-02-drifted-replay.jsonl")
    assert len(executed) == 3
    assert len(suppressed) == 3


def test_scenario_03_legit_identical_repeat_both_execute():
    executed, suppressed = _run_scenario("scenario-03-legit-identical-repeat.jsonl")
    assert len(executed) == 2
    assert suppressed == []


def test_scenario_04_post_user_input_repeat_executes():
    executed, suppressed = _run_scenario("scenario-04-post-user-input-repeat.jsonl")
    assert len(executed) == 2
    assert suppressed == []
