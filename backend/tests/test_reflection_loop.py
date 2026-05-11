"""B12 — reflection_loop unit tests (OP-850, master plan §2.14).

Covers the four AC-mandated test cases (test plan in OP-850):

  1. single reflection resolves
  2. 3 reflections resolve (cap not exceeded)
  3. 3 reflections cap (record beyond raises)
  4. half-weight accounting confirmed against main iteration counter

Plus shape / formatter / persistence tests that anchor the public API
the SDK launcher depends on.
"""

from __future__ import annotations

import json

import pytest

from backend.agents.loop_detector import RESET_LIMIT
from backend.agents.reflection_loop import (
    ERROR_CAP_EXCEEDED,
    FAILURE_TYPE_LINT,
    FAILURE_TYPE_TEST,
    PERSISTENCE_ENTRY_TYPE,
    REFLECTION_HALF_WEIGHT,
    REFLECTION_LIMIT,
    ReflectionCounter,
    ReflectionInput,
    VALID_FAILURE_TYPES,
    build_lint_reflection_input,
    build_test_reflection_input,
)
from backend.agents.static_analysis_gate import LintDiagnostic, StaticAnalysisResult


# ── Constants reuse B3 infra (AC #5) ─────────────────────────────────


def test_reflection_limit_couples_to_b3_reset_limit_per_ac5():
    """AC #5: 'Reuses B3's loop_detector infrastructure for cap counting'."""
    assert REFLECTION_LIMIT == RESET_LIMIT == 3


def test_half_weight_is_zero_point_five_per_ac4():
    assert REFLECTION_HALF_WEIGHT == 0.5


def test_valid_failure_types_is_test_and_lint_per_ac1():
    assert VALID_FAILURE_TYPES == frozenset({"test", "lint"})


# ── ReflectionInput shape (AC #1) ────────────────────────────────────


def test_reflection_input_to_dict_carries_all_ac1_fields():
    ri = ReflectionInput(
        failure_type=FAILURE_TYPE_TEST,
        file="backend/tests/test_x.py",
        line=42,
        expected="assert x == 1",
        actual="assert x == 0",
        traceback="AssertionError: 0 != 1",
    )
    d = ri.to_dict()
    assert d == {
        "failure_type": "test",
        "file": "backend/tests/test_x.py",
        "line": 42,
        "expected": "assert x == 1",
        "actual": "assert x == 0",
        "traceback": "AssertionError: 0 != 1",
    }


def test_reflection_input_rejects_unknown_failure_type():
    with pytest.raises(ValueError, match="reflection_invalid_failure_type"):
        ReflectionInput(
            failure_type="security",  # not in VALID_FAILURE_TYPES
            file="x", line=1, expected="y", actual="z", traceback="t",
        )


def test_reflection_input_to_user_turn_embeds_payload_for_injection_per_ac2():
    ri = ReflectionInput(
        failure_type=FAILURE_TYPE_LINT,
        file="b/agents/x.py", line=10,
        expected="clean lint", actual="E501: line too long",
        traceback="b/agents/x.py:10:1: E501: line too long",
    )
    turn = ri.to_user_turn()
    # Must carry the JSON payload so the model can parse it deterministically.
    assert "```json" in turn
    assert "\"failure_type\": \"lint\"" in turn
    assert "\"file\": \"b/agents/x.py\"" in turn
    assert "\"line\": 10" in turn
    # Must instruct the model to re-verify before signing off.
    assert "Re-run the verifier" in turn
    # Must reference the structured marker name so the model knows the
    # boundary belongs to B12 (vs B3's reset paragraph).
    assert "reflection_input" in turn
    assert "B12" in turn or "OP-850" in turn


# ── B2 lint adapter (AC #1) ──────────────────────────────────────────


def test_build_lint_reflection_input_returns_none_when_clean():
    clean = StaticAnalysisResult(status="clean", diagnostics=())
    assert build_lint_reflection_input(clean) is None


def test_build_lint_reflection_input_from_dirty_result():
    diags = (
        LintDiagnostic(file="a.py", line=3, col=1, code="E501", message="line too long"),
        LintDiagnostic(file="b.py", line=7, col=2, code="F401", message="unused import"),
    )
    result = StaticAnalysisResult(status="dirty", diagnostics=diags)
    ri = build_lint_reflection_input(result)
    assert ri is not None
    assert ri.failure_type == FAILURE_TYPE_LINT
    assert ri.file == "a.py"  # first diagnostic
    assert ri.line == 3
    assert "E501" in ri.actual
    assert "line too long" in ri.actual
    # Traceback covers every diagnostic so model sees the full set.
    assert "a.py:3:1: E501" in ri.traceback
    assert "b.py:7:2: F401" in ri.traceback


