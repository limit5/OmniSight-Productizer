"""OP-984 (AUDIT-26e) — end-to-end verification of the release-cut mechanism.

This drives the *behavioural* whole-chain test that AUDIT-26e calls for:

    develop tip → auto_promote_main → one Gerrit merge change → operator +2
    → merger-bot conditional +2 → submit (MERGE_ALWAYS) → main advances
    → gerrit-jira-bridge change-merged → release_audit row → release notification

The module under test is :mod:`backend.agents.auto_promote_main` (AUDIT-26d /
OP-983 — "one ``--no-ff`` merge commit pushed to ``refs/for/main``"). The
Gerrit side is a small in-process model of the AUDIT-26c / ADR-0020 / OP-982
configuration:

* ``refs/heads/main`` ``submitType = MERGE_ALWAYS`` — submitting the change
  advances ``main`` to the change's commit (the merge commit), never rebases.
* the ``release-cut-promote`` submit-requirement, quadruply-keyed on the
  CORRECTED (OP-1531 / OP-1534) signature deployed to refs/meta/config::

      applicableIf  = branch:main
                      AND (hashtag:"milestone:R3-fastforward" OR hashtag:R3-fastforward)
                      AND intopic:release-cut          # substring — topic: is exact-match
                      AND owner:sora
      submittableIf = (label:Code-Review=+2,group=merger-agent-bot)
                      AND (label:Code-Review=+2,group=non-ai-reviewer)

  (the human ``non-ai-reviewer`` +2 is the R8-equivalent hard gate at the
  Gerrit layer — the carve-out in ``Human-Plus-2`` only ever covers this one
  change shape, and ``release-cut-promote`` re-imposes the dual sign-off).
* a ``No-Veto`` requirement: a ``Code-Review -1`` / ``-2`` blocks submit.

AUDIT-26d design tension, resolved here: ``auto_promote_main`` is a *bot* that
pushes a ``release-v…`` topic, so under ``owner:sora`` + ``intopic:release-cut``
its change is NOT release-cut-promote-applicable — it falls to ``Human-Plus-2``
(``test_auto_promote_bot_change_falls_to_human_plus_two``). The merger-bot
conditional +2 path is exercised only for a genuine *sora-pushed* release cut
(``owner:sora`` + a ``…release-cut…`` topic), modelled by the ``owner=sora`` +
``topic=`` override on the in-process Gerrit.

The *static* shape of that config (the SR blocks, the quad-key negatives) is
already pinned by ``test_merger_bot_main_promote.py``; this file only exercises
what happens when the mechanism runs. The downstream consumers
(``gerrit-jira-bridge`` change-merged → JIRA + ``release_audit`` sink +
``release_notifications`` fan-out) live in the ``backend`` area, so they are
modelled here by thin in-process stand-ins — the test pins the *contract* the
release-cut event must hand them, not their internals.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from backend.agents import auto_promote_main as apm


# Release-cut artefact fixtures used across the tests. ``v0.5.0-rc2`` is the
# release AUDIT-26e is being validated against (OP-925 release chain).
RELEASE_VERSION = "v0.5.0-rc2"
META_TICKET = "OP-925"

# ADR-0020 / OP-982 + OP-1531 / OP-1534 ``release-cut-promote`` parameters,
# realigned to the corrected & DEPLOYED four-key signature:
#     branch:main
#     AND (hashtag:"milestone:R3-fastforward" OR hashtag:R3-fastforward)
#     AND intopic:release-cut          # substring — topic: is exact-match
#     AND owner:sora
# (the old topic:^release-v…$ regex + author:^…bot$ alternation are gone —
# the Gerrit lexer rejected the `[` in the regex, real cuts are owner:sora
# under the O10 lockdown, and the release-cut topic is matched by intopic:).
MERGER_GROUP = "merger-agent-bot"
HUMAN_GROUP = "non-ai-reviewer"
# owner:sora — the O10 lockdown forces sora to push a real cut (bots can't
# pushMerge to main).
RELEASE_CUT_OWNER = "sora"
# intopic:release-cut — the release-cut topic CONTAINS this substring.
RELEASE_CUT_TOPIC_SUBSTRING = "release-cut"
# A genuine sora-pushed release-cut topic (contains the substring above).
SORA_RELEASE_CUT_TOPIC = f"release-cut-{RELEASE_VERSION}"
# The two accepted R3-fastforward hashtag spellings (OP-1533 canonical bare
# form + the milestone: transition form).
R3_FASTFORWARD_HASHTAG = "R3-fastforward"
R3_FASTFORWARD_MILESTONE = "milestone:R3-fastforward"

_RELEASE_CUT_SUBJECT_RE = re.compile(
    r"^\[release-cut (?P<version>v[^\]]+)\] Merge develop into main for (?P<meta>\S+)"
)
_JIRA_BROWSE_BASE = "https://soraapp.atlassian.net/browse"

# OP-1536 / rcsr C3a + OP-1531 / OP-1534 — the static config the behavioural
# model above must stay in lock-step with.  RELEASE_CUT_OWNER /
# RELEASE_CUT_TOPIC_SUBSTRING / the R3-fastforward spellings are this file's
# *in-process* encoding of the four release-cut-promote keys; the assertions
# below cross-check them against the corrected ``applicableIf`` actually
# written in BOTH .gerrit config files (and against the C3a source of truth
# in test_merger_bot_main_promote.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROJECT_CONFIG = _REPO_ROOT / ".gerrit" / "project.config"
_PROJECT_CONFIG_EXAMPLE = _REPO_ROOT / ".gerrit" / "project.config.example"
# The corrected, DEPLOYED applicableIf (refs/meta/config 331c5665); identical
# to test_merger_bot_main_promote.C3A_APPLICABLE_IF["release-cut-promote"].
RELEASE_CUT_PROMOTE_APPLICABLE_IF = (
    'branch:main AND (hashtag:"milestone:R3-fastforward" OR '
    'hashtag:R3-fastforward) AND intopic:release-cut AND owner:sora'
)


def _applicable_if(config_text: str, sr_name: str) -> str | None:
    """Pull the ``applicableIf`` value of one ``[submit-requirement "<name>"]``
    block out of a project.config file (no live Gerrit needed)."""
    in_block = False
    for line in config_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[submit-requirement "):
            in_block = stripped == f'[submit-requirement "{sr_name}"]'
            continue
        if stripped.startswith("["):
            in_block = False
            continue
        if in_block and stripped.startswith("applicableIf"):
            return stripped.partition("=")[2].strip()
    return None


# ── git scaffolding ──────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit_file(repo: Path, name: str, body: str) -> str:
    (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"commit {name}")
    return _git(repo, "rev-parse", "HEAD")


def _repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A working repo with ``main`` (one commit) and ``develop`` (== main),
    plus a bare ``gerrit`` remote carrying both branches."""
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.name", "OP-984 Test")
    _git(repo, "config", "user.email", "op-984@example.test")
    _git(repo, "remote", "add", "gerrit", str(remote))
    _commit_file(repo, "base.txt", "base\n")
    _git(repo, "branch", "-M", "main")
    _git(repo, "push", "gerrit", "main:main")
    _git(repo, "checkout", "-b", "develop")
    _git(repo, "push", "gerrit", "develop:develop")
    return repo, remote


