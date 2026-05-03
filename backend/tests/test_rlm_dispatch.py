"""BP.A.5b — Contract tests for ``backend/rlm_dispatch.py``.

Pins the RLM-pattern decomposition decision branch: decision thresholds,
partition logic, fail-open guarantees, and the RlmDispatchPlan model
contract.  Exactly 30 tests; BP.A.7 will fold a superset into the
unified ~150-test ``test_templates.py`` suite.

Decision rule under test (ADR R10 + Appendix C, 2026-04-25):
    context_tokens > 100_000
    AND task_type ∈ {analysis, audit, forensics}
    AND task_type ∉ {crud, retrieval, simple_lookup}
    → "partition_map_summarize" (depth=1)
    else → "standard"
"""

from __future__ import annotations

import pytest

import backend.rlm_dispatch as mod
from backend.rlm_dispatch import (
    CONTEXT_TOKENS_THRESHOLD,
    DEPTH_CAP,
    MAX_PARTITIONS,
    PARTITION_SIZE_TOKENS,
    RLM_TASK_TYPES,
    SIMPLE_TASK_TYPES,
    RlmDispatchPlan,
    decide_dispatch_mode,
    partition_text,
    plan_dispatch,
)


# ── decide_dispatch_mode: RLM conditions ──────────────────────────────────────


class TestDecideDispatchModeRlm:
    def test_analysis_above_threshold(self) -> None:
        assert decide_dispatch_mode(200_000, "analysis") == "partition_map_summarize"

    def test_audit_above_threshold(self) -> None:
        assert decide_dispatch_mode(150_000, "audit") == "partition_map_summarize"

    def test_forensics_above_threshold(self) -> None:
        assert decide_dispatch_mode(500_000, "forensics") == "partition_map_summarize"

    def test_exactly_one_above_threshold(self) -> None:
        # strictly greater-than: 100_001 qualifies, 100_000 does not
        assert decide_dispatch_mode(100_001, "analysis") == "partition_map_summarize"


# ── decide_dispatch_mode: standard conditions ─────────────────────────────────


class TestDecideDispatchModeStandard:
    def test_at_threshold_not_rlm(self) -> None:
        # == CONTEXT_TOKENS_THRESHOLD is NOT strictly greater-than
        assert decide_dispatch_mode(100_000, "analysis") == "standard"

    def test_below_threshold(self) -> None:
        assert decide_dispatch_mode(50_000, "analysis") == "standard"

    def test_crud_excluded_even_above_threshold(self) -> None:
        assert decide_dispatch_mode(200_000, "crud") == "standard"

    def test_retrieval_excluded_even_above_threshold(self) -> None:
        assert decide_dispatch_mode(200_000, "retrieval") == "standard"

    def test_simple_lookup_excluded_even_above_threshold(self) -> None:
        assert decide_dispatch_mode(200_000, "simple_lookup") == "standard"

    def test_unknown_type_above_threshold(self) -> None:
        assert decide_dispatch_mode(200_000, "summary") == "standard"

    def test_empty_type_above_threshold(self) -> None:
        assert decide_dispatch_mode(200_000, "") == "standard"


# ── Task-type set membership invariants ──────────────────────────────────────


class TestTaskTypeSets:
    def test_rlm_types_exact(self) -> None:
        assert RLM_TASK_TYPES == frozenset({"analysis", "audit", "forensics"})

    def test_simple_types_exact(self) -> None:
        assert SIMPLE_TASK_TYPES == frozenset({"crud", "retrieval", "simple_lookup"})

    def test_sets_are_disjoint(self) -> None:
        assert not (RLM_TASK_TYPES & SIMPLE_TASK_TYPES)


# ── partition_text ────────────────────────────────────────────────────────────


