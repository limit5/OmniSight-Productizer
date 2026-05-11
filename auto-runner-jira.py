#!/usr/bin/env python3
"""auto-runner-jira.py — JIRA-driven runner (replaces TODO.md scan).

Per ``docs/sop/jira-ticket-conventions.md`` §16. Generic dispatch loop:

1. Fetch pickable tickets via JQL (filtered by agent_class).
2. Score via backend.agents.scheduler.
3. Pre-pickup check via backend.agents.jira_dispatch.pre_pickup_ok
   (live-state + future mutex + future blocker checks).
4. Transition TODO → In Progress, set assignee, add pickup comment.
5. Build prompt from ticket description + fetch fresh repo state.
6. Invoke CLI (codex / claude) per agent_class.
7. On success: prompt operator to push commits + transition →
   Under Review (this MVP doesn't auto-push; that's step 3 polish).
8. On failure: revert ticket to TODO with comment.

ENV:
  OMNISIGHT_RUNNER_CLASS       agent_class label, e.g. "subscription-codex"
                               (defaults to subscription-codex)
  OMNISIGHT_RUNNER_INSTANCE_ID horizontal-scaling instance ID (OP-783).
                               "default" = legacy single-instance setup
                               (codex-bot / claude-bot creds + state).
                               "2", "3", ... = per-instance bot accounts
                               (codex-bot-2, claude-bot-3, ...).
  OMNISIGHT_RUNNER_TARGET      optional ticket key override (skip scheduler,
                               pickup specific ticket — for testing)
  OMNISIGHT_RUNNER_DRY_RUN     if "1", do everything except transition + invoke
  OMNISIGHT_CODEX_WORKTREE     codex worktree override (defaults to
                               ../OmniSight-codex-worktree for default
                               instance; ../OmniSight-<bot>-worktree for
                               non-default instances).
  OMNISIGHT_CLAUDE_WORKTREE    claude worktree override (same shape).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from backend.agents import (
    circuit_breaker,
    jira_dispatch,
    orphan_salvage,
    runner_failure_classifier,
    runner_workspace_safety,
    scheduler,
)

AGENT_CLASS = os.environ.get("OMNISIGHT_RUNNER_CLASS", "subscription-codex")
INSTANCE_ID = os.environ.get("OMNISIGHT_RUNNER_INSTANCE_ID", "").strip() or "default"
TARGET_OVERRIDE = os.environ.get("OMNISIGHT_RUNNER_TARGET", "").strip()
DRY_RUN = os.environ.get("OMNISIGHT_RUNNER_DRY_RUN", "0") == "1"

# Recognised JIRA `area:<X>` label values. Exported so other tooling
# (seed scripts, label linters) can introspect the exact same set used
# by the prompt-builder. Drift between this and the seed-script copy is
# asserted by backend/tests/test_auto_runner_prompt_builder.py.
RECOGNISED_AREAS: frozenset[str] = frozenset({
    "backend", "frontend", "devops", "tests", "db",
    "docs", "security", "embedded", "tooling",
})


class UnknownAreaLabelError(ValueError):
    """Raised when a ticket carries an `area:<X>` label not in RECOGNISED_AREAS.

    Why typed: replaces the silent forbid-all behaviour that wedged OP-829 in a
    5-iteration self-revert loop (see OP-832 post-mortem). The runner traps this
    and reverts the ticket so a fresh pickup with corrected labels can succeed.
    """

    def __init__(self, unknown: list[str], recognised: frozenset[str]) -> None:
        self.unknown: list[str] = sorted(unknown)
        self.recognised: list[str] = sorted(recognised)
        super().__init__(
            f"Unknown area label(s): {self.unknown}. "
            f"Recognised areas: {self.recognised}."
        )


def _bot_username() -> str:
    """Resolve the per-instance bot username for this runner process."""
    return jira_dispatch.resolve_bot_username(AGENT_CLASS, INSTANCE_ID)


def _default_worktree_for(agent_class: str) -> str:
    """Compute the per-instance default worktree path.

    Default instance preserves the legacy paths used by docs and tmux
    sessions (``OmniSight-codex-worktree`` / ``OmniSight-claude-worktree``);
    non-default instances use a bot-keyed sibling directory
    (``OmniSight-codex-bot-2-worktree`` etc.) so two runner processes
    never trample each other's working tree.
    """
    if INSTANCE_ID == "default":
        suffix = "codex-worktree" if agent_class in ("subscription-codex", "api-openai") else "claude-worktree"
        return os.path.normpath(os.path.join(REPO, "..", f"OmniSight-{suffix}"))
    bot = jira_dispatch.resolve_bot_username(agent_class, INSTANCE_ID)
    return os.path.normpath(os.path.join(REPO, "..", f"OmniSight-{bot}-worktree"))


def _file_mutex_skip_comment(reason: str) -> str:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"[runner-file-mutex] {stamp}\n\nSkipped - {reason}. Will retry next tick."


def _dependency_skip_comment(reason: str) -> str:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"[runner-dependency-blocked] {stamp}\n\nSkipped - {reason}. Will retry next tick."


def _blocked_by_key(reason: str) -> str | None:
    if not reason.startswith("blocked-by:"):
        return None
    return reason.split(None, 1)[0].split(":", 1)[1]


def _add_dependency_waiting_marker(
    client: jira_dispatch.DispatchClient,
    snapshot: scheduler.TicketSnapshot,
    reason: str,
) -> None:
    blocker_key = _blocked_by_key(reason)
    if not blocker_key:
        return
    jira_dispatch.add_label(
        client,
        snapshot.key,
        jira_dispatch.dependency_waiting_label(blocker_key),
    )
    jira_dispatch.add_comment(client, snapshot.key, _dependency_skip_comment(reason))


def _clear_dependency_waiting_markers(
    client: jira_dispatch.DispatchClient,
    snapshot: scheduler.TicketSnapshot,
) -> None:
    for label in jira_dispatch.dependency_waiting_labels(getattr(snapshot, "labels", ())):
        jira_dispatch.remove_label(client, snapshot.key, label)


def already_merged_in_gerrit(ticket_key: str) -> tuple[int, str] | None:
    """Return merged Gerrit change metadata for ``ticket_key``, if any.

    Fail-open by design: a Gerrit/network problem must not block legitimate
    pickup. Strict subject-prefix matching avoids body-only false positives.
    """
    try:
        user, ssh_key = jira_dispatch._gerrit_auth_for_instance(AGENT_CLASS, INSTANCE_ID)
    except ValueError:
        user, ssh_key = jira_dispatch._GERRIT_AUTH_BY_CLASS["subscription-claude"]
    cmd = [
        "ssh", "-i", str(ssh_key), "-p", str(jira_dispatch.GERRIT_SSH_PORT),
        f"{user}@{jira_dispatch.GERRIT_SSH_HOST}",
        "gerrit", "query", "--format=JSON",
        f"message:{ticket_key} status:merged",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            f"[runner] H12: Gerrit merged-check failed open for {ticket_key}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None
    if result.returncode != 0:
        print(
            f"[runner] H12: Gerrit merged-check failed open for {ticket_key}: "
            f"rc={result.returncode}: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return None

    for line in result.stdout.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") == "stats":
            continue
        if not str(data.get("subject", "")).startswith(f"[{ticket_key}]"):
            continue
        try:
            return int(data["number"]), str(data["url"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _check_pre_pickup_candidate(
    client: jira_dispatch.DispatchClient,
    snapshot: scheduler.TicketSnapshot,
    stats: dict[str, int] | None = None,
) -> bool:
    """Runner selection predicate: existing pre-pickup gate + OP-731 file mutex."""
    ok, reason = jira_dispatch.pre_pickup_ok(client, snapshot)
    if not ok:
        if stats is not None:
            stats["other_blocked"] = stats.get("other_blocked", 0) + 1
        if not DRY_RUN and _blocked_by_key(reason):
            _add_dependency_waiting_marker(client, snapshot, reason)
        return False
    if not DRY_RUN:
        _clear_dependency_waiting_markers(client, snapshot)

    description = jira_dispatch.fetch_description(client, snapshot.key)
    ok, reason = jira_dispatch.file_mutex_check(snapshot, description=description)
    if ok:
        if not DRY_RUN:
            jira_dispatch.remove_label(
                client, snapshot.key, jira_dispatch.FILE_COLLISION_SKIP_LABEL
            )
        return True

    if stats is not None:
        stats["file_mutex_blocked"] = stats.get("file_mutex_blocked", 0) + 1
    print(f"[runner] runner.pickup_blocked_file_mutex {snapshot.key}: {reason}")
    if not DRY_RUN:
        jira_dispatch.add_label(client, snapshot.key, jira_dispatch.FILE_COLLISION_SKIP_LABEL)
        jira_dispatch.add_comment(client, snapshot.key, _file_mutex_skip_comment(reason))
    return False


def _build_prompt(client: jira_dispatch.DispatchClient, key: str, description: str) -> str:
    """Construct the agent prompt per §5 prompt-injection contract."""
    issue = jira_dispatch._request(client, "GET", f"/issue/{key}?fields=summary,labels,components")
    f = issue["fields"]
    summary = f.get("summary", "<no summary>")
    labels = f.get("labels", [])
    components = [c.get("name") for c in f.get("components", [])]

    declared_areas = sorted(l.split(":", 1)[1] for l in labels if l.startswith("area:"))
    unknown_areas = [a for a in declared_areas if a not in RECOGNISED_AREAS]
    if unknown_areas:
        raise UnknownAreaLabelError(unknown_areas, RECOGNISED_AREAS)
    all_areas = sorted(RECOGNISED_AREAS)
    forbidden_areas = [a for a in all_areas if a not in declared_areas]
    tier = next((l.split(":", 1)[1] for l in labels if l.startswith("tier:")), "M")
    component_label = components[0] if components else next(
        (l.split(":", 1)[1].upper() for l in labels if l.startswith("priority:")), "?"
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

For programmatic JIRA writes use ONLY these helpers from
`backend/agents/jira_dispatch.py`:

  - `add_comment(client, key, text)` — post the AC verification comment
    (and any other operator-facing notes).
  - `transition_back_to_todo(client, key, reason)` — only when you discover
    an out-of-area dependency and need to surrender the ticket per §11.

⚠ DO NOT call `transition_to_under_review` (or any other `transition_*`
helper that moves the ticket forward) yourself. The runner owns forward
transitions — after your CLI exits cleanly, the runner pushes to Gerrit
and transitions the ticket to Under Review on your behalf. If you do it
yourself you race the runner and create a duplicate
`[runner-pushed-to-gerrit]` comment + a misleading "runner failed"
signal even when the work shipped (OP-690 incident, 2026-05-07).

Full ticket description follows:

{description}

When you complete the work, your final commit message must include
[{key}] in the subject line.
"""


