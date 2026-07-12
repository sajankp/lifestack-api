"""Pure scoring logic for the spec-079 Stage A voice-routing eval.

Deliberately has no network/DB dependency so it can run in normal CI: the
live call to Gemini lives in ``scripts/run_capture_eval.py``, which imports
this module to score whatever the model actually returned. Kept separate so
the "exact tool+args match" definition is unit-tested without needing a
Gemini API key.
"""

from dataclasses import dataclass
from typing import Any

# The tool set as wired into app/capture/gemini_setup.py — kept here as an
# explicit list (not imported from the setup module) so a fixture referencing
# a stale/misspelled tool name fails fast in validation rather than only at
# eval-run time.
KNOWN_TOOLS = frozenset({
    "create_todo_task",
    "create_recurring_todo",
    "log_spending_transaction",
    "get_investing_summary",
    "list_todos",
    "get_todo",
    "update_todo",
    "delete_todo",
    "list_next_due_items",
    "log_weight",
    "log_medication_event",
})

VALID_CATEGORIES = frozenset({"real_usage", "adversarial"})


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ScoreResult:
    case_id: str
    passed: bool
    reason: str
    skipped: bool = False


def validate_case(case: dict) -> list[str]:
    """Return a list of schema errors for one fixture case (empty = valid)."""
    errors = []
    for key in ("id", "category", "utterance", "expected"):
        if key not in case:
            errors.append(f"missing required key '{key}'")
    if errors:
        return errors

    if case["category"] not in VALID_CATEGORIES:
        errors.append(
            f"category must be one of {sorted(VALID_CATEGORIES)}, got {case['category']!r}"
        )

    expected = case["expected"]
    tool = expected.get("tool")
    if tool is not None:
        if tool not in KNOWN_TOOLS:
            errors.append(f"expected.tool {tool!r} is not a known capture tool")
        if "args" not in expected:
            errors.append("expected.args is required when expected.tool is set")
    return errors


def score_case(case: dict, actual_tool_calls: list[ToolCall]) -> ScoreResult:
    """Exact tool+args match (spec-079 resolved question 3).

    A case with ``expected.tool: null`` passes only if the model made no
    tool call at all. Otherwise the model must make exactly one tool call
    whose name and args dict are identical to the expectation — Stage A
    measures single-intent routing; multi-item capture is Stage C.

    A case with ``skip: true`` (e.g. a relative-date utterance whose
    expected args aren't fixed) is excluded from the accuracy denominator —
    see ``summarize``.
    """
    case_id = case["id"]
    if case.get("skip"):
        return ScoreResult(
            case_id,
            True,
            f"skipped: {case.get('skip_reason', 'no reason given')}",
            skipped=True,
        )

    expected = case["expected"]
    expected_tool = expected.get("tool")

    if expected_tool is None:
        if not actual_tool_calls:
            return ScoreResult(case_id, True, "no tool call expected, none made")
        return ScoreResult(
            case_id,
            False,
            f"expected no tool call, got {[c.name for c in actual_tool_calls]}",
        )

    if len(actual_tool_calls) != 1:
        return ScoreResult(
            case_id,
            False,
            f"expected exactly 1 call to {expected_tool!r}, got {len(actual_tool_calls)}",
        )

    actual = actual_tool_calls[0]
    if actual.name != expected_tool:
        return ScoreResult(case_id, False, f"expected tool {expected_tool!r}, got {actual.name!r}")

    if actual.args != expected["args"]:
        return ScoreResult(
            case_id,
            False,
            f"args mismatch: expected {expected['args']!r}, got {actual.args!r}",
        )

    return ScoreResult(case_id, True, "exact match")


def summarize(results: list[ScoreResult]) -> dict[str, Any]:
    scored = [r for r in results if not r.skipped]
    skipped = [r for r in results if r.skipped]
    total = len(scored)
    passed = sum(1 for r in scored if r.passed)
    return {
        "total": total,
        "passed": passed,
        "accuracy": round(passed / total, 4) if total else 0.0,
        "failures": [{"id": r.case_id, "reason": r.reason} for r in scored if not r.passed],
        "skipped": [{"id": r.case_id, "reason": r.reason} for r in skipped],
    }