# ── B5 test adapter (AC #1) ──────────────────────────────────────────


def test_build_test_reflection_input_shape():
    ri = build_test_reflection_input(
        file="backend/tests/test_foo.py",
        line=88,
        expected="result == 42",
        actual="result == 7",
        traceback="AssertionError: ...",
    )
    assert ri.failure_type == FAILURE_TYPE_TEST
    assert ri.file == "backend/tests/test_foo.py"
    assert ri.line == 88


# ── ReflectionCounter — per-type cap (AC #3) ─────────────────────────


def test_case_1_single_reflection_resolves():
    """Test plan case 1: single reflection resolves — counter records 1
    and is NOT terminal; happy path on second attempt."""
    counter = ReflectionCounter(ticket_key="OP-850")
    assert counter.can_reflect(FAILURE_TYPE_LINT)
    new_count = counter.record_reflection(FAILURE_TYPE_LINT)
    assert new_count == 1
    assert counter.counts[FAILURE_TYPE_LINT] == 1
    assert counter.can_reflect(FAILURE_TYPE_LINT)  # still room for more
    assert not counter.is_terminal(FAILURE_TYPE_LINT)


def test_case_2_three_reflections_resolve():
    """Test plan case 2: 3 reflections resolve — counter at exactly the
    cap (3) is now terminal; the 4th would be capped (covered in case 3)."""
    counter = ReflectionCounter(ticket_key="OP-850")
    for i in range(REFLECTION_LIMIT):
        assert counter.can_reflect(FAILURE_TYPE_LINT)
        new_count = counter.record_reflection(FAILURE_TYPE_LINT)
        assert new_count == i + 1
    assert counter.counts[FAILURE_TYPE_LINT] == REFLECTION_LIMIT
    assert not counter.can_reflect(FAILURE_TYPE_LINT)
    assert counter.is_terminal(FAILURE_TYPE_LINT)


def test_case_3_three_reflections_cap_raises_on_overflow():
    """Test plan case 3: 3 reflections cap — record_reflection beyond the
    limit must raise ``reflection_cap_exceeded`` so the orchestrator can
    surrender the ticket."""
    counter = ReflectionCounter(ticket_key="OP-850")
    for _ in range(REFLECTION_LIMIT):
        counter.record_reflection(FAILURE_TYPE_LINT)
    with pytest.raises(RuntimeError, match=ERROR_CAP_EXCEEDED):
        counter.record_reflection(FAILURE_TYPE_LINT)
    # is_terminal stays true after the failed record (no silent increment)
    assert counter.counts[FAILURE_TYPE_LINT] == REFLECTION_LIMIT


def test_case_4_half_weight_accounting_against_main_max_iterations():
    """Test plan case 4: half-weight accounting confirmed against main
    iteration counter. AC #4 says each reflection costs 0.5 of the main
    ``max_iterations`` budget — so 3 reflections cost 1.5 iterations."""
    counter = ReflectionCounter(ticket_key="OP-850")
    main_max_iterations = 40
    # Zero reflections → zero half-weight, adjusted == main.
    assert counter.half_weight_consumed == 0.0
    assert counter.adjusted_max_iterations(main_max_iterations) == main_max_iterations
    # 1 reflection: 0.5 consumed; adjusted floors to 39 (int(40 - 0.5)).
    counter.record_reflection(FAILURE_TYPE_LINT)
    assert counter.half_weight_consumed == pytest.approx(0.5)
    assert counter.adjusted_max_iterations(main_max_iterations) == 39
    # 3 reflections total: 1.5 consumed; adjusted == int(40 - 1.5) = 38.
    counter.record_reflection(FAILURE_TYPE_LINT)
    counter.record_reflection(FAILURE_TYPE_TEST)
    assert counter.total_reflections == 3
    assert counter.half_weight_consumed == pytest.approx(1.5)
    assert counter.adjusted_max_iterations(main_max_iterations) == 38


def test_per_type_caps_count_separately_per_ac3():
    """AC #3 explicit: 'Max 3 reflection iterations per failure type;
    counted separately'. A maxed-out lint counter must NOT stop the test
    counter from accepting reflections."""
    counter = ReflectionCounter(ticket_key="OP-850")
    for _ in range(REFLECTION_LIMIT):
        counter.record_reflection(FAILURE_TYPE_LINT)
    assert counter.is_terminal(FAILURE_TYPE_LINT)
    # Test surface is independent — still has full budget.
    assert counter.can_reflect(FAILURE_TYPE_TEST)
    counter.record_reflection(FAILURE_TYPE_TEST)
    assert counter.counts[FAILURE_TYPE_TEST] == 1


