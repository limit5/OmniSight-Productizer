#!/usr/bin/env python3
"""OP-741 operator escape: force-submit a Gerrit change.

Adds the special ``runner-force-submit`` marker and writes an operator
audit comment. The Gerrit submit-rule side consumes the marker to bypass
the Verified gate; this script is intentionally small and JIRA-backed so
the exception is visible in the ticket history.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from backend.agents import jira_dispatch

FORCE_SUBMIT_LABEL = "runner-force-submit"


@dataclass(frozen=True)
class ForceSubmitResult:
    change_number: str
    jira_key: str
    label: str
    audit_comment: str
    dry_run: bool


def build_audit_comment(change_number: str, reason: str, operator: str) -> str:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return (
        "[ci-force-submit]\n"
        f"change={change_number}\n"
        f"operator={operator}\n"
        f"at={stamp}\n"
        f"reason={reason}\n\n"
        "Verified gate bypassed via runner-force-submit operator escape."
    )


def apply_force_submit(
    *,
    change_number: str,
    jira_key: str,
    reason: str,
    operator: str,
    agent_class: str = "subscription-codex",
    dry_run: bool = False,
    run_command=subprocess.run,
) -> ForceSubmitResult:
    """Apply the force-submit marker and audit comment."""

    comment = build_audit_comment(change_number, reason, operator)
    if not dry_run:
        client = jira_dispatch.make_client(agent_class)
        jira_dispatch.add_label(client, jira_key, FORCE_SUBMIT_LABEL)
        jira_dispatch.add_comment(client, jira_key, comment)
        run_command(
            [
                "ssh",
                "-p",
                str(jira_dispatch.GERRIT_SSH_PORT),
                f"{jira_dispatch._GERRIT_AUTH_BY_CLASS[agent_class][0]}@{jira_dispatch.GERRIT_SSH_HOST}",
                "gerrit",
                "set-topic",
                f"--topic={FORCE_SUBMIT_LABEL}",
                str(change_number),
            ],
            check=False,
        )
    return ForceSubmitResult(
        change_number=change_number,
        jira_key=jira_key,
        label=FORCE_SUBMIT_LABEL,
        audit_comment=comment,
        dry_run=dry_run,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("change_number")
    parser.add_argument("--jira-key", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument(
        "--operator",
        default=os.environ.get("USER", "unknown-operator"),
    )
    parser.add_argument(
        "--agent-class",
        default=os.environ.get("OMNISIGHT_RUNNER_CLASS", "subscription-codex"),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    result = apply_force_submit(
        change_number=args.change_number,
        jira_key=args.jira_key,
        reason=args.reason,
        operator=args.operator,
        agent_class=args.agent_class,
        dry_run=args.dry_run,
    )
    print(result.audit_comment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
