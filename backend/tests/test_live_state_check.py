"""Contract tests for backend.agents.live_state_check.

Per docs/sop/jira-ticket-conventions.md §13. Pins each check kind's
pass/fail behaviour + the dispatcher's malformed-input handling.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import live_state_check as lsc


# ── Dispatcher ────────────────────────────────────────────────────


def test_evaluate_unknown_kind_fails() -> None:
    results = lsc.evaluate([{"never_heard_of_this": "x"}])
    assert len(results) == 1
    assert not results[0].passed
    assert "unknown check kind" in results[0].detail


def test_evaluate_malformed_dict_too_many_keys() -> None:
    results = lsc.evaluate([{"file_exists": "a", "alembic_head": "b"}])
    assert not results[0].passed
    assert "exactly 1 key" in results[0].detail


def test_evaluate_malformed_not_dict() -> None:
    results = lsc.evaluate(["not a dict"])  # type: ignore[arg-type]
    assert not results[0].passed
    assert "must be dict" in results[0].detail


def test_all_passed_empty_list() -> None:
    assert lsc.all_passed([]) is True


def test_format_failures_only_failed_lines() -> None:
    results = [
        lsc.CheckResult(True, "alembic_head", "ok"),
        lsc.CheckResult(False, "file_exists", "missing X"),
    ]
    out = lsc.format_failures(results)
    assert "file_exists" in out
    assert "alembic_head" not in out


# ── file_exists ───────────────────────────────────────────────────


def test_file_exists_true_for_known_file() -> None:
    r = lsc.evaluate([{"file_exists": "TODO.md"}])
    assert r[0].passed
    assert "present" in r[0].detail


def test_file_exists_false_for_missing() -> None:
    r = lsc.evaluate([{"file_exists": "no/such/path/zzz.txt"}])
    assert not r[0].passed
    assert "MISSING" in r[0].detail


# ── command_succeeds ──────────────────────────────────────────────


def test_command_succeeds_true_zero_exit() -> None:
    r = lsc.evaluate([{"command_succeeds": "true"}])
    assert r[0].passed


def test_command_succeeds_false_nonzero() -> None:
    r = lsc.evaluate([{"command_succeeds": "false"}])
    assert not r[0].passed
    assert "exit 1" in r[0].detail


# ── feature_flag ──────────────────────────────────────────────────


def test_feature_flag_match(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_TEST_LSC_FLAG", "yes")
    r = lsc.evaluate([{"feature_flag": "OMNISIGHT_TEST_LSC_FLAG=yes"}])
    assert r[0].passed


def test_feature_flag_mismatch(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_TEST_LSC_FLAG", "no")
    r = lsc.evaluate([{"feature_flag": "OMNISIGHT_TEST_LSC_FLAG=yes"}])
    assert not r[0].passed


def test_feature_flag_unset(monkeypatch) -> None:
    monkeypatch.delenv("OMNISIGHT_TEST_LSC_FLAG", raising=False)
    r = lsc.evaluate([{"feature_flag": "OMNISIGHT_TEST_LSC_FLAG=yes"}])
    assert not r[0].passed
    assert "<unset>" in r[0].detail


# ── Multiple requirements + ordering ─────────────────────────────


def test_evaluate_preserves_order_and_independence() -> None:
    results = lsc.evaluate([
        {"file_exists": "TODO.md"},
        {"command_succeeds": "false"},
        {"file_exists": "no/such.txt"},
    ])
    assert [r.passed for r in results] == [True, False, False]
    assert lsc.all_passed(results) is False
