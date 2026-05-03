"""BP.A.5 — Contract tests for ``backend/cognitive_load.py``.

Pins the CognitiveLoadReport computation surface — fan-in, fan-out,
mock-limit, estimated_tokens, exceeds_ceiling — so downstream work
(BP.A.6 middleware, BP.A.7 unified suite) can rely on the quantizer
behaving exactly as specified.

BP.A.7 will fold a superset of these checks into the unified ~150-test
``test_templates.py`` suite. Until then this file is the authoritative
regression for the Cognitive Load Scanner alone — keep it green.
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from backend.cognitive_load import (
    CognitiveLoadReport,
    _BASE_TOKENS,
    _FAN_IN_WEIGHT,
    _FAN_OUT_BASE,
    _FAN_OUT_DEP_STEP,
    _FAN_OUT_WEIGHT,
    _MOCK_FRACTION,
    _MOCK_MAX,
    scan_cognitive_load,
)
from backend.templates.task import TaskTemplate


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _task(
    *,
    size: str = "M",
    allowed_dependencies: list[str] | None = None,
    max_cognitive_load_tokens: int = 10_000,
    target_triple: str = "x86_64-pc-linux-gnu",
    guild_id: str = "coder-guild",
) -> TaskTemplate:
    if allowed_dependencies is None:
        allowed_dependencies = []
    return TaskTemplate(
        target_triple=target_triple,
        allowed_dependencies=allowed_dependencies,
        max_cognitive_load_tokens=max_cognitive_load_tokens,
        guild_id=guild_id,
        size=size,
    )


# ── Module constants audit ────────────────────────────────────────────────────


class TestModuleConstants:
    def test_base_tokens_covers_all_sizes(self) -> None:
        assert set(_BASE_TOKENS.keys()) == {"S", "M", "XL"}

    def test_fan_out_base_covers_all_sizes(self) -> None:
        assert set(_FAN_OUT_BASE.keys()) == {"S", "M", "XL"}

    def test_all_base_tokens_positive(self) -> None:
        assert all(v > 0 for v in _BASE_TOKENS.values())

    def test_all_fan_out_bases_positive(self) -> None:
        assert all(v > 0 for v in _FAN_OUT_BASE.values())

    def test_fan_in_weight_positive(self) -> None:
        assert _FAN_IN_WEIGHT > 0

    def test_fan_out_weight_positive(self) -> None:
        assert _FAN_OUT_WEIGHT > 0

    def test_mock_fraction_between_zero_and_one(self) -> None:
        assert 0.0 < _MOCK_FRACTION <= 1.0

    def test_mock_max_positive(self) -> None:
        assert _MOCK_MAX > 0

    def test_fan_out_dep_step_positive(self) -> None:
        assert _FAN_OUT_DEP_STEP > 0

    def test_size_ordering_s_lt_m_lt_xl(self) -> None:
        assert _BASE_TOKENS["S"] < _BASE_TOKENS["M"] < _BASE_TOKENS["XL"]
        assert _FAN_OUT_BASE["S"] < _FAN_OUT_BASE["M"] < _FAN_OUT_BASE["XL"]


# ── fan_in ────────────────────────────────────────────────────────────────────


class TestFanIn:
    def test_empty_deps_gives_fan_in_zero(self) -> None:
        r = scan_cognitive_load(_task(allowed_dependencies=[]))
        assert r.fan_in == 0

    def test_single_dep_gives_fan_in_one(self) -> None:
        r = scan_cognitive_load(_task(allowed_dependencies=["spec.py"]))
        assert r.fan_in == 1

    def test_two_deps_gives_fan_in_two(self) -> None:
        r = scan_cognitive_load(_task(allowed_dependencies=["a.py", "b.py"]))
        assert r.fan_in == 2

    def test_five_deps_gives_fan_in_five(self) -> None:
        deps = [f"dep{i}.py" for i in range(5)]
        r = scan_cognitive_load(_task(allowed_dependencies=deps))
        assert r.fan_in == 5

    def test_ten_deps_gives_fan_in_ten(self) -> None:
        deps = [f"mod/dep{i}.py" for i in range(10)]
        r = scan_cognitive_load(_task(allowed_dependencies=deps))
        assert r.fan_in == 10

    def test_fan_in_counts_not_deduplicates(self) -> None:
        # TaskTemplate enforces non-empty; duplicates are allowed by schema
        deps = ["a.py", "b.py", "a.py"]
        r = scan_cognitive_load(_task(allowed_dependencies=deps))
        assert r.fan_in == 3


# ── fan_out ───────────────────────────────────────────────────────────────────


class TestFanOut:
    def test_size_s_no_deps_gives_base_fan_out(self) -> None:
        r = scan_cognitive_load(_task(size="S", allowed_dependencies=[]))
        assert r.fan_out == _FAN_OUT_BASE["S"]

    def test_size_m_no_deps_gives_base_fan_out(self) -> None:
        r = scan_cognitive_load(_task(size="M", allowed_dependencies=[]))
        assert r.fan_out == _FAN_OUT_BASE["M"]

    def test_size_xl_no_deps_gives_base_fan_out(self) -> None:
        r = scan_cognitive_load(_task(size="XL", allowed_dependencies=[]))
        assert r.fan_out == _FAN_OUT_BASE["XL"]

    def test_fan_out_increases_with_deps(self) -> None:
        r0 = scan_cognitive_load(_task(size="M", allowed_dependencies=[]))
        r2 = scan_cognitive_load(_task(size="M", allowed_dependencies=["a.py", "b.py"]))
        assert r2.fan_out > r0.fan_out

    def test_fan_out_formula_exact_size_s_two_deps(self) -> None:
        deps = ["a.py", "b.py"]
        r = scan_cognitive_load(_task(size="S", allowed_dependencies=deps))
        expected = _FAN_OUT_BASE["S"] + (2 // _FAN_OUT_DEP_STEP)
        assert r.fan_out == expected

    def test_fan_out_formula_exact_size_m_four_deps(self) -> None:
        deps = [f"d{i}.py" for i in range(4)]
        r = scan_cognitive_load(_task(size="M", allowed_dependencies=deps))
        expected = _FAN_OUT_BASE["M"] + (4 // _FAN_OUT_DEP_STEP)
        assert r.fan_out == expected

    def test_fan_out_formula_exact_size_xl_six_deps(self) -> None:
        deps = [f"d{i}.py" for i in range(6)]
        r = scan_cognitive_load(_task(size="XL", allowed_dependencies=deps))
        expected = _FAN_OUT_BASE["XL"] + (6 // _FAN_OUT_DEP_STEP)
        assert r.fan_out == expected

    def test_fan_out_minimum_is_one(self) -> None:
        # Smallest possible: size S, 0 deps → base=1
        r = scan_cognitive_load(_task(size="S", allowed_dependencies=[]))
        assert r.fan_out >= 1

    def test_odd_dep_count_floors(self) -> None:
        # 3 deps: 3 // 2 = 1 extra fan_out unit
        deps = ["a.py", "b.py", "c.py"]
        r = scan_cognitive_load(_task(size="M", allowed_dependencies=deps))
        expected = _FAN_OUT_BASE["M"] + (3 // _FAN_OUT_DEP_STEP)
        assert r.fan_out == expected


# ── mock_limit ────────────────────────────────────────────────────────────────


class TestMockLimit:
    def test_zero_deps_gives_zero_mock_limit(self) -> None:
        r = scan_cognitive_load(_task(allowed_dependencies=[]))
        assert r.mock_limit == 0

    def test_one_dep_gives_mock_limit_one(self) -> None:
        r = scan_cognitive_load(_task(allowed_dependencies=["a.py"]))
        assert r.mock_limit == min(math.ceil(1 * _MOCK_FRACTION), _MOCK_MAX)

    def test_two_deps_mock_limit_formula(self) -> None:
        r = scan_cognitive_load(_task(allowed_dependencies=["a.py", "b.py"]))
        assert r.mock_limit == min(math.ceil(2 * _MOCK_FRACTION), _MOCK_MAX)

    def test_three_deps_mock_limit_formula(self) -> None:
        deps = ["a.py", "b.py", "c.py"]
        r = scan_cognitive_load(_task(allowed_dependencies=deps))
        assert r.mock_limit == min(math.ceil(3 * _MOCK_FRACTION), _MOCK_MAX)

    def test_four_deps_mock_limit_formula(self) -> None:
        deps = [f"d{i}.py" for i in range(4)]
        r = scan_cognitive_load(_task(allowed_dependencies=deps))
        assert r.mock_limit == min(math.ceil(4 * _MOCK_FRACTION), _MOCK_MAX)

    def test_ten_deps_capped_at_mock_max(self) -> None:
        deps = [f"d{i}.py" for i in range(10)]
        r = scan_cognitive_load(_task(allowed_dependencies=deps))
        # ceil(10 * 0.5) = 5 == _MOCK_MAX
        assert r.mock_limit == _MOCK_MAX

    def test_twelve_deps_still_capped_at_mock_max(self) -> None:
        deps = [f"d{i}.py" for i in range(12)]
        r = scan_cognitive_load(_task(allowed_dependencies=deps))
        assert r.mock_limit == _MOCK_MAX

    def test_mock_limit_never_exceeds_mock_max(self) -> None:
        for n in range(0, 20):
            deps = [f"d{i}.py" for i in range(n)]
            r = scan_cognitive_load(_task(allowed_dependencies=deps))
            assert r.mock_limit <= _MOCK_MAX

    def test_mock_limit_never_negative(self) -> None:
        r = scan_cognitive_load(_task(allowed_dependencies=[]))
        assert r.mock_limit >= 0

    def test_mock_limit_at_most_fan_in(self) -> None:
        for n in range(0, 10):
            deps = [f"d{i}.py" for i in range(n)]
            r = scan_cognitive_load(_task(allowed_dependencies=deps))
            assert r.mock_limit <= r.fan_in


# ── estimated_tokens ─────────────────────────────────────────────────────────


class TestEstimatedTokens:
    def test_minimum_tokens_size_s_no_deps(self) -> None:
        r = scan_cognitive_load(_task(size="S", allowed_dependencies=[]))
        expected = (
            _BASE_TOKENS["S"]
            + _FAN_IN_WEIGHT * 0
            + _FAN_OUT_WEIGHT * _FAN_OUT_BASE["S"]
        )
        assert r.estimated_tokens == expected

    def test_formula_exact_size_m_two_deps(self) -> None:
        deps = ["a.py", "b.py"]
        r = scan_cognitive_load(_task(size="M", allowed_dependencies=deps))
        fan_out = _FAN_OUT_BASE["M"] + (2 // _FAN_OUT_DEP_STEP)
        expected = _BASE_TOKENS["M"] + _FAN_IN_WEIGHT * 2 + _FAN_OUT_WEIGHT * fan_out
        assert r.estimated_tokens == expected

    def test_formula_exact_size_xl_six_deps(self) -> None:
        deps = [f"d{i}.py" for i in range(6)]
        r = scan_cognitive_load(_task(size="XL", allowed_dependencies=deps))
        fan_out = _FAN_OUT_BASE["XL"] + (6 // _FAN_OUT_DEP_STEP)
        expected = _BASE_TOKENS["XL"] + _FAN_IN_WEIGHT * 6 + _FAN_OUT_WEIGHT * fan_out
        assert r.estimated_tokens == expected

    def test_tokens_increase_with_more_deps(self) -> None:
        r2 = scan_cognitive_load(_task(size="M", allowed_dependencies=["a.py", "b.py"]))
        r4 = scan_cognitive_load(_task(size="M", allowed_dependencies=["a.py", "b.py", "c.py", "d.py"]))
        assert r4.estimated_tokens > r2.estimated_tokens

    def test_tokens_increase_with_larger_size(self) -> None:
        r_s = scan_cognitive_load(_task(size="S", allowed_dependencies=[]))
        r_m = scan_cognitive_load(_task(size="M", allowed_dependencies=[]))
        r_xl = scan_cognitive_load(_task(size="XL", allowed_dependencies=[]))
        assert r_s.estimated_tokens < r_m.estimated_tokens < r_xl.estimated_tokens

    def test_estimated_tokens_always_positive(self) -> None:
        r = scan_cognitive_load(_task(size="S", allowed_dependencies=[]))
        assert r.estimated_tokens > 0


# ── exceeds_ceiling ───────────────────────────────────────────────────────────


class TestExceedsCeiling:
    def test_exactly_at_ceiling_does_not_exceed(self) -> None:
        # First compute the tokens for a known task, then use that as ceiling.
        tokens = scan_cognitive_load(_task(size="S", allowed_dependencies=[])).estimated_tokens
        r = scan_cognitive_load(_task(size="S", allowed_dependencies=[], max_cognitive_load_tokens=tokens))
        assert r.exceeds_ceiling is False

    def test_one_token_over_ceiling_exceeds(self) -> None:
        t_ref = _task(size="S", allowed_dependencies=[])
        tokens = scan_cognitive_load(t_ref).estimated_tokens
        r = scan_cognitive_load(_task(size="S", allowed_dependencies=[], max_cognitive_load_tokens=tokens - 1))
        assert r.exceeds_ceiling is True

    def test_large_ceiling_never_exceeds(self) -> None:
        r = scan_cognitive_load(_task(
            size="XL",
            allowed_dependencies=[f"d{i}.py" for i in range(5)],
            max_cognitive_load_tokens=999_999,
        ))
        assert r.exceeds_ceiling is False

    def test_tiny_ceiling_always_exceeds(self) -> None:
        r = scan_cognitive_load(_task(
            size="S",
            allowed_dependencies=[],
            max_cognitive_load_tokens=1,
        ))
        assert r.exceeds_ceiling is True

    def test_exceeds_ceiling_reflects_ceiling_field(self) -> None:
        t = _task(size="M", allowed_dependencies=["a.py", "b.py"], max_cognitive_load_tokens=5000)
        r = scan_cognitive_load(t)
        assert r.ceiling == 5000
        assert r.exceeds_ceiling == (r.estimated_tokens > 5000)

    def test_exceeds_ceiling_false_when_estimated_equals_ceiling(self) -> None:
        t_base = _task(size="S", allowed_dependencies=[])
        tokens = scan_cognitive_load(t_base).estimated_tokens
        r = scan_cognitive_load(_task(size="S", allowed_dependencies=[], max_cognitive_load_tokens=tokens))
        assert r.exceeds_ceiling is False

    def test_all_three_sizes_can_exceed(self) -> None:
        for size in ["S", "M", "XL"]:
            r = scan_cognitive_load(_task(size=size, allowed_dependencies=[], max_cognitive_load_tokens=1))
            assert r.exceeds_ceiling is True, f"size={size} should exceed ceiling=1"


# ── Report model invariants ───────────────────────────────────────────────────


class TestCognitiveLoadReportInvariants:
    def test_size_reflected_from_task(self) -> None:
        for size in ["S", "M", "XL"]:
            r = scan_cognitive_load(_task(size=size))
            assert r.size == size

    def test_ceiling_reflected_from_task(self) -> None:
        r = scan_cognitive_load(_task(max_cognitive_load_tokens=7777))
        assert r.ceiling == 7777

    def test_report_is_frozen(self) -> None:
        r = scan_cognitive_load(_task())
        with pytest.raises(ValidationError):
            r.fan_in = 999  # type: ignore[misc]

    def test_json_round_trip(self) -> None:
        r = scan_cognitive_load(_task(
            size="M",
            allowed_dependencies=["spec.py", "impl.py"],
            max_cognitive_load_tokens=5000,
        ))
        rebuilt = CognitiveLoadReport.model_validate_json(r.model_dump_json())
        assert rebuilt == r

    def test_all_fields_present_in_json_output(self) -> None:
        r = scan_cognitive_load(_task(size="M", allowed_dependencies=["x.py"]))
        data = r.model_dump()
        for field in ("fan_in", "fan_out", "mock_limit", "estimated_tokens", "ceiling", "exceeds_ceiling", "size"):
            assert field in data

    def test_idempotent_same_input_same_output(self) -> None:
        t = _task(size="M", allowed_dependencies=["a.py", "b.py"], max_cognitive_load_tokens=5000)
        r1 = scan_cognitive_load(t)
        r2 = scan_cognitive_load(t)
        assert r1 == r2

    def test_no_side_effects_on_task(self) -> None:
        t = _task(size="M", allowed_dependencies=["a.py"])
        _ = scan_cognitive_load(t)
        assert list(t.allowed_dependencies) == ["a.py"]
        assert t.size == "M"


# ── Cross-template integration ────────────────────────────────────────────────


class TestCrossTemplateIntegration:
    def test_report_fan_in_matches_task_dep_count(self) -> None:
        deps = ["spec.py", "task.py", "review.py"]
        t = _task(allowed_dependencies=deps)
        r = scan_cognitive_load(t)
        assert r.fan_in == len(t.allowed_dependencies)

    def test_max_cognitive_load_tokens_respected_as_ceiling(self) -> None:
        t = _task(
            size="M",
            allowed_dependencies=["a.py"],
            max_cognitive_load_tokens=4_096,
        )
        r = scan_cognitive_load(t)
        assert r.ceiling == t.max_cognitive_load_tokens

    def test_s_size_task_passes_generous_ceiling(self) -> None:
        t = _task(size="S", allowed_dependencies=[], max_cognitive_load_tokens=4_096)
        r = scan_cognitive_load(t)
        assert r.exceeds_ceiling is False

    def test_xl_many_deps_fails_tight_ceiling(self) -> None:
        deps = [f"d{i}.py" for i in range(8)]
        t = _task(size="XL", allowed_dependencies=deps, max_cognitive_load_tokens=1_000)
        r = scan_cognitive_load(t)
        assert r.exceeds_ceiling is True