def test_adjusted_max_iterations_floors_at_one():
    """Defensive floor: tiny main_max_iterations with many reflections
    should not produce a non-positive budget for the SDK."""
    counter = ReflectionCounter(ticket_key="OP-850")
    counter.record_reflection(FAILURE_TYPE_LINT)
    counter.record_reflection(FAILURE_TYPE_LINT)
    counter.record_reflection(FAILURE_TYPE_TEST)
    # 1.5 consumed; if main_max_iterations=1, naive math → -0.5 → floor 1.
    assert counter.adjusted_max_iterations(1) == 1


def test_invalid_failure_type_rejected_on_record():
    counter = ReflectionCounter(ticket_key="OP-850")
    with pytest.raises(ValueError, match="reflection_invalid_failure_type"):
        counter.record_reflection("security")
    with pytest.raises(ValueError):
        counter.can_reflect("security")


# ── Cross-reset persistence (AC #5) ──────────────────────────────────


def test_counter_persists_to_jsonl_and_loads(tmp_path):
    """AC #5 cross-reset persistence: each ``record_reflection`` writes a
    JSONL row; ``load()`` reconstructs the counts on restart."""
    persistence = tmp_path / "reflection-OP-850.jsonl"
    c1 = ReflectionCounter(ticket_key="OP-850", persistence_path=persistence)
    c1.record_reflection(FAILURE_TYPE_LINT)
    c1.record_reflection(FAILURE_TYPE_LINT)
    c1.record_reflection(FAILURE_TYPE_TEST)

    # JSONL contains exactly 3 rows, all tagged with the entry type.
    rows = [json.loads(l) for l in persistence.read_text().splitlines() if l.strip()]
    assert len(rows) == 3
    assert all(r["type"] == PERSISTENCE_ENTRY_TYPE for r in rows)
    assert all(r["ticket_key"] == "OP-850" for r in rows)

    # Simulate a runner restart and rehydrate the counter.
    c2 = ReflectionCounter.load(persistence, ticket_key="OP-850")
    assert c2.counts == {FAILURE_TYPE_LINT: 2, FAILURE_TYPE_TEST: 1}
    assert c2.half_weight_consumed == pytest.approx(1.5)


def test_load_skips_other_ticket_rows(tmp_path):
    """Persistence file may be shared across tickets — load must filter
    by ticket_key so cross-ticket records don't pollute."""
    persistence = tmp_path / "reflection.jsonl"
    persistence.write_text(
        json.dumps({
            "type": PERSISTENCE_ENTRY_TYPE, "ticket_key": "OP-999",
            "failure_type": "lint", "count_after": 1,
        }) + "\n"
        + json.dumps({
            "type": PERSISTENCE_ENTRY_TYPE, "ticket_key": "OP-850",
            "failure_type": "test", "count_after": 2,
        }) + "\n"
    )
    counter = ReflectionCounter.load(persistence, ticket_key="OP-850")
    assert counter.counts == {FAILURE_TYPE_LINT: 0, FAILURE_TYPE_TEST: 2}


def test_load_robust_against_malformed_lines(tmp_path):
    """Malformed JSONL rows must be skipped (matches ToMScratchpad)."""
    persistence = tmp_path / "reflection.jsonl"
    persistence.write_text(
        "garbled not json\n"
        + json.dumps({"type": "other_component", "stuff": "x"}) + "\n"
        + json.dumps({
            "type": PERSISTENCE_ENTRY_TYPE, "ticket_key": "OP-850",
            "failure_type": "lint", "count_after": 1,
        }) + "\n"
        "\n"  # blank line
    )
    counter = ReflectionCounter.load(persistence, ticket_key="OP-850")
    assert counter.counts[FAILURE_TYPE_LINT] == 1


def test_load_missing_file_returns_empty_counter(tmp_path):
    counter = ReflectionCounter.load(tmp_path / "nope.jsonl", ticket_key="OP-850")
    assert counter.counts == {FAILURE_TYPE_LINT: 0, FAILURE_TYPE_TEST: 0}


def test_counter_without_persistence_path_does_not_write(tmp_path):
    """Caller may pass ``persistence_path=None`` for unit tests / dry runs."""
    counter = ReflectionCounter(ticket_key="OP-850")
    counter.record_reflection(FAILURE_TYPE_LINT)
    # No file should exist; no exception raised.
    assert not any(tmp_path.iterdir())
