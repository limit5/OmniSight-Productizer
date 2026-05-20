"""OP-1541 — shared-account TOCTOU false-abort under burst.

Regression suite for the OP-1533 incident: codex-1/2/3 (and the claude
instances) authenticate as ONE shared JIRA account, so the assignee cannot
distinguish instances of a class. The OP-1062 pre-submit recheck used the
shared-account assignee as the ownership signal — when a sibling instance
reverted a ticket (``clear_assignee`` → ``assignee=None``) the in-flight
winner read ``None`` as a lost claim and reverted too, cascading into the
stoploss circuit (4 reverts on OP-1533).

The fix (A+C) makes the **per-instance fencing-token claim** the ownership
signal:

 - Recheck (``live_state_check._evaluate_recheck``): assignee drift bot→None
   is benign IFF our live winning fencing-token claim still proves we own the
   run; a change to a different *non-null* account (human/operator) still
   aborts (preserves OP-1062 / OP-1069).
 - Destructive recovery (``auto-runner-jira._handle_toctou_abort``): only the
   current winning claim holder may clear assignee / revert; a non-owner logs
   ``[runner-claim-lost-not-reverting]`` and steps away without mutating.
 - Snapshot (``capture_transition_boundary_snapshot``): records the exact
   winning claim identity (instance_id + bare token).
 - Ownership core (``jira_dispatch.is_winning_claim_owner``): the lowest live
   fenced token across **all** instances (global fencing-token order) wins.

AC mapping:
 - Code AC      → test_capture_snapshot_records_claim_identity,
                  test_is_winning_claim_owner_* (global order / staleness)
 - Integration  → test_two_same_account_instances_owner_continues_nonowner_steps_away
 - Exercised    → test_burst_zero_false_abort_assignee_changed
 - OP-1062 keep → test_recheck_aborts_on_human_reassign_even_when_claim_winning,
                  test_recheck_aborts_when_snapshot_has_no_claim_identity
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch, live_state_check  # noqa: E402

_BOT_ACC = "acc-bot-shared-001"
_IN_PROGRESS = "進行中"


class _StubClient:
    agent_class = "subscription-codex"
    bot_account_id = _BOT_ACC
    bot_email = "bot@example.invalid"


@pytest.fixture(autouse=True)
def _reset_toctou_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(live_state_check._TOCTOU_REREAD_ENV, raising=False)
    live_state_check._toctou_reset_cache()


def _fresh_token(offset_us: int = 0) -> str:
    """A non-stale fencing token; ``offset_us`` orders sibling tokens."""
    return jira_dispatch._mint_claim_token(int(time.time() * 1_000_000) + offset_us)


def _fenced(instance_id: str, token: str) -> str:
    return jira_dispatch._fenced_claim_label(instance_id, token)


def _install_request_stub(
    monkeypatch: pytest.MonkeyPatch, fields: dict[str, Any],
) -> list[int]:
    counts = [0]

    def fake_request(client: Any, method: str, path: str, body: Any = None,
                     idem_key: Any = None) -> dict[str, Any]:
        counts[0] += 1
        assert method == "GET"
        return {"fields": fields}

    monkeypatch.setattr(jira_dispatch, "_request", fake_request)
    return counts


def _snapshot(
    *, claim_instance_id: str | None, claim_token: str | None,
    labels: frozenset[str] = frozenset(),
) -> live_state_check.TransitionBoundarySnapshot:
    return live_state_check.TransitionBoundarySnapshot(
        key="OP-1541",
        assignee_account_id=_BOT_ACC,
        status_name=_IN_PROGRESS,
        labels=labels,
        blocker_keys=frozenset(),
        claim_instance_id=claim_instance_id,
        claim_token=claim_token,
    )


# ── jira_dispatch.is_winning_claim_owner — ownership core ─────────────


def test_is_winning_claim_owner_sole_claim_owns() -> None:
    tok = _fresh_token()
    assert jira_dispatch.is_winning_claim_owner([_fenced("1", tok)], "1", tok)


def test_is_winning_claim_owner_global_lowest_token_wins() -> None:
    """Across two instances on one ticket, the earliest-minted (lowest) token
    is the single owner — the higher-token instance is NOT an owner even
    though it is the lowest *within its own* instance_id."""
    tok_lo = _fresh_token(0)
    tok_hi = _fresh_token(10_000)
    labels = [_fenced("1", tok_lo), _fenced("2", tok_hi)]
    assert jira_dispatch.is_winning_claim_owner(labels, "1", tok_lo) is True
    assert jira_dispatch.is_winning_claim_owner(labels, "2", tok_hi) is False


def test_is_winning_claim_owner_false_when_label_absent() -> None:
    tok = _fresh_token()
    # Our label was stripped (sibling/operator removed it); a foreign claim remains.
    assert jira_dispatch.is_winning_claim_owner(
        [_fenced("2", _fresh_token(10_000))], "1", tok,
    ) is False


def test_is_winning_claim_owner_false_when_token_stale() -> None:
    stale_us = int(time.time() * 1_000_000) - (jira_dispatch._STALE_CLAIM_MAX_AGE_S + 60) * 1_000_000
    stale_tok = jira_dispatch._mint_claim_token(stale_us)
    assert jira_dispatch.is_winning_claim_owner([_fenced("1", stale_tok)], "1", stale_tok) is False


def test_is_winning_claim_owner_false_on_empty_token() -> None:
    assert jira_dispatch.is_winning_claim_owner([_fenced("1", _fresh_token())], "1", None) is False


# ── Recheck — false-abort guard (Code AC / point 2) ───────────────────


def test_recheck_continues_on_assignee_none_when_claim_still_winning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner's claim is still the live winner; a sibling cleared the
    shared assignee to None — recheck must CONTINUE (action="ok")."""
    tok = _fresh_token()
    snap = _snapshot(claim_instance_id="1", claim_token=tok)
    _install_request_stub(monkeypatch, {
        "assignee": None,
        "status": {"name": _IN_PROGRESS},
        "labels": [_fenced("1", tok), "area:backend"],
        "issuelinks": [],
    })
    result = live_state_check.recheck_transition_boundary_state(_StubClient(), "OP-1541", snap)
    assert result.ok and result.action == "ok"