CODEX_WORKTREE = os.environ.get(
    "OMNISIGHT_CODEX_WORKTREE",
    _default_worktree_for("subscription-codex"),
)
CLAUDE_WORKTREE = os.environ.get(
    "OMNISIGHT_CLAUDE_WORKTREE",
    _default_worktree_for("subscription-claude"),
)
TASK_TIMEOUT_S = int(os.environ.get("OMNISIGHT_RUNNER_TIMEOUT_S", "1800"))


def _invoke_cli(agent_class: str, prompt: str, failure_context: str | None = None) -> int:
    """Invoke the underlying CLI for this agent_class. Returns exit code."""
    full_prompt = prompt
    if failure_context:
        full_prompt = f"{prompt.rstrip()}\n\n{failure_context.strip()}\n"

    cwd: str | None = None
    if agent_class == "subscription-codex":
        if not os.path.isdir(CODEX_WORKTREE):
            print(f"[runner] codex worktree missing: {CODEX_WORKTREE}", file=sys.stderr)
            return 2
        cmd = ["codex", "exec", "--cd", CODEX_WORKTREE, "--yolo"]
    elif agent_class == "subscription-claude":
        if not os.path.isdir(CLAUDE_WORKTREE):
            print(f"[runner] claude worktree missing: {CLAUDE_WORKTREE}", file=sys.stderr)
            return 2
        cmd = ["claude", "--dangerously-skip-permissions", "-p", full_prompt]
        # OP-795 Bug 3: claude CLI doesn't take a --cd flag, so pin its cwd to
        # the worktree via subprocess.Popen — otherwise it inherits the
        # runner's cwd (main repo) and commits land outside the worktree,
        # causing "no new changes" rejections at push time.
        cwd = CLAUDE_WORKTREE
    elif agent_class.startswith("api-"):
        print(f"[runner] agent_class={agent_class} requires SDK invocation, not CLI. Skipping invoke.")
        return 99
    else:
        print(f"[runner] unknown agent_class: {agent_class}", file=sys.stderr)
        return 2

    if DRY_RUN:
        print(f"[runner] DRY_RUN: would invoke `{' '.join(cmd[:3])}...` with {len(full_prompt)} char prompt")
        return 0

    print(f"[runner] invoking {cmd[0]} (timeout {TASK_TIMEOUT_S}s)...")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.PIPE if cmd[0] == "codex" else None,
            stdout=sys.stdout,
            stderr=sys.stderr,
            text=True,
            start_new_session=True,
        )
        if cmd[0] == "codex":
            proc.communicate(input=full_prompt, timeout=TASK_TIMEOUT_S)
        else:
            proc.communicate(timeout=TASK_TIMEOUT_S)
        return proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        print(f"[runner] CLI timed out after {TASK_TIMEOUT_S}s", file=sys.stderr)
        return 124
    except FileNotFoundError as e:
        print(f"[runner] CLI not installed: {e}", file=sys.stderr)
        return 127


