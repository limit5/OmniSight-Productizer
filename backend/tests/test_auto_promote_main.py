"""OP-983 (AUDIT-26d) develop -> main single-merge promotion tests."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from backend.agents import auto_promote_main as apm


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
    subprocess.run(
        ["git", "-C", str(remote), "config", "receive.advertisePushOptions", "true"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.name", "OP-983 Test")
    _git(repo, "config", "user.email", "op-983@example.test")
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


def test_build_promote_merge_commit_has_main_then_develop_parents(tmp_path: Path) -> None:
    repo, _remote = _repo_with_remote(tmp_path)
    main_tip = _git(repo, "rev-parse", "main")
    develop_tip = _commit_file(repo, "feature.txt", "ready\n")

    merge_sha = apm._build_promote_merge_commit(
        repo, develop_tip, main_tip, "v9.99.0", "OP-922"
    )

    assert _git(repo, "show", "-s", "--format=%P", merge_sha).split() == [
        main_tip,
        develop_tip,
    ]
    assert _git(repo, "show", "-s", "--format=%s", merge_sha) == (
        "[release-cut v9.99.0] Merge develop into main for OP-922"
    )
    assert "Develop tip:" in _git(repo, "show", "-s", "--format=%B", merge_sha)


def test_promote_creates_single_merge_change(tmp_path: Path) -> None:
    repo, remote = _repo_with_remote(tmp_path)
    main_before = _git(remote, "rev-parse", "main")
    develop_tip = _commit_file(repo, "feature.txt", "ready\n")
    pusher = _RecordingPusher(
        apm.PushOutcome(
            ok=True,
            change_urls=("https://gerrit.example/c/sora-bridge/+/4242",),
            change_number="4242",
        )
    )
    events: list[tuple[str, dict]] = []
    audits: list[tuple[str, dict]] = []

    result = apm.promote_on_milestone_ready(
        {"event": "milestone_ready", "fixVersion": "v9.99.0", "metaTicket": "OP-922"},
        repo=repo,
        remote="gerrit",
        notify=lambda *_a: None,
        event_sink=lambda event, payload: events.append((event, payload)),
        audit_sink=lambda action, payload: audits.append((action, payload)),
        push_for_review=pusher,
    )

    assert result.status == "change_created"
    assert result.develop_tip == develop_tip
    assert _git(remote, "rev-parse", "main") == main_before
    assert len(pusher.calls) == 1
    call = pusher.calls[0]
    assert call["target_ref"] == "refs/for/main"
    assert call["commit_sha"] == events[0][1]["merge_sha"]
    assert _git(repo, "show", "-s", "--format=%P", call["commit_sha"]).split() == [
        main_before,
        develop_tip,
    ]
    assert audits[0][0] == apm.AUDIT_ACTION_MAIN_PROMOTE_CHANGE_PUSHED


def test_promote_includes_release_version_in_topic(tmp_path: Path) -> None:
    repo, _remote = _repo_with_remote(tmp_path)
    _commit_file(repo, "feature.txt", "ready\n")
    pusher = _RecordingPusher(apm.PushOutcome(ok=True))

    apm.promote_on_milestone_ready(
        {"event": "milestone_ready", "fixVersion": "v1.2.3", "metaTicket": "OP-922"},
        repo=repo,
        notify=lambda *_a: None,
        event_sink=lambda *_a: None,
        audit_sink=lambda *_a: None,
        push_for_review=pusher,
    )

    assert apm.PROMOTE_TOPIC == "release-vX.Y.Z"
    assert pusher.calls[0]["topic"] == "release-v1.2.3"


def test_promote_includes_hashtag_for_submit_rule(tmp_path: Path) -> None:
    repo, _remote = _repo_with_remote(tmp_path)
    _commit_file(repo, "feature.txt", "ready\n")
    pusher = _RecordingPusher(apm.PushOutcome(ok=True))

    apm.promote_on_milestone_ready(
        {"event": "milestone_ready", "fixVersion": "v9.99.0", "metaTicket": "OP-922"},
        repo=repo,
        notify=lambda *_a: None,
        event_sink=lambda *_a: None,
        audit_sink=lambda *_a: None,
        push_for_review=pusher,
    )

    # OP-1533 canonicalised the release-cut hashtag to the bare
    # ``R3-fastforward`` spelling; the corrected release-cut SR accepts it
    # (hashtag:R3-fastforward OR hashtag:"milestone:R3-fastforward").
    assert apm.PROMOTE_HASHTAGS == ("auto-promote", "R3-fastforward")
    assert pusher.calls[0]["hashtags"] == apm.PROMOTE_HASHTAGS


def test_promote_writes_audit_row_via_lazy_init(monkeypatch, tmp_path: Path) -> None:
    repo, _remote = _repo_with_remote(tmp_path)
    _commit_file(repo, "feature.txt", "ready\n")
    pusher = _RecordingPusher(apm.PushOutcome(ok=True, change_number="4242"))
    captured: list[dict[str, Any]] = []

    async def fake_log(**kwargs: Any) -> int:
        captured.append(kwargs)
        return 1

    monkeypatch.setattr("backend.audit.log", fake_log)

    result = apm.promote_on_milestone_ready(
        {"event": "milestone_ready", "fixVersion": "v9.99.0", "metaTicket": "OP-922"},
        repo=repo,
        notify=lambda *_a: None,
        event_sink=lambda *_a: None,
        push_for_review=pusher,
    )

    assert result.status == "change_created"
    assert captured[0]["action"] == "release.main_promote_change_pushed"
    assert captured[0]["entity_kind"] == "release_branch"
    assert captured[0]["before"] == {"main_sha": result.main_tip}
    assert captured[0]["after"]["develop_sha"] == result.develop_tip
    assert captured[0]["after"]["merge_sha"]


def test_promote_refuses_when_main_not_ancestor_of_develop(tmp_path: Path) -> None:
    repo, remote = _repo_with_remote(tmp_path)
    develop_tip = _commit_file(repo, "feature.txt", "ready\n")
    _git(repo, "checkout", "main")
    main_tip = _commit_file(repo, "hotfix.txt", "hotfix\n")
    alerts: list[tuple[str, str, str]] = []
    audits: list[tuple[str, dict]] = []

    result = apm.promote_on_milestone_ready(
        {"event": "milestone_ready", "fixVersion": "v9.99.0", "metaTicket": "OP-922"},
        repo=repo,
        remote="gerrit",
        notify=lambda channel, severity, detail: alerts.append((channel, severity, detail)),
        event_sink=lambda *_a: None,
        audit_sink=lambda action, payload: audits.append((action, payload)),
        push_for_review=_boom,
    )

    assert result.status == "blocked"
    assert result.develop_tip == develop_tip
    assert result.main_tip == main_tip
    assert _git(remote, "rev-parse", "main") != develop_tip
    assert alerts[0][1] == "critical"
    assert audits[0][1]["after"]["status"] == "non_ff"


def test_promote_noop_when_already_advanced(tmp_path: Path) -> None:
    repo, _remote = _repo_with_remote(tmp_path)
    audits: list[tuple[str, dict]] = []

    result = apm.promote_on_milestone_ready(
        {"event": "milestone_ready", "fixVersion": "v9.99.0", "metaTicket": "OP-922"},
        repo=repo,
        audit_sink=lambda action, payload: audits.append((action, payload)),
        push_for_review=_boom,
    )

    assert result.status == "noop"
    assert audits[0][1]["after"]["status"] == "noop"


def test_promote_handles_61_commit_chain_via_merge_commit(tmp_path: Path) -> None:
    repo, _remote = _repo_with_remote(tmp_path)
    for index in range(61):
        _commit_file(repo, f"feature-{index:02d}.txt", f"{index}\n")
    pusher = _RecordingPusher(apm.PushOutcome(ok=True))

    result = apm.promote_on_milestone_ready(
        {"event": "milestone_ready", "fixVersion": "v9.99.0", "metaTicket": "OP-922"},
        repo=repo,
        notify=lambda *_a: None,
        event_sink=lambda *_a: None,
        audit_sink=lambda *_a: None,
        push_for_review=pusher,
        max_promote_batch=10,
    )

    assert result.status == "change_created"
    assert len(result.develop_only) == 61
    assert len(pusher.calls) == 1
    assert _git(repo, "cat-file", "-p", pusher.calls[0]["commit_sha"]).startswith("tree ")


def test_promote_end_to_end_dry_run(tmp_path: Path) -> None:
    repo, remote = _repo_with_remote(tmp_path)
    main_before = _git(remote, "rev-parse", "main")
    _commit_file(repo, "feature.txt", "ready\n")
    event_log = tmp_path / "systemd.log"
    cursor = tmp_path / "cursor"
    event_log.write_text(
        "prefix "
        + json.dumps(
            {"event": "milestone_ready", "fixVersion": "v9.99.0", "metaTicket": "OP-922"}
        )
        + "\n",
        encoding="utf-8",
    )
    pusher = _RecordingPusher(apm.PushOutcome(ok=True))

    results = apm.run_once(
        event_log=event_log,
        cursor=cursor,
        repo=repo,
        remote="gerrit",
        source_branch="develop",
        target_branch="main",
        notify=lambda *_a: None,
        event_sink=lambda *_a: None,
        audit_sink=lambda *_a: None,
        push_for_review=pusher,
    )

    assert [result.status for result in results] == ["change_created"]
    assert len(pusher.calls) == 1
    assert pusher.calls[0]["target_ref"] == "refs/for/main"
    assert _git(remote, "rev-parse", "main") == main_before
    assert int(cursor.read_text(encoding="utf-8")) == event_log.stat().st_size


def test_default_push_for_review_targets_refs_for_with_merge_commit(tmp_path: Path) -> None:
    repo, remote = _repo_with_remote(tmp_path)
    main_before = _git(remote, "rev-parse", "main")
    develop_tip = _commit_file(repo, "feature.txt", "ready\n")
    merge_sha = apm._build_promote_merge_commit(
        repo, develop_tip, main_before, "v9.99.0", "OP-922"
    )

    outcome = apm._push_for_review(
        repo=repo,
        remote="gerrit",
        commit_sha=merge_sha,
        target_ref="refs/for/main",
        topic="release-v9.99.0",
    )

    assert outcome.ok is True
    assert _git(remote, "rev-parse", "refs/for/main") == merge_sha
    assert _git(remote, "rev-parse", "refs/heads/main") == main_before


def test_default_push_for_review_returns_outcome_on_failure(tmp_path: Path) -> None:
    repo, _remote = _repo_with_remote(tmp_path)
    main_tip = _git(repo, "rev-parse", "main")
    develop_tip = _commit_file(repo, "feature.txt", "ready\n")
    merge_sha = apm._build_promote_merge_commit(
        repo, develop_tip, main_tip, "v9.99.0", "OP-922"
    )
    _git(repo, "remote", "remove", "gerrit")
    _git(repo, "remote", "add", "gerrit", str(tmp_path / "nope.git"))

    outcome = apm._push_for_review(
        repo=repo,
        remote="gerrit",
        commit_sha=merge_sha,
        target_ref="refs/for/main",
    )

    assert outcome.ok is False
    assert outcome.raw


def test_non_milestone_ready_event_is_ignored(tmp_path: Path) -> None:
    repo, _remote = _repo_with_remote(tmp_path)

    result = apm.promote_on_milestone_ready(
        {"event": "milestone_blocked", "fixVersion": "v9.99.0"},
        repo=repo,
        audit_sink=lambda *_a: None,
        push_for_review=_boom,
    )

    assert result.status == "ignored"
