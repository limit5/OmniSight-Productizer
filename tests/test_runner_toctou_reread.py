"""SP-B-X-004 / OP-1062 — C3a TOCTOU live-state reread at transition boundaries.

Network-free contract tests for
:func:`backend.agents.live_state_check.recheck_transition_boundary_state`
and the runner-side wiring in ``auto-runner-jira.py``
(:func:`_handle_toctou_abort`). The reread primitive is exercised end-to-end
through a monkey-patched JIRA transport — every test injects fake field
payloads and asserts on the BoundaryRecheckResult action.

AC coverage:
 - **TOCTOU fields**: assignee swap, status drift (revert + advance),
   ``class:operator-window-*`` / ``class:operator-rehearsal`` label added,
   new ``Blocks`` issuelink with unpublished inwardIssue → one test each.
 - **Rate-limit**: 100 reread calls inside the 60s TTL window collapse to
   one underlying ``_request`` (``test_reread_rate_limited_to_one_fetch``).
 - **Config flag**: ``OMNISIGHT_RUNNER_TOCTOU_REREAD_ENABLED=false``
   short-circuits to ``action="disabled"`` without hitting JIRA
   (``test_env_flag_disables_recheck``).
 - **Runner handler**: ``_handle_toctou_abort`` posts the audit comment +
   transitions back to To Do for revert-class actions, and skips the
   revert for the two "leave the ticket alone" actions.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch, live_state_check  # noqa: E402


# ── Test fixtures ─────────────────────────────────────────────────


class _StubClient:
    """Opaque first arg for patched dispatch helpers."""

    agent_class = "subscription-claude"
    bot_account_id = "acc-bot-001"
    bot_email = "bot@example.invalid"


_PICKUP_FIELDS: dict[str, Any] = {
    "assignee": {"accountId": "acc-bot-001"},
    "status": {"name": "進行中"},
    "labels": ["class:subscription-claude", "tier:S", "area:backend"],
    "issuelinks": [
        {
            "type": {"name": "Blocks"},
            "inwardIssue": {
                "key": "OP-100",
                "fields": {"status": {"name": "公開済み"}},
            },
        },
    ],
}


@pytest.fixture(autouse=True)
def _reset_toctou_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with a fresh TTL cache and reread-enabled."""
    monkeypatch.delenv(
        live_state_check._TOCTOU_REREAD_ENV, raising=False,
    )
    live_state_check._toctou_reset_cache()


def _install_request_stub(
    monkeypatch: pytest.MonkeyPatch,
    fields_sequence: list[dict[str, Any]] | dict[str, Any],
    *,
    counter: list[int] | None = None,
) -> list[int]:
    """Patch ``jira_dispatch._request`` to yield each fields payload in turn.

    Returns the call counter so tests can assert on fetch frequency. If
    ``fields_sequence`` is a single dict, every call returns that dict
    (so a re-fetch after TTL expiry sees the same state again).
    """
    counts = counter if counter is not None else [0]
    seq = fields_sequence if isinstance(fields_sequence, list) else None
    static = fields_sequence if not isinstance(fields_sequence, list) else None

    def fake_request(client: Any, method: str, path: str, body: Any = None,
                     idem_key: Any = None) -> dict[str, Any]:
        counts[0] += 1
        assert method == "GET"
        assert "/issue/" in path and "fields=" in path
        if static is not None:
            payload = static
        else:
            assert seq is not None
            idx = min(counts[0] - 1, len(seq) - 1)
            payload = seq[idx]
        return {"fields": payload}

    monkeypatch.setattr(jira_dispatch, "_request", fake_request)
    return counts


def _build_snapshot() -> live_state_check.TransitionBoundarySnapshot:
    return live_state_check.TransitionBoundarySnapshot(
        key="OP-1062",
        assignee_account_id="acc-bot-001",
        status_name="進行中",
        labels=frozenset({"class:subscription-claude", "tier:S", "area:backend"}),
        blocker_keys=frozenset({"OP-100"}),
    )


# ── AC #1 — survey-first + new function shape ─────────────────────


