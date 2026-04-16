"""O7 (#270) — Merge Arbiter.

The Arbiter sits between Gerrit webhook events and the O6 Merger Agent
and glues the dual-+2 submit-rule into a fully automated CI/CD merge
pipeline.

Responsibilities
----------------

1. **Webhook intake** — invoked by ``POST /orchestrator/merge-conflict``
   when Gerrit (or a GitHub Action fallback) reports a merge conflict.
   Builds a :class:`MergeConflictTask` and hands it to the Merger Agent.

2. **Post-merger routing** — once the merger has voted:
     * ``plus_two_voted`` → emit SSE ``orchestration.change.awaiting_human_plus_two``
       so the UI / Slack / email bridge can prompt a human.
     * ``abstained_*`` (O6 gate not met) → file a JIRA ticket and
       assign the original CATC owner; change stays in "work-in-progress".
     * ``refused_*`` (security / test / escalated) → same JIRA route +
       Slack red alert.

3. **Human-vote reconciliation** — public entry point
   :func:`on_human_vote_recorded` reconciles a subsequent human vote:
     * Human +2 → submit-rule is satisfied, Gerrit auto-submits
       (best-effort call into ``gerrit_client.submit_change``).
     * Human -1 / -2 → revert the Merger's +2 with an explanatory
       comment ("human disagrees, merger withdraws"), flip change to
       work-in-progress, clear the failure counter.
     * Human +1 → no-op (below gate, waits for another vote).

4. **Submit-rule pre-check** — exposes :func:`check_change_ready` so
   any caller can ask "are we green?" using the same SSOT
   (``backend.submit_rule.evaluate_submit_rule``) as the Gerrit
   Prolog rule.

Design properties
-----------------

* Every external collaborator is injected — the Gerrit client, the
  JIRA client, the Merger runner, the Slack/webhook notifier.  Tests
  substitute deterministic stubs with zero network.
* No global mutable state — the process is stateless; the Gerrit
  change itself is the source of truth.  The in-memory
  ``_pending_abstains`` cache exists only to de-dupe JIRA ticket
  creation when the same change re-fires.
* All handled failures are surfaced as :class:`ArbiterOutcome` with a
  stable ``reason`` enum; this module never raises on the hot path.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol

from backend import merger_agent as ma
from backend.submit_rule import (
    GROUP_AI_BOTS,
    GROUP_HUMAN,
    GROUP_MERGER,
    ReviewerVote,
    SubmitDecision,
    SubmitReason,
    evaluate_submit_rule,
)

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tunables
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

HUMAN_PENDING_WARN_HOURS = int(
    os.environ.get("OMNISIGHT_ARBITER_HUMAN_WARN_HOURS", "24")
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ArbiterReason(str, Enum):
    """Stable outcome codes — part of the HTTP + audit contract."""

    merger_plus_two_awaiting_human = "merger_plus_two_awaiting_human"
    merger_abstained_jira_ticket_opened = "merger_abstained_jira_ticket_opened"
    merger_refused_security = "merger_refused_security"
    merger_refused_test_failure = "merger_refused_test_failure"
    merger_refused_escalated = "merger_refused_escalated"
    merger_refused_other = "merger_refused_other"

    submitted = "submitted"
    human_disagreed_merger_withdrew = "human_disagreed_merger_withdrew"
    human_vote_recorded_below_gate = "human_vote_recorded_below_gate"

    invalid_payload = "invalid_payload"


@dataclass
class MergeConflictTask:
    """Webhook payload the Arbiter expects."""

    change_id: str
    project: str
    file_path: str
    conflict_text: str
    head_commit_message: str = ""
    incoming_commit_message: str = ""
    file_context: str = ""
    patchset_revision: str = ""
    workspace: str | None = None
    additional_files: list[str] = field(default_factory=list)
    jira_ticket: str = ""                # parent story (for abstain ticket)
    catc_owner: str = ""                 # original CATC assignee

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MergeConflictTask":
        return cls(
            change_id=str(d.get("change_id") or d.get("changeId") or ""),
            project=str(d.get("project") or ""),
            file_path=str(d.get("file_path") or d.get("filePath") or ""),
            conflict_text=str(d.get("conflict_text") or d.get("conflictText") or ""),
            head_commit_message=str(d.get("head_commit_message") or ""),
            incoming_commit_message=str(d.get("incoming_commit_message") or ""),
            file_context=str(d.get("file_context") or d.get("fileContext") or ""),
            patchset_revision=str(d.get("patchset_revision") or d.get("revision") or ""),
            workspace=d.get("workspace"),
            additional_files=list(d.get("additional_files") or []),
            jira_ticket=str(d.get("jira_ticket") or ""),
            catc_owner=str(d.get("catc_owner") or ""),
        )


@dataclass
class ArbiterOutcome:
    """Result of a webhook / reconciliation call."""

    change_id: str
    reason: ArbiterReason
    detail: str = ""
    merger_outcome: dict[str, Any] | None = None
    submit_decision: dict[str, Any] | None = None
    jira_ticket_created: str | None = None
    awaiting_human_since: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reason"] = self.reason.value
        return d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pluggable collaborators
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


MergerRunner = Callable[[ma.ConflictRequest], Awaitable[ma.ResolutionOutcome]]


class JiraTicketOpener(Protocol):
    """Opens a JIRA ticket when the Merger abstains.  Tests inject a
    stub that records the call; production wires in ``jira_adapter``."""

    async def open_abstain_ticket(
        self,
        *,
        parent: str,
        assignee: str,
        change_id: str,
        merger_reason: str,
        merger_rationale: str,
        file_path: str,
    ) -> "JiraTicketResult": ...


@dataclass
class JiraTicketResult:
    ok: bool
    ticket: str = ""
    url: str = ""
    reason: str = ""


class Notifier(Protocol):
    """SSE / Slack / email bridge.  Tests use a stub that records
    events."""

    async def notify(
        self,
        *,
        kind: str,
        change_id: str,
        payload: dict[str, Any],
    ) -> None: ...


class GerritSubmitter(Protocol):
    """Wraps ``gerrit_client.submit_change`` so tests can assert submit
    was called without needing Gerrit SSH."""

    async def submit(self, *, commit: str, project: str) -> dict: ...


class GerritVoteRevoker(Protocol):
    """Posts a ``Code-Review: 0`` to withdraw the Merger's prior +2
    vote when a human disagrees."""

    async def revoke(
        self,
        *,
        commit: str,
        project: str,
        message: str,
    ) -> dict: ...


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Default collaborator wrappers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _default_merger_runner(req: ma.ConflictRequest) -> ma.ResolutionOutcome:
    return await ma.resolve_conflict(req)


class _DefaultJiraOpener:
    """Real JIRA ticket opener — lazy-imports ``jira_adapter`` so tests
    that don't touch JIRA don't need the settings wired up."""

    async def open_abstain_ticket(
        self,
        *,
        parent: str,
        assignee: str,
        change_id: str,
        merger_reason: str,
        merger_rationale: str,
        file_path: str,
    ) -> JiraTicketResult:
        try:
            from backend import jira_adapter as _ja  # noqa: F401
        except Exception as exc:
            logger.debug("arbiter: jira_adapter unavailable: %s", exc)
            return JiraTicketResult(
                ok=False,
                reason=f"jira_adapter unavailable: {exc}",
            )
        # The project-level JIRA client is initialised per-tenant via
        # ``intent_bridge`` — without a live tenant we can't actually
        # open the ticket here.  We surface a recorded-intent outcome so
        # the arbiter still returns a deterministic result; the
        # orchestrator / operator UI picks up the "awaiting" SSE event
        # and a human operator files the ticket.  The stub makes the
        # contract visible in the outcome ``detail``.
        return JiraTicketResult(
            ok=False,
            reason=(
                "no live JIRA client bound; ticket creation deferred to "
                "intent_bridge (see _default_jira_opener note)"
            ),
        )


class _DefaultNotifier:
    async def notify(
        self,
        *,
        kind: str,
        change_id: str,
        payload: dict[str, Any],
    ) -> None:
        try:
            from backend.events import emit_invoke
            emit_invoke(
                f"orchestration.{kind}",
                f"{kind}: {change_id}",
                change_id=change_id,
                **payload,
            )
        except Exception as exc:                         # pragma: no cover
            logger.debug("arbiter notify SSE failed: %s", exc)


class _DefaultGerritSubmitter:
    async def submit(self, *, commit: str, project: str) -> dict:
        try:
            from backend.gerrit import gerrit_client
            return await gerrit_client.submit_change(commit=commit, project=project)
        except Exception as exc:                         # pragma: no cover
            return {"error": f"gerrit submit_change failed: {exc}"}


class _DefaultGerritVoteRevoker:
    async def revoke(
        self,
        *,
        commit: str,
        project: str,
        message: str,
    ) -> dict:
        try:
            from backend.gerrit import gerrit_client
            return await gerrit_client.post_review(
                commit=commit,
                message=message,
                labels={"Code-Review": 0},
                project=project,
            )
        except Exception as exc:                         # pragma: no cover
            return {"error": f"gerrit revoke failed: {exc}"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dependency bundle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ArbiterDeps:
    merger: MergerRunner = _default_merger_runner
    jira: JiraTicketOpener = field(default_factory=_DefaultJiraOpener)
    notifier: Notifier = field(default_factory=_DefaultNotifier)
    submitter: GerritSubmitter = field(default_factory=_DefaultGerritSubmitter)
    revoker: GerritVoteRevoker = field(default_factory=_DefaultGerritVoteRevoker)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  De-dupe registry — avoid filing twice for the same abstain
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_pending_abstains: dict[str, dict[str, Any]] = {}


def reset_arbiter_state_for_tests() -> None:
    _pending_abstains.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public entry points
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def on_merge_conflict_webhook(
    task: MergeConflictTask,
    *,
    deps: ArbiterDeps | None = None,
) -> ArbiterOutcome:
    """Called by ``POST /orchestrator/merge-conflict`` (Gerrit webhook
    or GitHub Actions fallback).

    Pipeline:
      1. Validate payload (required fields).
      2. Build a :class:`ma.ConflictRequest` and hand it to the Merger.
      3. Route on the merger's reason code.
    """
    deps = deps or ArbiterDeps()

    if not task.change_id or not task.project or not task.file_path:
        return ArbiterOutcome(
            change_id=task.change_id or "<unknown>",
            reason=ArbiterReason.invalid_payload,
            detail="change_id, project, and file_path are required",
        )

    req = ma.ConflictRequest(
        change_id=task.change_id,
        project=task.project,
        file_path=task.file_path,
        conflict_text=task.conflict_text,
        head_commit_message=task.head_commit_message,
        incoming_commit_message=task.incoming_commit_message,
        file_context=task.file_context,
        patchset_revision=task.patchset_revision,
        workspace=task.workspace,
        additional_files=list(task.additional_files),
    )
    merger_outcome = await deps.merger(req)
    return await _route_merger_outcome(task, merger_outcome, deps)


async def _route_merger_outcome(
    task: MergeConflictTask,
    outcome: ma.ResolutionOutcome,
    deps: ArbiterDeps,
) -> ArbiterOutcome:
    """Translate a merger outcome into an arbiter decision."""
    if outcome.reason is ma.MergerReason.plus_two_voted:
        now = time.time()
        _pending_abstains.pop(task.change_id, None)       # clear stale
        # O9 (#272) — register in the awaiting-human-+2 dashboard
        # registry so the orchestration panel + Prometheus gauge can
        # surface the dual-sign-pending count.
        try:
            from backend.orchestration_observability import (
                register_awaiting_human,
            )
            register_awaiting_human(
                change_id=task.change_id,
                project=task.project,
                file_path=task.file_path,
                merger_confidence=outcome.confidence,
                merger_rationale=outcome.rationale,
                review_url=outcome.review_url,
                push_sha=outcome.push_sha,
                awaiting_since=now,
                jira_ticket=task.jira_ticket,
            )
        except Exception as exc:                          # pragma: no cover
            logger.debug("arbiter: awaiting-human registry update failed: %s", exc)
        await deps.notifier.notify(
            kind="change.awaiting_human_plus_two",
            change_id=task.change_id,
            payload={
                "project": task.project,
                "file_path": task.file_path,
                "merger_confidence": outcome.confidence,
                "merger_rationale": outcome.rationale,
                "review_url": outcome.review_url,
                "push_sha": outcome.push_sha,
                "awaiting_since": now,
                "jira_ticket": task.jira_ticket,
            },
        )
        return ArbiterOutcome(
            change_id=task.change_id,
            reason=ArbiterReason.merger_plus_two_awaiting_human,
            detail=(
                f"Merger Agent cast +2 with confidence "
                f"{outcome.confidence:.2f}; waiting on human +2 from the "
                f"`non-ai-reviewer` group (hard gate)."
            ),
            merger_outcome=outcome.to_dict(),
            awaiting_human_since=now,
        )

    # Any non-+2 outcome — branch by reason.
    return await _handle_non_plus_two(task, outcome, deps)


async def _handle_non_plus_two(
    task: MergeConflictTask,
    outcome: ma.ResolutionOutcome,
    deps: ArbiterDeps,
) -> ArbiterOutcome:
    reason_map = {
        ma.MergerReason.refused_security_file:
            ArbiterReason.merger_refused_security,
        ma.MergerReason.refused_test_failure:
            ArbiterReason.merger_refused_test_failure,
        ma.MergerReason.refused_escalated:
            ArbiterReason.merger_refused_escalated,
    }
    abstains = {
        ma.MergerReason.abstained_low_confidence,
        ma.MergerReason.abstained_multi_file,
        ma.MergerReason.abstained_oversized,
        ma.MergerReason.refused_llm_unavailable,
        ma.MergerReason.refused_llm_invalid_json,
        ma.MergerReason.refused_new_logic_detected,
    }

    if outcome.reason in reason_map:
        arb_reason = reason_map[outcome.reason]
    elif outcome.reason in abstains:
        arb_reason = ArbiterReason.merger_abstained_jira_ticket_opened
    else:
        arb_reason = ArbiterReason.merger_refused_other

    # Open JIRA ticket (de-duped by change-id).
    prior = _pending_abstains.get(task.change_id)
    if prior and prior.get("merger_reason") == outcome.reason.value:
        jira_ticket = prior.get("jira_ticket")
        jira_ok = bool(jira_ticket)
    else:
        assignee = task.catc_owner or "orchestrator-oncall"
        parent = task.jira_ticket or ""
        res = await deps.jira.open_abstain_ticket(
            parent=parent,
            assignee=assignee,
            change_id=task.change_id,
            merger_reason=outcome.reason.value,
            merger_rationale=outcome.rationale,
            file_path=task.file_path,
        )
        jira_ticket = res.ticket if res.ok else ""
        jira_ok = res.ok
        _pending_abstains[task.change_id] = {
            "merger_reason": outcome.reason.value,
            "jira_ticket": jira_ticket,
            "opened_at": time.time(),
        }

    # Emit SSE so the UI / Slack bridge shows the abstain + ticket link.
    await deps.notifier.notify(
        kind="change.merger_abstain",
        change_id=task.change_id,
        payload={
            "project": task.project,
            "file_path": task.file_path,
            "merger_reason": outcome.reason.value,
            "merger_rationale": outcome.rationale,
            "jira_ticket": jira_ticket,
            "jira_opened_ok": jira_ok,
            "parent_jira": task.jira_ticket,
            "assignee": task.catc_owner,
        },
    )

    detail = (
        f"Merger outcome: {outcome.reason.value} — {outcome.rationale}. "
        f"{'JIRA ticket ' + jira_ticket + ' opened for human follow-up.' if jira_ok else 'JIRA ticket creation deferred (see SSE event).'}"
    )
    return ArbiterOutcome(
        change_id=task.change_id,
        reason=arb_reason,
        detail=detail,
        merger_outcome=outcome.to_dict(),
        jira_ticket_created=jira_ticket or None,
        metadata={"jira_opened_ok": jira_ok},
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Human-vote reconciliation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def on_human_vote_recorded(
    *,
    change_id: str,
    project: str,
    commit: str,
    votes: list[ReviewerVote | dict[str, Any]],
    deps: ArbiterDeps | None = None,
) -> ArbiterOutcome:
    """Called when a human casts a Code-Review on a change the Merger
    has already +2'd.

    ``votes`` is the *full* vote list (merger + humans + other AI bots)
    so the evaluator can answer the submit question authoritatively.
    """
    deps = deps or ArbiterDeps()
    decision = evaluate_submit_rule(votes)

    # Negative from a human → Merger withdraws and change goes WIP.
    if decision.reason is SubmitReason.reject_negative_vote:
        revoke_res = await deps.revoker.revoke(
            commit=commit,
            project=project,
            message=(
                "Merger Agent withdraws +2: human reviewer cast a "
                f"negative score (voters: {', '.join(decision.negative_voters)}). "
                "Change returned to work-in-progress."
            ),
        )
        await deps.notifier.notify(
            kind="change.work_in_progress",
            change_id=change_id,
            payload={
                "project": project,
                "commit": commit,
                "negative_voters": decision.negative_voters,
                "revoke_ok": "error" not in revoke_res,
            },
        )
        # Re-arm the merger for the next patchset on this change.  We
        # clear only this change's strike counter, not the whole
        # registry, so other in-flight merges are unaffected.
        ma._reset_failure(change_id)  # type: ignore[attr-defined]
        # O9 (#272) — drop from awaiting-human dashboard registry.
        try:
            from backend.orchestration_observability import clear_awaiting_human
            clear_awaiting_human(change_id)
        except Exception as exc:                              # pragma: no cover
            logger.debug("arbiter: awaiting-human clear (withdrew) failed: %s", exc)
        return ArbiterOutcome(
            change_id=change_id,
            reason=ArbiterReason.human_disagreed_merger_withdrew,
            detail=(
                f"Human reviewer(s) {', '.join(decision.negative_voters)} "
                f"cast a negative score; merger withdrew its +2 and the "
                f"change is back in work-in-progress."
            ),
            submit_decision=decision.to_dict(),
            metadata={"revoke_response": revoke_res},
        )

    # All gates satisfied → submit.
    if decision.allow:
        submit_res = await deps.submitter.submit(commit=commit, project=project)
        submit_ok = "error" not in submit_res
        # O9 (#272) — change is shipping; drop from awaiting-human registry
        # regardless of submit_ok (a failed submit is operator-visible
        # via the "change.submitted" SSE event with submit_ok=false).
        try:
            from backend.orchestration_observability import clear_awaiting_human
            clear_awaiting_human(change_id)
        except Exception as exc:                              # pragma: no cover
            logger.debug("arbiter: awaiting-human clear (submitted) failed: %s", exc)
        await deps.notifier.notify(
            kind="change.submitted",
            change_id=change_id,
            payload={
                "project": project,
                "commit": commit,
                "submit_ok": submit_ok,
                "decision": decision.to_dict(),
            },
        )
        return ArbiterOutcome(
            change_id=change_id,
            reason=ArbiterReason.submitted,
            detail=(
                f"Dual-+2 satisfied (human +2 × {decision.human_plus_twos}, "
                f"merger +2 × {decision.merger_plus_twos}); submit call "
                f"{'succeeded' if submit_ok else 'failed'}."
            ),
            submit_decision=decision.to_dict(),
            metadata={"submit_response": submit_res},
        )

    # Otherwise still below the gate — emit an awaiting SSE so the UI
    # keeps showing the pending state.
    await deps.notifier.notify(
        kind="change.awaiting_more_votes",
        change_id=change_id,
        payload={
            "project": project,
            "missing": list(decision.missing),
            "decision": decision.to_dict(),
        },
    )
    return ArbiterOutcome(
        change_id=change_id,
        reason=ArbiterReason.human_vote_recorded_below_gate,
        detail=decision.detail,
        submit_decision=decision.to_dict(),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pre-check utility
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def check_change_ready(
    votes: list[ReviewerVote | dict[str, Any]],
) -> SubmitDecision:
    """Return the current submit-rule verdict without side-effects.

    Callers: orchestrator status panel, observability dashboard, CLI.
    """
    return evaluate_submit_rule(votes)


__all__ = [
    "ArbiterDeps",
    "ArbiterOutcome",
    "ArbiterReason",
    "MergeConflictTask",
    "JiraTicketOpener",
    "JiraTicketResult",
    "Notifier",
    "GerritSubmitter",
    "GerritVoteRevoker",
    "check_change_ready",
    "on_human_vote_recorded",
    "on_merge_conflict_webhook",
    "reset_arbiter_state_for_tests",
]