def _finalize_under_review(
    client: "jira_dispatch.DispatchClient",
    key: str,
    gerrit_change_url: str,
) -> None:
    """Phase 1.5 post-push idempotency block (OP-691).

    Reads ticket status BEFORE attempting the transition. If the agent
    already transitioned the ticket itself (the OP-690 incident pattern),
    skips both the runner-pushed comment and the transition POST so the
    audit trail stays clean. If the transition POST fails for any other
    reason, logs a ``[runner-transition-skip]`` comment instead of raising
    — the Gerrit push already succeeded so the ticket is shippable, and
    a JIRA cosmetic failure should not turn the runner exit non-zero.
    """
    try:
        status = jira_dispatch.get_issue_status(client, key)
    except Exception as e:  # noqa: BLE001 — JIRA read failure → degrade to legacy path
        print(f"[runner] could not read {key} status ({type(e).__name__}: {e}); "
              f"attempting transition anyway", file=sys.stderr)
        status = ""

    if status == jira_dispatch.UNDER_REVIEW_STATUS_NAME:
        print(f"[runner] {key} already in Under Review (agent transitioned itself); "
              f"skipping duplicate comment + transition")
        return

    try:
        jira_dispatch.post_runner_pushed_comment(client, key, gerrit_change_url)
        transitioned = jira_dispatch.transition_to_under_review_if_needed(client, key)
    except Exception as e:  # noqa: BLE001 — any 4xx etc → log skip, do not crash
        print(f"[runner] transition to Under Review failed ({type(e).__name__}: {e}); "
              f"Gerrit push already succeeded — logging skip comment", file=sys.stderr)
        try:
            jira_dispatch.add_comment(
                client, key,
                f"[runner-transition-skip] Could not transition to Under Review:\n"
                f"{type(e).__name__}: {e}\n\n"
                f"Gerrit push succeeded: {gerrit_change_url}\n\n"
                f"Operator: inspect ticket workflow state and transition manually if needed.",
            )
        except Exception as inner:  # noqa: BLE001 — comment-post failure is informational
            print(f"[runner] could not even post skip comment: {inner}", file=sys.stderr)
        return

    if transitioned:
        print(f"[runner] {key} → Under Review. Reviewer: +2 in Gerrit UI.")
    else:
        print(f"[runner] {key} already Under Review at transition step (raced); "
              f"comment posted, transition skipped")