# ── mock Gerrit (MERGE_ALWAYS + release-cut-promote submit-requirement) ───────


@dataclass
class GerritChange:
    number: int
    url: str
    commit_sha: str
    target_branch: str
    topic: str
    hashtags: tuple[str, ...]
    # OP-1534: the release-cut SR keys on owner:sora (the change owner /
    # pushing account), not the old author-identity regex.
    owner: str
    description: str = ""
    labels: dict[tuple[str, str], int] = field(default_factory=dict)
    status: str = "NEW"  # NEW | MERGED


class MockGerrit:
    """In-process Gerrit modelling the AUDIT-26c config for ``refs/heads/main``.

    Wired into :func:`apm.promote_on_milestone_ready` as its ``push_for_review``
    dependency so the real module produces real changes here.
    """

    def __init__(self, remote_repo: Path, *, owner: str = "auto-promote-bot") -> None:
        self._remote = remote_repo
        # The account that pushes the change == its Gerrit owner. The bot
        # default (auto-promote-bot) does NOT match owner:sora; a genuine cut
        # is pushed with owner="sora".
        self.owner = owner
        self.changes: dict[int, GerritChange] = {}
        self.merged_events: list[dict[str, Any]] = []
        self._next_number = 4200

    # called by auto_promote_main with keyword args; signature mirrors
    # apm._push_for_review (commit_sha, target_ref, hashtags, topic, ...).
    def push_for_review(
        self,
        *,
        repo: Path,
        remote: str,
        commit_sha: str,
        target_ref: str = "refs/for/main",
        hashtags: tuple[str, ...] = (),
        topic: str = "",
        change_description: str = "",
        timeout: int = 120,
    ) -> apm.PushOutcome:
        assert target_ref.startswith("refs/for/"), target_ref
        branch = target_ref[len("refs/for/") :]
        # transfer the (locally-built) merge-commit object into the bare remote
        # so a later submit can advance refs/heads/<branch> onto it.
        subprocess.run(
            ["git", "push", remote, f"{commit_sha}:{target_ref}"],
            cwd=repo, check=True, capture_output=True, text=True, timeout=timeout,
        )
        number = self._next_number
        self._next_number += 1
        url = f"https://gerrit.sora.services/c/sora-bridge/+/{number}"
        self.changes[number] = GerritChange(
            number=number, url=url, commit_sha=commit_sha, target_branch=branch,
            topic=topic, hashtags=tuple(hashtags), owner=self.owner,
            description=change_description,
        )
        return apm.PushOutcome(
            ok=True, change_urls=(url,), raw=f"remote: {url} {branch} [NEW]",
            change_number=str(number),
        )

    # ── reviewing ──
    def review(self, number: int, *, value: int, group: str, label: str = "Code-Review") -> None:
        self.changes[number].labels[(label, group)] = value

    def merger_bot_autovote(self, number: int) -> bool:
        """Model the AUDIT-26c merger-bot poller: cast a scoped ``Code-Review:+2``
        as ``merger-agent-bot`` *iff* the change matches the release-cut-promote
        ``applicableIf`` keys. Returns whether the vote fired."""
        change = self.changes[number]
        if not self._sr_applicable(change):
            return False
        self.review(number, value=2, group=MERGER_GROUP)
        return True

    # ── release-cut-promote submit-requirement evaluation ──
    @staticmethod
    def _sr_applicable(change: GerritChange) -> bool:
        # OP-1531 / OP-1534 corrected four-key signature:
        #   branch:main
        #   AND (hashtag:"milestone:R3-fastforward" OR hashtag:R3-fastforward)
        #   AND intopic:release-cut          # SUBSTRING match
        #   AND owner:sora
        return (
            change.target_branch == "main"
            and RELEASE_CUT_TOPIC_SUBSTRING in (change.topic or "")
            and (
                R3_FASTFORWARD_HASHTAG in change.hashtags
                or R3_FASTFORWARD_MILESTONE in change.hashtags
            )
            and change.owner == RELEASE_CUT_OWNER
        )

    def _has_blocking_veto(self, change: GerritChange) -> bool:
        return any(
            label == "Code-Review" and value < 0 for (label, _g), value in change.labels.items()
        )

    def submittable(self, number: int) -> bool:
        change = self.changes[number]
        if self._has_blocking_veto(change):  # No-Veto requirement
            return False
        if not self._sr_applicable(change):
            # release-cut-promote not applicable → this isn't a release-cut
            # change and the normal Human-Plus-2 hard gate (modelled minimally
            # as "needs a non-ai-reviewer +2") applies instead.
            return change.labels.get(("Code-Review", HUMAN_GROUP)) == 2
        # release-cut-promote: (merger +2) AND (non-ai-reviewer +2)
        return (
            change.labels.get(("Code-Review", MERGER_GROUP)) == 2
            and change.labels.get(("Code-Review", HUMAN_GROUP)) == 2
        )

    def submit(self, number: int) -> dict[str, Any]:
        change = self.changes[number]
        if change.status == "MERGED":
            raise RuntimeError(f"change {number} already merged")
        if not self.submittable(number):
            raise PermissionError(
                f"change {number} is not submittable: the release-cut-promote "
                f"submit-requirement is unsatisfied (labels={change.labels})"
            )
        # MERGE_ALWAYS on refs/heads/main: advance the branch to the change commit.
        subprocess.run(
            ["git", "-C", str(self._remote), "update-ref",
             f"refs/heads/{change.target_branch}", change.commit_sha],
            check=True, capture_output=True, text=True,
        )
        change.status = "MERGED"
        subject = _git(self._remote, "show", "-s", "--format=%s", change.commit_sha)
        event = {
            "type": "change-merged",
            "change": {
                "number": change.number,
                "url": change.url,
                "branch": change.target_branch,
                "topic": change.topic,
                "subject": subject,
                "owner": {"username": change.owner},
            },
            "newRev": change.commit_sha,
            "refUpdate": {
                "refName": f"refs/heads/{change.target_branch}",
                "newRev": change.commit_sha,
            },
        }
        self.merged_events.append(event)
        return event