def test_capture_snapshot_freezes_assignee_status_labels_blockers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``capture_transition_boundary_snapshot`` reads live JIRA and freezes
    the four TOCTOU-watched field families into a hashable record."""
    _install_request_stub(monkeypatch, _PICKUP_FIELDS)
    snap = live_state_check.capture_transition_boundary_snapshot(
        _StubClient(), "OP-1062",
    )
    assert snap.key == "OP-1062"
    assert snap.assignee_account_id == "acc-bot-001"
    assert snap.status_name == "進行中"
    assert "tier:S" in snap.labels
    assert snap.blocker_keys == frozenset({"OP-100"})


# ── AC #2 — assignee.accountId divergence → abort + revert ────────


def test_assignee_swapped_returns_abort_assignee_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = _build_snapshot()
    mutated = dict(_PICKUP_FIELDS)
    mutated["assignee"] = {"accountId": "acc-human-999"}
    _install_request_stub(monkeypatch, mutated)
    result = live_state_check.recheck_transition_boundary_state(
        _StubClient(), "OP-1062", snap,
    )
    assert not result.ok
    assert result.action == "abort_assignee_changed"
    assert "acc-bot-001" in result.reason and "acc-human-999" in result.reason


def test_assignee_cleared_returns_abort_assignee_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unassigned ticket mid-flight is just as bad as a swap — the
    runner's claim is gone."""
    snap = _build_snapshot()
    mutated = dict(_PICKUP_FIELDS)
    mutated["assignee"] = None
    _install_request_stub(monkeypatch, mutated)
    result = live_state_check.recheck_transition_boundary_state(
        _StubClient(), "OP-1062", snap,
    )
    assert result.action == "abort_assignee_changed"


# ── AC #3 — status transition table ────────────────────────────────


def test_status_reverted_to_todo_returns_abort_reverted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = _build_snapshot()
    mutated = dict(_PICKUP_FIELDS)
    mutated["status"] = {"name": "To Do"}
    _install_request_stub(monkeypatch, mutated)
    result = live_state_check.recheck_transition_boundary_state(
        _StubClient(), "OP-1062", snap,
    )
    assert not result.ok
    assert result.action == "abort_reverted"


@pytest.mark.parametrize(
    "advanced_status",
    ["公開済み", "完了", "承認待ち", "差し戻し", "Under Review", "Published"],
)
def test_status_advanced_returns_abort_already_advanced(
    monkeypatch: pytest.MonkeyPatch, advanced_status: str,
) -> None:
    """All four JP-locale forward states + their EN equivalents map to
    ``abort_already_advanced`` so the runner does not push a duplicate."""
    snap = _build_snapshot()
    mutated = dict(_PICKUP_FIELDS)
    mutated["status"] = {"name": advanced_status}
    _install_request_stub(monkeypatch, mutated)
    result = live_state_check.recheck_transition_boundary_state(
        _StubClient(), "OP-1062", snap,
    )
    assert not result.ok
    assert result.action == "abort_already_advanced"


# ── AC #4 — operator-window / rehearsal label refusal ─────────────


@pytest.mark.parametrize(
    "added_label",
    ["class:operator-window-prod", "class:operator-rehearsal"],
)
def test_operator_window_label_added_mid_flight_returns_abort(
    monkeypatch: pytest.MonkeyPatch, added_label: str,
) -> None:
    """Per ADR-0033: operator-window / operator-rehearsal sigils added
    mid-flight are an explicit human handoff — the runner must yield."""
    snap = _build_snapshot()
    mutated = dict(_PICKUP_FIELDS)
    mutated["labels"] = list(_PICKUP_FIELDS["labels"]) + [added_label]
    _install_request_stub(monkeypatch, mutated)
    result = live_state_check.recheck_transition_boundary_state(
        _StubClient(), "OP-1062", snap,
    )
    assert not result.ok
    assert result.action == "abort_operator_window"
    assert added_label in result.reason


def test_operator_window_label_present_at_pickup_is_not_a_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check fires only on **newly added** operator-window labels.
    A label already in the pickup snapshot is part of normal state and
    must not retro-abort the pickup."""
    pickup_with_label = dict(_PICKUP_FIELDS)
    pickup_with_label["labels"] = list(_PICKUP_FIELDS["labels"]) + [
        "class:operator-window-prod",
    ]
    _install_request_stub(monkeypatch, pickup_with_label)
    snap = live_state_check.capture_transition_boundary_snapshot(
        _StubClient(), "OP-1062",
    )
    live_state_check._toctou_reset_cache()
    _install_request_stub(monkeypatch, pickup_with_label)
    result = live_state_check.recheck_transition_boundary_state(
        _StubClient(), "OP-1062", snap,
    )
    assert result.ok


# ── AC #5 — new Blocks dep with unpublished inwardIssue → abort ───


def test_new_unpublished_blocks_link_returns_abort_newly_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = _build_snapshot()
    mutated = dict(_PICKUP_FIELDS)
    mutated["issuelinks"] = list(_PICKUP_FIELDS["issuelinks"]) + [
        {
            "type": {"name": "Blocks"},
            "inwardIssue": {
                "key": "OP-555",
                "fields": {"status": {"name": "In Progress"}},
            },
        },
    ]
    _install_request_stub(monkeypatch, mutated)
    result = live_state_check.recheck_transition_boundary_state(
        _StubClient(), "OP-1062", snap,
    )
    assert not result.ok
    assert result.action == "abort_newly_blocked"
    assert "OP-555" in result.reason


def test_new_blocks_link_already_published_is_not_a_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newly added Blocks dep whose inwardIssue is already Published
    is non-blocking — the runner should not abort."""
    snap = _build_snapshot()
    mutated = dict(_PICKUP_FIELDS)
    mutated["issuelinks"] = list(_PICKUP_FIELDS["issuelinks"]) + [
        {
            "type": {"name": "Blocks"},
            "inwardIssue": {
                "key": "OP-556",
                "fields": {"status": {"name": "公開済み"}},
            },
        },
    ]
    _install_request_stub(monkeypatch, mutated)
    result = live_state_check.recheck_transition_boundary_state(
        _StubClient(), "OP-1062", snap,
    )
    assert result.ok


