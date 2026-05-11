#!/usr/bin/env python3
"""OP-874 — audit JIRA Blocks-link directions against operator intent.

Background
----------
Gerrit #387 (OP-858, 2026-05-11) surfaced a class of latent operator
bug: wire scripts had been calling ``POST /issueLink`` with
``inwardIssue=blocked, outwardIssue=blocker``, which is the OPPOSITE of
Atlassian's documented semantics. With the link direction inverted,
``has_unresolved_blockedby`` saw the still-stale blocker as a blockee,
the gate silently passed, and the runner picked the ticket up against
stale develop.

This audit script walks every open OP ticket, looks at each ``Blocks``
issuelink, recovers operator intent from the comment trail
(``[blocked-by-link]`` or ``[file-coordinator] Linked blockedBy``
markers) and flags links whose direction does not match the intent.
With ``--fix`` it deletes + re-creates the wrong-direction links, after
saving the deleted link IDs to ``.audit-rollback-<date>.json`` so the
operator can mirror-restore the original direction if needed.

Output
------
Report lands at ``docs/audit/blockedby-direction-audit-<date>.md``.
With ``--fix``, the rollback file lands at
``docs/audit/.audit-rollback-<date>.json``.

Exit codes
----------
* 0 — all Blocks links match intent (or were repaired with ``--fix``).
* 1 — at least one direction mismatch remains in the report.
* 2 — JIRA query / connectivity failure.

Usage
-----
::

  python3 scripts/audit_blockedby_directions.py [--agent-class subscription-codex] \
      [--dry-run] [--fix] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.agents import file_coordinator as fc  # noqa: E402
from backend.agents import jira_dispatch as jd  # noqa: E402

AUDIT_REPORT_DIR = REPO_ROOT / "docs" / "audit"
BLOCKS_TYPE = "Blocks"

# Recover operator-intended direction from the comment trail. Both
# markers always appear on the BLOCKED ticket, mentioning the BLOCKER.
INTENT_MARKERS = (
    re.compile(rf"{re.escape(fc.ADD_BLOCKED_BY_COMMENT_PREFIX)}\s+blockedBy\s+(OP-\d+)"),
    re.compile(r"\[file-coordinator\]\s+Linked\s+blockedBy\s+(OP-\d+)"),
)

AUDIT_JQL = (
    'project = OP '
    'AND issueLinkType = "blocks" '
    'AND statusCategory != Done '
    'ORDER BY key ASC'
)


@dataclass(frozen=True)
class IntentedLink:
    """One Blocks link with operator intent recovered from comments."""

    link_id: str
    inward_key: str   # raw API: the issue posted as inwardIssue
    outward_key: str  # raw API: the issue posted as outwardIssue
    # Operator intent: who SHOULD be the blocker, who SHOULD be the blocked.
    intent_blocked: str
    intent_blocker: str
    # The ticket the audit walked when it found this link (always one of
    # inward_key / outward_key — used for the comment-marker lookup).
    walked_from: str


@dataclass
class Mismatch:
    """A Blocks link whose API direction contradicts operator intent."""

    link_id: str
    intent_blocked: str
    intent_blocker: str
    actual_inward: str
    actual_outward: str
    note: str = ""


@dataclass
class AuditResult:
    scanned_tickets: int = 0
    scanned_links: int = 0
    intent_recovered: int = 0
    matches: int = 0
    mismatches: list[Mismatch] = field(default_factory=list)
    unverifiable: list[str] = field(default_factory=list)
    # ``fixed`` entries record the link_id that was deleted + the new
    # (corrected) link payload that was created, so the rollback file
    # can mirror-restore the original direction if needed.
    fixed: list[dict[str, Any]] = field(default_factory=list)


def _fetch_open_tickets_with_links(client: jd.DispatchClient) -> list[dict]:
    """Return open OP tickets carrying at least one Blocks link."""
    resp = jd._request(
        client,
        "POST",
        "/search/jql",
        {
            "jql": AUDIT_JQL,
            "fields": ["issuelinks", "status"],
            "maxResults": 500,
        },
    )
    return resp.get("issues", []) or []


def _fetch_blocked_ticket_comments(
    client: jd.DispatchClient, key: str
) -> list[str]:
    """Return ADF-flattened comment bodies on ``key`` (oldest → newest)."""
    resp = jd._request(client, "GET", f"/issue/{key}/comment?orderBy=created")
    comments: list[str] = []
    for comment in resp.get("comments", []) or []:
        body = comment.get("body")
        comments.append(fc._adf_to_text(body))
    return comments


def _recover_intent(comments: list[str]) -> list[str]:
    """Return blocker keys named by ``[blocked-by-link]`` / file-coordinator markers."""
    intents: list[str] = []
    seen: set[str] = set()
    for comment in comments:
        for pattern in INTENT_MARKERS:
            for match in pattern.finditer(comment):
                key = match.group(1)
                if key not in seen:
                    seen.add(key)
                    intents.append(key)
    return intents


def collect_intented_links(
    client: jd.DispatchClient, issues: list[dict]
) -> tuple[list[IntentedLink], list[str], int, int]:
    """Walk ``issues`` once and pair each Blocks link with its operator intent.

    Returns ``(intented_links, unverifiable_keys, scanned_links, intent_count)``.
    ``unverifiable_keys`` are tickets that hold a Blocks link but have no
    comment marker, so the audit cannot reason about intended direction.
    """
    intented: list[IntentedLink] = []
    unverifiable: list[str] = []
    scanned_links = 0
    intent_count = 0
    visited_links: set[str] = set()

    for issue in issues:
        key = issue.get("key", "")
        fields = issue.get("fields") or {}
        links = (fields.get("issuelinks") or [])
        blocks_links = [
            link for link in links
            if ((link.get("type") or {}).get("name")) == BLOCKS_TYPE
        ]
        if not blocks_links:
            continue
        # Pull the comment trail once per ticket; intent markers always
        # live on the blocked ticket so we only need this view.
        comments = _fetch_blocked_ticket_comments(client, key)
        intents = _recover_intent(comments)
        if intents:
            intent_count += 1
        else:
            unverifiable.append(key)

        for link in blocks_links:
            link_id = str(link.get("id") or "")
            if not link_id or link_id in visited_links:
                continue
            visited_links.add(link_id)
            scanned_links += 1
            inward = ((link.get("inwardIssue") or {}).get("key")) or ""
            outward = ((link.get("outwardIssue") or {}).get("key")) or ""
            if not inward or not outward:
                continue
            partner = outward if inward == key else inward
            if not intents or partner not in intents:
                # No recoverable intent for this specific link; skip — the
                # ticket-level unverifiable list already captured it.
                continue
            intented.append(
                IntentedLink(
                    link_id=link_id,
                    inward_key=inward,
                    outward_key=outward,
                    intent_blocked=key,
                    intent_blocker=partner,
                    walked_from=key,
                )
            )
    return intented, unverifiable, scanned_links, intent_count


def classify(link: IntentedLink) -> Mismatch | None:
    """Return ``Mismatch`` when API direction contradicts operator intent.

    Atlassian REST: POST ``{inwardIssue: A, outwardIssue: B, type: Blocks}``
    yields ``A blocks B``. So the correct mapping is
    ``inward == blocker, outward == blocked``.
    """
    correct = (
        link.inward_key == link.intent_blocker
        and link.outward_key == link.intent_blocked
    )
    if correct:
        return None
    return Mismatch(
        link_id=link.link_id,
        intent_blocked=link.intent_blocked,
        intent_blocker=link.intent_blocker,
        actual_inward=link.inward_key,
        actual_outward=link.outward_key,
        note="inward/outward swapped vs. operator intent",
    )


def _delete_link(client: jd.DispatchClient, link_id: str) -> None:
    jd._request(client, "DELETE", f"/issueLink/{link_id}")


def fix_mismatch(client: jd.DispatchClient, mismatch: Mismatch) -> dict[str, Any]:
    """Delete the wrong-direction link and recreate it with the correct direction.

    Returns a rollback record: the deleted link's ID + the inward/outward
    keys it carried before deletion. Replaying ``POST /issueLink`` with
    those values restores the original (wrong) direction if needed.
    """
    rollback = {
        "deleted_link_id": mismatch.link_id,
        "deleted_inward_key": mismatch.actual_inward,
        "deleted_outward_key": mismatch.actual_outward,
        "restored_blocked_key": mismatch.intent_blocked,
        "restored_blocker_key": mismatch.intent_blocker,
    }
    _delete_link(client, mismatch.link_id)
    fc.jira_create_issue_link(
        client,
        inward=mismatch.intent_blocker,
        outward=mismatch.intent_blocked,
        link_type=BLOCKS_TYPE,
    )
    return rollback


def run_audit(
    client: jd.DispatchClient, *, fix: bool, dry_run: bool
) -> AuditResult:
    issues = _fetch_open_tickets_with_links(client)
    intented, unverifiable, scanned_links, intent_count = collect_intented_links(
        client, issues
    )
    result = AuditResult(
        scanned_tickets=len(issues),
        scanned_links=scanned_links,
        intent_recovered=intent_count,
        unverifiable=unverifiable,
    )
    for link in intented:
        mismatch = classify(link)
        if mismatch is None:
            result.matches += 1
            continue
        result.mismatches.append(mismatch)
        if fix and not dry_run:
            rollback = fix_mismatch(client, mismatch)
            result.fixed.append(rollback)
    return result


def render_report(result: AuditResult, *, fix_applied: bool) -> str:
    lines = [
        "# blockedBy direction audit",
        "",
        f"- scanned tickets: {result.scanned_tickets}",
        f"- scanned Blocks links: {result.scanned_links}",
        f"- intent recovered (tickets w/ marker): {result.intent_recovered}",
        f"- direction matches: {result.matches}",
        f"- direction mismatches: {len(result.mismatches)}",
        f"- unverifiable (no marker): {len(result.unverifiable)}",
        "",
    ]
    if result.mismatches:
        lines.append("## Mismatches (direction inverted vs. operator intent)")
        lines.append("")
        for mismatch in result.mismatches:
            lines.append(
                f"- link `{mismatch.link_id}`: "
                f"intent `{mismatch.intent_blocked}` blockedBy "
                f"`{mismatch.intent_blocker}` — "
                f"API has inward=`{mismatch.actual_inward}`, "
                f"outward=`{mismatch.actual_outward}` ({mismatch.note})"
            )
        lines.append("")
    if result.unverifiable:
        lines.append("## Unverifiable (no intent marker found)")
        lines.append("")
        for key in result.unverifiable:
            lines.append(f"- {key}")
        lines.append("")
    if fix_applied and result.fixed:
        lines.append("## Repaired by --fix")
        lines.append("")
        for entry in result.fixed:
            lines.append(
                f"- deleted link `{entry['deleted_link_id']}` "
                f"(was inward=`{entry['deleted_inward_key']}`, "
                f"outward=`{entry['deleted_outward_key']}`) → recreated "
                f"as `{entry['restored_blocker_key']}` blocks "
                f"`{entry['restored_blocked_key']}`"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def _today_token() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def write_artifacts(
    result: AuditResult, *, fix_applied: bool, output_dir: Path
) -> tuple[Path, Path | None]:
    """Write the Markdown report and (when ``--fix``) the rollback JSON."""
    output_dir.mkdir(parents=True, exist_ok=True)
    date_token = _today_token()
    report_path = output_dir / f"blockedby-direction-audit-{date_token}.md"
    report_path.write_text(render_report(result, fix_applied=fix_applied))
    rollback_path: Path | None = None
    if fix_applied and result.fixed:
        rollback_path = output_dir / f".audit-rollback-{date_token}.json"
        rollback_path.write_text(json.dumps(result.fixed, indent=2, sort_keys=True))
    return report_path, rollback_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent-class", default="subscription-codex")
    parser.add_argument(
        "--fix", action="store_true",
        help="Delete + recreate wrong-direction links; emits rollback JSON.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report mismatches but do not modify JIRA even with --fix.",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable report on stdout",
    )
    parser.add_argument(
        "--output-dir", default=str(AUDIT_REPORT_DIR),
        help="Where to write the report + rollback file (default: docs/audit/).",
    )
    args = parser.parse_args(argv)

    try:
        client = jd.make_client(args.agent_class)
        result = run_audit(client, fix=args.fix, dry_run=args.dry_run)
    except (RuntimeError, OSError) as exc:
        print(f"audit_blockedby_directions: JIRA error: {exc}", file=sys.stderr)
        return 2

    report_path, rollback_path = write_artifacts(
        result, fix_applied=args.fix and not args.dry_run,
        output_dir=Path(args.output_dir),
    )
    if args.json:
        payload = {
            "scanned_tickets": result.scanned_tickets,
            "scanned_links": result.scanned_links,
            "intent_recovered": result.intent_recovered,
            "matches": result.matches,
            "mismatches": [asdict(m) for m in result.mismatches],
            "unverifiable": result.unverifiable,
            "fixed": result.fixed,
            "report_path": str(report_path),
            "rollback_path": str(rollback_path) if rollback_path else None,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_report(result, fix_applied=args.fix and not args.dry_run))
        print(f"report written: {report_path}")
        if rollback_path:
            print(f"rollback file: {rollback_path}")

    # Exit 1 only if mismatches remain AFTER any --fix applied.
    remaining = (
        len(result.mismatches) - len(result.fixed)
        if args.fix and not args.dry_run
        else len(result.mismatches)
    )
    return 1 if remaining > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
