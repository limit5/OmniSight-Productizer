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
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from backend.agents import (
    capability_matrix,
    circuit_breaker,
    failure_graph,
    feature_flags as agent_feature_flags,
    memory_writeback,
    outcomes_consumer,
    jira_dispatch,
    orphan_salvage,
    runner_metrics_recorder,
    runner_failure_classifier,
    runner_sandbox,
    runner_workspace_safety,
    scheduler,
)
from backend.agents.loop_detector import (
    DEFAULT_GRADER_MODEL,
    OUTCOMES_GRADER_MODEL_ENV,
    OutcomesGraderUnavailable,
    extract_acceptance_criteria_section,
)

AGENT_CLASS = os.environ.get("OMNISIGHT_RUNNER_CLASS", "subscription-codex")
INSTANCE_ID = os.environ.get("OMNISIGHT_RUNNER_INSTANCE_ID", "").strip() or "default"
TARGET_OVERRIDE = os.environ.get("OMNISIGHT_RUNNER_TARGET", "").strip()
DRY_RUN = os.environ.get("OMNISIGHT_RUNNER_DRY_RUN", "0") == "1"

# OP-858 (C8): pickup-time failure-graph context injection. Until C2's
# runner_incidents Postgres table ships, the runner reads a JSON fixture
# pointed at by OMNISIGHT_FAILURE_GRAPH_FIXTURE so the wiring is
# exercisable end-to-end. Unset = no injection (zero-impact default).
FAILURE_GRAPH_FIXTURE = os.environ.get(
    "OMNISIGHT_FAILURE_GRAPH_FIXTURE", ""
).strip()

# OP-956 — global kill-switch for the ops-only forward-transition path.
# When set to "1", the runner ignores the ``runner:no-commits-expected``
# label entirely and falls back to the OP-827 always-revert behaviour
# (per AC §recovery / rollback). Default off so the feature stays on.
OPS_ONLY_DISABLED = (
    os.environ.get("OMNISIGHT_RUNNER_OPS_ONLY_DISABLED", "0").strip() == "1"
)

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


def _dependency_unblocked_comment() -> str:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return (
        f"[runner-dependency-unblocked] {stamp}\n\n"
        "All blockers resolved; runner picking up next tick."
    )


def _blocked_by_key(reason: str) -> str | None:
    if not reason.startswith("blocked-by:"):
        return None
    return reason.split(None, 1)[0].split(":", 1)[1]


def _add_dependency_waiting_marker(
    client: jira_dispatch.DispatchClient,
    snapshot: scheduler.TicketSnapshot,
    reason: str,
) -> None:
    """Mark a ticket as waiting on ``blocker_key`` once per blocker transition.

    OP-955: the OP-911 incident posted ~50 redundant comments over an hour
    because the marker label was idempotent but the comment was not. The
    label-presence check below caps the comment side at one per
    (ticket, blocker_key) transition. When the active blocker changes
    (was OP-X, now OP-Y), the stale ``runner-blocked:waiting-OP-X`` label
    is dropped and a fresh comment documents the new blocker.
    """
    blocker_key = _blocked_by_key(reason)
    if not blocker_key:
        return
    new_label = jira_dispatch.dependency_waiting_label(blocker_key)
    existing_markers = jira_dispatch.dependency_waiting_labels(
        getattr(snapshot, "labels", ())
    )

    if new_label in existing_markers:
        log.debug(
            "runner.dependency_blocked_skip_comment %s: marker %s already present",
            snapshot.key,
            new_label,
        )
        return

    for stale in existing_markers:
        if stale != new_label:
            jira_dispatch.remove_label(client, snapshot.key, stale)

    jira_dispatch.add_label(client, snapshot.key, new_label)

    date_bucket = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    idem_key = f"dep-blocked-{snapshot.key}-{blocker_key}-{date_bucket}"
    jira_dispatch.add_comment(
        client, snapshot.key, _dependency_skip_comment(reason), idem_key=idem_key
    )


def _clear_dependency_waiting_markers(
    client: jira_dispatch.DispatchClient,
    snapshot: scheduler.TicketSnapshot,
) -> None:
    """Drop all waiting-* markers and post one ``unblocked`` comment.

    OP-955 AC#3: when a ticket transitions from blocked → pickable, the
    runner posts exactly one ``[runner-dependency-unblocked]`` note
    (mirror of the blocked-side comment) and clears every
    ``runner-blocked:waiting-*`` label. No-op when no markers exist so
    tickets that were never blocked stay silent.
    """
    markers = jira_dispatch.dependency_waiting_labels(getattr(snapshot, "labels", ()))
    if not markers:
        return
    for label in markers:
        jira_dispatch.remove_label(client, snapshot.key, label)
    date_bucket = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    idem_key = f"dep-unblocked-{snapshot.key}-{date_bucket}"
    jira_dispatch.add_comment(
        client, snapshot.key, _dependency_unblocked_comment(), idem_key=idem_key
    )


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