def _handle_gerrit_push_failure(
    client: "jira_dispatch.DispatchClient",
    key: str,
    detail: str,
    agent_class: str = AGENT_CLASS,
) -> tuple[str, str]:
    """Classify a Gerrit push failure and apply the JIRA recovery action."""

    category, action = runner_failure_classifier.categorize_push_failure(detail)

    if action == "force-publish":
        merged_info = jira_dispatch.already_merged_in_gerrit(
            key, agent_class=agent_class, instance_id=INSTANCE_ID
        )
        if merged_info:
            jira_dispatch.force_walk_to_published(client, key)
            jira_dispatch.add_comment(
                client,
                key,
                (
                    f"[runner-push-fail:{category}] work already merged via "
                    f"#{merged_info.change_number}; auto-walked ticket to 公開済み."
                ),
            )
        elif category == "missing_tree":
            # OP-771 race: codex CLI's internal push may have just landed and
            # the merge event hasn't propagated to `gerrit query` yet (or the
            # PS is queued for submit). Don't revert — the bridge daemon will
            # walk the ticket forward when the merge lands. Reverting here
            # would re-queue the work and produce a duplicate PS.
            jira_dispatch.add_comment(
                client,
                key,
                (
                    f"[runner-push-fail:{category}] secondary push hit "
                    f"'Missing tree' but no merged PS found via query (timing "
                    f"race with submit, or repack mid-tick). Ticket left in "
                    f"current state; bridge daemon should walk forward when "
                    f"the CLI's internal-push PS lands. Operator: verify "
                    f"Gerrit if the ticket sticks."
                ),
            )
        else:
            jira_dispatch.transition_back_to_todo(
                client,
                key,
                "CLI produced no committable changes; reverting for re-pickup.",
            )
        return category, action

    if action == "revert":
        jira_dispatch.transition_back_to_todo(
            client,
            key,
            f"[runner-push-fail:{category}] {detail[:200]}",
        )
        return category, action

    if action == "retry":
        jira_dispatch.add_comment(
            client,
            key,
            (
                f"[runner-push-fail:transient] {category}; immediate Gerrit "
                f"push retries exhausted. Ticket left in current state for "
                f"human triage.\n\n{detail[:500]}"
            ),
        )
        return category, action

    jira_dispatch.add_comment(
        client,
        key,
        f"[runner-push-fail:unknown] Manual review needed:\n{detail[:500]}",
    )
    jira_dispatch.add_label(client, key, "runner-loop-paused-pending-review")
    return category, action


