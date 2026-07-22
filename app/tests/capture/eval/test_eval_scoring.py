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


def test_fixture_holds_adversarial_and_first_real_usage_cases():
    """spec-079 planned the real_usage cases to be added once transcript
    capture shipped; the first 6 landed 2026-07-22, derived from production
    capture-log usage patterns with all personal data scrubbed to synthetic
    values (spec-090 addendum). This test pins the current composition so a
    category or count drift is a deliberate act, not an accident."""
    cases = _load_cases()
    by_category = {}
    for c in cases:
        by_category.setdefault(c["category"], []).append(c["id"])
    assert len(by_category["adversarial"]) == 20
    assert len(by_category["real_usage"]) == 6
    assert set(by_category) == {"adversarial", "real_usage"}


# ── multi-call (expected.calls) scoring ──────────────────────────────────────


def _spend(amount: str, category: str, description: str, **extra) -> ToolCall:
    args = {"amount": amount, "category_name": category, "description": description, **extra}
    return ToolCall("log_spending_transaction", args)


def _multi_case(specs: list[dict]) -> dict:
    return {"id": "m1", "category": "real_usage", "utterance": "x", "expected": {"calls": specs}}


def _spend_spec(amount: str, category: str, description: str) -> dict:
    return {
        "tool": "log_spending_transaction",
        "args": {"amount": amount, "category_name": category, "description": description},
        "text_fields": ["description"],
        "numeric_fields": ["amount"],
    }


def test_multi_call_passes_regardless_of_order():
    case = _multi_case([
        _spend_spec("40", "food", "coffee"),
        _spend_spec("85", "transport", "bike rental"),
    ])
    actual = [_spend("85", "transport", "bike rental"), _spend("40", "food", "coffee")]
    result = score_case(case, actual)
    assert result.passed


def test_multi_call_fails_on_missing_call():
    case = _multi_case([
        _spend_spec("40", "food", "coffee"),
        _spend_spec("85", "transport", "bike rental"),
    ])
    result = score_case(case, [_spend("40", "food", "coffee")])
    assert not result.passed
    assert "expected 2 calls, got 1" in result.reason


def test_multi_call_fails_on_extra_call():
    case = _multi_case([_spend_spec("40", "food", "coffee")])
    actual = [_spend("40", "food", "coffee"), _spend("40", "food", "coffee")]
    result = score_case(case, actual)
    assert not result.passed


def test_multi_call_identical_repeats_require_matching_multiplicity():
    """The real-02 pattern: two genuinely identical items in one utterance —
    the model must emit both calls (not self-dedup), and each expected spec
    consumes exactly one actual call."""
    case = _multi_case([
        _spend_spec("30", "transport", "bus ticket"),
        _spend_spec("30", "transport", "bus ticket"),
    ])
    both = [_spend("30", "transport", "bus ticket"), _spend("30", "transport", "bus ticket")]
    assert score_case(case, both).passed
    assert not score_case(case, both[:1]).passed


def test_multi_call_backtracks_past_greedy_mismatch():
    """Two expected specs whose text_fields overlap: a greedy matcher could
    bind the ambiguous actual call to the wrong spec and fail; the assignment
    must backtrack to the consistent pairing."""
    ambiguous = _spend_spec("10", "food", "tea")  # word-subset: matches "tea" AND "tea snacks"
    specific = _spend_spec("10", "food", "tea snacks")
    case = _multi_case([ambiguous, specific])
    actual = [_spend("10", "food", "tea snacks"), _spend("10", "food", "tea")]
    result = score_case(case, actual)
    assert result.passed


def test_multi_call_arg_mismatch_fails():
    case = _multi_case([_spend_spec("40", "food", "coffee")])
    result = score_case(case, [_spend("41", "food", "coffee")])
    assert not result.passed


def test_multi_call_wrong_tool_fails():
    case = _multi_case([_spend_spec("40", "food", "coffee")])
    result = score_case(case, [ToolCall("log_weight", {"weight_kg": "72"})])
    assert not result.passed


def test_validate_case_accepts_calls_form():
    errors = validate_case(_multi_case([_spend_spec("40", "food", "coffee")]))
    assert errors == []


def test_validate_case_rejects_calls_and_tool_together():
    case = _multi_case([_spend_spec("40", "food", "coffee")])
    case["expected"]["tool"] = "log_weight"
    errors = validate_case(case)
    assert any("mutually exclusive" in e for e in errors)


def test_validate_case_rejects_empty_calls():
    case = _multi_case([])
    errors = validate_case(case)
    assert any("non-empty" in e for e in errors)


def test_validate_case_rejects_unknown_tool_in_calls():
    case = _multi_case([{"tool": "not_a_tool", "args": {}}])
    errors = validate_case(case)
    assert any("not a known capture tool" in e for e in errors)


def test_text_field_empty_expected_does_not_match_arbitrary_actual():
    """Review finding (PR #174): an empty expected string normalizes to zero
    words, and the empty set is a subset of everything — it must not pass."""
    case = {
        "id": "c1",
        "category": "adversarial",
        "utterance": "x",
        "expected": {
            "tool": "log_spending_transaction",
            "args": {"amount": "12", "category_name": "food", "description": ""},
            "text_fields": ["description"],
        },
    }
    result = score_case(
        case,
        [
            ToolCall(
                "log_spending_transaction",
                {"amount": "12", "category_name": "food", "description": "lunch"},
            )
        ],
    )
    assert not result.passed


def test_text_field_none_does_not_match_the_literal_string_none():
    """Review finding (PR #174): str(None) == 'none' after normalization, so a
    Python None expected value must not match a spoken 'none'."""
    case = {
        "id": "c1",
        "category": "adversarial",
        "utterance": "x",
        "expected": {
            "tool": "log_spending_transaction",
            "args": {"amount": "12", "category_name": "food", "description": None},
            "text_fields": ["description"],
        },
    }
    result = score_case(
        case,
        [
            ToolCall(
                "log_spending_transaction",
                {"amount": "12", "category_name": "food", "description": "none of these"},
            )
        ],
    )
    assert not result.passed


def test_text_field_none_still_matches_none():
    case = {
        "id": "c1",
        "category": "adversarial",
        "utterance": "x",
        "expected": {
            "tool": "log_spending_transaction",
            "args": {"amount": "12", "category_name": "food", "description": None},
            "text_fields": ["description"],
        },
    }
    result = score_case(
        case,
        [
            ToolCall(
                "log_spending_transaction",
                {"amount": "12", "category_name": "food", "description": None},
            )
        ],
    )
    assert result.passed
