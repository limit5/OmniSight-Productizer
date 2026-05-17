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
import subprocess
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


class _StubPusher:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def push(self, **kwargs):
        self.calls.append(kwargs)
        return ma.PatchsetPushResult(ok=True, sha="feedface")


class _StubReviewer:
    async def post_review(self, **kwargs):
        return ma.ReviewerResult(ok=True)


class _StubHashtagSetter:
    async def add_hashtag(self, **kwargs):
        return ma.HashtagSetterResult(ok=True)


class _StubVerifier:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def verify_and_push(
        self,
        *,
        task: arb.MergeConflictTask,
        outcome: ma.ResolutionOutcome,
    ) -> ma.ResolutionOutcome:
        self.calls.append({"task": task, "outcome": outcome})
        return ma.ResolutionOutcome(
            change_id=task.change_id,
            file_path=task.file_path,
            reason=ma.MergerReason.plus_two_voted,
            voted_score=ma.LabelVote.plus_two,
            confidence=outcome.confidence,
            rationale=outcome.rationale,
            diff_preview=outcome.diff_preview,
            push_sha="verified-low-risk",
            review_url="https://gerrit.example/change/low-risk",
            metadata={**outcome.metadata, "verify_result": "green"},
            resolved_text=outcome.resolved_text,
        )


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


def test_merge_conflict_passes_context_pack_fields_to_merger():
    seen: list[ma.ConflictRequest] = []

    async def merger(req: ma.ConflictRequest) -> ma.ResolutionOutcome:
        seen.append(req)
        return _plus_two_outcome(_task())

    task = _task(
        change_number="883",
        jira_ticket="OP-883",
        jira_description="JIRA context for guild_id change",
        sibling_file_contents={"backend/agents/nodes.py": "guild_id = 1\n"},
        git_logs={"backend/greetings.py": "commit abc\n"},
        symbol_table={"backend/greetings.py": "function greet line 1 calls: str"},
    )
    deps = arb.ArbiterDeps(
        merger=merger,
        jira=_StubJira(),
        notifier=_StubNotifier(),
    )

    outcome = _run(arb.on_merge_conflict_webhook(task, deps=deps))

    assert outcome.reason is arb.ArbiterReason.merger_plus_two_awaiting_human
    assert seen[0].change_number == "883"
    assert seen[0].jira_ticket == "OP-883"
    assert seen[0].jira_description == "JIRA context for guild_id change"
    assert (
        seen[0].sibling_file_contents["backend/agents/nodes.py"]
        == "guild_id = 1\n"
    )
    assert seen[0].git_logs["backend/greetings.py"] == "commit abc\n"
    assert seen[0].symbol_table["backend/greetings.py"].startswith(
        "function greet"
    )


def test_classify_risk_covers_low_medium_and_high_tiers():
    low = ma.parse_conflict_block(
        "<<<<<<< HEAD\n"
        "def head_helper():\n"
        "    return 'head'\n"
        "=======\n"
        "def incoming_helper():\n"
        "    return 'incoming'\n"
        ">>>>>>> feature/helpers\n"
    )
    medium = ma.parse_conflict_block(
        "<<<<<<< HEAD\n"
        "def resolve(guild, guild_id):\n"
        "    return guild_id\n"
        "=======\n"
        "def resolve(guild, tenant_id):\n"
        "    return tenant_id\n"
        ">>>>>>> feature/signature\n"
    )
    high = ma.parse_conflict_block(
        "<<<<<<< HEAD\n"
        "if enabled:\n"
        "    run_fast()\n"
        "=======\n"
        "if enabled:\n"
        "    run_safe()\n"
        ">>>>>>> feature/control-flow\n"
    )

    assert arb._classify_risk(low).tier is ma.MergerRiskTier.low
    assert arb._classify_risk(medium).tier is ma.MergerRiskTier.medium
    assert arb._classify_risk(high).tier is ma.MergerRiskTier.high


def test_signature_overlap_is_medium_and_invokes_merger_path():
    seen: list[ma.ConflictRequest] = []

    async def merger(req: ma.ConflictRequest) -> ma.ResolutionOutcome:
        seen.append(req)
        return _plus_two_outcome(_task())

    task = _task(
        change_number="883",
        conflict_text=(
            "<<<<<<< HEAD\n"
            "def resolve(guild, guild_id):\n"
            "    return guild_id\n"
            "=======\n"
            "def resolve(guild, tenant_id):\n"
            "    return tenant_id\n"
            ">>>>>>> feature/signature\n"
        ),
    )
    deps = arb.ArbiterDeps(
        merger=merger,
        jira=_StubJira(),
        notifier=_StubNotifier(),
    )

    outcome = _run(arb.on_merge_conflict_webhook(task, deps=deps))

    assert outcome.reason is arb.ArbiterReason.merger_plus_two_awaiting_human
    assert len(seen) == 1
    assert seen[0].change_number == "883"