def main() -> int:
    print(
        f"[runner] agent_class={AGENT_CLASS}, instance_id={INSTANCE_ID}, "
        f"bot={_bot_username()}, dry_run={DRY_RUN}"
    )
    open_services = circuit_breaker.open_services()
    if open_services:
        print(f"[runner] paused - {open_services} unreachable")
        return 0
    jira_dispatch.assert_worktree_config_enabled(REPO)

    worktree_path = Path(
        CODEX_WORKTREE if AGENT_CLASS in ("subscription-codex", "api-openai")
        else CLAUDE_WORKTREE
    )
    if DRY_RUN:
        print(f"[runner] DRY_RUN: would scan orphan commits in {worktree_path}")
    else:
        salvaged = orphan_salvage.salvage_orphan_commits(worktree_path, AGENT_CLASS)
        if salvaged:
            print(f"[runner] salvaged {salvaged} orphan commits before starting tick")

    ok_to_pick_up, backpressure_reason = jira_dispatch.backpressure_decide(
        AGENT_CLASS, INSTANCE_ID
    )
    if not ok_to_pick_up:
        print(f"[runner] backpressure paused: {backpressure_reason}. Sleeping until next tick.")
        return 0
    if backpressure_reason.startswith("resumed"):
        print(f"[runner] backpressure {backpressure_reason}")

    client = jira_dispatch.make_client(AGENT_CLASS, INSTANCE_ID)
    print(f"[runner] authenticated as {client.bot_email} ({client.bot_account_id})")

    # Step 1: ticket selection
    if TARGET_OVERRIDE:
        print(f"[runner] target override: {TARGET_OVERRIDE}")
        # Fetch single ticket instead of running JQL
        issue = jira_dispatch._request(client, "GET", f"/issue/{TARGET_OVERRIDE}")
        snapshot = jira_dispatch.to_snapshot(issue)
    else:
        candidates_raw = jira_dispatch.fetch_pickable_tickets(client)
        if not candidates_raw:
            print("[runner] no pickable tickets — idling")
            return 0
        snapshots = [jira_dispatch.to_snapshot(i) for i in candidates_raw]
        weights = scheduler.load_weights()
        pickup_stats: dict[str, int] = {"file_mutex_blocked": 0, "other_blocked": 0}
        winner = scheduler.dispatch(
            snapshots, weights,
            pre_pickup_check=lambda t: _check_pre_pickup_candidate(client, t, pickup_stats),
        )
        if winner is None:
            if pickup_stats["file_mutex_blocked"] and not pickup_stats["other_blocked"]:
                print("[runner] all candidates blocked by file-mutex")
            else:
                print("[runner] no candidate passed pre-pickup checks")
            return 0
        snapshot = winner

    print(f"[runner] selected: {snapshot.key} (component={snapshot.component})")

    merged_info = already_merged_in_gerrit(snapshot.key)
    if merged_info:
        change_number, change_url = merged_info
        print(
            f"[runner] H12: {snapshot.key} already merged via Gerrit "
            f"#{change_number}; auto-walking ticket to 公開済み"
        )
        if not DRY_RUN:
            transitioned = jira_dispatch.force_walk_to_published(client, snapshot.key)
            if transitioned:
                jira_dispatch.add_comment(
                    client,
                    snapshot.key,
                    (
                        f"[runner-h12-self-heal] Detected merged Gerrit change "
                        f"#{change_number} ({change_url}) before pickup. "
                        "Auto-walked ticket to 公開済み. "
                        "(Bridge daemon may have missed the change-merged event; "
                        "OP-743 fix addresses bridge side; H12 is independent "
                        "self-heal.)"
                    ),
                )
        return 0

    # Step 2 (was Step 3 in Phase 1.5): sync worktree FIRST so pre-pickup checks
    # see the actual workspace state, not stale runner-host main repo state.
    # Per L17 — operator's request to refactor pre_pickup_ok cwd.
    if DRY_RUN:
        print(f"[runner] DRY_RUN: would sync worktree {worktree_path}")
    else:
        try:
            print(f"[runner] preparing worktree {worktree_path}...")
            jira_dispatch.set_bot_identity_in_worktree(
                worktree_path, AGENT_CLASS, INSTANCE_ID
            )
            jira_dispatch.install_commit_msg_hook(worktree_path)
            sync_result = jira_dispatch.sync_to_gerrit_develop(
                worktree_path, AGENT_CLASS, snapshot.key, INSTANCE_ID
            )
            print(f"[runner] worktree synced: {sync_result.detail}")
        except Exception as e:
            print(f"[runner] worktree pre-sync failed: {type(e).__name__}: {e}", file=sys.stderr)
            jira_dispatch.add_comment(
                client, snapshot.key,
                f"[runner-presync-fail] Could not prepare worktree:\n"
                f"{type(e).__name__}: {e}\n\n"
                f"Operator: ensure {worktree_path} is a valid git worktree + "
                f"Gerrit is reachable, then re-launch.",
            )
            return 1

    # Step 3: pre-pickup check now runs against fresh worktree, not stale main repo.
    # OP-687: a "mutex conflict" reason means a sibling ticket holds the same
    # mutex:<path>; the dispatch loop already skipped to this candidate, so a
    # failure here means a race (state changed between selection and re-check).
    # Comment so the cron polling cycle can retry; dependency waits are a
    # normal skip outcome, while live-state/mutex races remain failures.
    ok, reason = jira_dispatch.pre_pickup_ok(
        client, snapshot,
        worktree_path=None if DRY_RUN else worktree_path,
    )
    if not ok:
        is_mutex = reason.startswith("mutex conflict")
        blocker_key = _blocked_by_key(reason)
        tag = (
            "runner-mutex-blocked"
            if is_mutex
            else "runner-dependency-blocked"
            if blocker_key
            else "runner-live-state-fail"
        )
        if is_mutex:
            print(f"[runner] runner.pickup_blocked_mutex {snapshot.key}: {reason}")
        else:
            print(f"[runner] pre-pickup fail: {reason}")
        if not DRY_RUN:
            if blocker_key:
                jira_dispatch.add_label(
                    client,
                    snapshot.key,
                    jira_dispatch.dependency_waiting_label(blocker_key),
                )
            jira_dispatch.add_comment(
                client, snapshot.key,
                f"[{tag}]\n\nPre-pickup gate failed; ticket not picked up.\n\n{reason}\n\nThis ticket will be retried on next polling cycle.",
            )
        return 0 if blocker_key else 1
    if not DRY_RUN:
        _clear_dependency_waiting_markers(client, snapshot)

    description = jira_dispatch.fetch_description(client, snapshot.key)
    ok, reason = jira_dispatch.file_mutex_check(snapshot, description=description)
    if not ok:
        print(f"[runner] runner.pickup_blocked_file_mutex {snapshot.key}: {reason}")
        if not DRY_RUN:
            jira_dispatch.add_label(client, snapshot.key, jira_dispatch.FILE_COLLISION_SKIP_LABEL)
            jira_dispatch.add_comment(client, snapshot.key, _file_mutex_skip_comment(reason))
        return 0
    if not DRY_RUN:
        jira_dispatch.remove_label(client, snapshot.key, jira_dispatch.FILE_COLLISION_SKIP_LABEL)

    # Step 4: build prompt + transition + invoke
    try:
        prompt = _build_prompt(client, snapshot.key, description)
    except UnknownAreaLabelError as e:
        # OP-832: bad area label → don't construct a wedge prompt. Revert so
        # a fresh pickup with operator-corrected labels can succeed.
        print(f"[runner] bad area label on {snapshot.key}: {e}", file=sys.stderr)
        if not DRY_RUN:
            jira_dispatch.add_comment(
                client, snapshot.key,
                f"[runner-bad-area-label]\n\n"
                f"Unknown `area:` label(s): {e.unknown}\n"
                f"Recognised areas: {e.recognised}\n\n"
                f"Operator: replace the unknown label(s) with one or more recognised "
                f"areas (or extend RECOGNISED_AREAS in `auto-runner-jira.py`), then "
                f"the next pickup will succeed.",
            )
            jira_dispatch.transition_back_to_todo(
                client, snapshot.key,
                f"Unknown area label(s) {e.unknown}; awaiting operator label correction.",
            )
        return 1

    if DRY_RUN:
        print(f"[runner] DRY_RUN: would transition {snapshot.key} → In Progress")
        print(f"[runner] DRY_RUN: prompt preview ({len(prompt)} chars):\n---\n{prompt[:1200]}\n---")
        return 0

    # OP-836 L1 prevention: refuse to launch the CLI if the main repo is
    # writable to the launching user (would let the CLI commit into the wrong
    # tree, the OP-811/813/832/835 wedge family). No-op unless
    # OMNISIGHT_RUNNER_CWD_ENFORCE is set so dev environments aren't broken.
    try:
        runner_workspace_safety.assert_main_repo_unwritable_for_cli(worktree_path)
    except runner_workspace_safety.MainRepoWritableInLaunchEnvError as e:
        print(f"[runner] cwd-unsafe for {snapshot.key}: {e}", file=sys.stderr)
        jira_dispatch.add_comment(
            client, snapshot.key,
            f"[runner-cwd-unsafe]\n\nLaunch refused: {e}\n\n"
            f"This guard (OP-836) prevents the OP-811/813/832/835 wedge "
            f"family where the CLI commits into the main repo instead of the "
            f"assigned worktree.",
        )
        jira_dispatch.transition_back_to_todo(
            client, snapshot.key,
            "[runner-cwd-unsafe] Launch refused; main repo writable.",
        )
        return 1

    # OP-836 sentinel — stamps worktree pre-launch so we can detect post-CLI
    # tamper (CLI deleted it, reset HEAD to non-descendant SHA, swapped
    # branches). Layered with the perm-isolation check above.
    sentinel_path = runner_workspace_safety.write_workspace_sentinel(
        worktree_path, snapshot.key,
    )

    print(f"[runner] transitioning {snapshot.key} → In Progress")
    jira_dispatch.transition_to_in_progress(client, snapshot.key)

    rc = _invoke_cli(AGENT_CLASS, prompt)

    # OP-836 post-CLI verify — abort the Gerrit-push pipeline if the CLI
    # tampered with the worktree.
    try:
        runner_workspace_safety.verify_workspace_sentinel(sentinel_path, worktree_path)
    except runner_workspace_safety.WorkspaceTamperedError as e:
        print(f"[runner] workspace tampered for {snapshot.key}: {e}", file=sys.stderr)
        jira_dispatch.add_comment(
            client, snapshot.key,
            f"[runner-workspace-tampered]\n\n{e}\n\n"
            f"Reverting to To Do; operator should investigate the CLI's "
            f"workspace mutations before re-pickup.",
        )
        try:
            jira_dispatch.transition_back_to_todo(
                client, snapshot.key,
                f"[runner-workspace-tampered] {e}",
            )
        except Exception as revert_err:
            print(f"[runner] revert-to-TODO also failed: {revert_err}", file=sys.stderr)
        return 1
    if rc == 0:
        # Phase 1 of OP-247: auto-push to Gerrit + transition Under Review.
        # Phase 3 SHIPPED in OP-689; events-stream consumer:
        # backend/agents/gerrit_jira_bridge.py.
        try:
            print(f"[runner] {snapshot.key} CLI returned 0; preparing Gerrit push...")
            # ensure_change_ids rebases onto sync_result.develop_sha (Phase 1.5 fix per L16),
            # not local main; codex's commits get Change-Id via commit-msg hook.
            jira_dispatch.ensure_change_ids(worktree_path, base_ref=sync_result.develop_sha)
            push_result = jira_dispatch.push_to_gerrit_for_review(
                worktree_path, AGENT_CLASS, target="develop", instance_id=INSTANCE_ID
            )
        except jira_dispatch.NoCommitsOnBranchError as e:
            # OP-827 fix: claude/codex CLI exited without committing. Posting AC
            # and exiting bypasses the commit, so there is nothing to push and
            # the right move is to revert to To Do for fresh re-pickup. Leaving
            # the ticket as In Progress here was the OP-811/OP-813 wedge that
            # ran for 2.5 days.
            print(f"[runner] CLI produced no commits: {e}", file=sys.stderr)
            jira_dispatch.add_comment(
                client, snapshot.key,
                f"[runner-no-commits-from-cli] CLI exited cleanly but produced "
                f"0 commits between {e.base_ref[:12]}..{e.head[:12]}. "
                f"Reverting to To Do for re-pickup (post-mortem: OP-827).",
            )
            try:
                jira_dispatch.transition_back_to_todo(
                    client, snapshot.key,
                    "[runner-no-commits-from-cli] CLI exited without committing.",
                )
            except Exception as revert_err:
                print(f"[runner] revert-to-TODO also failed: {revert_err}", file=sys.stderr)
            return 1
        except jira_dispatch.WorktreeDirtyError as e:
            # OP-827 fix: CLI wrote files but never committed (or skipped
            # ``git add``). Same wedge class as NoCommitsOnBranchError; the
            # recovery path is identical (revert + re-pickup).
            print(f"[runner] CLI left worktree dirty: {e}", file=sys.stderr)
            jira_dispatch.add_comment(
                client, snapshot.key,
                f"[runner-dirty-worktree] CLI exited with {len(e.dirty_files)} "
                f"uncommitted path(s); rebase cannot proceed. First few: "
                f"`{e.dirty_files[:5]}`. Reverting to To Do for re-pickup "
                f"(post-mortem: OP-827).",
            )
            try:
                jira_dispatch.transition_back_to_todo(
                    client, snapshot.key,
                    "[runner-dirty-worktree] CLI exited without committing.",
                )
            except Exception as revert_err:
                print(f"[runner] revert-to-TODO also failed: {revert_err}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"[runner] Gerrit push setup failed: {e}", file=sys.stderr)
            jira_dispatch.add_comment(
                client, snapshot.key,
                f"[runner-gerrit-setup-fail] Could not prepare Gerrit push:\n{type(e).__name__}: {e}\n\n"
                f"Operator: review changes in `{worktree_path}`, push manually, then transition Under Review.",
            )
            return 1

        if push_result.success:
            print(f"[runner] pushed Change #{push_result.change_number}: {push_result.change_url}")
            if push_result.recovery_note:
                jira_dispatch.add_comment(
                    client,
                    snapshot.key,
                    f"[runner-gerrit-push-recovered] {push_result.recovery_note}",
                )
            _finalize_under_review(client, snapshot.key, push_result.change_url)
        else:
            print(f"[runner] Gerrit push failed:\n{push_result.detail}", file=sys.stderr)
            _handle_gerrit_push_failure(
                client,
                snapshot.key,
                push_result.detail,
                agent_class=AGENT_CLASS,
            )
            return 1
    elif rc == 99:
        print(f"[runner] {snapshot.key} skipped (API agent_class not yet wired in MVP)")
        jira_dispatch.transition_back_to_todo(client, snapshot.key, "API agent_class not yet supported in auto-runner-jira.py MVP")
    else:
        print(f"[runner] {snapshot.key} CLI failed rc={rc}; reverting ticket")
        jira_dispatch.transition_back_to_todo(client, snapshot.key, f"CLI exited {rc}; needs operator review.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
