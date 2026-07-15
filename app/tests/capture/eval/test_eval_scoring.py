import json
from pathlib import Path

from app.capture.eval_scoring import ToolCall, score_case, summarize, validate_case

FIXTURE_PATH = Path(__file__).parent / "utterances.json"


def _load_cases() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text())["cases"]


def test_no_tool_call_expected_passes_on_empty_actual():
    case = {"id": "c1", "category": "adversarial", "utterance": "x", "expected": {"tool": None}}
    result = score_case(case, [])
    assert result.passed
    assert not result.skipped


def test_no_tool_call_expected_fails_when_model_calls_anyway():
    case = {"id": "c1", "category": "adversarial", "utterance": "x", "expected": {"tool": None}}
    result = score_case(case, [ToolCall("delete_todo", {"public_id": "abc"})])
    assert not result.passed


def test_exact_tool_and_args_match_passes():
    case = {
        "id": "c1",
        "category": "adversarial",
        "utterance": "x",
        "expected": {"tool": "log_weight", "args": {"weight_kg": "72.4"}},
    }
    result = score_case(case, [ToolCall("log_weight", {"weight_kg": "72.4"})])
    assert result.passed


def test_wrong_tool_fails():
    case = {
        "id": "c1",
        "category": "adversarial",
        "utterance": "x",
        "expected": {"tool": "log_weight", "args": {"weight_kg": "72.4"}},
    }
    result = score_case(case, [ToolCall("log_medication_event", {"name": "x", "status": "taken"})])
    assert not result.passed


def test_extra_or_missing_arg_fails_exact_match():
    case = {
        "id": "c1",
        "category": "adversarial",
        "utterance": "x",
        "expected": {"tool": "log_weight", "args": {"weight_kg": "72.4"}},
    }
    result = score_case(case, [ToolCall("log_weight", {"weight_kg": "72.4", "note": "unexpected"})])
    assert not result.passed


def test_multiple_tool_calls_fail_single_intent_expectation():
    case = {
        "id": "c1",
        "category": "adversarial",
        "utterance": "x",
        "expected": {"tool": "log_weight", "args": {"weight_kg": "72.4"}},
    }
    result = score_case(
        case,
        [
            ToolCall("log_weight", {"weight_kg": "72.4"}),
            ToolCall("log_medication_event", {"name": "x", "status": "taken"}),
        ],
    )
    assert not result.passed


def test_text_fields_ignore_case_and_whitespace():
    case = {
        "id": "c1",
        "category": "adversarial",
        "utterance": "x",
        "expected": {
            "tool": "log_spending_transaction",
            "args": {"amount": "12", "category_name": "food", "description": "Lunch"},
            "text_fields": ["description"],
        },
    }
    result = score_case(
        case,
        [
            ToolCall(
                "log_spending_transaction",
                {"amount": "12", "category_name": "food", "description": "  lunch  "},
            )
        ],
    )
    assert result.passed


def test_text_fields_still_fail_on_real_difference():
    case = {
        "id": "c1",
        "category": "adversarial",
        "utterance": "x",
        "expected": {
            "tool": "log_spending_transaction",
            "args": {"amount": "12", "category_name": "food", "description": "Lunch"},
            "text_fields": ["description"],
        },
    }
    result = score_case(
        case,
        [
            ToolCall(
                "log_spending_transaction",
                {"amount": "12", "category_name": "food", "description": "Dinner"},
            )
        ],
    )
    assert not result.passed


def test_text_fields_tolerate_actual_being_a_superset_of_expected():
    case = {
        "id": "c1",
        "category": "adversarial",
        "utterance": "x",
        "expected": {
            "tool": "log_spending_transaction",
            "args": {"amount": "12", "category_name": "food", "description": "Lunch"},
            "text_fields": ["description"],
        },
    }
    result = score_case(
        case,
        [
            ToolCall(
                "log_spending_transaction",
                {"amount": "12", "category_name": "food", "description": "lunch food"},
            )
        ],
    )
    assert result.passed


def test_text_fields_still_fail_when_expected_word_is_missing():
    case = {
        "id": "c1",
        "category": "adversarial",
        "utterance": "x",
        "expected": {
            "tool": "log_spending_transaction",
            "args": {"amount": "12", "category_name": "food", "description": "Lunch"},
            "text_fields": ["description"],
        },
    }
    result = score_case(
        case,
        [
            ToolCall(
                "log_spending_transaction",
                {"amount": "12", "category_name": "food", "description": "food"},
            )
        ],
    )
    assert not result.passed


def test_numeric_fields_tolerate_formatting_differences():
    case = {
        "id": "c1",
        "category": "adversarial",
        "utterance": "x",
        "expected": {
            "tool": "log_spending_transaction",
            "args": {"amount": "-50", "category_name": "food", "description": "Refund"},
            "numeric_fields": ["amount"],
            "text_fields": ["description"],
        },
    }
    result = score_case(
        case,
        [
            ToolCall(
                "log_spending_transaction",
                {"amount": "-50.00", "category_name": "food", "description": "refund"},
            )
        ],
    )
    assert result.passed