def test_recheck_aborts_on_assignee_none_when_claim_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assignee None AND our fenced claim is gone → genuine loss → abort."""
    tok = _fresh_token()
    snap = _snapshot(claim_instance_id="1", claim_token=tok)
    _install_request_stub(monkeypatch, {
        "assignee": None,
        "status": {"name": _IN_PROGRESS},
        "labels": ["area:backend"],  # our claim:1:tok no longer present
        "issuelinks": [],
    })
    result = live_state_check.recheck_transition_boundary_state(_StubClient(), "OP-1541", snap)
    assert not result.ok and result.action == "abort_assignee_changed"


def test_recheck_aborts_on_human_reassign_even_when_claim_winning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OP-1062 preserved: a change to a different non-null account (human /
    operator) aborts regardless of claim ownership (point 3)."""
    tok = _fresh_token()
    snap = _snapshot(claim_instance_id="1", claim_token=tok)
    _install_request_stub(monkeypatch, {
        "assignee": {"accountId": "acc-human-999"},
        "status": {"name": _IN_PROGRESS},
        "labels": [_fenced("1", tok)],  # our claim still present + winning
        "issuelinks": [],
    })
    result = live_state_check.recheck_transition_boundary_state(_StubClient(), "OP-1541", snap)
    assert not result.ok and result.action == "abort_assignee_changed"
    assert "acc-human-999" in result.reason