class TestPartitionText:
    def test_reassembly_lossless(self) -> None:
        text = "hello world this is a test payload with some real content in it"
        parts = partition_text(text, 200_000)
        assert "".join(parts) == text

    def test_count_200k_tokens(self) -> None:
        # ceil(200_000 / 50_000) = 4; max(2,4) = 4
        parts = partition_text("x" * 1000, 200_000)
        assert len(parts) == 4

    def test_count_150k_tokens(self) -> None:
        # ceil(150_000 / 50_000) = 3; max(2,3) = 3
        parts = partition_text("x" * 1500, 150_000)
        assert len(parts) == 3

    def test_count_capped_at_max_partitions(self) -> None:
        # ceil(1_000_000 / 50_000) = 20; capped at MAX_PARTITIONS = 8
        parts = partition_text("x" * 8000, 1_000_000)
        assert len(parts) == MAX_PARTITIONS

    def test_empty_text_lossless(self) -> None:
        # Empty text: reassembly must still equal ""
        parts = partition_text("", 200_000)
        assert "".join(parts) == ""


# ── plan_dispatch integration ─────────────────────────────────────────────────


class TestPlanDispatch:
    def test_rlm_mode_returned(self) -> None:
        plan = plan_dispatch(200_000, "analysis", "some long payload")
        assert plan.mode == "partition_map_summarize"

    def test_standard_mode_returned(self) -> None:
        plan = plan_dispatch(50_000, "analysis", "short payload")
        assert plan.mode == "standard"

    def test_rlm_partitions_nonempty(self) -> None:
        plan = plan_dispatch(200_000, "analysis", "payload text")
        assert len(plan.partitions) > 0

    def test_standard_partitions_empty(self) -> None:
        plan = plan_dispatch(50_000, "analysis", "payload text")
        assert plan.partitions == ()

    def test_rlm_depth_cap_is_one(self) -> None:
        plan = plan_dispatch(200_000, "audit", "payload")
        assert plan.depth_cap == DEPTH_CAP == 1

    def test_standard_depth_cap_is_zero(self) -> None:
        plan = plan_dispatch(50_000, "crud", "payload")
        assert plan.depth_cap == 0

    def test_context_tokens_preserved(self) -> None:
        plan = plan_dispatch(150_000, "forensics", "payload")
        assert plan.context_tokens == 150_000

    def test_task_type_preserved(self) -> None:
        plan = plan_dispatch(200_000, "audit", "payload")
        assert plan.task_type == "audit"


# ── Fail-open behaviour ───────────────────────────────────────────────────────


class TestFailOpen:
    def test_decide_fail_open_on_broken_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _BrokenSet:
            def __contains__(self, item: object) -> bool:
                raise RuntimeError("simulated heuristic failure")

        monkeypatch.setattr(mod, "RLM_TASK_TYPES", _BrokenSet())
        # Must not raise; must return "standard"
        result = mod.decide_dispatch_mode(200_000, "analysis")
        assert result == "standard"

    def test_plan_dispatch_fail_open_on_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _bad_decide(*_args: object) -> str:
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(mod, "decide_dispatch_mode", _bad_decide)
        plan = mod.plan_dispatch(200_000, "analysis", "payload")
        assert plan.mode == "standard"
        assert plan.partitions == ()
        assert plan.depth_cap == 0


# ── RlmDispatchPlan model contract ───────────────────────────────────────────


class TestRlmDispatchPlanModel:
    def test_plan_is_frozen(self) -> None:
        plan = RlmDispatchPlan(
            mode="standard",
            partitions=(),
            depth_cap=0,
            context_tokens=50_000,
            task_type="analysis",
        )
        with pytest.raises(Exception):
            plan.mode = "partition_map_summarize"  # type: ignore[misc]

    def test_json_roundtrip(self) -> None:
        plan = plan_dispatch(200_000, "audit", "sample payload for testing")
        restored = RlmDispatchPlan.model_validate_json(plan.model_dump_json())
        assert restored == plan

    def test_constants_types_are_frozensets(self) -> None:
        assert isinstance(RLM_TASK_TYPES, frozenset)
        assert isinstance(SIMPLE_TASK_TYPES, frozenset)
        assert isinstance(CONTEXT_TOKENS_THRESHOLD, int)
        assert isinstance(PARTITION_SIZE_TOKENS, int)
        assert isinstance(MAX_PARTITIONS, int)
        assert isinstance(DEPTH_CAP, int)