def test_numeric_fields_still_fail_on_real_difference():
    case = {
        "id": "c1",
        "category": "adversarial",
        "utterance": "x",
        "expected": {
            "tool": "log_weight",
            "args": {"weight_kg": "72.4"},
            "numeric_fields": ["weight_kg"],
        },
    }
    result = score_case(case, [ToolCall("log_weight", {"weight_kg": "80.0"})])
    assert not result.passed


def test_optional_extra_args_are_ignored_when_present():
    case = {
        "id": "c1",
        "category": "adversarial",
        "utterance": "x",
        "expected": {
            "tool": "create_recurring_todo",
            "args": {"title": "Take out the trash", "frequency": "weekly"},
            "optional_extra_args": ["by_weekday", "interval"],
        },
    }
    result = score_case(
        case,
        [
            ToolCall(
                "create_recurring_todo",
                {"title": "Take out the trash", "frequency": "weekly", "by_weekday": 0},
            )
        ],
    )
    assert result.passed


def test_unlisted_extra_arg_still_fails():
    case = {
        "id": "c1",
        "category": "adversarial",
        "utterance": "x",
        "expected": {
            "tool": "log_weight",
            "args": {"weight_kg": "72.4"},
            "optional_extra_args": ["note"],
        },
    }
    result = score_case(
        case, [ToolCall("log_weight", {"weight_kg": "72.4", "unexpected_field": "x"})]
    )
    assert not result.passed


def test_allow_read_only_tools_passes_on_read_only_call():
    case = {
        "id": "c1",
        "category": "adversarial",
        "utterance": "x",
        "expected": {"tool": None, "allow_read_only_tools": True},
    }
    result = score_case(case, [ToolCall("list_todos", {})])
    assert result.passed


def test_allow_read_only_tools_still_fails_on_mutating_call():
    case = {
        "id": "c1",
        "category": "adversarial",
        "utterance": "x",
        "expected": {"tool": None, "allow_read_only_tools": True},
    }
    result = score_case(case, [ToolCall("delete_todo", {"public_id": "1"})])
    assert not result.passed


def test_skipped_case_excluded_from_summary_denominator():
    case = {
        "id": "c1",
        "category": "adversarial",
        "utterance": "x",
        "expected": {"tool": "create_todo_task", "args": {"title": "x"}},
        "skip": True,
        "skip_reason": "relative date",
    }
    result = score_case(case, [])
    assert result.passed
    assert result.skipped

    summary = summarize([result])
    assert summary["total"] == 0
    assert summary["skipped"] == [{"id": "c1", "reason": "skipped: relative date"}]


def test_summarize_accuracy_and_failures():
    results = [
        score_case(
            {"id": "a", "category": "adversarial", "utterance": "x", "expected": {"tool": None}}, []
        ),
        score_case(
            {"id": "b", "category": "adversarial", "utterance": "x", "expected": {"tool": None}},
            [ToolCall("delete_todo", {"public_id": "1"})],
        ),
    ]
    summary = summarize(results)
    assert summary["total"] == 2
    assert summary["passed"] == 1
    assert summary["accuracy"] == 0.5
    assert summary["failures"] == [{"id": "b", "reason": summary["failures"][0]["reason"]}]


def test_validate_case_flags_unknown_tool():
    errors = validate_case({
        "id": "c1",
        "category": "adversarial",
        "utterance": "x",
        "expected": {"tool": "not_a_real_tool", "args": {}},
    })
    assert any("not_a_real_tool" in e for e in errors)


def test_validate_case_flags_non_dict_expected():
    errors = validate_case({
        "id": "c1",
        "category": "adversarial",
        "utterance": "x",
        "expected": None,
    })
    assert any("dict" in e for e in errors)


def test_validate_case_flags_bad_category():
    errors = validate_case({
        "id": "c1",
        "category": "bogus",
        "utterance": "x",
        "expected": {"tool": None},
    })
    assert any("category" in e for e in errors)


def test_fixture_file_cases_are_all_schema_valid():
    cases = _load_cases()
    assert cases, "fixture must have at least one case"
    for case in cases:
        errors = validate_case(case)
        assert not errors, f"{case['id']}: {errors}"


def test_fixture_ids_are_unique():
    cases = _load_cases()
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))


def test_fixture_currently_only_holds_adversarial_cases():
    """spec-079: real_usage transcripts aren't captured anywhere yet (no
    source to draw the 60% split from) — tracked as an open item, not
    silently faked. This test documents the gap so it fails loudly (as a
    reminder to update it) once real_usage cases are added."""
    cases = _load_cases()
    categories = {c["category"] for c in cases}
    assert categories == {"adversarial"}
    assert len(cases) == 20