_CAPABILITY_MATRIX: capability_matrix.CapabilityMatrix | None = None

# Side channel for `_build_prompt` → `main()`: stash the resolved capability
# set under the ticket key so main() can gate sensitive ops (gerrit push,
# jira update, ...) without re-fetching the issue. Cleared once main() has
# consumed the value; the slot tolerates back-to-back pickups in the same
# process because each tick overwrites the previous entry.
_LAST_RESOLVED_CAPABILITIES: dict[str, frozenset[str]] = {}
_LAST_TICKET_METADATA: dict[str, dict[str, str]] = {}


def _load_capability_matrix() -> capability_matrix.CapabilityMatrix:
    """Lazy-load + cache ``config/capability_matrix.yaml`` for this process."""
    global _CAPABILITY_MATRIX
    if _CAPABILITY_MATRIX is None:
        _CAPABILITY_MATRIX = capability_matrix.load_capability_matrix()
    return _CAPABILITY_MATRIX


def _resolve_runner_capabilities(
    ticket_type: str,
    declared_areas: list[str],
    tier: str,
    labels: list[str],
) -> frozenset[str]:
    """Resolve runner capabilities for the current ticket (AC#3 + AC#4).

    Multi-area tickets union the per-area capability sets; the operator
    label overrides (``capability:enable=`` / ``capability:disable=``)
    apply on top so a problematic capability can be removed without
    touching the YAML.
    """
    matrix = _load_capability_matrix()
    if declared_areas:
        return matrix.resolve_for_areas(
            ticket_type, declared_areas, tier, labels=labels,
        )
    return matrix.resolve(ticket_type, "<no-area>", tier, labels=labels)


def _require_runner_capability(
    client: jira_dispatch.DispatchClient,
    snapshot: scheduler.TicketSnapshot,
    enabled: frozenset[str],
    capability: str,
) -> bool:
    """Enforce a capability before the runner performs a sensitive op."""
    try:
        capability_matrix.require_capability(enabled, capability)
        return True
    except capability_matrix.CapabilityNotPermitted as e:
        print(
            f"[runner-capability-blocked] {snapshot.key}: refusing {capability!r}; "
            f"enabled={sorted(enabled)}",
            file=sys.stderr,
        )
        if not DRY_RUN:
            jira_dispatch.add_comment(
                client, snapshot.key,
                f"[runner-capability-blocked]\n\n{e}\n\n"
                f"Operator: extend `config/capability_matrix.yaml` for this "
                f"(ticket_type × area × tier) combination, or add label "
                f"`capability:enable={capability}` to grant a one-shot override.",
            )
            try:
                jira_dispatch.transition_back_to_todo(
                    client, snapshot.key,
                    f"[runner-capability-blocked] {capability!r} not permitted",
                )
            except Exception as revert_err:  # noqa: BLE001 — log + continue
                print(
                    f"[runner] revert-to-TODO after capability refusal failed: {revert_err}",
                    file=sys.stderr,
                )
        return False


# OP-905 (F7) — runner-side project-state injection knobs.
#
# Backend base URL: defaults to the local backend so the runner can reach
# the aggregator without a deploy-specific knob; operators override via
# ``OMNISIGHT_BACKEND_URL`` in the systemd unit when the backend lives
# elsewhere (e.g. a different host inside the same VPC).
PROJECT_STATE_BACKEND_URL = os.environ.get(
    "OMNISIGHT_BACKEND_URL", "http://localhost:8000"
).rstrip("/")

# 3 s budget per AC #3 — 1 s margin over the API's 2 s total budget so a
# backend that hugs its own ceiling never hangs the runner.
PROJECT_STATE_FETCH_TIMEOUT_S = 3.0

# Operator override label (AC #5). When present on a ticket, the runner
# skips injection for that pickup even if the feature flag is enabled.
PROJECT_STATE_SKIP_LABEL = "project-state:skip"