# ── stand-in for the gerrit-jira-bridge change-merged path (release-cut only) ─


@dataclass
class ReleaseAuditRow:
    outcome: str
    fix_version: str
    develop_sha: str
    main_sha: str
    change_number: str
    detail: dict[str, Any]


@dataclass
class ReleaseNotification:
    release_version: str
    release_meta_url: str
    change_number: str
    meta_ticket: str


class ReleaseCutBridge:
    """Models the slice of ``backend.agents.gerrit_jira_bridge`` that reacts to
    a ``change-merged`` event for a release-cut merge on ``refs/heads/main``:
    emit a structured log line, write a ``release_audit`` row, fan out a
    release notification. (The real bridge skips non-``develop`` branches for
    JIRA *Published* transitions; the release-cut path is the H-sprint
    extension verified here at the contract level.)"""

    def __init__(self, remote_repo: Path) -> None:
        self._remote = remote_repo
        self.log_lines: list[dict[str, Any]] = []
        self.audit_rows: list[ReleaseAuditRow] = []
        self.notifications: list[ReleaseNotification] = []

    def process_change_merged(self, event: dict[str, Any]) -> ReleaseAuditRow | None:
        change = event["change"]
        if change["branch"] != "main":
            self.log_lines.append({"event": "change_merged_non_main_skip", "branch": change["branch"]})
            return None
        match = _RELEASE_CUT_SUBJECT_RE.match(change["subject"] or "")
        if match is None:
            self.log_lines.append({"event": "change_merged_not_release_cut", "subject": change["subject"]})
            return None
        version = match.group("version")
        meta_ticket = match.group("meta")
        change_number = str(change["number"])
        main_sha = event["newRev"]
        # the merge commit's second parent is the develop tip that was promoted
        develop_sha = _git(self._remote, "rev-parse", f"{main_sha}^2")
        self.log_lines.append({
            "event": "release_cut_main_advanced",
            "release_version": version,
            "meta_ticket": meta_ticket,
            "change": change_number,
            "main_sha": main_sha,
            "develop_sha": develop_sha,
        })
        row = ReleaseAuditRow(
            outcome="success",
            fix_version=version,
            develop_sha=develop_sha,
            main_sha=main_sha,
            change_number=change_number,
            detail={"meta_ticket": meta_ticket, "topic": change.get("topic") or ""},
        )
        self.audit_rows.append(row)
        self.notifications.append(ReleaseNotification(
            release_version=version,
            release_meta_url=f"{_JIRA_BROWSE_BASE}/{meta_ticket}",
            change_number=change_number,
            meta_ticket=meta_ticket,
        ))
        return row


