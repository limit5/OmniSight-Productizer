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


# ── L17 cwd parameter (per pre_pickup_ok refactor) ────────────────


def test_evaluate_uses_custom_cwd_for_file_exists(tmp_path) -> None:
    """When cwd is passed, file_exists resolves relative to it."""
    (tmp_path / "marker.txt").write_text("present")
    results = lsc.evaluate([{"file_exists": "marker.txt"}], cwd=tmp_path)
    assert results[0].passed
    assert "present" in results[0].detail


def test_evaluate_custom_cwd_isolates_from_repo_root(tmp_path) -> None:
    """A file present in REPO_ROOT but absent in custom cwd → fail."""
    # TODO.md exists in REPO_ROOT
    repo_results = lsc.evaluate([{"file_exists": "TODO.md"}])
    assert repo_results[0].passed
    # but not in tmp_path
    tmp_results = lsc.evaluate([{"file_exists": "TODO.md"}], cwd=tmp_path)
    assert not tmp_results[0].passed
    assert "MISSING" in tmp_results[0].detail


def test_evaluate_custom_cwd_for_command_succeeds(tmp_path) -> None:
    """command_succeeds runs in custom cwd."""
    (tmp_path / "smoke").write_text("hello")
    results = lsc.evaluate([{"command_succeeds": "test -f smoke"}], cwd=tmp_path)
    assert results[0].passed


def test_evaluate_default_cwd_is_repo_root() -> None:
    """When cwd=None (default), checks resolve relative to REPO_ROOT (backward-compat)."""
    results_none = lsc.evaluate([{"file_exists": "TODO.md"}], cwd=None)
    results_unset = lsc.evaluate([{"file_exists": "TODO.md"}])
    # both should pass — TODO.md is in REPO_ROOT
    assert results_none[0].passed
    assert results_unset[0].passed


def test_check_kinds_signature_takes_two_args() -> None:
    """All registered handlers must accept (expected, cwd) per L17 refactor."""
    import inspect
    for kind, handler in lsc.CHECK_KINDS.items():
        sig = inspect.signature(handler)
        params = list(sig.parameters.values())
        assert len(params) == 2, f"{kind} handler must take 2 args, got {len(params)}"
        assert params[0].name == "expected"
        assert params[1].name == "cwd"


# ── Transition-boundary TOCTOU re-read ───────────────────────────


def _fields(
    *,
    assignee: str | None = "acct-1",
    status: str = "In Progress",
    labels: list[str] | None = None,
    links: list[dict] | None = None,
) -> dict:
    assignee_payload = {"accountId": assignee} if assignee is not None else None
    return {
        "assignee": assignee_payload,
        "status": {"name": status},
        "labels": labels or [],
        "issuelinks": links or [],
    }


def _blocks_link(key: str | None, status: str = "To Do") -> dict:
    inward: dict = {"fields": {"status": {"name": status}}}
    if key is not None:
        inward["key"] = key
    return {
        "type": {"name": "Blocks"},
        "inwardIssue": inward,
    }


def test_snapshot_from_fields_captures_mutable_ticket_state() -> None:
    fields = _fields(
        assignee="acct-7",
        status="In Progress",
        labels=["runner", "class:normal"],
        links=[
            _blocks_link("OP-1"),
            {"type": {"name": "Relates"}, "inwardIssue": {"key": "OP-2"}},
            _blocks_link(None),
        ],
    )

    snap = lsc._snapshot_from_fields("OP-1308", fields)

    assert snap.key == "OP-1308"
    assert snap.assignee_account_id == "acct-7"
    assert snap.status_name == "In Progress"
    assert snap.labels == frozenset({"runner", "class:normal"})
    assert snap.blocker_keys == frozenset({"OP-1"})


def test_evaluate_recheck_detects_assignee_change() -> None:
    snap = lsc._snapshot_from_fields("OP-1308", _fields(assignee="acct-1"))

    result = lsc._evaluate_recheck(snap, _fields(assignee="acct-2"))

    assert not result.ok
    assert result.action == "abort_assignee_changed"
    assert "assignee diverged" in result.reason


@pytest.mark.parametrize(
    ("live_status", "action"),
    [
        ("To Do", "abort_reverted"),
        ("Under Review", "abort_already_advanced"),
    ],
)
def test_evaluate_recheck_detects_status_drift(live_status: str, action: str) -> None:
    snap = lsc._snapshot_from_fields("OP-1308", _fields(status="In Progress"))

    result = lsc._evaluate_recheck(snap, _fields(status=live_status))

    assert not result.ok
    assert result.action == action
    assert live_status in result.reason


def test_evaluate_recheck_detects_new_operator_window_label() -> None:
    snap = lsc._snapshot_from_fields(
        "OP-1308",
        _fields(labels=["area:backend"]),
    )

    result = lsc._evaluate_recheck(
        snap,
        _fields(labels=["area:backend", "class:operator-window-live"]),
    )

    assert not result.ok
    assert result.action == "abort_operator_window"
    assert "operator-window/rehearsal" in result.reason


def test_evaluate_recheck_detects_new_unpublished_blocker() -> None:
    snap = lsc._snapshot_from_fields(
        "OP-1308",
        _fields(links=[_blocks_link("OP-1", status="Published")]),
    )

    result = lsc._evaluate_recheck(
        snap,
        _fields(links=[
            _blocks_link("OP-1", status="Published"),
            _blocks_link("OP-2", status="In Progress"),
        ]),
    )

    assert not result.ok
    assert result.action == "abort_newly_blocked"
    assert "OP-2(In Progress)" in result.reason


def test_evaluate_recheck_allows_published_new_blocker() -> None:
    snap = lsc._snapshot_from_fields("OP-1308", _fields())

    result = lsc._evaluate_recheck(
        snap,
        _fields(links=[_blocks_link("OP-2", status="Published")]),
    )

    assert result.ok
    assert result.action == "ok"


def test_recheck_transition_boundary_state_can_be_disabled(monkeypatch) -> None:
    snap = lsc._snapshot_from_fields("OP-1308", _fields())
    monkeypatch.setenv(lsc._TOCTOU_REREAD_ENV, "false")

    result = lsc.recheck_transition_boundary_state(object(), "OP-1308", snap)

    assert result.ok
    assert result.action == "disabled"


def test_recheck_transition_boundary_state_uses_ttl_cache(monkeypatch) -> None:
    calls: list[str] = []

    def fake_fetch(client, key: str) -> dict:
        calls.append(key)
        return _fields()

    monkeypatch.setattr(lsc, "_toctou_fetch_live", fake_fetch)
    lsc._toctou_reset_cache()
    try:
        snap = lsc.capture_transition_boundary_snapshot(object(), "OP-1308", now=10.0)
        result = lsc.recheck_transition_boundary_state(
            object(),
            "OP-1308",
            snap,
            now=20.0,
        )
    finally:
        lsc._toctou_reset_cache()

    assert result.ok
    assert calls == ["OP-1308"]
