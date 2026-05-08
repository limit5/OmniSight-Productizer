"""One-shot Gerrit/JIRA bridge state catchup for OP-743.

Scans bridge-eligible OP tickets whose JIRA state is not Published, finds
the runner-pushed Gerrit change in comments, and reuses the bridge force-walk
path for changes that are already merged.
"""
from __future__ import annotations

import argparse
from typing import Sequence

from backend.agents import gerrit_jira_bridge as bridge


def run(agent_class: str, *, dry_run: bool = False) -> int:
    daemon = bridge.build_bridge(agent_class)
    processed = 0

    for issue in daemon.search_catchup_candidate_tickets():
        ticket_key = issue["key"]
        comments = daemon.fetch_issue_comments(ticket_key)
        change_numbers = bridge.extract_change_numbers_from_comments(comments)
        if not change_numbers:
            daemon.log("INFO", "backfill_no_change_mapping", ticket_key=ticket_key)
            continue
        if len(change_numbers) > 1:
            daemon.log(
                "ERROR",
                "multiple_changes_for_ticket",
                ticket_key=ticket_key,
                err=",".join(change_numbers),
            )
            continue
        change = daemon.query_gerrit_change(change_numbers[0])
        if change is None:
            continue
        if change.status.upper() != "MERGED":
            daemon.log(
                "INFO",
                "backfill_change_not_merged",
                ticket_key=ticket_key,
                change_id=change.change_id,
            )
            continue
        if dry_run:
            daemon.log(
                "INFO",
                "backfill_would_force_publish",
                ticket_key=ticket_key,
                change_id=change.change_id,
            )
            continue
        if daemon.process_ticket_for_change(ticket_key, change.change_id):
            processed += 1

    daemon.log("INFO", "backfill_done", processed=processed, dry_run=dry_run)
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent-class",
        default="subscription-claude",
        help="JIRA/Gerrit bot identity to use (default: subscription-claude).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log eligible merged tickets without transitioning JIRA.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return run(args.agent_class, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