# ── helpers ──────────────────────────────────────────────────────────────────


def _milestone_ready_event() -> dict[str, Any]:
    return {"event": "milestone_ready", "fixVersion": RELEASE_VERSION, "metaTicket": META_TICKET}


def _run_auto_promote(
    repo: Path, gerrit: MockGerrit, *, topic: str | None = None
) -> tuple[apm.PromotionResult, list, list, list]:
    """Drive the real ``auto_promote_main`` module against the in-process
    Gerrit. ``topic`` overrides the module's default ``release-v…`` topic so a
    genuine sora-pushed release cut (``…release-cut…`` topic + owner:sora) can
    be modelled through the same code path."""
    notifies: list[tuple[str, str, str]] = []
    events: list[tuple[str, dict]] = []
    audits: list[tuple[str, dict]] = []
    result = apm.promote_on_milestone_ready(
        _milestone_ready_event(),
        repo=repo,
        remote="gerrit",
        notify=lambda channel, severity, detail: notifies.append((channel, severity, detail)),
        event_sink=lambda event, payload: events.append((event, payload)),
        audit_sink=lambda action, payload: audits.append((action, payload)),
        push_for_review=gerrit.push_for_review,
        topic=topic,
    )
    return result, notifies, events, audits


# ── tests ────────────────────────────────────────────────────────────────────


