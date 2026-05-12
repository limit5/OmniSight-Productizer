#!/usr/bin/env python3
"""OP-1015 runner-fresh branch triage and supervised cleanup.

Lists local ``feature/OP-*-runner-fresh`` branches with last-touch metadata,
linked JIRA status, and a conservative cleanup classification. By default this
script only reports; pass ``--delete-safe`` for the one-shot operator-supervised
cleanup path.
"""
from __future__ import annotations

import argparse
import csv
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch as jd  # noqa: E402


LOG = logging.getLogger("orphan_branch_triage")
BRANCH_PATTERN = "feature/OP-*-runner-fresh"
BRANCH_RE = re.compile(r"^feature/(OP-\d+)(?:-.+)?-runner-fresh$")
DEFAULT_ABANDONED_DAYS = 3
JIRA_BATCH_SIZE = 50


@dataclass(frozen=True)
class JiraStatus:
    key: str
    status: str
    status_category: str
    assignee: str
    updated: str

    @property
    def is_closed(self) -> bool:
        return self.status_category.lower() in {"done", "完了"}


@dataclass(frozen=True)
class BranchInfo:
    branch: str
    ticket_key: str
    last_touch: datetime
    checked_out: bool
    worktree: str
    local_commits: int
    has_diff: bool
    jira: JiraStatus | None
    classification: str
    action: str
    reason: str