def test_non_blocks_issuelinks_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Relates`` and other link types must not trigger the Blocks check."""
    snap = _build_snapshot()
    mutated = dict(_PICKUP_FIELDS)
    mutated["issuelinks"] = [
        {
            "type": {"name": "Relates"},
            "inwardIssue": {
                "key": "OP-557",
                "fields": {"status": {"name": "In Progress"}},
            },
        },
    ]
    _install_request_stub(monkeypatch, mutated)
    result = live_state_check.recheck_transition_boundary_state(
        _StubClient(), "OP-1062", snap,
    )
    assert result.ok


# ── AC #6 — happy path: no mutation → ok ───────────────────────────


def test_no_mutation_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    snap = _build_snapshot()
    _install_request_stub(monkeypatch, _PICKUP_FIELDS)
    result = live_state_check.recheck_transition_boundary_state(
        _StubClient(), "OP-1062", snap,
    )
    assert result.ok
    assert result.action == "ok"


# ── AC #7 — rate-limit: one fetch per ticket per 60s ───────────────


def test_reread_rate_limited_to_one_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """100 reread calls inside the TTL window collapse to one JIRA fetch."""
    snap = _build_snapshot()
    counts = _install_request_stub(monkeypatch, _PICKUP_FIELDS)
    now = 1000.0
    for _ in range(100):
        live_state_check.recheck_transition_boundary_state(
            _StubClient(), "OP-1062", snap, now=now,
        )
        # Advance the clock 0.5s per call → 100 calls = 50s, inside the
        # 60s TTL window, so the cache must absorb them all.
        now += 0.5
    assert counts[0] == 1, (
        f"expected 1 _request call inside the TTL window, got {counts[0]}"
    )


def test_reread_refetches_after_ttl_elapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past 60s the cache must invalidate so a real state change is seen."""
    snap = _build_snapshot()
    counts = _install_request_stub(monkeypatch, _PICKUP_FIELDS)
    live_state_check.recheck_transition_boundary_state(
        _StubClient(), "OP-1062", snap, now=0.0,
    )
    live_state_check.recheck_transition_boundary_state(
        _StubClient(), "OP-1062", snap, now=61.0,
    )
    assert counts[0] == 2


def test_capture_snapshot_seeds_rate_limit_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capture and the first immediate recheck share one fetch — the
    pickup snapshot warms the TTL cache so the runner doesn't pay
    twice in the first 60s."""
    counts = _install_request_stub(monkeypatch, _PICKUP_FIELDS)
    snap = live_state_check.capture_transition_boundary_snapshot(
        _StubClient(), "OP-1062", now=10.0,
    )
    live_state_check.recheck_transition_boundary_state(
        _StubClient(), "OP-1062", snap, now=15.0,
    )
    assert counts[0] == 1


def test_rate_limit_is_per_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two different tickets within the TTL window each get their own
    fetch — the cache must not collide across keys."""
    snap_a = live_state_check.TransitionBoundarySnapshot(
        key="OP-A", assignee_account_id="acc-bot-001",
        status_name="進行中", labels=frozenset(), blocker_keys=frozenset(),
    )
    snap_b = live_state_check.TransitionBoundarySnapshot(
        key="OP-B", assignee_account_id="acc-bot-001",
        status_name="進行中", labels=frozenset(), blocker_keys=frozenset(),
    )
    counts = _install_request_stub(monkeypatch, {
        "assignee": {"accountId": "acc-bot-001"},
        "status": {"name": "進行中"},
        "labels": [],
        "issuelinks": [],
    })
    live_state_check.recheck_transition_boundary_state(
        _StubClient(), "OP-A", snap_a, now=0.0,
    )
    live_state_check.recheck_transition_boundary_state(
        _StubClient(), "OP-B", snap_b, now=0.0,
    )
    assert counts[0] == 2