def test_release_cut_change_artifact_shape(tmp_path: Path) -> None:
    """AC #2 — one Gerrit change on ``refs/for/main`` carrying the canonical
    ``R3-fastforward`` hashtag + topic ``release-v0.5.0-rc2``, ``main``
    untouched until submit. The bot's daily-promote change is NOT
    release-cut-promote-applicable (owner != sora, topic lacks the
    ``release-cut`` substring) — AUDIT-26d."""
    repo, remote = _repo_with_remote(tmp_path)
    main_before = _git(remote, "rev-parse", "main")
    develop_tip = _commit_file(repo, "feature.txt", "ship it\n")

    gerrit = MockGerrit(remote)
    result, notifies, events, audits = _run_auto_promote(repo, gerrit)

    assert result.status == "change_created"
    assert len(gerrit.changes) == 1
    (number,) = gerrit.changes
    change = gerrit.changes[number]
    assert change.target_branch == "main"
    assert change.owner == "auto-promote-bot"
    assert apm.PROMOTE_HASHTAGS == ("auto-promote", "R3-fastforward")
    assert R3_FASTFORWARD_HASHTAG in change.hashtags
    assert change.topic == f"release-{RELEASE_VERSION}" == "release-v0.5.0-rc2"
    # The bot's release-v… topic does NOT contain the "release-cut" substring
    # intopic: keys on, and the owner is the bot not sora → NOT applicable.
    assert RELEASE_CUT_TOPIC_SUBSTRING not in change.topic
    assert MockGerrit._sr_applicable(change) is False
    # the change commit is a no-ff merge with parents [main, develop]
    assert _git(remote, "show", "-s", "--format=%P", change.commit_sha).split() == [
        main_before, develop_tip,
    ]
    assert change.commit_sha == events[0][1]["merge_sha"]
    # main has not advanced — submitting is an operator / merger-bot action
    assert _git(remote, "rev-parse", "main") == main_before
    assert audits[0][0] == apm.AUDIT_ACTION_MAIN_PROMOTE_CHANGE_PUSHED
    assert notifies[0][1] == "warning"


def test_auto_promote_bot_change_falls_to_human_plus_two(tmp_path: Path) -> None:
    """AUDIT-26d resolution — the ``auto_promote_main`` *bot* push (owner !=
    sora, ``release-v…`` topic) does NOT match release-cut-promote, so the
    merger-bot declines to auto-vote and the change is governed by
    ``Human-Plus-2``: a single ``non-ai-reviewer`` +2 submits it. (The merger
    conditional +2 path is reserved for a genuine sora-pushed cut — see
    ``test_release_cut_end_to_end_success``.)"""
    repo, remote = _repo_with_remote(tmp_path)
    main_before = _git(remote, "rev-parse", "main")
    _commit_file(repo, "feature.txt", "ship it\n")

    gerrit = MockGerrit(remote)  # default owner = auto-promote-bot
    result, *_ = _run_auto_promote(repo, gerrit)
    assert result.status == "change_created"
    (number,) = gerrit.changes
    change = gerrit.changes[number]

    assert change.owner == "auto-promote-bot"
    assert RELEASE_CUT_TOPIC_SUBSTRING not in change.topic
    assert MockGerrit._sr_applicable(change) is False
    # merger-bot declines (SR not applicable to a bot push).
    assert gerrit.merger_bot_autovote(number) is False
    # falls to Human-Plus-2: a single human +2 is enough to submit.
    assert gerrit.submittable(number) is False
    gerrit.review(number, value=2, group=HUMAN_GROUP)
    assert gerrit.submittable(number) is True
    gerrit.submit(number)
    assert _git(remote, "rev-parse", "main") == change.commit_sha != main_before


