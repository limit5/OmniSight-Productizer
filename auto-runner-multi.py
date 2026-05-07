#!/usr/bin/env python3
"""auto-runner-multi.py -- JIRA runner with MP provider orchestration.

This is the MP.W3 subscription runner entrypoint.  It keeps the JIRA
pickup / worktree / Gerrit lifecycle from ``auto-runner-jira.py`` and
replaces the single ``OMNISIGHT_RUNNER_CLASS`` invoke path with a small
provider-orchestrated dispatch loop:

1. Fetch pickable tickets for the configured subscription classes.
2. Score all candidates together with ``backend.agents.scheduler``.
3. Prepare the selected class's worktree and JIRA assignment.
4. Dispatch the prompt through ``backend.agents.provider_orchestrator``.
5. If a provider reports a cap/rate-limit failure, retry once at the
   next provider boundary; otherwise preserve the existing fail / review
   transitions.

ENV:
  OMNISIGHT_MULTI_CLASSES    comma-separated class list. Defaults to the
                             MVP subscription classes:
                             subscription-codex,subscription-claude
  OMNISIGHT_RUNNER_TARGET    optional ticket key override
  OMNISIGHT_RUNNER_DRY_RUN   if "1", no transition, invoke, or push
  OMNISIGHT_CODEX_WORKTREE   codex worktree override
  OMNISIGHT_CLAUDE_WORKTREE  claude worktree override
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from backend.agents import jira_dispatch, scheduler  # noqa: E402
from backend.agents import provider_orchestrator  # noqa: E402
from backend.agents.provider_orchestrator import DispatchResult, TaskSpec  # noqa: E402

# Import subscription adapters for their registry side effects.
import backend.agents.provider_adapters.anthropic_subscription  # noqa: E402,F401
import backend.agents.provider_adapters.openai_subscription  # noqa: E402,F401


DEFAULT_CLASSES = ("subscription-codex", "subscription-claude")
TARGET_OVERRIDE = os.environ.get("OMNISIGHT_RUNNER_TARGET", "").strip()
DRY_RUN = os.environ.get("OMNISIGHT_RUNNER_DRY_RUN", "0") == "1"

CODEX_WORKTREE = Path(os.environ.get(
    "OMNISIGHT_CODEX_WORKTREE",
    os.path.normpath(os.path.join(REPO, "..", "OmniSight-codex-worktree")),
))
CLAUDE_WORKTREE = Path(os.environ.get(
    "OMNISIGHT_CLAUDE_WORKTREE",
    os.path.normpath(os.path.join(REPO, "..", "OmniSight-claude-worktree")),
))

CLASS_TO_PROVIDER = {
    "subscription-codex": "openai-subscription",
    "subscription-claude": "anthropic-subscription",
}


@dataclass(frozen=True)
class Candidate:
    """JIRA candidate plus the client/class that can own its transitions."""

    snapshot: scheduler.TicketSnapshot
    client: jira_dispatch.DispatchClient
    agent_class: str


@dataclass(frozen=True)
class MultiDispatchOutcome:
    """Final result of provider-boundary dispatch attempts."""

    result: DispatchResult | None
    attempted_provider_ids: tuple[str, ...]


def _configured_classes() -> tuple[str, ...]:
    raw = os.environ.get("OMNISIGHT_MULTI_CLASSES", "").strip()
    if not raw:
        return DEFAULT_CLASSES
    classes = tuple(part.strip() for part in raw.split(",") if part.strip())
    return classes or DEFAULT_CLASSES


def _worktree_for(agent_class: str) -> Path:
    if agent_class in ("subscription-codex", "api-openai"):
        return CODEX_WORKTREE
    return CLAUDE_WORKTREE


def _build_prompt(client: jira_dispatch.DispatchClient, key: str, description: str) -> str:
    """Construct the agent prompt per JIRA SOP prompt-injection contract."""
    issue = jira_dispatch._request(client, "GET", f"/issue/{key}?fields=summary,labels,components")
    fields = issue["fields"]
    summary = fields.get("summary", "<no summary>")
    labels = fields.get("labels", [])
    components = [c.get("name") for c in fields.get("components", [])]

    declared_areas = sorted(l.split(":", 1)[1] for l in labels if l.startswith("area:"))
    all_areas = ["backend", "frontend", "devops", "tests", "db", "docs", "security", "embedded", "tooling"]
    forbidden_areas = [area for area in all_areas if area not in declared_areas]
    tier = next((l.split(":", 1)[1] for l in labels if l.startswith("tier:")), "M")
    component_label = components[0] if components else next(
        (l.split(":", 1)[1].upper() for l in labels if l.startswith("priority:")),
        "?",
    )

    forbidden_block = "\n  - ".join(forbidden_areas) if forbidden_areas else "(none)"
    return f"""You are working on JIRA ticket {key}.