def test_low_risk_add_only_take_both_without_merger():
    async def merger(_req: ma.ConflictRequest) -> ma.ResolutionOutcome:
        raise AssertionError("LOW risk conflict must not invoke merger LLM path")

    verifier = _StubVerifier()
    task = _task(
        conflict_text=(
            "<<<<<<< HEAD\n"
            "=======\n"
            "def incoming_helper():\n"
            "    return 'incoming'\n"
            ">>>>>>> feature/helpers\n"
        ),
    )
    deps = arb.ArbiterDeps(
        merger=merger,
        jira=_StubJira(),
        notifier=_StubNotifier(),
        verifier=verifier,
    )

    outcome = _run(arb.on_merge_conflict_webhook(task, deps=deps))

    assert outcome.reason is arb.ArbiterReason.merger_plus_two_awaiting_human
    assert len(verifier.calls) == 1
    low_outcome = verifier.calls[0]["outcome"]
    assert low_outcome.metadata["risk_tier"] == "LOW"
    assert low_outcome.metadata["deterministic_take_both"] is True
    assert "def incoming_helper" in low_outcome.resolved_text


def test_high_risk_control_flow_escalates_without_merger():
    async def merger(_req: ma.ConflictRequest) -> ma.ResolutionOutcome:
        raise AssertionError("HIGH risk conflict must not invoke merger LLM path")

    jira = _StubJira(ok=True, ticket="PROJ-HIGH")
    task = _task(
        conflict_text=(
            "<<<<<<< HEAD\n"
            "if enabled:\n"
            "    run_fast()\n"
            "=======\n"
            "if enabled:\n"
            "    run_safe()\n"
            ">>>>>>> feature/control-flow\n"
        ),
    )
    deps = arb.ArbiterDeps(
        merger=merger,
        jira=jira,
        notifier=_StubNotifier(),
    )

    outcome = _run(arb.on_merge_conflict_webhook(task, deps=deps))

    assert outcome.reason is arb.ArbiterReason.merger_refused_escalated
    assert outcome.jira_ticket_created == "PROJ-HIGH"
    assert len(jira.calls) == 1
    assert jira.calls[0]["merger_reason"] == "refused_escalated"
    assert "risk_tier=HIGH" in jira.calls[0]["merger_rationale"]


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


# ──────────────────────────────────────────────────────────────────
#  OP-1196 phase 3 — deferred-push routing
# ──────────────────────────────────────────────────────────────────


def _deferred_push_outcome(task: arb.MergeConflictTask) -> ma.ResolutionOutcome:
    """A merger outcome where the LLM produced resolved_text but the
    in-process pusher was skipped because push_locally=False."""
    o = ma.ResolutionOutcome(
        change_id=task.change_id,
        file_path=task.file_path,
        reason=ma.MergerReason.deferred_push_to_caller,
        voted_score=ma.LabelVote.abstain,
        confidence=0.93,
        rationale="LLM picked HEAD; deferring push to caller",
        diff_preview="--- (conflict)\n+++ (resolved)\n@@ ... @@\n+x = 42\n",
    )
    o.resolved_text = "x = 42  # resolved\n"
    o.changed_identifiers = ["greet"]
    return o


def _deferred_resolution(
    task: arb.MergeConflictTask,
    *,
    resolved_text: str,
    changed_identifiers: list[str] | None = None,
) -> ma.ResolutionOutcome:
    return ma.ResolutionOutcome(
        change_id=task.change_id,
        file_path=task.file_path,
        reason=ma.MergerReason.deferred_push_to_caller,
        voted_score=ma.LabelVote.abstain,
        confidence=0.94,
        rationale="LLM produced a deferred resolution",
        diff_preview="diff",
        resolved_text=resolved_text,
        changed_identifiers=changed_identifiers or ["greet"],
    )


def _git_workspace(tmp_path):
    ws = tmp_path / "repo"
    (ws / "backend").mkdir(parents=True)
    (ws / "backend" / "greetings.py").write_text(
        "def greet(name):\n    return f'Hello {name}!'\n",
        encoding="utf-8",
    )
    (ws / "test_greetings.py").write_text(
        "from backend.greetings import greet\n\n"
        "def test_greet():\n"
        "    assert greet('Ada') == 'Hello Ada!'\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"],
                   cwd=ws, check=True)
    subprocess.run(["git", "add", "."], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=ws,
                   check=True, capture_output=True)
    return ws


