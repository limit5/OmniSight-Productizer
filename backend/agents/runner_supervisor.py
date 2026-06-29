"""L1 rule-engine runner supervisor (OP-2488, EPIC OP-2485) — the automated
"Claude-in-tmux".

Observes the fleet's JIRA footprint (Invariant A: it NEVER commands runner
processes — it only READS JIRA and ACTS via JIRA) and applies
pattern -> NON-DESTRUCTIVE-ONLY actions (Invariant B).

It is an OPTIMIZER, not a safety system. The real safety net is the L0
per-runner floor (stoploss, revert-on-error, terminal markers — all
JIRA-label-backed, DB-independent). So a failed/overwhelmed supervisor merely
PAUSES rescues; it can never cause a disaster, because it has no destructive
powers — it may only do the actions in :data:`NONDESTRUCTIVE_DOCTRINE`, NEVER
close / abandon / merge / +2 / change-config. "It can't cause problems" is thus
provable by construction, not by enumerating every bad scenario.

Seed rules — the patterns a human kept hand-rescuing in tmux (accretion model;
add rules as new novel failures are hand-handled):
  - stale-claim-on-todo    : a reverted ticket kept its ``claim:*`` label ->
                             re-pickup blocked (lowest-token-wins). Strip it.
  - assignee-stuck-on-todo : To Do + agent:auto + a BOT assignee -> PICKUP_JQL
                             needs assignee EMPTY -> idles invisibly. Clear it.
  - zombie-in-progress     : In Progress + bot-assigned + stale -> a wedged
                             runner. SURFACE it (escalate-only in v1; auto-revert
                             is deferred until the in-flight-work race-check is
                             wired, so v1 never races an active runner).

Run: ``python -m backend.agents.runner_supervisor [--execute]`` (default = dry-run).
A systemd timer + a trivial heartbeat watchdog (cron comparing a timestamp) are
the deployment shape — both deferred to the activation step.
"""
from __future__ import annotations

import argparse
import dataclasses
import sys
import urllib.parse
from typing import Callable

from backend.agents import jira_dispatch


PROJECT = "OP"
# Only rescue BOT-owned wedges — never touch a human's deliberate state.
BOT_ACCOUNT_NAMES = ("claude-bot", "codex-bot")
SUPERVISOR_LABEL = "supervisor"
# Stale window for the zombie rule: comfortably greater than the 30-min task
# timeout, so a genuinely-active runner has already finished/failed by then.
ZOMBIE_STALE_MINUTES = 45

# Invariant B — the ONLY actions a rule may take. Enforced BY CONSTRUCTION: the
# rules below call only these jira_dispatch primitives. NEVER close / abandon /
# merge / +2 / change-config — those escalate to a human.
NONDESTRUCTIVE_DOCTRINE: tuple[str, ...] = (
    "strip claim labels",
    "clear assignee",
    "revert to To Do",
    "add label",
    "add comment",
)


@dataclasses.dataclass
class Finding:
    key: str
    rule: str
    detail: str
    actions: tuple[str, ...]
    dry_run: bool


def _search(
    client,
    jql: str,
    *,
    fields: str = "key,status,assignee,labels,updated",
    max_results: int = 100,
) -> list[dict]:
    q = urllib.parse.urlencode({"jql": jql, "fields": fields, "maxResults": max_results})
    resp = jira_dispatch._request(client, "GET", f"/search/jql?{q}")
    return resp.get("issues", []) or []


def _labels(issue: dict) -> list[str]:
    return list((issue.get("fields") or {}).get("labels") or [])


def _is_bot_assignee(issue: dict) -> bool:
    assignee = (issue.get("fields") or {}).get("assignee") or None
    if not assignee:
        return False
    name = (assignee.get("displayName") or assignee.get("name") or "").lower()
    return any(b in name for b in BOT_ACCOUNT_NAMES)


# ── Rules — each returns the findings it acted on (or, in dry-run, would) ─────