def _fetch_project_state(key: str) -> dict | None:
    """Call ``GET /api/v1/project-state?ticket=<key>`` with a hard 3 s timeout.

    Returns the parsed payload on success or ``None`` on any failure
    (unreachable / timeout / non-200 / malformed JSON). The error catalog
    in the AC says every failure mode degrades to "no injection + log",
    so we collapse them into one ``None`` return and let the caller emit
    the appropriate marker.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    url = (
        f"{PROJECT_STATE_BACKEND_URL}/api/v1/project-state"
        f"?ticket={urllib.parse.quote(key)}"
    )
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=PROJECT_STATE_FETCH_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        # ProjectStateAPIUnreachable / ProjectStateAPITimeout — same
        # degrade path per AC error catalog.
        print(
            f"[runner] project_state.fetch_failed key={key} err={exc!r}",
            file=sys.stderr,
        )
        return None

    try:
        parsed = json.loads(raw) if raw else None
    except json.JSONDecodeError as exc:
        print(
            f"[runner] project_state.malformed_response key={key} err={exc!r}",
            file=sys.stderr,
        )
        return None
    if not isinstance(parsed, dict):
        # ProjectStateMalformedResponse — defensive against API drift.
        print(
            f"[runner] project_state.malformed_response key={key} "
            f"type={type(parsed).__name__}",
            file=sys.stderr,
        )
        return None
    return parsed


def _render_project_state_block(payload: dict) -> str:
    """Format the aggregator payload into the prompt's ``# Project context`` block.

    The aggregator already shapes the three-axis dict; we serialise it as
    indented JSON so the downstream CLI sees stable, copy-pasteable
    context rather than a flattened bullet list that loses field names.
    """
    rendered = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    return (
        "\n# Project context (OP-905 cross-task awareness)\n\n"
        "The /api/v1/project-state aggregator returned the following payload\n"
        "for this ticket at pickup time. Treat null axes as 'no context'.\n\n"
        f"```json\n{rendered}\n```\n"
    )


def _build_project_state_block(key: str, labels: list[str]) -> str:
    """Resolve flag + label override and return the prompt block (or '').

    AC #4: flag default-off — when disabled, we never call the API.
    AC #5: ``project-state:skip`` label on the ticket disables injection
    for that pickup even when the flag is on.
    AC error catalog: any fetch failure degrades to an empty block + log.
    """
    if not agent_feature_flags.is_project_state_inject_enabled_sync():
        return ""
    if PROJECT_STATE_SKIP_LABEL in labels:
        print(
            f"[runner] project_state.skipped_by_label key={key} "
            f"label={PROJECT_STATE_SKIP_LABEL}",
            file=sys.stderr,
        )
        return ""
    payload = _fetch_project_state(key)
    if payload is None:
        return ""
    return _render_project_state_block(payload)


def _load_failure_graph_for_pickup() -> failure_graph.FailureGraph | None:
    """Return a FailureGraph rebuilt from the operator-supplied fixture.

    OP-858 wiring point. Returns None when the fixture isn't configured
    or fails to load — pickup must remain functional even when the
    failure-graph layer is offline (the AC §error-catalog degrade path).
    """
    if not FAILURE_GRAPH_FIXTURE:
        return None
    fixture_path = Path(FAILURE_GRAPH_FIXTURE)
    if not fixture_path.is_file():
        print(
            f"[runner] failure_graph.fixture_missing path={fixture_path}",
            file=sys.stderr,
        )
        return None
    try:
        import json as _json
        rows = _json.loads(fixture_path.read_text())
        from datetime import datetime as _dt
        incidents = [
            failure_graph.RunnerIncident(
                incident_id=row["incident_id"],
                ticket_key=row["ticket_key"],
                failure_class=row["failure_class"],
                mutex_label=row.get("mutex_label"),
                occurred_at=_dt.fromisoformat(
                    row["occurred_at"].replace("Z", "+00:00")
                ),
                summary=row.get("summary", ""),
            )
            for row in rows
        ]
        return failure_graph.FailureGraph.build(incidents)
    except Exception as e:  # noqa: BLE001 — degrade on any parse error
        print(
            f"[runner] failure_graph.fixture_load_failed err={e}",
            file=sys.stderr,
        )
        return None


def _build_prompt(
    client: jira_dispatch.DispatchClient,
    key: str,
    description: str,
) -> str:
    """Construct the agent prompt per §5 prompt-injection contract.

    Side effect (OP-855): resolves the capability matrix for this ticket
    and stashes the result in :data:`_LAST_RESOLVED_CAPABILITIES` so
    ``main()`` can gate sensitive runner operations without a second
    issue fetch.

    OP-858: when failure-graph fixture is configured, injects neighbour
    incident context into the prompt before AC.
    """
    issue = jira_dispatch._request(
        client, "GET", f"/issue/{key}?fields=summary,labels,components,issuetype",
    )
    f = issue["fields"]
    summary = f.get("summary", "<no summary>")
    labels = f.get("labels", [])
    components = [c.get("name") for c in f.get("components", [])]
    issuetype_raw = f.get("issuetype") or {}
    ticket_type = issuetype_raw.get("name", "Story") if isinstance(issuetype_raw, dict) else "Story"

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

    enabled_capabilities = _resolve_runner_capabilities(
        ticket_type, declared_areas, tier, list(labels),
    )
    _LAST_RESOLVED_CAPABILITIES[key] = enabled_capabilities
    _LAST_TICKET_METADATA[key] = {
        "ticket_type": ticket_type,
        "tier": tier,
        "area": ",".join(declared_areas) if declared_areas else "<none>",
    }
    cap_lines = "\n  - ".join(sorted(enabled_capabilities)) or "(none)"
    capabilities_block = (
        f"\n# Enabled capabilities (OP-855 capability matrix)\n\n"
        f"This pickup runs with the following capabilities enabled. Do NOT\n"
        f"attempt operations outside this list — the runner blocks them at\n"
        f"the tool-dispatch boundary (raises CapabilityNotPermitted):\n\n"
        f"  - {cap_lines}\n"
    )

    # OP-858 (C8) — inject failure-graph context for prior failed attempts on
    # this ticket. The block is empty when the ticket has no prior incidents
    # or when the fixture/Cognee source is offline (degrade path).
    fg_block = ""
    fg = _load_failure_graph_for_pickup()
    if fg is not None:
        rendered = failure_graph.render_pickup_context(fg, key)
        if rendered:
            fg_block = "\n\n" + rendered + "\n"

    # OP-905 (F7) — inject cross-task awareness payload from the
    # /api/v1/project-state aggregator. Gated by the feature flag + skip
    # label; any fetch failure degrades silently to an empty block.
    ps_block = _build_project_state_block(key, list(labels))

    # OP-956 — when the ticket carries the `runner:no-commits-expected`
    # sigil, tell the CLI it MUST exit with 0 commits. Without this hint
    # the model often "self-corrects" by inventing a marker commit just
    # to satisfy the OP-827 zero-commit revert path — which is exactly
    # the wedge OP-956 fixes.
    ops_only_block = ""
    if jira_dispatch.has_ops_only_label(labels) and not OPS_ONLY_DISABLED:
        ops_only_block = (
            "\n# Ops-only ticket (OP-956)\n\n"
            "This ticket carries the `runner:no-commits-expected` label.\n"
            "It expects ZERO commits — your job is to execute the runbook,\n"
            "post AC verification + any audit/report comments via\n"
            "`backend/agents/jira_dispatch.add_comment`, then EXIT 0.\n\n"
            "Do NOT fabricate a placeholder commit to satisfy the runner's\n"
            "zero-commit revert path. The runner detects this label and\n"
            "forward-transitions Submit-for-Review → Approve → Deploy on\n"
            "your behalf when the CLI exits cleanly with no commits.\n"
        )

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
{capabilities_block}{fg_block}{ps_block}{ops_only_block}
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


def _invoke_cli(
    agent_class: str,
    prompt: str,
    failure_context: str | None = None,
    *,
    ticket_key: str = "default",
    worktree_path: Path | None = None,
) -> int:
    """Invoke the underlying CLI for this agent_class. Returns exit code.

    OP-845: when running on a platform that supports a sandbox binary
    (Linux ``bwrap`` / macOS ``sandbox-exec``), the CLI argv is wrapped
    via :func:`runner_sandbox.wrap_in_bubblewrap` so an injection-driven
    write outside the worktree is blocked by the kernel, not just by
    OP-836's post-hoc sentinel check. Wrap-vs-degrade gating lives in
    that module — here we just thread the inputs.
    """
    full_prompt = prompt
    if failure_context:
        full_prompt = f"{prompt.rstrip()}\n\n{failure_context.strip()}\n"

    cwd: str | None = None
    if agent_class == "subscription-codex":
        if not os.path.isdir(CODEX_WORKTREE):
            print(f"[runner] codex worktree missing: {CODEX_WORKTREE}", file=sys.stderr)
            return 2
        cmd = ["codex", "exec", "--cd", CODEX_WORKTREE, "--yolo"]
        sandbox_worktree = Path(CODEX_WORKTREE)
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
        sandbox_worktree = Path(CLAUDE_WORKTREE)
    elif agent_class.startswith("api-"):
        print(f"[runner] agent_class={agent_class} requires SDK invocation, not CLI. Skipping invoke.")
        return 99
    else:
        print(f"[runner] unknown agent_class: {agent_class}", file=sys.stderr)
        return 2

    # OP-845: wrap the CLI argv in the platform sandbox jail. Caller can
    # pass an explicit worktree_path override; otherwise we use the
    # per-class default chosen above.
    effective_worktree = worktree_path or sandbox_worktree
    try:
        wrapped_cmd = runner_sandbox.wrap_in_bubblewrap(
            cmd, worktree_path=effective_worktree, ticket_key=ticket_key,
        )
    except runner_sandbox.SandboxBinaryMissing as e:
        # ENFORCE=1 + binary missing → abort. We surface a distinct exit
        # code so the caller can operator-alert + revert the ticket
        # rather than treating it like a generic CLI failure.
        print(f"[runner] sandbox binary missing (ENFORCE=1): {e}", file=sys.stderr)
        return 126

    if DRY_RUN:
        print(f"[runner] DRY_RUN: would invoke `{' '.join(cmd[:3])}...` with {len(full_prompt)} char prompt")
        return 0

    print(f"[runner] invoking {cmd[0]} (timeout {TASK_TIMEOUT_S}s)...")
    try:
        proc = subprocess.Popen(
            wrapped_cmd,
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


def _run_memory_writeback(
    client: "jira_dispatch.DispatchClient",
    ticket_key: str,
    *,
    outcome: str,
    summary: str = "",
    failure_class: str | None = None,
    raw_traceback: str = "",
    mutex_label: str | None = None,
    area: str | None = None,
) -> None:
    """OP-906 (F8) — fan write-back across Memory Tool / incidents / Cognee.

    Idempotent on ``(ticket_key, attempt_n)`` where attempt_n is the
    count of prior writebacks for this ticket plus one. Per AC #3 a
    backing-store outage logs + continues; this helper never raises.
    """
    try:
        attempt_n = len(memory_writeback.get_writebacks_for(ticket_key)) + 1
        request = memory_writeback.WritebackRequest(
            ticket_key=ticket_key,
            attempt_n=attempt_n,
            outcome=outcome,
            summary=summary,
            failure_class=failure_class,
            raw_traceback=raw_traceback,
            mutex_label=mutex_label,
            area=area,
            runner_class=AGENT_CLASS,
        )
        result = memory_writeback.MemoryWriteback().write(request)
    except Exception as exc:  # noqa: BLE001 — writeback is fail-open per AC #3
        print(
            f"[runner] memory_writeback unexpected error ticket={ticket_key} "
            f"err={type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return
    if result.idempotent_skip:
        return
    if result.memory_tool_lesson_id:
        try:
            jira_dispatch.add_comment(
                client,
                ticket_key,
                f"[memory-writeback] lesson={result.memory_tool_lesson_id}",
            )
        except Exception as exc:  # noqa: BLE001 — comment-post is informational
            print(
                f"[runner] memory_writeback comment failed ticket={ticket_key} "
                f"err={type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
    if result.stores_failed:
        print(
            f"[runner] memory_writeback partial ticket={ticket_key} "
            f"stores_failed={list(result.stores_failed)}"
        )


def _ops_only_active_for(snapshot: "scheduler.TicketSnapshot") -> bool:
    """Return True if the OP-956 ops-only forward-transition path applies.

    Two gates: (1) the global ``OMNISIGHT_RUNNER_OPS_ONLY_DISABLED`` env
    knob is not set (AC §recovery — operator escape hatch), and (2) the
    ticket carries the ``runner:no-commits-expected`` sigil label.
    """
    if OPS_ONLY_DISABLED:
        return False
    return jira_dispatch.has_ops_only_label(getattr(snapshot, "labels", ()))


def _handle_ops_only_forward_transition(
    client: "jira_dispatch.DispatchClient",
    key: str,
    *,
    unexpected_commits: int = 0,
) -> int:
    """Forward-walk an ops-only ticket to 公開済み + post audit comment.

    Returns the runner exit code (0 on success, 1 on permission-refused
    / unexpected JIRA failure — per AC #2 the latter degrades to operator
    notification with the ticket left in its current workflow state so
    no work is lost).

    OP-956 AC #2: skips the OP-827 ``[runner-no-commits-from-cli]``
    revert path entirely and walks the workflow Submit-for-Review →
    Approve → Deploy (ids 3 → 4 → 7).

    When ``unexpected_commits`` is non-zero, the caller already pushed
    the commits via the normal Gerrit path; we simply continue forward
    from Under Review to Published and emit the
    ``OpsLabelButCommitsProduced`` diagnostic comment.
    """
    if unexpected_commits > 0:
        jira_dispatch.add_comment(
            client, key,
            (
                f"[runner-ops-only-unexpected-commits] CLI exited 0 with "
                f"{unexpected_commits} commit(s) despite ops-only label "
                f"`{jira_dispatch.OPS_ONLY_LABEL}` — pushing commits AND "
                f"forward-transitioning (possible mis-classification; "
                f"operator: confirm the label still applies)."
            ),
        )
    try:
        jira_dispatch.forward_transition_ops_only(client, key)
    except jira_dispatch.WorkflowTransitionPermissionRefused as e:
        print(
            f"[runner] ops-only forward refused for {key}: {e}",
            file=sys.stderr,
        )
        try:
            jira_dispatch.add_comment(
                client, key,
                (
                    f"[runner-ops-only-permission-refused] JIRA refused "
                    f"transition {e.transition_name!r} (HTTP 403). "
                    f"Ticket left in current workflow state; operator "
                    f"must complete the forward walk manually.\n\n"
                    f"Detail: {e.detail}"
                ),
            )
        except Exception as inner:  # noqa: BLE001 — informational
            print(
                f"[runner] could not post ops-only refusal comment: {inner}",
                file=sys.stderr,
            )
        return 1
    except Exception as e:  # noqa: BLE001 — unknown JIRA fault
        print(
            f"[runner] ops-only forward failed for {key}: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        try:
            jira_dispatch.add_comment(
                client, key,
                (
                    f"[runner-ops-only-fail]\n\n{type(e).__name__}: {e}\n\n"
                    f"Operator: complete the forward walk manually."
                ),
            )
        except Exception as inner:  # noqa: BLE001 — informational
            print(
                f"[runner] could not post ops-only fail comment: {inner}",
                file=sys.stderr,
            )
        return 1
    print(f"[runner] {key} ops-only forward-transition complete → 公開済み")
    return 0


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


def _run_git_text(worktree_path: Path, args: list[str], *, timeout: int = 20) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def _collect_outcomes_context(worktree_path: Path, base_ref: str) -> tuple[str, str]:
    completion = _run_git_text(
        worktree_path,
        ["log", "--oneline", "--decorate=no", f"{base_ref}..HEAD"],
    ).strip()
    diff = _run_git_text(
        worktree_path,
        ["diff", "--no-ext-diff", f"{base_ref}..HEAD"],
        timeout=60,
    )
    return completion or "(no commit summary)", diff


def _current_patchset_number(change_number: int, agent_class: str = AGENT_CLASS) -> str:
    user, ssh_key = jira_dispatch._gerrit_auth_for_instance(agent_class, INSTANCE_ID)
    cmd = [
        "ssh", "-i", str(ssh_key), "-p", str(jira_dispatch.GERRIT_SSH_PORT),
        f"{user}@{jira_dispatch.GERRIT_SSH_HOST}",
        "gerrit", "query", "--current-patch-set", "--format=JSON",
        f"change:{change_number}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    for line in result.stdout.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") == "stats":
            continue
        patchset = data.get("currentPatchSet") or {}
        number = str(patchset.get("number") or "")
        if number:
            return number
    return "1"


def _abandon_gerrit_change(change_number: int, *, reason: str) -> None:
    patchset_number = _current_patchset_number(change_number)
    user, ssh_key = jira_dispatch._gerrit_auth_for_instance(AGENT_CLASS, INSTANCE_ID)
    cmd = [
        "ssh", "-i", str(ssh_key), "-p", str(jira_dispatch.GERRIT_SSH_PORT),
        f"{user}@{jira_dispatch.GERRIT_SSH_HOST}",
        "gerrit", "review", "--abandon", "--message", reason[:500],
        f"{change_number},{patchset_number}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def _grade_and_consume_outcomes(
    client: "jira_dispatch.DispatchClient",
    key: str,
    description: str,
    worktree_path: Path,
    base_ref: str,
    change_number: int | None,
) -> str:
    """Return ``pass``/``partial``/``fail``/``disabled`` after OP-860 handling."""
    if not outcomes_consumer.outcomes_enabled():
        return "disabled"

    tracker = outcomes_consumer.OutcomesBudgetTracker(
        daily_budget_usd=outcomes_consumer.outcomes_budget_usd_per_day(),
    )
    try:
        tracker.ensure_available()
    except outcomes_consumer.OutcomesBudgetExceeded as exc:
        print(f"[outcomes-budget] {exc}; disabled until next UTC day")
        return "disabled"

    try:
        completion_text, diff_text = _collect_outcomes_context(worktree_path, base_ref)
        from backend.agents.anthropic_native_client import AnthropicClient

        grader_model = os.environ.get(OUTCOMES_GRADER_MODEL_ENV, DEFAULT_GRADER_MODEL)
        verdict = outcomes_consumer.grade_outcomes(
            client=AnthropicClient(default_model=grader_model),
            ticket_key=key,
            ac_text=extract_acceptance_criteria_section(description),
            completion_text=completion_text,
            diff_text=diff_text,
            grader_model=grader_model,
        )
        tracker.record(verdict.cost_usd)
    except OutcomesGraderUnavailable as exc:
        print(f"[outcomes-unavailable] {exc}; continuing without grader")
        return "disabled"
    except outcomes_consumer.OutcomesGraderRefused as exc:
        detail = f"[outcomes-refused] {type(exc).__name__}: {exc}"
        print(detail, file=sys.stderr)
        jira_dispatch.add_comment(
            client,
            key,
            f"{detail}\n\nRunner paused this ticket for operator review.",
        )
        jira_dispatch.add_label(client, key, "runner-loop-paused-pending-review")
        jira_dispatch.notify_operator("runner-alerts", "high", detail)
        raise

    def revert_patchsets() -> None:
        if change_number is None:
            return
        _abandon_gerrit_change(
            change_number,
            reason=f"OP-860 Outcomes grader failed {key}: {verdict.grader_reasoning}",
        )

    outcomes_consumer.consume_outcomes_verdict(
        client=client,
        key=key,
        verdict=verdict,
        revert_patchsets=revert_patchsets,
    )
    return verdict.verdict


def main() -> int:
    print(
        f"[runner] agent_class={AGENT_CLASS}, instance_id={INSTANCE_ID}, "
        f"bot={_bot_username()}, dry_run={DRY_RUN}"
    )
    # OP-845: log sandbox state once at startup so journalctl shows the
    # operator whether bubblewrap is wired before any ticket is processed.
    _sandbox_enforce = os.environ.get(runner_sandbox.ENV_ENFORCE, "0")
    _sandbox_status = (
        runner_sandbox.LOG_SANDBOX_WRAPPED
        if runner_sandbox.sandbox_available()
        else runner_sandbox.LOG_SANDBOX_DEGRADED
    )
    print(
        f"[runner] {_sandbox_status} platform={runner_sandbox.detect_platform()} "
        f"enforce={_sandbox_enforce}"
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
    except capability_matrix.CapabilityMatrixError as e:
        # Malformed capability matrix / unknown override → halt and surface to
        # operator. Reverting matters because the runner can't decide what the
        # CLI is allowed to do.
        print(f"[runner] capability matrix invalid for {snapshot.key}: {e}", file=sys.stderr)
        if not DRY_RUN:
            jira_dispatch.add_comment(
                client, snapshot.key,
                f"[runner-capability-matrix-invalid]\n\n{e}\n\n"
                f"Operator: fix `config/capability_matrix.yaml` (or the "
                f"`capability:enable=` / `capability:disable=` label) before "
                f"the next pickup.",
            )
            jira_dispatch.transition_back_to_todo(
                client, snapshot.key,
                f"[runner-capability-matrix-invalid] {e}",
            )
        return 1
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

    enabled_caps = _LAST_RESOLVED_CAPABILITIES.get(snapshot.key, frozenset())
    print(
        f"[runner] {snapshot.key} capabilities: {sorted(enabled_caps)}"
    )

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

    # OP-838: atomic claim mutex before transition_to_in_progress. The JQL
    # pickup filter (`assignee is EMPTY`) and `transition_to_in_progress` are
    # separated by several seconds of worktree prep + pre-pickup checks; two
    # runner instances ticking concurrently can both pass every gate up to
    # this point and both proceed to push to Gerrit, generating duplicate
    # Change-Ids. Observed on OP-836 #356 and OP-837 #358 on 2026-05-11.
    try:
        claim = jira_dispatch.claim_ticket_atomic(client, snapshot.key, INSTANCE_ID)
    except jira_dispatch.RunnerMutexAPIError as e:
        print(
            f"[runner-mutex-api-error] {snapshot.key}: {e}; skipping pickup, "
            f"will retry on next tick.",
            file=sys.stderr,
        )
        return 0
    if not claim.ok:
        print(
            f"[runner-mutex-lost] {snapshot.key}: lost claim to {claim.lost_to} "
            f"(our token: {claim.claim_token}); skipping pickup, will retry "
            f"on next tick."
        )
        return 0
    print(f"[runner] {snapshot.key} claim acquired (token: {claim.claim_token})")

    # OP-836 sentinel — stamps worktree pre-launch so we can detect post-CLI
    # tamper (CLI deleted it, reset HEAD to non-descendant SHA, swapped
    # branches). Layered with the perm-isolation check above.
    sentinel_path = runner_workspace_safety.write_workspace_sentinel(
        worktree_path, snapshot.key,
    )

    print(f"[runner] transitioning {snapshot.key} → In Progress")
    jira_dispatch.transition_to_in_progress(client, snapshot.key)

    metric_meta = _LAST_TICKET_METADATA.get(snapshot.key, {})
    metric_id, metric_started_at = runner_metrics_recorder.record_pickup_sync(
        runner_metrics_recorder.RunnerMetricStart(
            agent_class=AGENT_CLASS,
            instance_id=INSTANCE_ID,
            ticket_key=snapshot.key,
            ticket_type=metric_meta.get("ticket_type", snapshot.component or "unknown"),
            tier=metric_meta.get("tier", "M"),
            area=metric_meta.get("area", "<none>"),
            claude_model_used=os.environ.get("ANTHROPIC_MODEL")
            or os.environ.get("CLAUDE_MODEL"),
        )
    )
    rc = _invoke_cli(
        AGENT_CLASS, prompt,
        ticket_key=snapshot.key, worktree_path=worktree_path,
    )

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
        runner_metrics_recorder.record_completion_sync(
            metric_id=metric_id,
            ticket_key=snapshot.key,
            agent_class=AGENT_CLASS,
            instance_id=INSTANCE_ID,
            outcome="failure",
            started_at=metric_started_at,
        )
        _run_memory_writeback(
            client,
            snapshot.key,
            outcome=memory_writeback.OUTCOME_FAILURE,
            summary="workspace tampered post-CLI",
            failure_class="WORKTREE_DIRTY",
            area=metric_meta.get("area"),
        )
        return 1
    runner_metrics_recorder.record_completion_sync(
        metric_id=metric_id,
        ticket_key=snapshot.key,
        agent_class=AGENT_CLASS,
        instance_id=INSTANCE_ID,
        outcome=runner_metrics_recorder.outcome_from_return_code(rc),
        started_at=metric_started_at,
    )
    if rc == 0:
        # OP-855 capability gate: refuse the auto-push if `gerrit_push` is
        # not in the matrix for this (ticket_type × area × tier). Operator
        # can re-enable per-pickup via `capability:enable=gerrit_push`.
        if not _require_runner_capability(
            client, snapshot, enabled_caps, "gerrit_push",
        ):
            return 1
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
            # OP-956: ops-only ticket-type path. When the operator has
            # tagged the ticket as `runner:no-commits-expected`, the
            # zero-commits-from-CLI signal is the EXPECTED outcome
            # (operator/automation runbooks produce reports + audit
            # comments, not commits). Skip OP-827's always-revert path
            # entirely and forward-walk Submit → Approve → Deploy.
            if _ops_only_active_for(snapshot):
                print(
                    f"[runner] {snapshot.key} CLI produced 0 commits + "
                    f"ops-only label present → forward-transitioning to 公開済み"
                )
                rc_fwd = _handle_ops_only_forward_transition(
                    client, snapshot.key,
                )
                _run_memory_writeback(
                    client,
                    snapshot.key,
                    outcome=(
                        memory_writeback.OUTCOME_SUCCESS
                        if rc_fwd == 0
                        else memory_writeback.OUTCOME_FAILURE
                    ),
                    summary="ops-only forward-transition (0 commits, label-tagged)",
                    failure_class=None if rc_fwd == 0 else "OPS_ONLY_TRANSITION_REFUSED",
                    area=metric_meta.get("area"),
                )
                return rc_fwd
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
            try:
                outcomes_status = _grade_and_consume_outcomes(
                    client,
                    snapshot.key,
                    description,
                    worktree_path,
                    sync_result.develop_sha,
                    push_result.change_number,
                )
            except outcomes_consumer.OutcomesGraderRefused:
                return 1
            if outcomes_status == "fail":
                print(f"[runner] {snapshot.key} Outcomes grader failed; ticket reopened")
                _run_memory_writeback(
                    client,
                    snapshot.key,
                    outcome=memory_writeback.OUTCOME_FAILURE,
                    summary="outcomes-grader refused",
                    failure_class="OUTCOMES_GRADER_REFUSED",
                    area=metric_meta.get("area"),
                )
                return 0
            _finalize_under_review(client, snapshot.key, push_result.change_url)
            # OP-956: OpsLabelButCommitsProduced — CLI was tagged ops-only
            # but produced commits anyway. AC error catalog says "Log
            # warning + still push the commits (don't lose work) +
            # forward-transition". The push above already landed the
            # commits and walked the ticket to Under Review; continue
            # the forward walk from there to 公開済み.
            if _ops_only_active_for(snapshot):
                push_count = max(1, len(getattr(push_result, "change_numbers", []) or [push_result.change_number]))
                _handle_ops_only_forward_transition(
                    client, snapshot.key, unexpected_commits=push_count,
                )
            _run_memory_writeback(
                client,
                snapshot.key,
                outcome=memory_writeback.OUTCOME_SUCCESS,
                summary=f"runner_pushed_gerrit change={push_result.change_number}",
                area=metric_meta.get("area"),
            )
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
        _run_memory_writeback(
            client,
            snapshot.key,
            outcome=memory_writeback.OUTCOME_FAILURE,
            summary=f"CLI exited rc={rc}",
            area=metric_meta.get("area"),
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())
