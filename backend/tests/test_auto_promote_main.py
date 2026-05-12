"""OP-766 / OP-960 (AUDIT-13) develop -> main auto-promotion tests.

OP-766 originally direct-pushed ``develop:main``; OP-960 (AUDIT-13)
proved Gerrit's ACL on ``refs/heads/main`` correctly refuses bot direct
push, so the mechanism now creates Gerrit review change(s) on
``refs/for/main`` (hashtag ``milestone:R3-fastforward``, topic
``develop-to-main``) and leaves the submit to an operator / future
merger-bot. These tests pin that contract.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from backend.agents import auto_promote_main as apm


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE = REPO_ROOT / "deploy" / "systemd" / "auto-promote-main.service"
ADR = REPO_ROOT / "docs" / "adr" / "ADR-0010-deployment-automation.md"
LESSON = (
    REPO_ROOT
    / "docs"
    / "sop"
    / "lessons"
    / "L-OP-766-release-promotion-must-stay-ff-only.md"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit_file(repo: Path, name: str, body: str) -> str:
    path = repo / name
    path.write_text(body, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"commit {name}")
    return _git(repo, "rev-parse", "HEAD")


def _repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    # The promote push uses ``git push -o hashtag=...``; the receiving end
    # must advertise push options or git aborts before contacting it. Real
    # Gerrit advertises them; a plain bare repo needs this flipped on.
    subprocess.run(
        ["git", "-C", str(remote), "config", "receive.advertisePushOptions", "true"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.name", "OP-766 Test")
    _git(repo, "config", "user.email", "op-766@example.test")
    _git(repo, "remote", "add", "gerrit", str(remote))
    _commit_file(repo, "base.txt", "base\n")
    _git(repo, "branch", "-M", "main")
    _git(repo, "push", "gerrit", "main:main")
    _git(repo, "checkout", "-b", "develop")
    _git(repo, "push", "gerrit", "develop:develop")
    return repo, remote


class _RecordingPusher:
    """Stub :data:`apm.PushForReviewFn` — records calls, returns a canned outcome."""

    def __init__(self, outcome: apm.PushOutcome) -> None:
        self._outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> apm.PushOutcome:
        self.calls.append(kwargs)
        return self._outcome


def _boom(**kwargs: Any) -> apm.PushOutcome:  # pragma: no cover - asserted not called
    raise AssertionError(f"push_for_review must not be called; got {kwargs!r}")


# ─────────────────────────────────────────────────────────────────────
# Happy path: clean FF => Gerrit review change(s), main untouched
# ─────────────────────────────────────────────────────────────────────


def test_milestone_ready_clean_develop_ahead_creates_review_change(tmp_path: Path) -> None:
    repo, remote = _repo_with_remote(tmp_path)
    main_before = _git(remote, "rev-parse", "main")
    develop_tip = _commit_file(repo, "feature.txt", "ready\n")
    pusher = _RecordingPusher(
        apm.PushOutcome(ok=True, change_urls=("https://gerrit.example/c/sora-bridge/+/4242",))
    )
    alerts: list[tuple[str, str, str]] = []
    events: list[tuple[str, dict]] = []
    audits: list[tuple[str, dict]] = []

    result = apm.promote_on_milestone_ready(
        {"event": "milestone_ready", "fixVersion": "v9.99.0"},
        repo=repo,
        remote="gerrit",
        notify=lambda channel, severity, detail: alerts.append((channel, severity, detail)),
        event_sink=lambda event, payload: events.append((event, payload)),
        audit_sink=lambda action, payload: audits.append((action, payload)),
        push_for_review=pusher,
    )

    assert result.status == "change_created"
    assert result.develop_tip == develop_tip
    assert result.created_changes == ("https://gerrit.example/c/sora-bridge/+/4242",)
    # main on the remote must NOT have advanced — it moves through review.
    assert _git(remote, "rev-parse", "main") == main_before

    # The push targeted the Gerrit magic ref with the AUDIT-13 hashtag/topic.
    assert len(pusher.calls) == 1
    call = pusher.calls[0]
    assert call["source_branch"] == "develop"
    assert call["target_branch"] == "main"
    assert call["hashtags"] == apm.PROMOTE_HASHTAGS
    assert "milestone:R3-fastforward" in apm.PROMOTE_HASHTAGS
    assert call["topic"] == apm.PROMOTE_TOPIC

    assert events == [
        (
            apm.EVENT_MAIN_PROMOTE_CHANGE_CREATED,
            {
                "fixVersion": "v9.99.0",
                "source_branch": "develop",
                "target_branch": "main",
                "develop_tip": develop_tip,
                "main_tip": main_before,
                "develop_only_count": 1,
                "main_only_count": 0,
                "review_ref": "develop:refs/for/main",
                "hashtags": list(apm.PROMOTE_HASHTAGS),
                "topic": apm.PROMOTE_TOPIC,
                "created_changes": ["https://gerrit.example/c/sora-bridge/+/4242"],
            },
        )
    ]
    assert audits[0][0] == apm.AUDIT_ACTION_MAIN_PROMOTE_CHANGE_CREATED
    assert audits[0][1]["after"]["status"] == "change_created"
    # Operator is told a change awaits submit (warning, not critical).
    assert len(alerts) == 1
    assert alerts[0][0] == "release-auto-promote"
    assert alerts[0][1] == "warning"
    assert "Gerrit Code Review" in alerts[0][2]


# ─────────────────────────────────────────────────────────────────────
# Gerrit refuses the refs/for push (e.g. bot key not yet trusted)
# ─────────────────────────────────────────────────────────────────────


def test_milestone_ready_push_refused_records_blocked(tmp_path: Path) -> None:
    repo, remote = _repo_with_remote(tmp_path)
    main_before = _git(remote, "rev-parse", "main")
    _commit_file(repo, "feature.txt", "ready\n")
    pusher = _RecordingPusher(
        apm.PushOutcome(ok=False, raw="remote: Permission denied: not Forge Author")
    )
    alerts: list[tuple[str, str, str]] = []
    events: list[tuple[str, dict]] = []
    audits: list[tuple[str, dict]] = []

    result = apm.promote_on_milestone_ready(
        {"event": "milestone_ready", "fixVersion": "v9.99.0"},
        repo=repo,
        remote="gerrit",
        notify=lambda channel, severity, detail: alerts.append((channel, severity, detail)),
        event_sink=lambda event, payload: events.append((event, payload)),
        audit_sink=lambda action, payload: audits.append((action, payload)),
        push_for_review=pusher,
    )

    assert result.status == "push_rejected"
    assert _git(remote, "rev-parse", "main") == main_before
    assert events == []
    assert audits[0][0] == apm.AUDIT_ACTION_MAIN_PROMOTE_BLOCKED
    assert audits[0][1]["after"]["status"] == "push_rejected"
    assert audits[0][1]["after"]["review_ref"] == "develop:refs/for/main"
    assert "Permission denied" in audits[0][1]["after"]["err"]
    assert len(alerts) == 1 and alerts[0][1] == "critical"


# ─────────────────────────────────────────────────────────────────────
# Non-FF shape: never merge, never push, alert the operator
# ─────────────────────────────────────────────────────────────────────


def test_milestone_ready_non_ff_aborts_and_alerts(tmp_path: Path) -> None:
    repo, remote = _repo_with_remote(tmp_path)
    develop_tip = _commit_file(repo, "feature.txt", "ready\n")
    _git(repo, "checkout", "main")
    main_tip = _commit_file(repo, "hotfix.txt", "hotfix\n")
    alerts: list[tuple[str, str, str]] = []
    events: list[tuple[str, dict]] = []
    audits: list[tuple[str, dict]] = []

    result = apm.promote_on_milestone_ready(
        {"event": "milestone_ready", "fixVersion": "v9.99.0"},
        repo=repo,
        remote="gerrit",
        notify=lambda channel, severity, detail: alerts.append((channel, severity, detail)),
        event_sink=lambda event, payload: events.append((event, payload)),
        audit_sink=lambda action, payload: audits.append((action, payload)),
        push_for_review=_boom,
    )

    assert result.status == "blocked"
    assert result.develop_tip == develop_tip
    assert result.main_tip == main_tip
    assert _git(remote, "rev-parse", "main") != develop_tip
    assert events == []
    assert len(alerts) == 1
    assert alerts[0][0] == "release-auto-promote"
    assert alerts[0][1] == "critical"
    assert "main-only commits" in alerts[0][2]
    assert "hotfix.txt" in alerts[0][2]
    assert audits[0][0] == apm.AUDIT_ACTION_MAIN_PROMOTE_BLOCKED
    assert audits[0][1]["after"]["status"] == "non_ff"


# ─────────────────────────────────────────────────────────────────────
# develop too far ahead of main => Gerrit batch limit => refuse + alert
# ─────────────────────────────────────────────────────────────────────


def test_milestone_ready_batch_too_large_refuses(tmp_path: Path) -> None:
    repo, remote = _repo_with_remote(tmp_path)
    main_before = _git(remote, "rev-parse", "main")
    _commit_file(repo, "a.txt", "a\n")
    _commit_file(repo, "b.txt", "b\n")
    _commit_file(repo, "c.txt", "c\n")
    alerts: list[tuple[str, str, str]] = []
    audits: list[tuple[str, dict]] = []

    result = apm.promote_on_milestone_ready(
        {"event": "milestone_ready", "fixVersion": "v9.99.0"},
        repo=repo,
        remote="gerrit",
        notify=lambda channel, severity, detail: alerts.append((channel, severity, detail)),
        event_sink=lambda _event, _payload: None,
        audit_sink=lambda action, payload: audits.append((action, payload)),
        push_for_review=_boom,
        max_promote_batch=2,
    )

    assert result.status == "blocked"
    assert _git(remote, "rev-parse", "main") == main_before
    assert audits[0][0] == apm.AUDIT_ACTION_MAIN_PROMOTE_BLOCKED
    assert audits[0][1]["after"]["status"] == "batch_too_large"
    assert len(alerts) == 1 and alerts[0][1] == "critical"
    assert "catch-up merge" in alerts[0][2]


# ─────────────────────────────────────────────────────────────────────
# main already at develop tip => noop, no push
# ─────────────────────────────────────────────────────────────────────


def test_noop_when_main_already_at_develop(tmp_path: Path) -> None:
    repo, _remote = _repo_with_remote(tmp_path)
    # develop == main (no extra commits since fixture).
    audits: list[tuple[str, dict]] = []

    result = apm.promote_on_milestone_ready(
        {"event": "milestone_ready", "fixVersion": "v9.99.0"},
        repo=repo,
        remote="gerrit",
        event_sink=lambda _event, _payload: None,
        audit_sink=lambda action, payload: audits.append((action, payload)),
        push_for_review=_boom,
    )

    assert result.status == "noop"
    assert audits[0][0] == apm.AUDIT_ACTION_MAIN_PROMOTE_BLOCKED
    assert audits[0][1]["after"]["status"] == "noop"


# ─────────────────────────────────────────────────────────────────────
# Non-authorizing records are ignored (no event, blocked, ...)
# ─────────────────────────────────────────────────────────────────────


def test_non_milestone_ready_event_is_ignored(tmp_path: Path) -> None:
    repo, _ = _repo_with_remote(tmp_path)

    result = apm.promote_on_milestone_ready(
        {"event": "milestone_blocked", "fixVersion": "v9.99.0"},
        repo=repo,
        audit_sink=lambda _action, _payload: None,
        push_for_review=_boom,
    )

    assert result.status == "ignored"


def test_missing_event_field_is_noop(tmp_path: Path) -> None:
    repo, remote = _repo_with_remote(tmp_path)
    main_before = _git(remote, "rev-parse", "main")
    _commit_file(repo, "feature.txt", "ready\n")
    audits: list[tuple[str, dict]] = []

    result = apm.promote_on_milestone_ready(
        {"fixVersion": "v9.99.0"},  # no "event" key at all
        repo=repo,
        audit_sink=lambda action, payload: audits.append((action, payload)),
        push_for_review=_boom,
    )

    assert result.status == "ignored"
    assert audits == []
    # main untouched, exactly as before AUDIT-18c.
    assert _git(remote, "rev-parse", "main") == main_before


def test_is_promotion_authorized_event_accepts_both_event_types() -> None:
    assert apm.is_promotion_authorized_event({"event": "milestone_ready"})
    assert apm.is_promotion_authorized_event({"event": "milestone_force_promoted"})
    assert not apm.is_promotion_authorized_event({"event": "milestone_blocked"})
    assert not apm.is_promotion_authorized_event({})
    assert apm.EVENT_MILESTONE_FORCE_PROMOTED == "milestone_force_promoted"


# ─────────────────────────────────────────────────────────────────────
# AUDIT-18c — release:force-promote operator override (ADR-0019)
# ─────────────────────────────────────────────────────────────────────


def test_force_promoted_event_pushes_with_warning_prefix(tmp_path: Path) -> None:
    repo, remote = _repo_with_remote(tmp_path)
    main_before = _git(remote, "rev-parse", "main")
    develop_tip = _commit_file(repo, "feature.txt", "ready\n")
    pusher = _RecordingPusher(
        apm.PushOutcome(ok=True, change_urls=("https://gerrit.example/c/sora-bridge/+/7777",))
    )
    alerts: list[tuple[str, str, str]] = []
    events: list[tuple[str, dict]] = []
    audits: list[tuple[str, dict]] = []

    result = apm.promote_on_milestone_ready(
        {
            "event": "milestone_force_promoted",
            "fixVersion": "v9.99.0",
            "reasons": ["staging-canary red", "smoke-suite timed out"],
        },
        repo=repo,
        remote="gerrit",
        notify=lambda channel, severity, detail: alerts.append((channel, severity, detail)),
        event_sink=lambda event, payload: events.append((event, payload)),
        audit_sink=lambda action, payload: audits.append((action, payload)),
        push_for_review=pusher,
    )

    assert result.status == "change_created"
    assert result.develop_tip == develop_tip
    # main on the remote must NOT have advanced — it still moves through review.
    assert _git(remote, "rev-parse", "main") == main_before

    # The push carried the OPERATOR FORCE-PROMOTE WARNING block as the
    # Gerrit change description, naming the bypassed gates (catches
    # ``WarningPrefixMissing``).
    assert len(pusher.calls) == 1
    desc = pusher.calls[0]["change_description"]
    assert desc.startswith("OPERATOR FORCE-PROMOTE WARNING: gates ")
    assert "staging-canary red" in desc
    assert "smoke-suite timed out" in desc

    # Audit row records ``force_promoted`` (not ``success``) and preserves
    # the original blocker reasons.
    assert audits[0][0] == apm.AUDIT_ACTION_MAIN_PROMOTE_CHANGE_CREATED
    after = audits[0][1]["after"]
    assert after["status"] == "change_created"
    assert after["outcome"] == apm.PROMOTE_OUTCOME_FORCE_PROMOTED == "force_promoted"
    assert "staging-canary red" in after["force_promote_reasons"]
    assert "smoke-suite timed out" in after["force_promote_reasons"]

    # Operator notification leads with the warning block.
    assert len(alerts) == 1
    assert alerts[0][2].startswith("OPERATOR FORCE-PROMOTE WARNING: gates ")
    # Event payload flags the override.
    assert events[0][0] == apm.EVENT_MAIN_PROMOTE_CHANGE_CREATED
    assert events[0][1]["force_promoted"] is True


def test_milestone_ready_push_carries_no_warning_prefix(tmp_path: Path) -> None:
    repo, _remote = _repo_with_remote(tmp_path)
    _commit_file(repo, "feature.txt", "ready\n")
    pusher = _RecordingPusher(apm.PushOutcome(ok=True, change_urls=()))
    audits: list[tuple[str, dict]] = []

    result = apm.promote_on_milestone_ready(
        {"event": "milestone_ready", "fixVersion": "v9.99.0"},
        repo=repo,
        remote="gerrit",
        notify=lambda *a: None,
        event_sink=lambda _e, _p: None,
        audit_sink=lambda action, payload: audits.append((action, payload)),
        push_for_review=pusher,
    )

    assert result.status == "change_created"
    # No force-promote warning rides a genuine ready promotion.
    assert pusher.calls[0]["change_description"] == ""
    after = audits[0][1]["after"]
    assert after["outcome"] == apm.PROMOTE_OUTCOME_SUCCESS == "success"
    assert "force_promote_reasons" not in after
    assert "force_promoted" not in audits[0][1]


def test_audit_outcome_distinguishes_force_promoted_from_success(tmp_path: Path) -> None:
    """``AuditOutcomeAmbiguous`` guard: the two paths must be queryable apart."""
    repo, _remote = _repo_with_remote(tmp_path)
    _commit_file(repo, "feature.txt", "ready\n")
    pusher = _RecordingPusher(apm.PushOutcome(ok=True, change_urls=()))
    ready_audits: list[tuple[str, dict]] = []
    forced_audits: list[tuple[str, dict]] = []

    apm.promote_on_milestone_ready(
        {"event": "milestone_ready", "fixVersion": "v9.99.0"},
        repo=repo,
        remote="gerrit",
        notify=lambda *a: None,
        event_sink=lambda _e, _p: None,
        audit_sink=lambda action, payload: ready_audits.append((action, payload)),
        push_for_review=pusher,
    )
    apm.promote_on_milestone_ready(
        {"event": "milestone_force_promoted", "fixVersion": "v9.99.0", "reasons": ["gate X"]},
        repo=repo,
        remote="gerrit",
        notify=lambda *a: None,
        event_sink=lambda _e, _p: None,
        audit_sink=lambda action, payload: forced_audits.append((action, payload)),
        push_for_review=pusher,
    )

    ready_outcome = ready_audits[0][1]["after"]["outcome"]
    forced_outcome = forced_audits[0][1]["after"]["outcome"]
    assert ready_outcome == "success"
    assert forced_outcome == "force_promoted"
    assert ready_outcome != forced_outcome


def test_force_promoted_event_with_no_reasons_still_warns(tmp_path: Path) -> None:
    repo, _remote = _repo_with_remote(tmp_path)
    _commit_file(repo, "feature.txt", "ready\n")
    pusher = _RecordingPusher(apm.PushOutcome(ok=True, change_urls=()))

    result = apm.promote_on_milestone_ready(
        {"event": "milestone_force_promoted", "fixVersion": "v9.99.0"},
        repo=repo,
        remote="gerrit",
        notify=lambda *a: None,
        event_sink=lambda _e, _p: None,
        audit_sink=lambda _a, _p: None,
        push_for_review=pusher,
    )

    assert result.status == "change_created"
    assert pusher.calls[0]["change_description"] == (
        "OPERATOR FORCE-PROMOTE WARNING: gates (unspecified)"
    )


def test_force_promoted_event_via_run_once(tmp_path: Path) -> None:
    repo, remote = _repo_with_remote(tmp_path)
    main_before = _git(remote, "rev-parse", "main")
    _commit_file(repo, "feature.txt", "ready\n")
    event_log = tmp_path / "systemd.log"
    cursor = tmp_path / "cursor"
    event_log.write_text(
        json.dumps(
            {
                "event": "milestone_force_promoted",
                "fixVersion": "v9.99.0",
                "reasons": ["gate Y"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    pusher = _RecordingPusher(apm.PushOutcome(ok=True, change_urls=()))
    audits: list[tuple[str, dict]] = []

    results = apm.run_once(
        event_log=event_log,
        cursor=cursor,
        repo=repo,
        remote="gerrit",
        source_branch="develop",
        target_branch="main",
        notify=lambda *a: None,
        event_sink=lambda _e, _p: None,
        audit_sink=lambda action, payload: audits.append((action, payload)),
        push_for_review=pusher,
    )

    assert [r.status for r in results] == ["change_created"]
    assert _git(remote, "rev-parse", "main") == main_before
    assert pusher.calls[0]["change_description"].startswith(
        "OPERATOR FORCE-PROMOTE WARNING: gates "
    )
    assert audits[0][1]["after"]["outcome"] == "force_promoted"


def test_default_push_for_review_forwards_change_description(tmp_path: Path) -> None:
    repo, remote = _repo_with_remote(tmp_path)
    develop_tip = _commit_file(repo, "feature.txt", "ready\n")
    main_before = _git(remote, "rev-parse", "main")

    outcome = apm._push_for_review(
        repo=repo,
        remote="gerrit",
        source_branch="develop",
        target_branch="main",
        change_description="OPERATOR FORCE-PROMOTE WARNING: gates gate Z",
    )

    # A plain bare repo just ignores the unknown ``message`` push option;
    # the push itself still lands under refs/for/main and never touches
    # refs/heads/main.
    assert outcome.ok is True
    assert _git(remote, "rev-parse", "refs/for/main") == develop_tip
    assert _git(remote, "rev-parse", "refs/heads/main") == main_before


# ─────────────────────────────────────────────────────────────────────
# run_once: drains the release log, threads the pusher through
# ─────────────────────────────────────────────────────────────────────


def test_run_once_reads_milestone_ready_from_release_log(tmp_path: Path) -> None:
    repo, remote = _repo_with_remote(tmp_path)
    main_before = _git(remote, "rev-parse", "main")
    _commit_file(repo, "feature.txt", "ready\n")
    event_log = tmp_path / "systemd.log"
    cursor = tmp_path / "cursor"
    event_log.write_text(
        "prefix that systemd may add "
        + json.dumps({"event": "milestone_ready", "fixVersion": "v9.99.0"})
        + "\n",
        encoding="utf-8",
    )
    pusher = _RecordingPusher(apm.PushOutcome(ok=True, change_urls=()))

    results = apm.run_once(
        event_log=event_log,
        cursor=cursor,
        repo=repo,
        remote="gerrit",
        source_branch="develop",
        target_branch="main",
        notify=lambda *a: None,
        event_sink=lambda _event, _payload: None,
        audit_sink=lambda _action, _payload: None,
        push_for_review=pusher,
    )

    assert [result.status for result in results] == ["change_created"]
    # main untouched; the change is awaiting submit.
    assert _git(remote, "rev-parse", "main") == main_before
    assert len(pusher.calls) == 1
    assert int(cursor.read_text(encoding="utf-8")) == event_log.stat().st_size


# ─────────────────────────────────────────────────────────────────────
# Default pusher: builds the refs/for refspec + push options; never raises
# ─────────────────────────────────────────────────────────────────────


def test_default_push_for_review_targets_refs_for(tmp_path: Path) -> None:
    repo, remote = _repo_with_remote(tmp_path)
    develop_tip = _commit_file(repo, "feature.txt", "ready\n")
    main_before = _git(remote, "rev-parse", "main")

    outcome = apm._push_for_review(
        repo=repo,
        remote="gerrit",
        source_branch="develop",
        target_branch="main",
    )

    assert outcome.ok is True
    # Pushed under refs/for/main (a plain bare repo just creates the ref);
    # refs/heads/main is untouched.
    assert _git(remote, "rev-parse", "refs/for/main") == develop_tip
    assert _git(remote, "rev-parse", "refs/heads/main") == main_before
    # No Gerrit-shaped change URL to parse from a plain bare repo.
    assert outcome.change_urls == ()


def test_default_push_for_review_returns_outcome_on_failure(tmp_path: Path) -> None:
    repo, _remote = _repo_with_remote(tmp_path)
    _commit_file(repo, "feature.txt", "ready\n")
    _git(repo, "remote", "remove", "gerrit")
    _git(repo, "remote", "add", "gerrit", str(tmp_path / "nope.git"))

    outcome = apm._push_for_review(
        repo=repo,
        remote="gerrit",
        source_branch="develop",
        target_branch="main",
    )

    assert outcome.ok is False
    assert outcome.raw  # carries git's stderr for the audit detail


# ─────────────────────────────────────────────────────────────────────
# systemd + docs contract (unchanged surfaces; pinned for regressions)
# ─────────────────────────────────────────────────────────────────────


def test_systemd_service_tails_release_checker_log() -> None:
    text = SERVICE.read_text(encoding="utf-8")

    assert "ExecStart=/usr/bin/python3 -m backend.agents.auto_promote_main" in text
    assert "--event-log /home/user/work/sora/logs/release-milestone/systemd.log" in text
    assert "--source-branch develop" in text
    assert "--target-branch main" in text
    assert "Restart=always" in text


def test_docs_record_auto_promote_contract() -> None:
    adr = ADR.read_text(encoding="utf-8")
    lesson = LESSON.read_text(encoding="utf-8")

    assert "backend.agents.auto_promote_main" in adr
    assert "main_promoted" in adr
    assert "git push gerrit develop:main" in lesson
    assert "ticket: OP-766" in lesson