def test_recheck_aborts_when_snapshot_has_no_claim_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy/unfenced pickup (no claim identity captured) → strict OP-1062
    behaviour: any assignee divergence aborts."""
    snap = _snapshot(claim_instance_id=None, claim_token=None)
    _install_request_stub(monkeypatch, {
        "assignee": None,
        "status": {"name": _IN_PROGRESS},
        "labels": [],
        "issuelinks": [],
    })
    result = live_state_check.recheck_transition_boundary_state(_StubClient(), "OP-1541", snap)
    assert not result.ok and result.action == "abort_assignee_changed"


# ── Snapshot records the claim identity (Code AC / point 1) ───────────


def test_capture_snapshot_records_claim_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    tok = _fresh_token()
    _install_request_stub(monkeypatch, {
        "assignee": {"accountId": _BOT_ACC},
        "status": {"name": _IN_PROGRESS},
        "labels": [_fenced("1", tok)],
        "issuelinks": [],
    })
    snap = live_state_check.capture_transition_boundary_snapshot(
        _StubClient(), "OP-1541", claim_instance_id="1", claim_token=tok,
    )
    assert snap.claim_instance_id == "1"
    assert snap.claim_token == tok


# ── Runner handler — ownership-gated destructive recovery (point 4) ──


_RUNNER_PATH = REPO_ROOT / "auto-runner-jira.py"


def _load_runner() -> Any:
    sys.modules.pop("jira_runner_under_test_op1541", None)
    spec = importlib.util.spec_from_file_location("jira_runner_under_test_op1541", _RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _patch_handler_deps(
    mod: Any, monkeypatch: pytest.MonkeyPatch, *, live_labels: list[str],
) -> tuple[list, list]:
    """Patch the handler's JIRA deps; return (comments, reverts) sinks."""
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
    monkeypatch.setattr(
        mod.jira_dispatch, "release_ticket_claim",
        lambda client, key, instance, token=None, **kw: None,
    )
    monkeypatch.setattr(mod.jira_dispatch, "fetch_labels", lambda client, key: list(live_labels))
    # No human in the changelog — exercise the claim-ownership gate, not the
    # OP-1069 human-authority carve-out.
    monkeypatch.setattr(mod.jira_authority_check, "latest_authority_change", lambda client, key: None)
    return comments, reverts