def test_release_cut_end_to_end_success(tmp_path: Path) -> None:
    """AC #1 / #5 — a genuine sora-pushed cut (owner:sora + ``…release-cut…``
    topic): develop SHA → auto_promote_main → merge change → operator +2 →
    merger-bot conditional +2 → submit → main advances → bridge change-merged
    → release_audit row → release notification."""
    repo, remote = _repo_with_remote(tmp_path)
    main_before = _git(remote, "rev-parse", "main")
    develop_tip = _commit_file(repo, "feature.txt", "ship it\n")

    gerrit = MockGerrit(remote, owner=RELEASE_CUT_OWNER)  # sora pushes the cut
    bridge = ReleaseCutBridge(remote)

    # (1) auto_promote_main produces exactly one merge change, owner:sora,
    #     topic containing "release-cut" → release-cut-promote applies.
    result, _notifies, _events, audits = _run_auto_promote(
        repo, gerrit, topic=SORA_RELEASE_CUT_TOPIC
    )
    assert result.status == "change_created"
    (number,) = gerrit.changes
    change = gerrit.changes[number]
    assert change.owner == RELEASE_CUT_OWNER
    assert RELEASE_CUT_TOPIC_SUBSTRING in change.topic
    assert audits[0][1]["after"]["develop_sha"] == develop_tip

    # nothing votes yet → not submittable.
    assert gerrit.submittable(number) is False

    # (2) operator +2 (non-ai-reviewer) — still blocked: merger +2 missing.
    gerrit.review(number, value=2, group=HUMAN_GROUP)
    assert gerrit.submittable(number) is False

    # (3) merger-bot conditional +2 fires automatically (AUDIT-26c).
    assert MockGerrit._sr_applicable(change) is True
    assert gerrit.merger_bot_autovote(number) is True
    assert change.labels[("Code-Review", MERGER_GROUP)] == 2
    assert gerrit.submittable(number) is True

    # (4) submit → MERGE_ALWAYS advances refs/heads/main to the merge commit.
    merged_event = gerrit.submit(number)
    assert change.status == "MERGED"
    assert _git(remote, "rev-parse", "main") == change.commit_sha != main_before

    # (5) gerrit-jira-bridge processes the change-merged event.
    row = bridge.process_change_merged(merged_event)
    assert any(line["event"] == "release_cut_main_advanced" for line in bridge.log_lines)

    # (6) release_audit row written with outcome=success.
    assert row is not None
    assert row.outcome == "success"
    assert row.fix_version == RELEASE_VERSION
    assert row.main_sha == change.commit_sha
    assert row.develop_sha == develop_tip
    assert row.change_number == str(number)
    assert row.detail["meta_ticket"] == META_TICKET

    # (7) release notification fan-out carries version + RELEASE META link +
    #     change number (the payload an H4/OP-949 approval UI would surface).
    assert len(bridge.notifications) == 1
    notif = bridge.notifications[0]
    assert notif.release_version == RELEASE_VERSION
    assert notif.change_number == str(number)
    assert notif.meta_ticket == META_TICKET
    assert notif.release_meta_url == f"{_JIRA_BROWSE_BASE}/{META_TICKET}"


def test_release_cut_handles_long_develop_chain_as_single_change(tmp_path: Path) -> None:
    """AUDIT-26d intent — a 40-commit develop lead still produces ONE merge
    change (not one change per intervening commit), and it submits cleanly.
    This is the bot daily-promote path (owner != sora, release-v… topic), so
    it is governed by Human-Plus-2: a single human +2 submits it and the
    merger-bot declines."""
    repo, remote = _repo_with_remote(tmp_path)
    for index in range(40):
        _commit_file(repo, f"feat-{index:02d}.txt", f"{index}\n")
    develop_tip = _git(repo, "rev-parse", "develop")

    gerrit = MockGerrit(remote)
    result, _notifies, _events, _audits = _run_auto_promote(repo, gerrit)

    assert result.status == "change_created"
    assert len(result.develop_only) == 40
    assert len(gerrit.changes) == 1
    (number,) = gerrit.changes
    change = gerrit.changes[number]
    assert MockGerrit._sr_applicable(change) is False  # bot push → not a real cut
    assert gerrit.merger_bot_autovote(number) is False
    gerrit.review(number, value=2, group=HUMAN_GROUP)  # Human-Plus-2 governs
    assert gerrit.submittable(number) is True
    gerrit.submit(number)
    assert _git(remote, "rev-parse", "main") == gerrit.changes[number].commit_sha
    # the promoted develop tip is the merge commit's second parent.
    assert _git(remote, "rev-parse", "main^2") == develop_tip


# ── negative cases ───────────────────────────────────────────────────────────


