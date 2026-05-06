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
  OMNISIGHT_RUNNER_CLASS   agent_class label, e.g. "subscription-codex"
                           (defaults to subscription-codex)
  OMNISIGHT_RUNNER_TARGET  optional ticket key override (skip scheduler,
                           pickup specific ticket — for testing)
  OMNISIGHT_RUNNER_DRY_RUN if "1", do everything except transition + invoke
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from backend.agents import jira_dispatch, scheduler

AGENT_CLASS = os.environ.get("OMNISIGHT_RUNNER_CLASS", "subscription-codex")
TARGET_OVERRIDE = os.environ.get("OMNISIGHT_RUNNER_TARGET", "").strip()
DRY_RUN = os.environ.get("OMNISIGHT_RUNNER_DRY_RUN", "0") == "1"


def _build_prompt(client: jira_dispatch.DispatchClient, key: str, description: str) -> str:
    """Construct the agent prompt per §5 prompt-injection contract."""
    issue = jira_dispatch._request(client, "GET", f"/issue/{key}?fields=summary,labels,components")
    f = issue["fields"]
    summary = f.get("summary", "<no summary>")
    labels = f.get("labels", [])
    components = [c.get("name") for c in f.get("components", [])]

    declared_areas = sorted(l.split(":", 1)[1] for l in labels if l.startswith("area:"))
    all_areas = ["backend", "frontend", "devops", "tests", "db", "docs", "security", "embedded", "tooling"]
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

Use the runner's transition_back_to_todo or add_comment helpers in
backend/agents/jira_dispatch.py if you need to comment programmatically.

Full ticket description follows:

{description}

When you complete the work, your final commit message must include
[{key}] in the subject line.
"""


CODEX_WORKTREE = os.environ.get(
    "OMNISIGHT_CODEX_WORKTREE",
    os.path.normpath(os.path.join(REPO, "..", "OmniSight-codex-worktree")),
)
CLAUDE_WORKTREE = os.environ.get(
    "OMNISIGHT_CLAUDE_WORKTREE",
    os.path.normpath(os.path.join(REPO, "..", "OmniSight-claude-worktree")),
)
TASK_TIMEOUT_S = int(os.environ.get("OMNISIGHT_RUNNER_TIMEOUT_S", "1800"))


def _invoke_cli(agent_class: str, prompt: str) -> int:
    """Invoke the underlying CLI for this agent_class. Returns exit code."""
    if agent_class == "subscription-codex":
        if not os.path.isdir(CODEX_WORKTREE):
            print(f"[runner] codex worktree missing: {CODEX_WORKTREE}", file=sys.stderr)
            return 2
        cmd = ["codex", "exec", "--cd", CODEX_WORKTREE, "--yolo"]
    elif agent_class == "subscription-claude":
        if not os.path.isdir(CLAUDE_WORKTREE):
            print(f"[runner] claude worktree missing: {CLAUDE_WORKTREE}", file=sys.stderr)
            return 2
        cmd = ["claude", "--dangerously-skip-permissions", "-p", prompt]
    elif agent_class.startswith("api-"):
        print(f"[runner] agent_class={agent_class} requires SDK invocation, not CLI. Skipping invoke.")
        return 99
    else:
        print(f"[runner] unknown agent_class: {agent_class}", file=sys.stderr)
        return 2

    if DRY_RUN:
        print(f"[runner] DRY_RUN: would invoke `{' '.join(cmd[:3])}...` with {len(prompt)} char prompt")
        return 0

    print(f"[runner] invoking {cmd[0]} (timeout {TASK_TIMEOUT_S}s)...")
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if cmd[0] == "codex" else None,
            stdout=sys.stdout,
            stderr=sys.stderr,
            text=True,
            start_new_session=True,
        )
        if cmd[0] == "codex":
            proc.communicate(input=prompt, timeout=TASK_TIMEOUT_S)
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


def main() -> int:
    print(f"[runner] agent_class={AGENT_CLASS}, dry_run={DRY_RUN}")
    client = jira_dispatch.make_client(AGENT_CLASS)
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
        winner = scheduler.dispatch(
            snapshots, weights,
            pre_pickup_check=lambda t: jira_dispatch.pre_pickup_ok(client, t)[0],
        )
        if winner is None:
            print("[runner] no candidate passed pre-pickup checks")
            return 0
        snapshot = winner

    print(f"[runner] selected: {snapshot.key} (component={snapshot.component})")

    # Resolve worktree path early — needed for sync, pre-pickup checks, push.
    worktree_path = Path(
        CODEX_WORKTREE if AGENT_CLASS in ("subscription-codex", "api-openai")
        else CLAUDE_WORKTREE
    )

    # Step 2 (was Step 3 in Phase 1.5): sync worktree FIRST so pre-pickup checks
    # see the actual workspace state, not stale runner-host main repo state.
    # Per L17 — operator's request to refactor pre_pickup_ok cwd.
    if DRY_RUN:
        print(f"[runner] DRY_RUN: would sync worktree {worktree_path}")
    else:
        try:
            print(f"[runner] preparing worktree {worktree_path}...")
            jira_dispatch.set_bot_identity_in_worktree(worktree_path, AGENT_CLASS)
            jira_dispatch.install_commit_msg_hook(worktree_path)
            sync_result = jira_dispatch.sync_to_gerrit_develop(
                worktree_path, AGENT_CLASS, snapshot.key
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
    ok, reason = jira_dispatch.pre_pickup_ok(
        client, snapshot,
        worktree_path=None if DRY_RUN else worktree_path,
    )
    if not ok:
        print(f"[runner] pre-pickup fail: {reason}")
        if not DRY_RUN:
            jira_dispatch.add_comment(client, snapshot.key, f"[runner-live-state-fail]\n\nPre-pickup live-state check failed; not picking up.\n\n{reason}\n\nThis ticket will be retried on next polling cycle.")
        return 1

    # Step 4: build prompt + transition + invoke
    description = jira_dispatch.fetch_description(client, snapshot.key)
    prompt = _build_prompt(client, snapshot.key, description)

    if DRY_RUN:
        print(f"[runner] DRY_RUN: would transition {snapshot.key} → In Progress")
        print(f"[runner] DRY_RUN: prompt preview ({len(prompt)} chars):\n---\n{prompt[:1200]}\n---")
        return 0

    print(f"[runner] transitioning {snapshot.key} → In Progress")
    jira_dispatch.transition_to_in_progress(client, snapshot.key)

    rc = _invoke_cli(AGENT_CLASS, prompt)
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
                worktree_path, AGENT_CLASS, target="develop"
            )
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
            jira_dispatch.transition_to_under_review(
                client, snapshot.key, push_result.change_url
            )
            print(f"[runner] {snapshot.key} → Under Review. Reviewer: +2 in Gerrit UI.")
        else:
            print(f"[runner] Gerrit push failed:\n{push_result.detail}", file=sys.stderr)
            jira_dispatch.add_comment(
                client, snapshot.key,
                f"[runner-gerrit-push-fail] Gerrit rejected push:\n```\n{push_result.detail}\n```\n\n"
                f"Operator: review + push manually.",
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