def test_handle_toctou_abort_does_not_revert_when_claim_lost(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-owner (claim no longer the global winner) must NOT clear assignee
    or revert — it logs [runner-claim-lost-not-reverting] and steps away."""
    mod = _load_runner()
    tok_mine = _fresh_token(10_000)        # higher token → not the winner
    tok_winner = _fresh_token(0)           # an earlier sibling owns it
    live_labels = [_fenced("9", tok_winner)]  # our claim:{INSTANCE}:{tok_mine} absent
    comments, reverts = _patch_handler_deps(mod, monkeypatch, live_labels=live_labels)

    claim = jira_dispatch.ClaimResult(
        ok=True, lost_to=None, claim_token=f"{mod.INSTANCE_ID}:{tok_mine}",
    )
    recheck = live_state_check.BoundaryRecheckResult(
        False, "abort_assignee_changed", "assignee diverged: bot→None",
    )
    rc = mod._handle_toctou_abort(_StubClient(), "OP-1541", recheck, phase="pre-submit", claim=claim)

    assert rc == 1
    assert reverts == [], "non-owner must not revert the ticket"
    assert comments == [], "non-owner must not post the toctou-revert audit comment"
    assert "[runner-claim-lost-not-reverting]" in capsys.readouterr().err


def test_handle_toctou_abort_reverts_when_claim_still_winning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner (claim still the global winner) on a non-None foreign reassign:
    OP-1062 revert path preserved — the winning holder MAY revert."""
    mod = _load_runner()
    tok = _fresh_token()
    live_labels = [_fenced(mod.INSTANCE_ID, tok)]
    comments, reverts = _patch_handler_deps(mod, monkeypatch, live_labels=live_labels)

    claim = jira_dispatch.ClaimResult(ok=True, lost_to=None, claim_token=f"{mod.INSTANCE_ID}:{tok}")
    recheck = live_state_check.BoundaryRecheckResult(
        False, "abort_assignee_changed", "assignee diverged to a different account",
    )
    rc = mod._handle_toctou_abort(_StubClient(), "OP-1541", recheck, phase="pre-submit", claim=claim)

    assert rc == 1
    assert len(reverts) == 1 and reverts[0][0] == "OP-1541"
    assert len(comments) == 1 and "[runner-toctou:pre-submit:abort_assignee_changed]" in comments[0][1]


# ── Integration AC — two same-account instances race one ticket ──────


def test_two_same_account_instances_owner_continues_nonowner_steps_away(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Instance-1 (lower token) owns OP-1541; instance-2 (higher token) is the
    non-owner. Instance-2's revert cleared the shared assignee to None.

    Assert: (a) the owner's recheck CONTINUES despite assignee=None while its
    claim still wins, and (b) the non-owner's _handle_toctou_abort does NOT
    clear assignee / revert (it would clobber the owner's run)."""
    tok_owner = _fresh_token(0)
    tok_nonowner = _fresh_token(10_000)
    ticket_labels = [_fenced("1", tok_owner), _fenced("2", tok_nonowner), "area:backend"]

    # (a) Owner (instance 1) recheck — assignee drifted to None, claim wins.
    owner_snap = _snapshot(claim_instance_id="1", claim_token=tok_owner)
    _install_request_stub(monkeypatch, {
        "assignee": None,
        "status": {"name": _IN_PROGRESS},
        "labels": ticket_labels,
        "issuelinks": [],
    })
    owner_result = live_state_check.recheck_transition_boundary_state(
        _StubClient(), "OP-1541", owner_snap,
    )
    assert owner_result.ok, "owner must complete despite shared-account assignee drift"

    # (b) Non-owner (instance 2) destructive-recovery gate — must step away.
    mod = _load_runner()
    monkeypatch.setattr(mod, "INSTANCE_ID", "2")
    comments, reverts = _patch_handler_deps(mod, monkeypatch, live_labels=ticket_labels)
    claim2 = jira_dispatch.ClaimResult(ok=True, lost_to=None, claim_token=f"2:{tok_nonowner}")
    recheck = live_state_check.BoundaryRecheckResult(
        False, "abort_assignee_changed", "assignee diverged: bot→None",
    )
    rc = mod._handle_toctou_abort(_StubClient(), "OP-1541", recheck, phase="pre-submit", claim=claim2)
    assert rc == 1
    assert reverts == [], "non-owner must not revert the owner's ticket"
    assert "[runner-claim-lost-not-reverting]" in capsys.readouterr().err


# ── Exercised AC — burst produces zero false abort_assignee_changed ──


def test_burst_zero_false_abort_assignee_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A burst of N tickets across 3 instances: every ticket whose winning
    claim is still live yields recheck ok despite the shared assignee being
    cleared to None mid-burst — 0 false abort_assignee_changed positives."""
    n_tickets = 12
    instances = ["1", "2", "3"]
    false_aborts = 0

    for i in range(n_tickets):
        owner = instances[i % 3]
        tok = _fresh_token(i)  # distinct, fresh
        # The ticket carries the owner's winning claim + (sometimes) a stale
        # loser label from a sibling that already stepped away.
        labels = [_fenced(owner, tok)]
        if i % 2 == 0:
            labels.append(_fenced(instances[(i + 1) % 3], _fresh_token(i + 5_000)))
        snap = _snapshot(claim_instance_id=owner, claim_token=tok)
        live_state_check._toctou_reset_cache()
        _install_request_stub(monkeypatch, {
            "assignee": None,  # a sibling's revert cleared the shared assignee
            "status": {"name": _IN_PROGRESS},
            "labels": labels,
            "issuelinks": [],
        })
        result = live_state_check.recheck_transition_boundary_state(
            _StubClient(), f"OP-burst-{i}", snap,
        )
        if result.action == "abort_assignee_changed":
            false_aborts += 1

    assert false_aborts == 0, f"expected 0 shared-account false aborts, got {false_aborts}"
