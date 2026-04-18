"""O7 (#270) — Merge Arbiter tests.

Covers the webhook → merger → human-vote pipeline plus reconciliation
branches:

  * merge-conflict webhook with valid payload → merger runs → +2 path
    emits SSE ``change.awaiting_human_plus_two``.
  * merger abstain → JIRA ticket opened, SSE ``change.merger_abstain``.
  * human +2 after merger +2 → submit-rule allow → Gerrit submit called.
  * human -1 after merger +2 → merger +2 withdrawn, WIP SSE, failure
    counter cleared.
  * human +1 only → below-gate SSE, no submit.
  * invalid payload → invalid_payload outcome.
  * E2E happy path chain (webhook → merger → human +2 → submit).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend import merge_arbiter as arb
from backend import merger_agent as ma
from backend.submit_rule import human_vote, merger_vote


def _run(coro):
    return asyncio.run(coro)


# ──────────────────────────────────────────────────────────────
#  Helpers / stubs
# ──────────────────────────────────────────────────────────────


SIMPLE_CONFLICT = (
    "def greet(name):\n"
    "<<<<<<< HEAD\n"
    "    return f'Hello {name}!'\n"
    "=======\n"
    "    return f'Hi {name}!'\n"
    ">>>>>>> feature/greeting\n"
    "\n"
)


def _task(**overrides) -> arb.MergeConflictTask:
    defaults: dict[str, Any] = dict(
        change_id="Iabc123",
        project="omnisight",
        file_path="backend/greetings.py",
        conflict_text=SIMPLE_CONFLICT,
        head_commit_message="friendly",
        incoming_commit_message="shorter",
        file_context="def greet(name):",
        patchset_revision="deadbeef",
        workspace="/tmp/fake",
        jira_ticket="PROJ-42",
        catc_owner="alice@omnisight.internal",
    )
    defaults.update(overrides)
    return arb.MergeConflictTask(**defaults)


class _StubNotifier:
    def __init__(self):
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def notify(self, *, kind, change_id, payload):
        self.calls.append((kind, change_id, dict(payload)))


class _StubJira:
    def __init__(self, *, ok: bool = True, ticket: str = "PROJ-43"):
        self.ok = ok
        self.ticket = ticket
        self.calls: list[dict[str, Any]] = []

    async def open_abstain_ticket(self, **kwargs):
        self.calls.append(kwargs)
        return arb.JiraTicketResult(
            ok=self.ok,
            ticket=self.ticket if self.ok else "",
            url=f"https://jira/browse/{self.ticket}" if self.ok else "",
            reason="" if self.ok else "jira unavailable",
        )


class _StubSubmitter:
    def __init__(self, *, ok: bool = True):
        self.ok = ok
        self.calls: list[dict[str, Any]] = []

    async def submit(self, *, commit, project):
        self.calls.append({"commit": commit, "project": project})
        return {"status": "submitted"} if self.ok else {"error": "submit failed"}


class _StubRevoker:
    def __init__(self, *, ok: bool = True):
        self.ok = ok
        self.calls: list[dict[str, Any]] = []

    async def revoke(self, *, commit, project, message):
        self.calls.append({"commit": commit, "project": project, "message": message})
        return {"status": "ok"} if self.ok else {"error": "revoke failed"}


def _merger_runner(outcome: ma.ResolutionOutcome):
    async def run(_req: ma.ConflictRequest) -> ma.ResolutionOutcome:
        run.called_with = _req
        return outcome
    run.called_with = None
    return run


def _plus_two_outcome(task: arb.MergeConflictTask) -> ma.ResolutionOutcome:
    return ma.ResolutionOutcome(
        change_id=task.change_id,
        file_path=task.file_path,
        reason=ma.MergerReason.plus_two_voted,
        voted_score=ma.LabelVote.plus_two,
        confidence=0.94,
        rationale="LLM merge of single-line greeting",
        diff_preview="...diff...",
        push_sha="beadface",
        review_url="https://gerrit.example/change/42",
    )


def _abstain_outcome(task, reason=ma.MergerReason.abstained_low_confidence):
    return ma.ResolutionOutcome(
        change_id=task.change_id,
        file_path=task.file_path,
        reason=reason,
        voted_score=ma.LabelVote.abstain,
        confidence=0.6,
        rationale="not confident enough",
        diff_preview="",
    )


def _refuse_outcome(task, reason=ma.MergerReason.refused_security_file):
    return ma.ResolutionOutcome(
        change_id=task.change_id,
        file_path=task.file_path,
        reason=reason,
        voted_score=ma.LabelVote.abstain,
        confidence=0.0,
        rationale="security-sensitive path",
        diff_preview="",
    )


@pytest.fixture(autouse=True)
def _fresh_arbiter():
    arb.reset_arbiter_state_for_tests()
    ma.reset_failure_counts_for_tests()
    yield
    arb.reset_arbiter_state_for_tests()
    ma.reset_failure_counts_for_tests()


# ──────────────────────────────────────────────────────────────
#  Webhook intake tests
# ──────────────────────────────────────────────────────────────


def test_invalid_payload_returns_invalid_payload_outcome():
    deps = arb.ArbiterDeps(
        merger=_merger_runner(_plus_two_outcome(_task())),
        jira=_StubJira(),
        notifier=_StubNotifier(),
    )
    task = arb.MergeConflictTask(
        change_id="", project="", file_path="",
        conflict_text="",
    )
    outcome = _run(arb.on_merge_conflict_webhook(task, deps=deps))
    assert outcome.reason is arb.ArbiterReason.invalid_payload


def test_merger_plus_two_emits_awaiting_human_sse():
    task = _task()
    notifier = _StubNotifier()
    deps = arb.ArbiterDeps(
        merger=_merger_runner(_plus_two_outcome(task)),
        jira=_StubJira(),
        notifier=notifier,
    )
    outcome = _run(arb.on_merge_conflict_webhook(task, deps=deps))

    assert outcome.reason is arb.ArbiterReason.merger_plus_two_awaiting_human
    assert outcome.awaiting_human_since is not None
    assert outcome.merger_outcome["reason"] == "plus_two_voted"
    # SSE fired with the right kind.
    kinds = [c[0] for c in notifier.calls]
    assert "change.awaiting_human_plus_two" in kinds
    # Payload includes the merger confidence.
    payload = next(c[2] for c in notifier.calls
                   if c[0] == "change.awaiting_human_plus_two")
    assert payload["merger_confidence"] == pytest.approx(0.94)
    assert payload["jira_ticket"] == "PROJ-42"


def test_merger_abstain_opens_jira_and_emits_sse():
    task = _task()
    jira = _StubJira(ok=True, ticket="PROJ-99")
    notifier = _StubNotifier()
    deps = arb.ArbiterDeps(
        merger=_merger_runner(_abstain_outcome(task)),
        jira=jira,
        notifier=notifier,
    )
    outcome = _run(arb.on_merge_conflict_webhook(task, deps=deps))

    assert outcome.reason is arb.ArbiterReason.merger_abstained_jira_ticket_opened
    assert outcome.jira_ticket_created == "PROJ-99"
    assert len(jira.calls) == 1
    assert jira.calls[0]["assignee"] == "alice@omnisight.internal"
    assert jira.calls[0]["parent"] == "PROJ-42"
    kinds = [c[0] for c in notifier.calls]
    assert "change.merger_abstain" in kinds


def test_merger_abstain_dedupes_jira_for_same_change():
    task = _task()
    jira = _StubJira(ok=True, ticket="PROJ-100")
    deps = arb.ArbiterDeps(
        merger=_merger_runner(_abstain_outcome(task)),
        jira=jira,
        notifier=_StubNotifier(),
    )
    _run(arb.on_merge_conflict_webhook(task, deps=deps))
    _run(arb.on_merge_conflict_webhook(task, deps=deps))
    assert len(jira.calls) == 1, "duplicate abstain must not re-file JIRA"


def test_security_refusal_routes_to_security_reason():
    task = _task()
    deps = arb.ArbiterDeps(
        merger=_merger_runner(_refuse_outcome(task)),
        jira=_StubJira(),
        notifier=_StubNotifier(),
    )
    outcome = _run(arb.on_merge_conflict_webhook(task, deps=deps))
    assert outcome.reason is arb.ArbiterReason.merger_refused_security


def test_escalated_reason_maps_to_escalated_outcome():
    task = _task()
    deps = arb.ArbiterDeps(
        merger=_merger_runner(_refuse_outcome(
            task, reason=ma.MergerReason.refused_escalated,
        )),
        jira=_StubJira(),
        notifier=_StubNotifier(),
    )
    outcome = _run(arb.on_merge_conflict_webhook(task, deps=deps))
    assert outcome.reason is arb.ArbiterReason.merger_refused_escalated


def test_test_failure_maps_to_test_failure_outcome():
    task = _task()
    deps = arb.ArbiterDeps(
        merger=_merger_runner(_refuse_outcome(
            task, reason=ma.MergerReason.refused_test_failure,
        )),
        jira=_StubJira(),
        notifier=_StubNotifier(),
    )
    outcome = _run(arb.on_merge_conflict_webhook(task, deps=deps))
    assert outcome.reason is arb.ArbiterReason.merger_refused_test_failure


# ──────────────────────────────────────────────────────────────
#  Human-vote reconciliation
# ──────────────────────────────────────────────────────────────


def test_human_plus_two_after_merger_plus_two_submits():
    notifier = _StubNotifier()
    submitter = _StubSubmitter(ok=True)
    revoker = _StubRevoker()
    deps = arb.ArbiterDeps(
        merger=_merger_runner(_plus_two_outcome(_task())),
        notifier=notifier,
        submitter=submitter,
        revoker=revoker,
        jira=_StubJira(),
    )
    outcome = _run(arb.on_human_vote_recorded(
        change_id="Iabc123",
        project="omnisight",
        commit="beadface",
        votes=[
            merger_vote(),
            human_vote("alice@example.com"),
        ],
        deps=deps,
    ))
    assert outcome.reason is arb.ArbiterReason.submitted
    assert submitter.calls == [{"commit": "beadface", "project": "omnisight"}]
    assert not revoker.calls
    # SSE for submitted fired.
    assert any(c[0] == "change.submitted" for c in notifier.calls)


def test_human_minus_one_withdraws_merger_plus_two():
    notifier = _StubNotifier()
    submitter = _StubSubmitter()
    revoker = _StubRevoker(ok=True)
    deps = arb.ArbiterDeps(
        merger=_merger_runner(_plus_two_outcome(_task())),
        notifier=notifier,
        submitter=submitter,
        revoker=revoker,
        jira=_StubJira(),
    )
    outcome = _run(arb.on_human_vote_recorded(
        change_id="Iabc123",
        project="omnisight",
        commit="beadface",
        votes=[
            merger_vote(),
            human_vote("alice@example.com", score=-1),
        ],
        deps=deps,
    ))
    assert outcome.reason is arb.ArbiterReason.human_disagreed_merger_withdrew
    assert not submitter.calls
    assert len(revoker.calls) == 1
    assert "withdraws" in revoker.calls[0]["message"].lower()
    assert any(c[0] == "change.work_in_progress" for c in notifier.calls)


def test_human_plus_one_stays_below_gate():
    notifier = _StubNotifier()
    submitter = _StubSubmitter()
    revoker = _StubRevoker()
    deps = arb.ArbiterDeps(
        merger=_merger_runner(_plus_two_outcome(_task())),
        notifier=notifier,
        submitter=submitter,
        revoker=revoker,
        jira=_StubJira(),
    )
    outcome = _run(arb.on_human_vote_recorded(
        change_id="Iabc123",
        project="omnisight",
        commit="beadface",
        votes=[
            merger_vote(),
            human_vote("alice@example.com", score=1),
        ],
        deps=deps,
    ))
    assert outcome.reason is arb.ArbiterReason.human_vote_recorded_below_gate
    assert not submitter.calls
    assert not revoker.calls
    assert any(c[0] == "change.awaiting_more_votes" for c in notifier.calls)


# ──────────────────────────────────────────────────────────────
#  End-to-end happy path
# ──────────────────────────────────────────────────────────────


def test_e2e_happy_path_webhook_to_submit():
    """Spec §完整 E2E 測試: two PRs same file → second conflicts →
    merger resolves + +2 → notify → human +2 → submit → both commits
    remain."""
    task = _task(change_id="Iend2end")
    notifier = _StubNotifier()
    jira = _StubJira()
    submitter = _StubSubmitter(ok=True)
    revoker = _StubRevoker()
    deps = arb.ArbiterDeps(
        merger=_merger_runner(_plus_two_outcome(task)),
        notifier=notifier,
        submitter=submitter,
        revoker=revoker,
        jira=jira,
    )
    # Stage 1 — webhook fires.
    wh = _run(arb.on_merge_conflict_webhook(task, deps=deps))
    assert wh.reason is arb.ArbiterReason.merger_plus_two_awaiting_human
    assert any(c[0] == "change.awaiting_human_plus_two" for c in notifier.calls)

    # Stage 2 — human +2 arrives.
    hv = _run(arb.on_human_vote_recorded(
        change_id="Iend2end",
        project="omnisight",
        commit="beadface",
        votes=[
            merger_vote(),
            human_vote("alice@example.com"),
        ],
        deps=deps,
    ))
    assert hv.reason is arb.ArbiterReason.submitted
    assert submitter.calls and submitter.calls[0]["commit"] == "beadface"


# ──────────────────────────────────────────────────────────────
#  check_change_ready convenience helper
# ──────────────────────────────────────────────────────────────


def test_check_change_ready_matches_evaluator():
    d1 = arb.check_change_ready([merger_vote(), human_vote("alice@x")])
    assert d1.allow is True
    d2 = arb.check_change_ready([merger_vote()])
    assert d2.allow is False
