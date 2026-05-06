"""JIRA Prerequisites audit — cycles, broken refs, stale schema_locks.

Per ``docs/sop/jira-ticket-conventions.md`` §12. Operator-runnable,
read-only. CI also runs this in JSON mode via
``backend/tests/test_jira_prereq_integrity.py`` to fail PR merge when
new tickets introduce a cycle.

Builds a directed graph from every OP-project ticket's
``blocks_on`` links + Prerequisites YAML, then:

1. DFS for cycles → ERR
2. Resolve every blocks_on / soft_prereqs target → WARN if missing/Archived
3. Resolve every schema_locks source_priority → WARN if Superseded
4. Output human-readable report (text mode) or audit_report.json (JSON mode)

Exit codes:
- 0  no errors (warnings OK)
- 1  cycles or broken refs detected (CI must fail)
- 2  audit script itself errored (network / auth / config)

Usage::

    python3 scripts/jira_prereq_audit.py             # text report
    python3 scripts/jira_prereq_audit.py --json      # CI-consumable JSON
    python3 scripts/jira_prereq_audit.py --component MP   # filter scope
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class TicketRef:
    """Minimal JIRA ticket view for audit."""

    key: str
    component: str
    status: str
    summary: str
    blocks_on: tuple[str, ...]
    soft_prereqs: tuple[str, ...]
    schema_locks: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class AuditReport:
    """Audit output. JSON-serialisable."""

    cycles: tuple[tuple[str, ...], ...]  # one tuple per cycle, ticket keys in order
    broken_refs: tuple[tuple[str, str], ...]  # (source_key, target_key)
    stale_schema_locks: tuple[tuple[str, str], ...]  # (ticket_key, adr_id)
    healthy_count: int
    total_scanned: int


# ── JIRA fetch ─────────────────────────────────────────────────────


def fetch_all_tickets(component_filter: str | None = None) -> list[TicketRef]:
    """Fetch all tickets in OP project (optionally filtered by Component).

    Uses Claude bot credentials from ~/.config/omnisight/jira-claude.env
    + ~/.config/omnisight/jira-claude-token. Pages through JQL search
    until exhausted.

    Parses Prerequisites YAML from each ticket description (regex
    ``## Prerequisites\\n```yaml ... ```\\n``).
    """
    raise NotImplementedError(
        "skeleton — JQL paginate, ADF→markdown→YAML parse, build TicketRef list"
    )


# ── Graph algorithms ───────────────────────────────────────────────


def detect_cycles(tickets: list[TicketRef]) -> list[tuple[str, ...]]:
    """DFS-based cycle detection on blocks_on graph.

    Returns list of cycles, each cycle is a tuple of ticket keys in
    traversal order. Empty list = no cycles.
    """
    raise NotImplementedError(
        "skeleton — Tarjan or 3-colour DFS over directed graph"
    )


def find_broken_refs(tickets: list[TicketRef]) -> list[tuple[str, str]]:
    """Identify blocks_on / soft_prereqs targets that don't exist or are Archived."""
    raise NotImplementedError("skeleton — set difference vs known ticket keys")


def find_stale_schema_locks(tickets: list[TicketRef]) -> list[tuple[str, str]]:
    """Identify schema_locks pointing to ADRs that are Superseded.

    Reads docs/adr/README.md index for ADR statuses.
    """
    raise NotImplementedError("skeleton — parse ADR index, cross-check refs")


# ── Reporting ──────────────────────────────────────────────────────


def render_text(report: AuditReport) -> str:
    """Human-readable report matching §12 sample output format."""
    raise NotImplementedError("skeleton — emit ERR/WARN lines + summary")


def render_json(report: AuditReport) -> str:
    """JSON for CI consumption. Schema used by test_jira_prereq_integrity."""
    raise NotImplementedError("skeleton — dataclasses.asdict + json.dumps")


# ── CLI ────────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="JSON output for CI")
    parser.add_argument("--component", help="Filter by Component (e.g. MP)")
    args = parser.parse_args(argv)

    try:
        tickets = fetch_all_tickets(component_filter=args.component)
        report = AuditReport(
            cycles=tuple(detect_cycles(tickets)),
            broken_refs=tuple(find_broken_refs(tickets)),
            stale_schema_locks=tuple(find_stale_schema_locks(tickets)),
            healthy_count=len(tickets),  # adjusted in real impl
            total_scanned=len(tickets),
        )
    except NotImplementedError:
        print("ERROR: scripts/jira_prereq_audit.py is a skeleton", file=sys.stderr)
        return 2

    output = render_json(report) if args.json else render_text(report)
    print(output)

    if report.cycles or report.broken_refs:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