# ── AC #8 — env-flag disable ───────────────────────────────────────


def test_env_flag_disables_recheck(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OMNISIGHT_RUNNER_TOCTOU_REREAD_ENABLED=false`` returns ok without
    hitting JIRA at all — emergency operator bypass."""
    monkeypatch.setenv("OMNISIGHT_RUNNER_TOCTOU_REREAD_ENABLED", "false")

    def _explode(*a: Any, **kw: Any) -> dict[str, Any]:
        raise AssertionError("_request must not be called when disabled")

    monkeypatch.setattr(jira_dispatch, "_request", _explode)
    snap = _build_snapshot()
    result = live_state_check.recheck_transition_boundary_state(
        _StubClient(), "OP-1062", snap,
    )
    assert result.ok
    assert result.action == "disabled"


@pytest.mark.parametrize("value", ["true", "1", "TRUE", "yes", ""])
def test_env_flag_default_or_truthy_values_keep_recheck_enabled(
    monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    """Default (unset) and any truthy spelling keep the recheck active."""
    if value:
        monkeypatch.setenv("OMNISIGHT_RUNNER_TOCTOU_REREAD_ENABLED", value)
    else:
        monkeypatch.delenv("OMNISIGHT_RUNNER_TOCTOU_REREAD_ENABLED", raising=False)
    _install_request_stub(monkeypatch, _PICKUP_FIELDS)
    snap = _build_snapshot()
    result = live_state_check.recheck_transition_boundary_state(
        _StubClient(), "OP-1062", snap,
    )
    # Either ok or one of the abort actions — the point is it ran, not the verdict.
    assert result.action != "disabled"


# ── AC #9 — runner-side handler routes each action correctly ──────


_RUNNER_PATH = REPO_ROOT / "auto-runner-jira.py"


def _load_jira_runner() -> Any:
    """Load auto-runner-jira.py via importlib (hyphen-named file)."""
    sys.modules.pop("jira_runner_under_test_toctou", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_under_test_toctou", _RUNNER_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "action,expect_revert",
    [
        ("abort_assignee_changed", True),
        ("abort_operator_window", True),
        ("abort_newly_blocked", True),
        ("abort_reverted", False),
        ("abort_already_advanced", False),
    ],
)
def test_handle_toctou_abort_routing(
    monkeypatch: pytest.MonkeyPatch, action: str, expect_revert: bool,
) -> None:
    """``_handle_toctou_abort`` posts a [runner-toctou:<phase>:<action>]
    audit comment for every action; reverts to To Do only for the three
    "runner steps aside" actions (assignee/window/newly_blocked)."""
    mod = _load_jira_runner()

    comments: list[tuple[str, str]] = []
    reverts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mod.jira_dispatch, "add_comment",
        lambda client, key, text, idem_key=None: comments.append((key, text)),
    )
    monkeypatch.setattr(
        mod.jira_dispatch, "transition_back_to_todo",
        lambda client, key, reason, **kw: reverts.append((key, reason)),
    )

    recheck = live_state_check.BoundaryRecheckResult(
        ok=False, action=action, reason=f"synthetic mutation for {action}",
    )
    rc = mod._handle_toctou_abort(
        _StubClient(), "OP-1062", recheck, phase="pre-submit",
    )
    assert rc == 1
    assert len(comments) == 1
    body = comments[0][1]
    assert "[runner-toctou:pre-submit:" + action + "]" in body
    if expect_revert:
        assert len(reverts) == 1
        assert reverts[0][0] == "OP-1062"
    else:
        assert reverts == []


# ── AC #10 — synthetic integration: rate-limit holds across both
#     phase boundaries when they hit inside the TTL window ─────────


def test_two_boundary_rechecks_within_ttl_share_one_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner calls recheck twice per pickup (pre-submit + pre-push).
    When both fire within the 60s window, only one JIRA fetch happens —
    pinning the AC#3 reread points spec: one fetch covers both boundaries."""
    counts = _install_request_stub(monkeypatch, _PICKUP_FIELDS)
    snap = live_state_check.capture_transition_boundary_snapshot(
        _StubClient(), "OP-1062", now=0.0,
    )
    r1 = live_state_check.recheck_transition_boundary_state(
        _StubClient(), "OP-1062", snap, now=5.0,
    )
    r2 = live_state_check.recheck_transition_boundary_state(
        _StubClient(), "OP-1062", snap, now=12.0,
    )
    assert r1.ok and r2.ok
    assert counts[0] == 1