def test_missing_r8_approval_yields_no_change(tmp_path: Path) -> None:
    """AC #5 negative — without R8 ship approval the milestone checker emits
    ``milestone_blocked`` (not ``milestone_ready``); auto_promote_main ignores
    it, so there is no Gerrit change to submit and ``main`` never moves."""
    repo, remote = _repo_with_remote(tmp_path)
    main_before = _git(remote, "rev-parse", "main")
    _commit_file(repo, "feature.txt", "ship it\n")
    gerrit = MockGerrit(remote)

    result = apm.promote_on_milestone_ready(
        {"event": "milestone_blocked", "fixVersion": RELEASE_VERSION,
         "reasons": ["R8 prod ship approval not granted"]},
        repo=repo, remote="gerrit",
        notify=lambda *_a: None, event_sink=lambda *_a: None, audit_sink=lambda *_a: None,
        push_for_review=gerrit.push_for_review,
    )

    assert result.status == "ignored"
    assert gerrit.changes == {}
    assert _git(remote, "rev-parse", "main") == main_before


def test_submit_requirement_refuses_without_human_plus2(tmp_path: Path) -> None:
    """AC #5 negative — the ``non-ai-reviewer`` +2 is the R8-equivalent hard
    gate: a merger-bot +2 alone leaves the release-cut-promote requirement
    unsatisfied, and ``submit`` is refused."""
    repo, remote = _repo_with_remote(tmp_path)
    main_before = _git(remote, "rev-parse", "main")
    _commit_file(repo, "feature.txt", "ship it\n")
    gerrit = MockGerrit(remote, owner=RELEASE_CUT_OWNER)  # genuine sora cut
    result, *_ = _run_auto_promote(repo, gerrit, topic=SORA_RELEASE_CUT_TOPIC)
    assert result.status == "change_created"
    (number,) = gerrit.changes

    assert gerrit.merger_bot_autovote(number) is True  # merger half satisfied
    assert gerrit.submittable(number) is False  # human half missing
    with pytest.raises(PermissionError):
        gerrit.submit(number)
    assert _git(remote, "rev-parse", "main") == main_before
    assert gerrit.changes[number].status == "NEW"


def test_submit_requirement_not_applicable_without_r3_hashtag(tmp_path: Path) -> None:
    """Quad-key guard — strip BOTH accepted R3-fastforward hashtag spellings
    from a genuine sora cut and the release-cut-promote requirement stops
    being applicable; the merger-bot declines to vote and the change falls
    back to the human hard gate."""
    repo, remote = _repo_with_remote(tmp_path)
    _commit_file(repo, "feature.txt", "ship it\n")
    gerrit = MockGerrit(remote, owner=RELEASE_CUT_OWNER)
    _run_auto_promote(repo, gerrit, topic=SORA_RELEASE_CUT_TOPIC)
    (number,) = gerrit.changes
    change = gerrit.changes[number]
    change.hashtags = tuple(
        h
        for h in change.hashtags
        if h not in (R3_FASTFORWARD_HASHTAG, R3_FASTFORWARD_MILESTONE)
    )

    assert MockGerrit._sr_applicable(change) is False
    assert gerrit.merger_bot_autovote(number) is False
    # the change now falls to the human hard gate — a merger +2 alone (no
    # non-ai-reviewer +2) does not submit it.
    gerrit.review(number, value=2, group=MERGER_GROUP)
    assert gerrit.submittable(number) is False


def test_release_cut_promote_not_applicable_for_non_sora_owner(tmp_path: Path) -> None:
    """Quad-key guard — a change whose owner is NOT sora can't ride the
    release-cut-promote conditional +2 even with the right
    branch/topic/hashtag (OP-1534 owner:sora; O10 forces sora to push a real
    cut, so a bot/other-account owner is rejected)."""
    repo, remote = _repo_with_remote(tmp_path)
    _commit_file(repo, "feature.txt", "ship it\n")
    gerrit = MockGerrit(remote, owner="lint-bot")
    _run_auto_promote(repo, gerrit, topic=SORA_RELEASE_CUT_TOPIC)
    (number,) = gerrit.changes
    change = gerrit.changes[number]

    assert change.owner == "lint-bot"
    assert MockGerrit._sr_applicable(change) is False
    assert gerrit.merger_bot_autovote(number) is False


