"""OP-956 — auto-runner ops-only ticket-type tests (6 AC cases).

Targets the new ``runner:no-commits-expected`` forward-transition path in
:mod:`auto-runner-jira` and the supporting helpers
:func:`backend.agents.jira_dispatch.forward_transition_ops_only` /
:func:`backend.agents.jira_dispatch.has_ops_only_label`.

Network-free: the runner is loaded via importlib (the file has a hyphen
in its name) and ``jira_dispatch`` is patched at the module-attribute
level on both the runner module and on its imported dispatch module.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from backend.agents import jira_dispatch, scheduler


_RUNNER_PATH = Path(__file__).resolve().parents[2] / "auto-runner-jira.py"


def _load_jira_runner() -> Any:
    """Load auto-runner-jira.py despite the hyphen in its filename."""
    sys.modules.pop("jira_runner_under_test_ops_only", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_under_test_ops_only", _RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _StubClient:
    """Minimal stand-in for jira_dispatch.DispatchClient — exists only so
    the patched dispatch helpers have an opaque first argument."""

    agent_class = "subscription-claude"
    bot_email = "rt3628+claude-bot@gmail.com"


def _snapshot(labels: tuple[str, ...]) -> scheduler.TicketSnapshot:
    return scheduler.TicketSnapshot(
        key="OP-9999",
        component="default",
        fix_version=None,
        created_at="2026-05-12T00:00:00+00:00",
        days_since_created=0.0,
        days_to_fix_version=None,
        downstream_blocked_count=0,
        mutex_labels=(),
        has_mutex_in_progress_sibling=False,
        labels=labels,
    )


# ── Tier 1: pure helpers on jira_dispatch (no runner module load) ─────


def test_has_ops_only_label_matches_exact_sigil() -> None:
    assert jira_dispatch.has_ops_only_label(
        ["runner:no-commits-expected", "tier:M"]
    )
    assert not jira_dispatch.has_ops_only_label(["tier:M"])
    # LabelInjectionAttack defence: substring match must not trigger.
    assert not jira_dispatch.has_ops_only_label(
        ["x-runner:no-commits-expected-y"]
    )
    assert not jira_dispatch.has_ops_only_label([])


def test_forward_transition_ops_only_walks_in_progress_to_published() -> None:
    """Happy path: ticket is In Progress when ops-only forward fires →
    POSTs transitions 3 → 4 → 7 and emits one audit comment per step."""
    calls: dict[str, list] = {"transitions": [], "comments": []}

    # Re-bind helpers used inside forward_transition_ops_only.
    from backend.agents import jira_dispatch as jd

    statuses = iter(["進行中", "Under Review", "Approved"])

    def fake_get_status(client, key):
        return next(statuses)

    def fake_request_idempotent(client, method, path, body, idem_key):
        if "transitions" in path:
            calls["transitions"].append(
                (path, body["transition"]["id"], idem_key)
            )
        return {}

    def fake_add_comment(client, key, text, idem_key=None):
        calls["comments"].append((key, text, idem_key))

    real_get = jd.get_issue_status
    real_req = jd._request_idempotent
    real_add = jd.add_comment
    try:
        jd.get_issue_status = fake_get_status  # type: ignore[assignment]
        jd._request_idempotent = fake_request_idempotent  # type: ignore[assignment]
        jd.add_comment = fake_add_comment  # type: ignore[assignment]

        jd.forward_transition_ops_only(_StubClient(), "OP-9999")

        assert [tid for (_path, tid, _idem) in calls["transitions"]] == ["3", "4", "7"]
        assert len(calls["comments"]) == 3
        for _key, text, _idem in calls["comments"]:
            assert "[runner-ops-only-transition]" in text
            assert "runner:no-commits-expected" in text
    finally:
        jd.get_issue_status = real_get  # type: ignore[assignment]
        jd._request_idempotent = real_req  # type: ignore[assignment]
        jd.add_comment = real_add  # type: ignore[assignment]


def test_forward_transition_ops_only_raises_permission_refused_on_403() -> None:
    """JIRA returns 403 on a transition POST → typed exception so the
    caller can fall back to operator notification (AC #5)."""
    from backend.agents import jira_dispatch as jd

    def fake_get_status(client, key):
        return "進行中"

    def fake_request_idempotent(client, method, path, body, idem_key):
        # Mimic the transport's HTTPError → RuntimeError wrap.
        raise RuntimeError(
            f"POST {path} → 403: claude-bot has no Approve permission"
        )

    real_get = jd.get_issue_status
    real_req = jd._request_idempotent
    try:
        jd.get_issue_status = fake_get_status  # type: ignore[assignment]
        jd._request_idempotent = fake_request_idempotent  # type: ignore[assignment]
        with pytest.raises(jd.WorkflowTransitionPermissionRefused) as ei:
            jd.forward_transition_ops_only(_StubClient(), "OP-9999")
        assert ei.value.key == "OP-9999"
        assert ei.value.transition_name == "to_under_review"
    finally:
        jd.get_issue_status = real_get  # type: ignore[assignment]
        jd._request_idempotent = real_req  # type: ignore[assignment]


def test_forward_transition_ops_only_noop_when_already_published() -> None:
    """Idempotent: re-running on an already-Published ticket does nothing."""
    from backend.agents import jira_dispatch as jd

    def fake_get_status(client, key):
        return "公開済み"

    called: list[tuple] = []

    def fake_request_idempotent(*args, **kwargs):
        called.append((args, kwargs))
        return {}

    real_get = jd.get_issue_status
    real_req = jd._request_idempotent
    try:
        jd.get_issue_status = fake_get_status  # type: ignore[assignment]
        jd._request_idempotent = fake_request_idempotent  # type: ignore[assignment]
        jd.forward_transition_ops_only(_StubClient(), "OP-9999")
        assert called == []
    finally:
        jd.get_issue_status = real_get  # type: ignore[assignment]
        jd._request_idempotent = real_req  # type: ignore[assignment]


# ── Tier 2: runner-side helpers (loaded via importlib) ────────────────


def test_ops_only_active_for_label_present_default_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #1 — sigil label recognised by default (env knob off)."""
    monkeypatch.delenv("OMNISIGHT_RUNNER_OPS_ONLY_DISABLED", raising=False)
    mod = _load_jira_runner()
    assert mod._ops_only_active_for(_snapshot(("runner:no-commits-expected",)))
    assert not mod._ops_only_active_for(_snapshot(()))


def test_ops_only_active_for_env_knob_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC test plan case 6 + AC §recovery — env knob disables the feature
    globally; the OP-827 always-revert path becomes the only path again."""
    monkeypatch.setenv("OMNISIGHT_RUNNER_OPS_ONLY_DISABLED", "1")
    mod = _load_jira_runner()
    assert not mod._ops_only_active_for(
        _snapshot(("runner:no-commits-expected",))
    )


# ── AC test plan: case 1 — happy path with label, 0 commits ───────────


def test_runner_helper_forwards_on_zero_commits_when_label_present(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """AC test plan case 1: ops-only label + CLI exit 0 + 0 commits →
    forward-transitioned to 公開済み, OP-827 revert path NOT invoked."""
    monkeypatch.delenv("OMNISIGHT_RUNNER_OPS_ONLY_DISABLED", raising=False)
    mod = _load_jira_runner()

    fwd_calls: list[tuple] = []
    revert_calls: list[tuple] = []
    comments: list[str] = []

    def fake_forward(client, key, idem_key=None):
        fwd_calls.append((key, idem_key))

    def fake_revert(*args, **kwargs):
        revert_calls.append((args, kwargs))

    def fake_add_comment(client, key, text, idem_key=None):
        comments.append(text)

    monkeypatch.setattr(
        mod.jira_dispatch, "forward_transition_ops_only", fake_forward
    )
    monkeypatch.setattr(
        mod.jira_dispatch, "transition_back_to_todo", fake_revert
    )
    monkeypatch.setattr(mod.jira_dispatch, "add_comment", fake_add_comment)

    rc = mod._handle_ops_only_forward_transition(_StubClient(), "OP-9999")
    assert rc == 0
    assert fwd_calls == [("OP-9999", None)]
    assert revert_calls == []
    # No unexpected-commits diagnostic when commit_count == 0.
    assert not any("unexpected-commits" in c for c in comments)


# ── AC test plan: case 2 — no label + 0 commits → OP-827 preserved ────


def test_runner_falls_through_to_revert_when_label_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #3 negative path: tickets WITHOUT the label keep OP-827's
    `[runner-no-commits-from-cli]` revert behaviour intact. We assert
    via the gating helper: `_ops_only_active_for` returns False, so the
    runner's main loop skips the ops-only branch entirely."""
    monkeypatch.delenv("OMNISIGHT_RUNNER_OPS_ONLY_DISABLED", raising=False)
    mod = _load_jira_runner()
    # area:backend present, ops-only label absent
    snap = _snapshot(("area:backend", "tier:M"))
    assert not mod._ops_only_active_for(snap)


# ── AC test plan: case 3 — exit non-zero with label → revert TODO ─────


def test_runner_does_not_invoke_ops_only_path_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #4: ticket WITH label + exit ≠ 0 → existing revert behaviour
    unchanged. The ops-only branch only fires inside the `rc == 0` arm
    of main(); when rc != 0 the runner takes the existing
    `transition_back_to_todo` path.

    Contract test: `_handle_ops_only_forward_transition` is the only
    helper that triggers forward transitions, and the main() rc-nonzero
    branch (lines around 1555 in auto-runner-jira.py) never reaches it.
    """
    monkeypatch.delenv("OMNISIGHT_RUNNER_OPS_ONLY_DISABLED", raising=False)
    mod = _load_jira_runner()
    src = _RUNNER_PATH.read_text()
    # The rc != 0 branch must revert via transition_back_to_todo and
    # MUST NOT call `_handle_ops_only_forward_transition`.
    nonzero_branch = src.split("elif rc == 99:", 1)[1].split(
        "_run_memory_writeback", 1
    )[0]
    assert "transition_back_to_todo" in nonzero_branch
    assert "_handle_ops_only_forward_transition" not in nonzero_branch


# ── AC test plan: case 4 — commits produced unexpectedly ─────────────


def test_runner_emits_unexpected_commits_warning_and_forwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC error catalog `OpsLabelButCommitsProduced` — log warning,
    don't lose work, still forward-transition past Under Review."""
    monkeypatch.delenv("OMNISIGHT_RUNNER_OPS_ONLY_DISABLED", raising=False)
    mod = _load_jira_runner()

    comments: list[str] = []
    fwd_calls: list[tuple] = []

    def fake_forward(client, key, idem_key=None):
        fwd_calls.append((key, idem_key))

    def fake_add_comment(client, key, text, idem_key=None):
        comments.append(text)

    monkeypatch.setattr(
        mod.jira_dispatch, "forward_transition_ops_only", fake_forward
    )
    monkeypatch.setattr(mod.jira_dispatch, "add_comment", fake_add_comment)

    rc = mod._handle_ops_only_forward_transition(
        _StubClient(), "OP-9999", unexpected_commits=3,
    )
    assert rc == 0
    assert fwd_calls == [("OP-9999", None)]
    assert any(
        "[runner-ops-only-unexpected-commits]" in c and "3 commit" in c
        for c in comments
    )


# ── AC test plan: case 5 — transition permission fail ─────────────────


def test_runner_falls_back_to_operator_notification_on_permission_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """AC error catalog `WorkflowTransitionPermissionRefused` — when
    claude-bot lacks the Approve transition, post operator-facing
    comment + return non-zero (ticket left in current workflow state)."""
    monkeypatch.delenv("OMNISIGHT_RUNNER_OPS_ONLY_DISABLED", raising=False)
    mod = _load_jira_runner()

    comments: list[str] = []

    def fake_forward(client, key, idem_key=None):
        raise mod.jira_dispatch.WorkflowTransitionPermissionRefused(
            key, "to_approved", "POST /issue/OP-9999/transitions → 403: forbidden"
        )

    def fake_add_comment(client, key, text, idem_key=None):
        comments.append(text)

    monkeypatch.setattr(
        mod.jira_dispatch, "forward_transition_ops_only", fake_forward
    )
    monkeypatch.setattr(mod.jira_dispatch, "add_comment", fake_add_comment)

    rc = mod._handle_ops_only_forward_transition(_StubClient(), "OP-9999")
    assert rc == 1
    assert any(
        "[runner-ops-only-permission-refused]" in c
        and "to_approved" in c
        for c in comments
    )
    err = capsys.readouterr().err
    assert "ops-only forward refused" in err


# ── AC test plan: case 6 — env knob disables the feature globally ─────


def test_env_knob_disables_ops_only_path_globally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per AC §recovery: `OMNISIGHT_RUNNER_OPS_ONLY_DISABLED=1` makes
    the runner ignore the label entirely (the OP-827 revert path becomes
    the only path again, restoring pre-OP-956 semantics for the operator
    while a label-misuse investigation is in flight)."""
    monkeypatch.setenv("OMNISIGHT_RUNNER_OPS_ONLY_DISABLED", "1")
    mod = _load_jira_runner()
    snap = _snapshot(("runner:no-commits-expected",))
    assert not mod._ops_only_active_for(snap)


# ── Prompt-builder: tells the CLI "expect zero commits" when label set ─


def test_prompt_block_present_when_label_present_via_runner_source() -> None:
    """AC #5 (prompt-update): the ``_build_prompt`` helper concatenates
    the `ops_only_block` segment when the label is on the ticket. We
    verify the wiring at the source level since the full prompt-builder
    requires JIRA + capability matrix mocks; the logical contract is
    that the ops-only string is rendered into the prompt when
    ``has_ops_only_label(labels)`` AND ``not OPS_ONLY_DISABLED``."""
    src = _RUNNER_PATH.read_text()
    # Block defined and conditionally non-empty
    assert "ops_only_block = \"\"" in src
    assert "has_ops_only_label(labels)" in src
    assert "ZERO commits" in src
    # And it gets included in the returned prompt string
    assert "{ops_only_block}" in src


# ══════════════════════════════════════════════════════════════════════
# OP-958 — AUDIT-11: snapshot labels not propagated to
# `_ops_only_active_for` (LabelsPropagationDrift). The fix re-reads the
# live JIRA label set at the no-commits decision point so a sigil added
# after pickup (the OP-925 R3 cascade, 2026-05-12) is still seen.
# ══════════════════════════════════════════════════════════════════════


def _issue_payload(key: str, labels: list[str]) -> dict:
    """Synthetic JIRA issue payload shaped like `fetch_pickable_tickets`."""
    return {
        "key": key,
        "fields": {
            "summary": f"{key} synthetic ops-only ticket",
            "labels": list(labels),
            "status": {"name": "To Do"},
            "issuetype": {"name": "Story"},
            "fixVersions": [{"name": "v0.5.0-rc1"}],
            "created": "2026-05-12T00:00:00.000+0000",
            "components": [{"name": "CRITICAL"}],
            "issuelinks": [],
            "parent": None,
        },
    }


class _LabelStubClient(_StubClient):
    """`_StubClient` that also serves a mutable live-label set so
    `jira_dispatch.fetch_ticket_labels` (which issues a real `_request`)
    can be exercised through a monkeypatched transport."""

    def __init__(self, live_labels: list[str]) -> None:
        self.live_labels = list(live_labels)


def test_fetch_ticket_labels_reads_live_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OP-958: the new `jira_dispatch.fetch_ticket_labels` helper issues
    `GET /issue/<key>?fields=labels` and returns the labels tuple."""
    from backend.agents import jira_dispatch as jd

    seen: list[tuple[str, str]] = []

    def fake_request(client, method, path, body=None, idem_key=None):
        seen.append((method, path))
        return {"fields": {"labels": ["runner:no-commits-expected", "tier:S"]}}

    monkeypatch.setattr(jd, "_request", fake_request)
    out = jd.fetch_ticket_labels(_StubClient(), "OP-925")
    assert out == ("runner:no-commits-expected", "tier:S")
    assert seen == [("GET", "/issue/OP-925?fields=labels")]


def test_fetch_ticket_labels_empty_payload_is_empty_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.agents import jira_dispatch as jd

    monkeypatch.setattr(jd, "_request", lambda *a, **k: {"fields": {}})
    assert jd.fetch_ticket_labels(_StubClient(), "OP-1") == ()
    monkeypatch.setattr(jd, "_request", lambda *a, **k: {})
    assert jd.fetch_ticket_labels(_StubClient(), "OP-1") == ()


# ── AC #1 + #4 — reproduce OP-925: snapshot built BEFORE the operator
#    added the sigil; live JIRA already carries it → forward, not revert.


def test_ops_only_active_for_sees_sigil_added_after_pickup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OP-958 regression — the OP-925 failure mode.

    Selection-time snapshot has NO sigil (operator hadn't tagged the
    ticket yet); by the time the CLI exits 0 with 0 commits the operator
    HAS added `runner:no-commits-expected`. Before the fix
    `_ops_only_active_for(snapshot)` read only the stale snapshot copy
    and returned False → `[runner-no-commits-from-cli]` revert. After
    the fix it unions the live label set and returns True → forward.
    """
    monkeypatch.delenv("OMNISIGHT_RUNNER_OPS_ONLY_DISABLED", raising=False)
    mod = _load_jira_runner()

    # Snapshot frozen at selection time — sigil absent.
    stale_snap = _snapshot(("class:subscription-claude", "tier:S", "area:devops"))

    # Live JIRA now carries the sigil (operator tagged it mid-flight).
    def fake_fetch_labels(client, key):
        assert key == "OP-9999"
        return ("class:subscription-claude", "tier:S", "area:devops",
                "runner:no-commits-expected")

    monkeypatch.setattr(
        mod.jira_dispatch, "fetch_ticket_labels", fake_fetch_labels
    )

    # Old behaviour (no client → snapshot-only): still False.
    assert not mod._ops_only_active_for(stale_snap)
    # Fixed behaviour (client supplied → live re-read): True.
    assert mod._ops_only_active_for(stale_snap, client=_StubClient())


# ── Test plan case 1 — happy path through the REAL snapshot pipeline ──


def test_ops_only_active_for_real_to_snapshot_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test plan §1: build the snapshot via `jira_dispatch.to_snapshot`
    (the same function the runner's pickup loop uses) from an issue that
    carries the sigil → `_ops_only_active_for` recognises it both with
    and without a live re-read (the snapshot copy already has it)."""
    monkeypatch.delenv("OMNISIGHT_RUNNER_OPS_ONLY_DISABLED", raising=False)
    mod = _load_jira_runner()
    snap = mod.jira_dispatch.to_snapshot(
        _issue_payload("OP-925", [
            "class:subscription-claude", "tier:S",
            "runner:no-commits-expected", "RELEASE-v0.5.0-rc1",
        ])
    )
    assert "runner:no-commits-expected" in snap.labels
    assert mod._ops_only_active_for(snap)
    # Live re-read agrees too (returns the same set).
    monkeypatch.setattr(
        mod.jira_dispatch, "fetch_ticket_labels",
        lambda c, k: snap.labels,
    )
    assert mod._ops_only_active_for(snap, client=_StubClient())


# ── Test plan case 2 — mixed labels (sigil + several area:* labels) ──


def test_ops_only_active_for_mixed_label_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test plan §2: the sigil is detected even when surrounded by the
    full real-world label salad (`class:*`, `tier:*`, several `area:*`,
    a `claim:*`, the RELEASE label)."""
    monkeypatch.delenv("OMNISIGHT_RUNNER_OPS_ONLY_DISABLED", raising=False)
    mod = _load_jira_runner()
    snap = mod.jira_dispatch.to_snapshot(
        _issue_payload("OP-928", [
            "class:subscription-claude", "tier:S", "area:backend",
            "area:tests", "area:devops", "claim:claude-1",
            "RELEASE-v0.5.0-rc1", "runner:no-commits-expected",
        ])
    )
    assert mod._ops_only_active_for(snap)
    assert mod._ops_only_active_for(snap, client=_StubClient())


# ── Test plan case 3 — atomic_claim race: label set changes between
#    claim and the no-commits check; live re-read still detects it. ────


def test_ops_only_active_for_atomic_claim_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test plan §3: snapshot taken pre-claim; the live label set has
    drifted (sigil added) by the time the CLI returns. The live re-read
    catches the new state. Symmetric to `test_ops_only_active_for_sees_
    sigil_added_after_pickup` but framed around the claim window."""
    monkeypatch.delenv("OMNISIGHT_RUNNER_OPS_ONLY_DISABLED", raising=False)
    mod = _load_jira_runner()
    pre_claim_snap = _snapshot(("class:subscription-claude", "tier:S"))

    live: list[str] = ["class:subscription-claude", "tier:S", "claim:claude-1"]
    monkeypatch.setattr(
        mod.jira_dispatch, "fetch_ticket_labels", lambda c, k: tuple(live)
    )
    assert not mod._ops_only_active_for(pre_claim_snap, client=_StubClient())
    # Operator tags the ticket while it's In Progress.
    live.append("runner:no-commits-expected")
    assert mod._ops_only_active_for(pre_claim_snap, client=_StubClient())


# ── Degradation — a live-fetch fault must never be *worse* than the
#    pre-OP-958 snapshot-only behaviour. ───────────────────────────────


def test_ops_only_active_for_degrades_to_snapshot_on_fetch_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.delenv("OMNISIGHT_RUNNER_OPS_ONLY_DISABLED", raising=False)
    mod = _load_jira_runner()

    def boom(client, key):
        raise RuntimeError("GET /issue/OP-9999?fields=labels → 503: upstream")

    monkeypatch.setattr(mod.jira_dispatch, "fetch_ticket_labels", boom)

    # Sigil in the snapshot copy → still True despite the fetch fault.
    assert mod._ops_only_active_for(
        _snapshot(("runner:no-commits-expected",)), client=_StubClient()
    )
    # No sigil anywhere → False (same as pre-OP-958).
    assert not mod._ops_only_active_for(
        _snapshot(("tier:S",)), client=_StubClient()
    )
    assert "ops-only live-label re-read failed" in capsys.readouterr().err


def test_ops_only_active_for_env_knob_short_circuits_live_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kill-switch wins even when the live label set carries the
    sigil — and we must NOT issue the extra GET when disabled."""
    monkeypatch.setenv("OMNISIGHT_RUNNER_OPS_ONLY_DISABLED", "1")
    mod = _load_jira_runner()
    called: list[str] = []
    monkeypatch.setattr(
        mod.jira_dispatch, "fetch_ticket_labels",
        lambda c, k: (called.append(k), ("runner:no-commits-expected",))[1],
    )
    assert not mod._ops_only_active_for(
        _snapshot(("runner:no-commits-expected",)), client=_StubClient()
    )
    assert called == []


# ── AC #5 — end-to-end: stale snapshot + live sigil → the forward walk
#    To Do/In Progress → Under Review → 承認済み → 公開済み fires. ──────


def test_ops_only_drift_then_forward_walk_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Glue the OP-958 fix to the OP-956 forward-walk: a ticket whose
    sigil was added after pickup is detected (`_ops_only_active_for`,
    client-supplied) and `_handle_ops_only_forward_transition` walks the
    workflow Submit-for-Review → Approve → Deploy (ids 3 → 4 → 7) with
    no `transition_back_to_todo` (revert) call anywhere."""
    monkeypatch.delenv("OMNISIGHT_RUNNER_OPS_ONLY_DISABLED", raising=False)
    mod = _load_jira_runner()

    stale_snap = _snapshot(("class:subscription-claude", "tier:S"))
    monkeypatch.setattr(
        mod.jira_dispatch, "fetch_ticket_labels",
        lambda c, k: ("class:subscription-claude", "tier:S",
                      "runner:no-commits-expected"),
    )
    assert mod._ops_only_active_for(stale_snap, client=_StubClient())

    transitions: list[str] = []
    reverts: list[tuple] = []
    statuses = iter(["進行中", "Under Review", "Approved"])

    from backend.agents import jira_dispatch as jd

    real_get = jd.get_issue_status
    real_req = jd._request_idempotent
    real_add = jd.add_comment
    try:
        jd.get_issue_status = lambda c, k: next(statuses)  # type: ignore[assignment]

        def fake_req(client, method, path, body, idem_key):
            if "transitions" in path:
                transitions.append(body["transition"]["id"])
            return {}

        jd._request_idempotent = fake_req  # type: ignore[assignment]
        jd.add_comment = lambda *a, **k: None  # type: ignore[assignment]
        monkeypatch.setattr(
            mod.jira_dispatch, "transition_back_to_todo",
            lambda *a, **k: reverts.append((a, k)),
        )

        rc = mod._handle_ops_only_forward_transition(_StubClient(), "OP-9999")
        assert rc == 0
        assert transitions == ["3", "4", "7"]
        assert reverts == []
    finally:
        jd.get_issue_status = real_get  # type: ignore[assignment]
        jd._request_idempotent = real_req  # type: ignore[assignment]
        jd.add_comment = real_add  # type: ignore[assignment]
