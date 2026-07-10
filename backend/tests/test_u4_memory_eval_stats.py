"""OP-2574 U4-F-math — pure paired-eval statistics core tests (offline).

Covers the ticket AC (a)-(h): hand-computed exact McNemar pins,
the clustered-collapse matrix (independent per-side majority, tie ->
False, ordering), summarize counts + duplicate-case_id loudness, every
branch of the FROZEN decision table (boundaries included), the frozen
4-key stat_summary / 2-key stat_detail shapes cross-checked against the
D approval-card contract, determinism, dormancy, and the module-source
string bans. Pure stdlib module under test — no scipy, no numpy, no
skips.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from backend.learned_item_approval import _EVAL_SUMMARY_KEYS
from backend.memory_eval_stats import (
    PairedCase,
    PairedSummary,
    build_stat_detail,
    build_stat_summary,
    collapse_clustered,
    decide,
    mcnemar_exact_p,
    summarize,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PURE_MODULE = BACKEND_ROOT / "memory_eval_stats.py"


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=0, abs_tol=1e-12)


def _case(cid: str, base: bool, cand: bool) -> PairedCase:
    return PairedCase(case_id=cid, baseline_pass=base, candidate_pass=cand)


# ━━ (a) mcnemar_exact_p — hand-computed pins ━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMcNemarExactP:
    def test_zero_zero_is_one(self) -> None:
        assert mcnemar_exact_p(0, 0) == 1.0

    def test_one_zero_is_one_via_capped_path(self) -> None:
        # n=1, k=0: 2*comb(1,0)*0.5 = 1.0 (the 2*0.5 capped path).
        assert mcnemar_exact_p(1, 0) == 1.0

    def test_nine_one_pin(self) -> None:
        # 2*(comb(10,0)+comb(10,1))*0.5**10 = 22/1024 = 0.021484375
        assert _close(mcnemar_exact_p(9, 1), 22 / 1024)
        assert _close(mcnemar_exact_p(9, 1), 0.021484375)

    def test_five_five_capped_to_one(self) -> None:
        # Doubled smaller tail exceeds 1 when perfectly balanced — cap.
        assert mcnemar_exact_p(5, 5) == 1.0

    @pytest.mark.parametrize(
        ("a", "b"), [(0, 0), (1, 0), (9, 1), (5, 5), (3, 7), (12, 2)]
    )
    def test_symmetry(self, a: int, b: int) -> None:
        assert mcnemar_exact_p(a, b) == mcnemar_exact_p(b, a)

    @pytest.mark.parametrize(("a", "b"), [(-1, 0), (0, -1), (-2, -3)])
    def test_negatives_raise(self, a: int, b: int) -> None:
        with pytest.raises(ValueError):
            mcnemar_exact_p(a, b)


# ━━ (b) collapse_clustered — the collapse matrix ━━━━━━━━━━━━━━━━━━━━━


class TestCollapseClustered:
    def test_three_sample_majority_per_side_independently(self) -> None:
        # Same case: baseline (T,T,F) -> True while candidate (F,F,T)
        # -> False — the sides are voted INDEPENDENTLY.
        samples = [
            _case("c1", True, False),
            _case("c1", True, False),
            _case("c1", False, True),
        ]
        assert collapse_clustered(samples) == [_case("c1", True, False)]

    def test_four_sample_two_two_tie_is_false(self) -> None:
        # Ties need an even count; 2-2 -> False on both sides
        # (conservative: a tie is not a pass).
        samples = [
            _case("c1", True, True),
            _case("c1", True, True),
            _case("c1", False, False),
            _case("c1", False, False),
        ]
        assert collapse_clustered(samples) == [_case("c1", False, False)]

    def test_variable_repeat_counts_strict_majority(self) -> None:
        # c1 has 5 samples (3/5 baseline True), c2 has 1, c3 has 2
        # (1/2 -> tie -> False). Strict majority: true*2 > total.
        samples = [
            _case("c1", True, False),
            _case("c1", True, False),
            _case("c1", True, True),
            _case("c1", False, True),
            _case("c1", False, True),
            _case("c2", False, True),
            _case("c3", True, False),
            _case("c3", False, False),
        ]
        assert collapse_clustered(samples) == [
            _case("c1", True, True),
            _case("c2", False, True),
            _case("c3", False, False),
        ]

    def test_multi_case_ordered_by_case_id_input_order_irrelevant(
        self,
    ) -> None:
        samples = [
            _case("c30", True, True),
            _case("c02", False, True),
            _case("c10", True, False),
        ]
        collapsed = collapse_clustered(samples)
        assert [c.case_id for c in collapsed] == ["c02", "c10", "c30"]
        assert collapse_clustered(list(reversed(samples))) == collapsed

    def test_empty_input_empty_output(self) -> None:
        assert collapse_clustered([]) == []


# ━━ (c) summarize — counts + duplicate loudness ━━━━━━━━━━━━━━━━━━━━━━


class TestSummarize:
    def test_counts(self) -> None:
        cases = [
            _case("c1", False, True),   # improvement
            _case("c2", False, True),   # improvement
            _case("c3", True, False),   # regression
            _case("c4", True, True),    # concordant pass
            _case("c5", False, False),  # concordant fail
        ]
        s = summarize(cases)
        assert s.n == 5
        assert s.improvements == 2
        assert s.regressions == 1
        assert s.net_flips == 1
        assert _close(s.mcnemar_p, mcnemar_exact_p(2, 1))

    def test_empty(self) -> None:
        s = summarize([])
        assert s == PairedSummary(
            n=0, improvements=0, regressions=0, net_flips=0, mcnemar_p=1.0
        )

    def test_duplicate_case_id_raises(self) -> None:
        cases = [_case("c1", False, True), _case("c1", False, True)]
        with pytest.raises(ValueError, match="duplicate case_id"):
            summarize(cases)


# ━━ (d) decide — every branch of the FROZEN table ━━━━━━━━━━━━━━━━━━━━


def _summary(
    n: int, improvements: int, regressions: int, mcnemar_p: float
) -> PairedSummary:
    return PairedSummary(
        n=n,
        improvements=improvements,
        regressions=regressions,
        net_flips=improvements - regressions,
        mcnemar_p=mcnemar_p,
    )


class TestDecide:
    def test_gate1_below_min_cases_insufficient(self) -> None:
        s = _summary(9, 9, 0, 0.001)
        assert decide(s) == "insufficient_evidence"

    def test_gate1_boundary_n_equals_min_cases_passes(self) -> None:
        # n == min_cases passes gate 1 (the crafted set below proves
        # it reaches promote, so gate 1 did not fire).
        s = _summary(10, 9, 1, mcnemar_exact_p(9, 1))
        assert decide(s) == "promote"

    def test_gate2_boundary_discordant_equals_min_promotes(self) -> None:
        # Real 3-discordant p can never beat alpha (best is 0.25), so
        # the gate-2 boundary is only observable SYNTHETICALLY.
        s = _summary(10, 3, 0, 0.001)
        assert decide(s) == "promote"

    def test_gate2_two_discordant_insufficient(self) -> None:
        s = _summary(10, 2, 0, 0.001)
        assert decide(s) == "insufficient_evidence"

    def test_p_exactly_alpha_is_insufficient(self) -> None:
        # p < alpha is STRICT: p == alpha does not promote.
        s = _summary(10, 4, 0, 0.05)
        assert decide(s) == "insufficient_evidence"

    def test_significant_improvement_promotes(self) -> None:
        cases = [_case(f"c{i:02d}", False, True) for i in range(9)]
        cases.append(_case("c99", True, False))
        s = summarize(cases)
        assert s.n == 10
        assert s.improvements == 9
        assert s.regressions == 1
        assert s.mcnemar_p == 11 / 512  # 0.021484375 exactly
        assert s.mcnemar_p == 0.021484375
        assert decide(s) == "promote"

    def test_significant_regression_rejects(self) -> None:
        cases = [_case(f"c{i:02d}", True, False) for i in range(9)]
        cases.append(_case("c99", False, True))
        s = summarize(cases)
        assert s.net_flips == -8
        assert decide(s) == "reject"

    def test_nonsignificant_insufficient(self) -> None:
        s = _summary(10, 5, 5, mcnemar_exact_p(5, 5))
        assert decide(s) == "insufficient_evidence"

    def test_no_pp_scalar_branch_in_source(self) -> None:
        # The FROZEN table has no +pp scalar threshold — belt-and-braces
        # source assertion (codex B1).
        src = PURE_MODULE.read_text()
        assert "pass_rate" not in src
        assert "percentage_point" not in src


# ━━ (e) stat_summary / stat_detail shapes + D cross-check ━━━━━━━━━━━━


class TestStatSummaryShape:
    def test_exact_four_keys(self) -> None:
        s = _summary(10, 9, 1, mcnemar_exact_p(9, 1))
        out = build_stat_summary(s, decide(s))
        assert set(out) == {"decision", "mcnemar_p", "net_flips", "n"}
        assert out["decision"] == "promote"
        assert _close(out["mcnemar_p"], 11 / 512)
        assert out["net_flips"] == 8
        assert out["n"] == 10

    @pytest.mark.parametrize(
        "decision",
        ["promote", "reject", "insufficient_evidence", "infra_invalid"],
    )
    def test_all_allowed_decisions_accepted(self, decision: str) -> None:
        s = _summary(0, 0, 0, 1.0)
        assert build_stat_summary(s, decision)["decision"] == decision

    @pytest.mark.parametrize(
        "decision", ["", "PROMOTE", "approved", "infra-invalid", "maybe"]
    )
    def test_bad_decision_raises(self, decision: str) -> None:
        s = _summary(0, 0, 0, 1.0)
        with pytest.raises(ValueError, match="unknown decision"):
            build_stat_summary(s, decision)

    def test_detail_exact_two_keys(self) -> None:
        s = _summary(10, 9, 1, mcnemar_exact_p(9, 1))
        detail = build_stat_detail(s)
        assert detail == {"improvements": 9, "regressions": 1}
        assert set(detail) == {"improvements", "regressions"}

    def test_cross_check_against_d_approval_card_contract(self) -> None:
        # The D card builder rejects unknown eval_summary keys — our
        # frozen 4-key shape must equal its allowlist EXACTLY. (This
        # import lives in the TEST file only; the module under test
        # never names the D module.)
        s = _summary(10, 9, 1, mcnemar_exact_p(9, 1))
        out = build_stat_summary(s, decide(s))
        assert set(out) == set(_EVAL_SUMMARY_KEYS)


# ━━ (f) determinism ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDeterminism:
    def test_same_inputs_identical_dataclasses(self) -> None:
        samples = [
            _case("c2", True, False),
            _case("c1", False, True),
            _case("c1", False, True),
            _case("c1", True, False),
            _case("c2", True, True),
        ]
        first = collapse_clustered(samples)
        second = collapse_clustered(samples)
        assert first == second
        assert summarize(first) == summarize(second)
        s = summarize(first)
        assert decide(s) == decide(s)
        assert build_stat_summary(s, decide(s)) == build_stat_summary(
            s, decide(s)
        )
        assert build_stat_detail(s) == build_stat_detail(s)


# ━━ (g) dormant — no caller until U4-F-exec ━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDormantShip:
    def test_no_nontest_module_references_the_new_module(self) -> None:
        own = {"memory_eval_stats.py", "memory_promotion_eval.py"}
        offenders: list[str] = []
        for py in BACKEND_ROOT.rglob("*.py"):
            rel = py.relative_to(BACKEND_ROOT)
            parts = rel.parts
            if parts[0] in ("tests", "node_modules") or (
                parts[:2] == ("alembic", "versions")
            ):
                continue
            if len(parts) == 1 and parts[0] in own:
                continue
            if "memory_eval_stats" in py.read_text(errors="ignore"):
                offenders.append(str(rel))
        assert offenders == [], f"dormant-ship violated by: {offenders}"


# ━━ (h) module-source string bans ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestModuleSourceBans:
    def test_no_ledger_table_names(self) -> None:
        src = PURE_MODULE.read_text()
        for table in (
            "learned_item_versions",
            "learned_item_evidence",
            "memory_eval_runs",
            "memory_eval_cases",
            "memory_approvals",
            "memory_publications",
            "memory_transition_events",
            "learned_item_snapshots",
        ):
            assert table not in src, table

    def test_no_sibling_module_names(self) -> None:
        src = PURE_MODULE.read_text()
        # "learned_item" covers record/renderer/approval/provenance/
        # publisher/publication/loader in one sweep.
        assert "learned_item" not in src
        assert "_SKILLS_LIVE" not in src

    def test_no_clock_reads(self) -> None:
        # E precedent: the substring check matches comments too — keep
        # the clock substrings out of the whole file.
        src = PURE_MODULE.read_text()
        assert "datetime.now" not in src
        assert "time.time" not in src
        assert "import datetime" not in src
        assert "import time" not in src

    def test_only_stdlib_math_and_dataclasses_imported(self) -> None:
        src = PURE_MODULE.read_text()
        import_lines = [
            line.strip()
            for line in src.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        assert import_lines == [
            "from __future__ import annotations",
            "import math",
            "from dataclasses import dataclass",
        ]

    def test_no_scipy_numpy(self) -> None:
        src = PURE_MODULE.read_text()
        assert "scipy" not in src
        assert "numpy" not in src