Component: {component_label}
Areas: {', '.join(declared_areas) or '<none declared>'}
Tier: {tier}

Ticket summary: {summary}

Stay strictly within these boundaries. Do NOT introduce changes to:
  - {forbidden_block}

If you find that completing this ticket requires touching an out-of-area
domain, halt, comment on the ticket, and transition back to TODO with
a discovered-dependency note (per docs/sop/jira-ticket-conventions.md §11).

# Documentation rules (per CLAUDE.md L1, amended 2026-05-06)

DO NOT append to HANDOFF.md — that file is FROZEN as of 2026-05-06.
Future per-ticket resolution notes go into JIRA ticket comments, not
HANDOFF.md. If a generalisable lesson emerged, append a new entry to
docs/sop/lessons-learned.md instead.

# Acceptance Criteria verification (REQUIRED before exit)

Before you finish, post ONE final JIRA comment to ticket {key} listing
each Acceptance Criteria item from the description with ✓ (verified)
or ✗ (skipped/blocked, with reason). Each ✓ MUST cite concrete evidence
— test name, file:line range, or Gerrit Change-Id. Vague evidence ("looks
right", "should work") is auto-rejected by the convention §3 DoD spirit
and will be flagged in retrospective.

Format:

  AC verification for {key}:
  ✓ <AC item 1 paraphrased> — <test_name|file:Lstart-Lend|change-id>
  ✓ <AC item 2 paraphrased> — <evidence>
  ✗ <AC item N paraphrased> — <reason it could not be verified>

Use the runner's transition_back_to_todo or add_comment helpers in
backend/agents/jira_dispatch.py if you need to comment programmatically.

Full ticket description follows:

{description}

When you complete the work, your final commit message must include
[{key}] in the subject line.
"""


def _fetch_target_candidate(ticket_key: str) -> Candidate:
    """Fetch one target ticket using the class label on the issue."""
    # Try all configured credentials; ticket visibility is equivalent in
    # normal operation, but this keeps local credential failures isolated.
    last_error: Exception | None = None
    for agent_class in _configured_classes():
        try:
            client = jira_dispatch.make_client(agent_class)
            issue = jira_dispatch._request(
                client,
                "GET",
                f"/issue/{ticket_key}?fields=summary,labels,status,issuetype,fixVersions,created,components,issuelinks,parent",
            )
            labels = issue["fields"].get("labels", [])
            ticket_class = next(
                (label.split(":", 1)[1] for label in labels if label.startswith("class:")),
                agent_class,
            )
            if ticket_class != agent_class:
                client = jira_dispatch.make_client(ticket_class)
            return Candidate(jira_dispatch.to_snapshot(issue), client, ticket_class)
        except Exception as exc:  # pragma: no cover - exercised by operator env
            last_error = exc
            print(f"[multi-runner] target lookup via {agent_class} failed: {exc}", file=sys.stderr)
    raise RuntimeError(f"could not fetch {ticket_key}: {last_error}")


def _fetch_candidates() -> list[Candidate]:
    candidates: list[Candidate] = []
    for agent_class in _configured_classes():
        try:
            client = jira_dispatch.make_client(agent_class)
            raw_issues = jira_dispatch.fetch_pickable_tickets(client)
        except Exception as exc:  # pragma: no cover - depends on operator creds
            print(f"[multi-runner] skipping {agent_class}: {exc}", file=sys.stderr)
            continue

        for issue in raw_issues:
            candidates.append(Candidate(jira_dispatch.to_snapshot(issue), client, agent_class))
    return candidates


def _select_candidate(candidates: list[Candidate]) -> Candidate | None:
    weights = scheduler.load_weights()
    scored = [(scheduler.score(candidate.snapshot, weights), candidate) for candidate in candidates]
    scored.sort(key=lambda item: item[0], reverse=True)

    winner: Candidate | None = None
    for _, candidate in scored:
        ok, _ = jira_dispatch.pre_pickup_ok(candidate.client, candidate.snapshot)
        if ok:
            winner = candidate
            break

    scheduler.log_dispatch_decision(
        winner.snapshot if winner else None,
        [(score, candidate.snapshot) for score, candidate in scored],
        datetime.utcnow().isoformat(),
    )
    return winner


def _provider_order(agent_class: str) -> tuple[str, ...]:
    preferred = CLASS_TO_PROVIDER.get(agent_class)
    registered = provider_orchestrator.list_adapters()
    if preferred is None:
        return tuple(registered)
    return tuple([preferred] + [provider for provider in registered if provider != preferred])


@contextlib.contextmanager
def _temporary_cwd(path: Path) -> Iterator[None]:
    old_cwd = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old_cwd)


def _is_cap_failure(result: DispatchResult) -> bool:
    if not result.error:
        return False
    try:
        payload = json.loads(result.error)
    except json.JSONDecodeError:
        return "429" in result.error or "rate_limit" in result.error.lower() or "cap" in result.error.lower()
    kind = str(payload.get("kind", "")).lower()
    return kind in {"cap_exceeded", "rate_limit_exceeded"}


def _dispatch_with_orchestrator(
    *,
    prompt: str,
    agent_class: str,
    tier: str,
    areas: list[str],
    ticket_key: str,
    worktree_path: Path,
) -> MultiDispatchOutcome:
    attempted: list[str] = []
    task = TaskSpec(
        prompt=prompt,
        agent_class=agent_class,
        tier=tier,
        area=areas,
        correlation_id=ticket_key,
    )

    with _temporary_cwd(worktree_path):
        for provider_id in _provider_order(agent_class):
            attempted.append(provider_id)
            try:
                adapter = provider_orchestrator.get_adapter(provider_id)
            except provider_orchestrator.ProviderNotRegistered:
                continue

            health = adapter.health_check()
            if not health.reachable:
                print(f"[multi-runner] provider {provider_id} not reachable; trying next")
                continue

            print(f"[multi-runner] dispatching {ticket_key} via {provider_id}")
            try:
                result = adapter.dispatch(task)
            except Exception as exc:
                return MultiDispatchOutcome(
                    DispatchResult(
                        success=False,
                        tokens_used=0,
                        latency_seconds=0.0,
                        error=f"{type(exc).__name__}: {exc}",
                        provider_id=provider_id,
                    ),
                    tuple(attempted),
                )
            if result.success:
                return MultiDispatchOutcome(result, tuple(attempted))
            if _is_cap_failure(result):
                print(f"[multi-runner] provider {provider_id} capped; trying next")
                continue
            return MultiDispatchOutcome(result, tuple(attempted))

    return MultiDispatchOutcome(None, tuple(attempted))


def _labels_for_prompt(client: jira_dispatch.DispatchClient, key: str) -> tuple[str, list[str]]:
    issue = jira_dispatch._request(client, "GET", f"/issue/{key}?fields=labels")
    labels = issue["fields"].get("labels", [])
    tier = next((label.split(":", 1)[1] for label in labels if label.startswith("tier:")), "M")
    areas = sorted(label.split(":", 1)[1] for label in labels if label.startswith("area:"))
    return tier, areas


def _prepare_worktree(
    candidate: Candidate,
    worktree_path: Path,
) -> jira_dispatch.WorktreeSyncResult | None:
    if DRY_RUN:
        print(f"[multi-runner] DRY_RUN: would sync worktree {worktree_path}")
        return None

    print(f"[multi-runner] preparing worktree {worktree_path}...")
    jira_dispatch.set_bot_identity_in_worktree(worktree_path, candidate.agent_class)
    jira_dispatch.install_commit_msg_hook(worktree_path)
    sync_result = jira_dispatch.sync_to_gerrit_develop(
        worktree_path,
        candidate.agent_class,
        candidate.snapshot.key,
    )
    print(f"[multi-runner] worktree synced: {sync_result.detail}")
    return sync_result


def _push_successful_work(
    candidate: Candidate,
    worktree_path: Path,
    sync_result: jira_dispatch.WorktreeSyncResult,
) -> int:
    try:
        jira_dispatch.ensure_change_ids(worktree_path, base_ref=sync_result.develop_sha)
        push_result = jira_dispatch.push_to_gerrit_for_review(
            worktree_path,
            candidate.agent_class,
            target="develop",
        )
    except Exception as exc:
        print(f"[multi-runner] Gerrit push setup failed: {exc}", file=sys.stderr)
        jira_dispatch.add_comment(
            candidate.client,
            candidate.snapshot.key,
            f"[runner-gerrit-setup-fail] Could not prepare Gerrit push:\n"
            f"{type(exc).__name__}: {exc}\n\n"
            f"Operator: review changes in `{worktree_path}`, push manually, then transition Under Review.",
        )
        return 1

    if push_result.success:
        print(f"[multi-runner] pushed Change #{push_result.change_number}: {push_result.change_url}")
        jira_dispatch.transition_to_under_review(
            candidate.client,
            candidate.snapshot.key,
            push_result.change_url,
        )
        return 0

    print(f"[multi-runner] Gerrit push failed:\n{push_result.detail}", file=sys.stderr)
    jira_dispatch.add_comment(
        candidate.client,
        candidate.snapshot.key,
        f"[runner-gerrit-push-fail] Gerrit rejected push:\n```\n{push_result.detail}\n```\n\n"
        f"Operator: review + push manually.",
    )
    return 1


def main() -> int:
    print(f"[multi-runner] classes={','.join(_configured_classes())}, dry_run={DRY_RUN}")
    if TARGET_OVERRIDE:
        candidate = _fetch_target_candidate(TARGET_OVERRIDE)
    else:
        candidates = _fetch_candidates()
        if not candidates:
            print("[multi-runner] no pickable tickets -- idling")
            return 0
        candidate = _select_candidate(candidates)
        if candidate is None:
            print("[multi-runner] no candidate passed pre-pickup checks")
            return 0

    ticket_key = candidate.snapshot.key
    print(f"[multi-runner] selected: {ticket_key} ({candidate.agent_class})")

    worktree_path = _worktree_for(candidate.agent_class)
    if not DRY_RUN and not worktree_path.is_dir():
        print(f"[multi-runner] worktree missing: {worktree_path}", file=sys.stderr)
        return 2

    try:
        sync_result = _prepare_worktree(candidate, worktree_path)
    except Exception as exc:
        print(f"[multi-runner] worktree pre-sync failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        jira_dispatch.add_comment(
            candidate.client,
            ticket_key,
            f"[runner-presync-fail] Could not prepare worktree:\n"
            f"{type(exc).__name__}: {exc}\n\n"
            f"Operator: ensure {worktree_path} is a valid git worktree + Gerrit is reachable, then re-launch.",
        )
        return 1

    ok, reason = jira_dispatch.pre_pickup_ok(
        candidate.client,
        candidate.snapshot,
        worktree_path=None if DRY_RUN else worktree_path,
    )
    if not ok:
        print(f"[multi-runner] pre-pickup fail: {reason}")
        if not DRY_RUN:
            jira_dispatch.add_comment(
                candidate.client,
                ticket_key,
                "[runner-live-state-fail]\n\n"
                "Pre-pickup live-state check failed; not picking up.\n\n"
                f"{reason}\n\nThis ticket will be retried on next polling cycle.",
            )
        return 1

    description = jira_dispatch.fetch_description(candidate.client, ticket_key)
    prompt = _build_prompt(candidate.client, ticket_key, description)
    tier, areas = _labels_for_prompt(candidate.client, ticket_key)

    if DRY_RUN:
        providers = ", ".join(_provider_order(candidate.agent_class))
        print(f"[multi-runner] DRY_RUN: would transition {ticket_key} -> In Progress")
        print(f"[multi-runner] DRY_RUN: provider order: {providers}")
        print(f"[multi-runner] DRY_RUN: prompt preview ({len(prompt)} chars):\n---\n{prompt[:1200]}\n---")
        return 0

    print(f"[multi-runner] transitioning {ticket_key} -> In Progress")
    jira_dispatch.transition_to_in_progress(candidate.client, ticket_key)

    outcome = _dispatch_with_orchestrator(
        prompt=prompt,
        agent_class=candidate.agent_class,
        tier=tier,
        areas=areas,
        ticket_key=ticket_key,
        worktree_path=worktree_path,
    )
    if outcome.result is None:
        reason = f"No reachable provider in order: {', '.join(outcome.attempted_provider_ids) or '<none>'}"
        print(f"[multi-runner] {reason}", file=sys.stderr)
        jira_dispatch.transition_back_to_todo(candidate.client, ticket_key, reason)
        return 1

    result = outcome.result
    jira_dispatch.add_comment(
        candidate.client,
        ticket_key,
        "[runner-provider-dispatch]\n"
        f"provider={result.provider_id}\n"
        f"success={result.success}\n"
        f"tokens_used={result.tokens_used}\n"
        f"latency_seconds={result.latency_seconds:.2f}\n"
        f"attempted={', '.join(outcome.attempted_provider_ids)}",
    )

    if result.success:
        if sync_result is None:
            raise RuntimeError("sync_result missing outside dry-run")
        return _push_successful_work(candidate, worktree_path, sync_result)

    reason = result.error or f"provider {result.provider_id} failed"
    print(f"[multi-runner] {ticket_key} provider failed: {reason}", file=sys.stderr)
    jira_dispatch.transition_back_to_todo(candidate.client, ticket_key, reason)
    return 1


if __name__ == "__main__":
    sys.exit(main())