def test_negative_vote_blocks_submit(tmp_path: Path) -> None:
    """``No-Veto`` requirement — a ``Code-Review -1`` blocks submit even with
    both +2 votes present on a genuine sora cut."""
    repo, remote = _repo_with_remote(tmp_path)
    main_before = _git(remote, "rev-parse", "main")
    _commit_file(repo, "feature.txt", "ship it\n")
    gerrit = MockGerrit(remote, owner=RELEASE_CUT_OWNER)
    _run_auto_promote(repo, gerrit, topic=SORA_RELEASE_CUT_TOPIC)
    (number,) = gerrit.changes

    gerrit.review(number, value=2, group=HUMAN_GROUP)
    assert gerrit.merger_bot_autovote(number) is True
    assert gerrit.submittable(number) is True
    gerrit.review(number, value=-1, group="ci-bot")  # late veto
    assert gerrit.submittable(number) is False
    with pytest.raises(PermissionError):
        gerrit.submit(number)
    assert _git(remote, "rev-parse", "main") == main_before


def test_bridge_skips_non_release_cut_main_merge(tmp_path: Path) -> None:
    """The bridge's release-cut path only fires for a ``[release-cut vX.Y.Z]``
    merge subject — an ordinary merge landing on ``main`` is logged-and-skipped
    (no spurious release_audit row / notification)."""
    repo, remote = _repo_with_remote(tmp_path)
    bridge = ReleaseCutBridge(remote)
    event = {
        "type": "change-merged",
        "change": {
            "number": 9001, "url": "https://gerrit.sora.services/c/sora-bridge/+/9001",
            "branch": "main", "topic": "", "subject": "Some unrelated change on main",
            "owner": {"username": "rt3628"},
        },
        "newRev": _git(remote, "rev-parse", "main"),
        "refUpdate": {"refName": "refs/heads/main", "newRev": _git(remote, "rev-parse", "main")},
    }
    assert bridge.process_change_merged(event) is None
    assert bridge.audit_rows == []
    assert bridge.notifications == []
    assert bridge.log_lines[-1]["event"] == "change_merged_not_release_cut"


# ── Corrected-config contract (OP-1531 / OP-1534 / OP-1536 C3a) ────────────────


def test_release_cut_promote_applicable_if_matches_e2e_model_in_both_files() -> None:
    """Code AC — the corrected ``applicableIf`` for release-cut-promote is
    pinned byte-for-byte in BOTH .gerrit/project.config and
    .gerrit/project.config.example, AND it is the *same* four-key rule the
    behavioural model in this file exercises.

    The four corrected keys parsed straight out of the config — ``branch:main``
    / the R3-fastforward hashtag (canonical OR transition spelling) /
    ``intopic:release-cut`` / ``owner:sora`` — are exactly the keys
    :meth:`MockGerrit._sr_applicable` enforces (``RELEASE_CUT_TOPIC_SUBSTRING``
    / the R3-fastforward spellings / ``RELEASE_CUT_OWNER``). The old
    ``topic:^release-v…$`` regex + ``author:^…bot$`` alternation are gone."""
    for path in (_PROJECT_CONFIG, _PROJECT_CONFIG_EXAMPLE):
        applicable = _applicable_if(path.read_text(encoding="utf-8"), "release-cut-promote")
        assert applicable == RELEASE_CUT_PROMOTE_APPLICABLE_IF, (
            f"{path.name}: release-cut-promote applicableIf is not the "
            "corrected (OP-1531/OP-1534) form"
        )

        # The parsed keys are the same ones the in-process model enforces…
        assert "branch:main" in applicable
        assert f"intopic:{RELEASE_CUT_TOPIC_SUBSTRING}" in applicable
        assert f'hashtag:"{R3_FASTFORWARD_MILESTONE}"' in applicable
        assert f"hashtag:{R3_FASTFORWARD_HASHTAG}" in applicable
        assert f"owner:{RELEASE_CUT_OWNER}" in applicable
        # …and the obsolete old-model encodings are absent.
        assert "topic:^release-v" not in applicable
        assert "author:" not in applicable


def test_e2e_applicable_if_matches_merger_source_of_truth() -> None:
    """OP-1543 — cross-check this file's encoding against the C3a source of
    truth (``C3A_APPLICABLE_IF["release-cut-promote"]``) in
    test_merger_bot_main_promote.py, so the two suites can never drift."""
    from backend.tests.test_merger_bot_main_promote import C3A_APPLICABLE_IF

    assert RELEASE_CUT_PROMOTE_APPLICABLE_IF == C3A_APPLICABLE_IF["release-cut-promote"]
