"""Z.7.9 — Unit tests for scripts/ci_budget_guard.py.

Verifies that the budget guard correctly:
  - Reads token ceiling and max-iter from env vars
  - Computes worst-case cost estimates
  - Parses pytest-json-report files
  - Fails and writes error annotations when cost exceeds the budget
  - Passes and writes notice annotations when under budget

These tests import the guard module directly (no subprocess) so that
pytest can measure coverage.  The module is under ``scripts/``, so we
add the repo root to sys.path rather than making it a package.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from unittest.mock import patch

import pytest

# Add repo root to path so we can import scripts/ci_budget_guard.py
_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, _REPO_ROOT)

import scripts.ci_budget_guard as guard  # noqa: E402  (path manip before import)


# ── helpers ───────────────────────────────────────────────────────────────────


def _write_report(tmp_path: str, passed: int, failed: int, skipped: int) -> str:
    """Write a minimal pytest-json-report file and return its path."""
    data = {
        "summary": {
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "total": passed + failed + skipped,
        }
    }
    path = os.path.join(tmp_path, "report.json")
    with open(path, "w") as f:
        json.dump(data, f)
    return path


# ── _get_max_tokens_per_call ──────────────────────────────────────────────────


def test_get_max_tokens_default(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_CI_MAX_TOKENS_PER_CALL", raising=False)
    assert guard._get_max_tokens_per_call() == guard._DEFAULT_MAX_TOKENS_PER_CALL


def test_get_max_tokens_from_env(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_CI_MAX_TOKENS_PER_CALL", "500")
    assert guard._get_max_tokens_per_call() == 500


def test_get_max_tokens_invalid_env(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_CI_MAX_TOKENS_PER_CALL", "not_a_number")
    assert guard._get_max_tokens_per_call() == guard._DEFAULT_MAX_TOKENS_PER_CALL


# ── _get_max_iter ─────────────────────────────────────────────────────────────


def test_get_max_iter_default(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_CI_MAX_ITER", raising=False)
    assert guard._get_max_iter() == guard._DEFAULT_MAX_ITER


def test_get_max_iter_from_env(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_CI_MAX_ITER", "5")
    assert guard._get_max_iter() == 5


def test_get_max_iter_invalid_env(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_CI_MAX_ITER", "abc")
    assert guard._get_max_iter() == guard._DEFAULT_MAX_ITER


# ── estimate_cost ─────────────────────────────────────────────────────────────


def test_estimate_cost_zero_tests(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_CI_MAX_TOKENS_PER_CALL", raising=False)
    monkeypatch.delenv("OMNISIGHT_CI_MAX_ITER", raising=False)
    assert guard.estimate_cost(0) == 0.0


def test_estimate_cost_positive(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_CI_MAX_TOKENS_PER_CALL", raising=False)
    monkeypatch.delenv("OMNISIGHT_CI_MAX_ITER", raising=False)
    cost = guard.estimate_cost(15)  # 15 tests = 3 providers × 5 tests
    assert cost > 0.0
    # With defaults (2000 tokens, 3 iter), 15 tests should be well under $0.50
    assert cost < 0.50, f"15 tests with default caps should be < $0.50, got ${cost:.4f}"


def test_estimate_cost_uses_token_ceiling(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_CI_MAX_TOKENS_PER_CALL", "100")
    monkeypatch.setenv("OMNISIGHT_CI_MAX_ITER", "1")
    cost_small = guard.estimate_cost(10)

    monkeypatch.setenv("OMNISIGHT_CI_MAX_TOKENS_PER_CALL", "10000")
    monkeypatch.setenv("OMNISIGHT_CI_MAX_ITER", "1")
    cost_large = guard.estimate_cost(10)

    assert cost_large > cost_small, "higher token ceiling must produce higher estimated cost"


def test_estimate_cost_uses_max_iter(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_CI_MAX_TOKENS_PER_CALL", "500")
    monkeypatch.setenv("OMNISIGHT_CI_MAX_ITER", "1")
    cost_1iter = guard.estimate_cost(5)

    monkeypatch.setenv("OMNISIGHT_CI_MAX_ITER", "3")
    cost_3iter = guard.estimate_cost(5)

    assert cost_3iter > cost_1iter, "more iterations must produce higher estimated cost"
    # 3 iterations should be approximately 3× the 1-iteration cost
    assert abs(cost_3iter / cost_1iter - 3.0) < 0.01, (
        f"3-iter cost should be 3× 1-iter cost; ratio={cost_3iter / cost_1iter:.3f}"
    )


# ── _count_tests ──────────────────────────────────────────────────────────────


def test_count_tests_normal():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_report(tmp, passed=10, failed=2, skipped=3)
        tests_run, tests_passed, tests_skipped = guard._count_tests(path)
    assert tests_run == 12  # passed + failed
    assert tests_passed == 10
    assert tests_skipped == 3


def test_count_tests_all_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_report(tmp, passed=0, failed=0, skipped=15)
        tests_run, tests_passed, tests_skipped = guard._count_tests(path)
    assert tests_run == 0
    assert tests_skipped == 15


def test_count_tests_missing_file():
    tests_run, tests_passed, tests_skipped = guard._count_tests("/nonexistent/path/report.json")
    assert tests_run == 0
    assert tests_passed == 0
    assert tests_skipped == 0


def test_count_tests_invalid_json():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bad.json")
        with open(path, "w") as f:
            f.write("not valid json {{{")
        tests_run, tests_passed, tests_skipped = guard._count_tests(path)
    assert tests_run == 0


# ── main (integration-level) ─────────────────────────────────────────────────


def test_main_pass(monkeypatch, capsys):
    monkeypatch.setenv("OMNISIGHT_CI_MAX_COST_USD", "0.50")
    monkeypatch.setenv("OMNISIGHT_CI_MAX_TOKENS_PER_CALL", "2000")
    monkeypatch.setenv("OMNISIGHT_CI_MAX_ITER", "3")
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_report(tmp, passed=15, failed=0, skipped=0)
        # Patch sys.argv
        with patch.object(sys, "argv", ["ci_budget_guard.py", path]):
            rc = guard.main()
    assert rc == 0, "15 tests with default caps should pass the $0.50 budget"
    out = capsys.readouterr().out
    assert "PASS" in out
    assert "::notice::" in out


def test_main_fail_over_budget(monkeypatch, capsys):
    # Force budget failure: tiny budget, many tests, high token ceiling
    monkeypatch.setenv("OMNISIGHT_CI_MAX_COST_USD", "0.0001")
    monkeypatch.setenv("OMNISIGHT_CI_MAX_TOKENS_PER_CALL", "2000")
    monkeypatch.setenv("OMNISIGHT_CI_MAX_ITER", "3")
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_report(tmp, passed=15, failed=0, skipped=0)
        with patch.object(sys, "argv", ["ci_budget_guard.py", path]):
            rc = guard.main()
    assert rc == 1, "should fail when estimated cost exceeds tiny budget"
    out = capsys.readouterr().out
    assert "::error::" in out
    assert "budget exceeded" in out.lower() or "budget" in out.lower()


def test_main_no_args(capsys):
    with patch.object(sys, "argv", ["ci_budget_guard.py"]):
        rc = guard.main()
    assert rc == 2


def test_main_missing_report(monkeypatch, capsys):
    monkeypatch.setenv("OMNISIGHT_CI_MAX_COST_USD", "0.50")
    monkeypatch.delenv("OMNISIGHT_CI_MAX_TOKENS_PER_CALL", raising=False)
    monkeypatch.delenv("OMNISIGHT_CI_MAX_ITER", raising=False)
    with patch.object(sys, "argv", ["ci_budget_guard.py", "/nonexistent/report.json"]):
        rc = guard.main()
    # Missing report → 0 tests run → estimated cost = 0 → PASS
    assert rc == 0


def test_main_includes_token_and_iter_in_output(monkeypatch, capsys):
    monkeypatch.setenv("OMNISIGHT_CI_MAX_COST_USD", "0.50")
    monkeypatch.setenv("OMNISIGHT_CI_MAX_TOKENS_PER_CALL", "1234")
    monkeypatch.setenv("OMNISIGHT_CI_MAX_ITER", "2")
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_report(tmp, passed=5, failed=0, skipped=0)
        with patch.object(sys, "argv", ["ci_budget_guard.py", path]):
            guard.main()
    out = capsys.readouterr().out
    assert "1234" in out, "token ceiling should appear in budget-guard output"
    assert "2" in out, "max_iter should appear in budget-guard output"