class TestDeferredPushRouting:
    """OP-1196 phase 3 — arbiter must route MergerReason.deferred_
    push_to_caller to ArbiterReason.merger_resolved_pending_caller_push,
    carrying merger_outcome (with resolved_text) back to the caller.
    Must NOT open a JIRA abstain ticket (the caller will complete the
    resolution and is responsible for surfacing failures).
    """

    def test_deferred_push_routes_to_pending_caller_push(self):
        task = _task(push_locally=False)
        deps = arb.ArbiterDeps(
            merger=_merger_runner(_deferred_push_outcome(task)),
            jira=_StubJira(),
            notifier=_StubNotifier(),
        )

        outcome = _run(arb.on_merge_conflict_webhook(task, deps=deps))

        assert outcome.reason is arb.ArbiterReason.merger_resolved_pending_caller_push
        # The merger_outcome dict must include resolved_text so the
        # daemon caller can apply it.
        assert outcome.merger_outcome is not None
        assert outcome.merger_outcome.get("resolved_text") == "x = 42  # resolved\n"
        assert outcome.merger_outcome.get("reason") == "deferred_push_to_caller"

    def test_deferred_push_does_NOT_open_jira_abstain_ticket(self):
        """The caller is expected to complete the resolution. Opening
        an abstain ticket here would create confusing noise — the
        flow is mid-execution, not abandoned."""
        task = _task(push_locally=False)
        stub_jira = _StubJira()
        deps = arb.ArbiterDeps(
            merger=_merger_runner(_deferred_push_outcome(task)),
            jira=stub_jira,
            notifier=_StubNotifier(),
        )

        outcome = _run(arb.on_merge_conflict_webhook(task, deps=deps))

        assert outcome.reason is arb.ArbiterReason.merger_resolved_pending_caller_push
        assert len(stub_jira.calls) == 0, (
            "deferred-push path must not open a JIRA abstain ticket"
        )
        assert outcome.jira_ticket_created is None

    def test_push_locally_default_true_unaffected(self):
        """Backwards-compat: omitting push_locally in the request keeps
        the default in-process push pathway."""
        task = _task()  # default push_locally=True per dataclass default
        assert task.push_locally is True
        # from_dict() with no push_locally key keeps the default True
        roundtrip = arb.MergeConflictTask.from_dict({
            "change_id": "X", "project": "p", "file_path": "f",
            "conflict_text": "...",
        })
        assert roundtrip.push_locally is True
        # explicit False roundtrips correctly
        rt2 = arb.MergeConflictTask.from_dict({
            "change_id": "X", "project": "p", "file_path": "f",
            "conflict_text": "...", "push_locally": False,
        })
        assert rt2.push_locally is False


class TestBuildThenVerify:

    def test_broken_resolution_abstains_without_pushing(self, tmp_path):
        ws = _git_workspace(tmp_path)
        task = _task(workspace=str(ws))
        pusher = _StubPusher()
        deps = arb.ArbiterDeps(
            merger=_merger_runner(_deferred_resolution(
                task,
                resolved_text="def greet(name):\n    return 'unterminated\n",
            )),
            jira=_StubJira(),
            notifier=_StubNotifier(),
            verifier=arb._DefaultResolutionVerifier(  # type: ignore[attr-defined]
                pusher=pusher,
                reviewer=_StubReviewer(),
                hashtag_setter=_StubHashtagSetter(),
            ),
        )

        outcome = _run(arb.on_merge_conflict_webhook(task, deps=deps))

        assert outcome.reason is arb.ArbiterReason.merger_refused_test_failure
        assert pusher.calls == []
        assert outcome.merger_outcome is not None
        assert outcome.merger_outcome["metadata"]["verify_result"] == "red"
        assert outcome.merger_outcome["metadata"]["verify_stage"] == "py_compile"

    def test_semantically_wrong_resolution_abstains_with_pytest_output(self, tmp_path):
        ws = _git_workspace(tmp_path)
        task = _task(workspace=str(ws))
        pusher = _StubPusher()
        deps = arb.ArbiterDeps(
            merger=_merger_runner(_deferred_resolution(
                task,
                resolved_text="def greet(name):\n    return f'Hi {name}!'\n",
            )),
            jira=_StubJira(),
            notifier=_StubNotifier(),
            verifier=arb._DefaultResolutionVerifier(  # type: ignore[attr-defined]
                pusher=pusher,
                reviewer=_StubReviewer(),
                hashtag_setter=_StubHashtagSetter(),
            ),
        )

        outcome = _run(arb.on_merge_conflict_webhook(task, deps=deps))

        assert outcome.reason is arb.ArbiterReason.merger_refused_test_failure
        assert pusher.calls == []
        assert outcome.merger_outcome is not None
        test_result = outcome.merger_outcome["test_result"]
        assert test_result["ok"] is False
        assert "test_greet" in test_result["last_30_lines"]
        assert outcome.merger_outcome["metadata"]["verify_stage"] == "pytest"