def _rule_stale_claim_on_todo(client, *, dry_run: bool) -> list[Finding]:
    out: list[Finding] = []
    jql = f'project = {PROJECT} AND statusCategory = "To Do" AND assignee is EMPTY'
    for issue in _search(client, jql):
        key = issue["key"]
        claims = [lbl for lbl in _labels(issue) if lbl.startswith("claim:")]
        if not claims:
            continue
        if not dry_run:
            for c in claims:
                jira_dispatch.remove_label(client, key, c)
            jira_dispatch.add_comment(
                client, key,
                f"[supervisor:stale-claim] stripped {len(claims)} stale claim label(s) "
                f"that blocked re-pickup (lowest-token-wins). Non-destructive rescue (OP-2488).",
            )
        out.append(Finding(
            key, "stale-claim-on-todo",
            f"{len(claims)} stale claim:* on a To Do unassigned ticket",
            tuple(f"remove {c}" for c in claims) + ("comment",), dry_run,
        ))
    return out


def _rule_assignee_stuck_on_todo(client, *, dry_run: bool) -> list[Finding]:
    out: list[Finding] = []
    jql = (f'project = {PROJECT} AND statusCategory = "To Do" '
           f'AND assignee is not EMPTY AND labels = "agent:auto"')
    for issue in _search(client, jql):
        key = issue["key"]
        if not _is_bot_assignee(issue):
            continue  # never clear a human's deliberate assignment
        if not dry_run:
            jira_dispatch.clear_assignee(client, key)
            jira_dispatch.add_comment(
                client, key,
                "[supervisor:assignee-stuck] cleared a bot assignee on a To Do agent:auto "
                "ticket — PICKUP_JQL requires assignee EMPTY, so it was idling invisibly. "
                "Non-destructive rescue (OP-2488).",
            )
        out.append(Finding(
            key, "assignee-stuck-on-todo",
            "bot assignee on a To Do agent:auto ticket (idling, un-pickable)",
            ("clear_assignee", "comment"), dry_run,
        ))
    return out


def _rule_zombie_in_progress(client, *, dry_run: bool) -> list[Finding]:
    out: list[Finding] = []
    escalated = f"{SUPERVISOR_LABEL}:stuck-in-progress"
    jql = (f'project = {PROJECT} AND statusCategory = "In Progress" '
           f'AND assignee is not EMPTY AND updated <= "-{ZOMBIE_STALE_MINUTES}m"')
    for issue in _search(client, jql):
        key = issue["key"]
        if not _is_bot_assignee(issue):
            continue
        if escalated in _labels(issue):
            continue  # already surfaced — idempotent
        if not dry_run:
            jira_dispatch.add_label(client, key, escalated)
            jira_dispatch.add_comment(
                client, key,
                f"[supervisor:zombie] In Progress + bot-assigned + no update for "
                f">{ZOMBIE_STALE_MINUTES}min — likely a wedged runner (any work is in a "
                f"reaped ephemeral worktree). SURFACED for the operator/coordinator. "
                f"v1 = escalate-only; auto-revert is deferred until the in-flight-work "
                f"race-check is wired, so the supervisor never races an active runner. "
                f"Non-destructive (OP-2488).",
            )
        out.append(Finding(
            key, "zombie-in-progress",
            f"In Progress + bot-assigned, stale >{ZOMBIE_STALE_MINUTES}min",
            (f"add_label {escalated}", "comment"), dry_run,
        ))
    return out


RULES: tuple[Callable[..., list[Finding]], ...] = (
    _rule_stale_claim_on_todo,
    _rule_assignee_stuck_on_todo,
    _rule_zombie_in_progress,
)


def run_once(client, *, dry_run: bool = True) -> list[Finding]:
    """Run every rule once. One rule's failure must not stop the others
    (resilient — a transient JIRA fault on rule N still lets rule N+1 run)."""
    findings: list[Finding] = []
    for rule in RULES:
        try:
            findings.extend(rule(client, dry_run=dry_run))
        except Exception as e:  # noqa: BLE001 — resilient: isolate per-rule faults
            print(
                f"[supervisor] rule {rule.__name__} failed: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="L1 rule-engine runner supervisor (OP-2488)")
    ap.add_argument("--execute", action="store_true",
                    help="apply the non-destructive rescues (default: dry-run / observe-only)")
    ap.add_argument("--agent-class", default="subscription-claude")
    args = ap.parse_args(argv)
    client = jira_dispatch.make_client(args.agent_class)
    findings = run_once(client, dry_run=not args.execute)
    mode = "EXECUTED" if args.execute else "DRY-RUN"
    print(f"[supervisor] {mode}: {len(findings)} finding(s)")
    for f in findings:
        print(f"  [{f.rule}] {f.key}: {f.detail} -> {', '.join(f.actions)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