def _run_git(worktree: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _parse_git_datetime(raw: str) -> datetime:
    return datetime.fromisoformat(raw.strip()).astimezone(timezone.utc)


def _runner_branches(worktree: Path) -> list[tuple[str, datetime]]:
    result = _run_git(
        worktree,
        [
            "for-each-ref",
            "--format=%(refname:short)%09%(committerdate:iso-strict)",
            f"refs/heads/{BRANCH_PATTERN}",
        ],
    )
    branches: list[tuple[str, datetime]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        branch, touched = line.split("\t", 1)
        branches.append((branch, _parse_git_datetime(touched)))
    return sorted(branches)


def _checked_out_branches(worktree: Path) -> dict[str, str]:
    result = _run_git(worktree, ["worktree", "list", "--porcelain"])
    branches: dict[str, str] = {}
    current_path = ""
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line.removeprefix("worktree ").strip()
        elif line.startswith("branch "):
            ref = line.removeprefix("branch refs/heads/").strip()
            branches[ref] = current_path
    return branches


def _branch_base(worktree: Path, branch: str) -> str | None:
    result = _run_git(worktree, ["merge-base", branch, "develop"], check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _local_commit_count(worktree: Path, branch: str) -> int:
    base = _branch_base(worktree, branch)
    if base is None:
        return 0
    result = _run_git(worktree, ["rev-list", "--count", f"{base}..{branch}"])
    return int(result.stdout.strip() or "0")


def _has_diff(worktree: Path, branch: str) -> bool:
    base = _branch_base(worktree, branch)
    if base is None:
        return False
    result = _run_git(worktree, ["diff", "--quiet", base, branch], check=False)
    return result.returncode != 0


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[idx : idx + size] for idx in range(0, len(items), size)]


def fetch_jira_statuses(
    client: jd.DispatchClient,
    ticket_keys: list[str],
) -> dict[str, JiraStatus]:
    statuses: dict[str, JiraStatus] = {}
    for batch in _chunks(sorted(set(ticket_keys)), JIRA_BATCH_SIZE):
        quoted = ", ".join(batch)
        resp = jd._request(
            client,
            "POST",
            "/search/jql",
            {
                "jql": f"key in ({quoted}) ORDER BY key ASC",
                "fields": ["status", "assignee", "updated"],
                "maxResults": len(batch),
            },
        )
        for issue in resp.get("issues", []) or []:
            fields: dict[str, Any] = issue.get("fields") or {}
            status_data = fields.get("status") or {}
            category = status_data.get("statusCategory") or {}
            assignee = fields.get("assignee") or {}
            key = str(issue.get("key") or "")
            statuses[key] = JiraStatus(
                key=key,
                status=str(status_data.get("name") or "unknown"),
                status_category=str(category.get("name") or "unknown"),
                assignee=str(assignee.get("displayName") or assignee.get("emailAddress") or ""),
                updated=str(fields.get("updated") or ""),
            )
    return statuses


def classify_branch(
    *,
    now: datetime,
    branch: str,
    last_touch: datetime,
    checked_out: bool,
    local_commits: int,
    has_diff: bool,
    jira: JiraStatus | None,
    abandoned_days: int,
) -> tuple[str, str, str]:
    age_days = int((now - last_touch).total_seconds() // 86400)
    if checked_out:
        return "claimed_open", "keep", "branch is checked out by an active worktree"
    if jira is None:
        if local_commits or has_diff:
            return "needs_salvage_review", "keep", "branch has local commits or diff"
        return "jira_unknown", "keep", "JIRA status unavailable"
    if jira.is_closed:
        suffix = "; branch has local commits/diff" if local_commits or has_diff else ""
        return "ticket_closed", "delete", f"ticket status is {jira.status}{suffix}"
    if local_commits or has_diff:
        return "needs_salvage_review", "keep", "open-ticket branch has local commits or diff"
    if age_days >= abandoned_days:
        return "abandoned", "delete", f"open ticket branch untouched for {age_days}d"
    return "claimed_open", "keep", f"open ticket branch is {age_days}d old"


def triage(
    *,
    worktree: Path,
    client: jd.DispatchClient | None,
    now: datetime,
    abandoned_days: int,
) -> list[BranchInfo]:
    raw_branches = _runner_branches(worktree)
    checked_out = _checked_out_branches(worktree)
    ticket_keys: list[str] = []
    branch_tickets: dict[str, str] = {}
    for branch, _ in raw_branches:
        match = BRANCH_RE.match(branch)
        if match:
            ticket = match.group(1)
            ticket_keys.append(ticket)
            branch_tickets[branch] = ticket

    jira_statuses = fetch_jira_statuses(client, ticket_keys) if client is not None else {}

    rows: list[BranchInfo] = []
    for branch, last_touch in raw_branches:
        ticket = branch_tickets.get(branch, "")
        local_commits = _local_commit_count(worktree, branch)
        has_diff = _has_diff(worktree, branch)
        jira = jira_statuses.get(ticket)
        classification, action, reason = classify_branch(
            now=now,
            branch=branch,
            last_touch=last_touch,
            checked_out=branch in checked_out,
            local_commits=local_commits,
            has_diff=has_diff,
            jira=jira,
            abandoned_days=abandoned_days,
        )
        rows.append(
            BranchInfo(
                branch=branch,
                ticket_key=ticket,
                last_touch=last_touch,
                checked_out=branch in checked_out,
                worktree=checked_out.get(branch, ""),
                local_commits=local_commits,
                has_diff=has_diff,
                jira=jira,
                classification=classification,
                action=action,
                reason=reason,
            )
        )
    return rows


def render_csv(rows: list[BranchInfo]) -> str:
    fieldnames = [
        "branch",
        "ticket",
        "last_touch",
        "jira_status",
        "jira_status_category",
        "jira_assignee",
        "checked_out",
        "worktree",
        "local_commits",
        "has_diff",
        "classification",
        "action",
        "reason",
    ]
    from io import StringIO

    buf = StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "branch": row.branch,
                "ticket": row.ticket_key,
                "last_touch": row.last_touch.isoformat(),
                "jira_status": row.jira.status if row.jira else "",
                "jira_status_category": row.jira.status_category if row.jira else "",
                "jira_assignee": row.jira.assignee if row.jira else "",
                "checked_out": str(row.checked_out).lower(),
                "worktree": row.worktree,
                "local_commits": row.local_commits,
                "has_diff": str(row.has_diff).lower(),
                "classification": row.classification,
                "action": row.action,
                "reason": row.reason,
            }
        )
    return buf.getvalue()


def delete_safe_branches(worktree: Path, rows: list[BranchInfo]) -> list[str]:
    deleted: list[str] = []
    for row in rows:
        if row.action != "delete":
            continue
        _run_git(worktree, ["branch", "-D", row.branch])
        deleted.append(row.branch)
    return deleted


def _parse_now(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", type=Path, default=REPO_ROOT)
    parser.add_argument("--agent-class", default="subscription-codex")
    parser.add_argument("--abandoned-days", type=int, default=DEFAULT_ABANDONED_DAYS)
    parser.add_argument("--now", default=None, help="ISO-8601 timestamp for replay")
    parser.add_argument("--no-jira", action="store_true", help="Skip JIRA lookup; report git-only safety data")
    parser.add_argument("--delete-safe", action="store_true", help="Delete rows classified with action=delete")
    parser.add_argument("--output", type=Path, default=None, help="Optional CSV output path")
    parser.add_argument("--quiet", action="store_true", help="Only print summary")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    client = None if args.no_jira else jd.make_client(args.agent_class)
    rows = triage(
        worktree=args.worktree,
        client=client,
        now=_parse_now(args.now),
        abandoned_days=args.abandoned_days,
    )
    csv_text = render_csv(rows)
    if args.output:
        args.output.write_text(csv_text, encoding="utf-8")
    if not args.quiet:
        print(csv_text, end="")

    deleted = delete_safe_branches(args.worktree, rows) if args.delete_safe else []
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.classification] = counts.get(row.classification, 0) + 1
    remaining = len(rows) - len(deleted)
    LOG.info(
        "orphan_branch_triage_done total=%s deleted=%s remaining=%s counts=%s",
        len(rows),
        len(deleted),
        remaining,
        counts,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
