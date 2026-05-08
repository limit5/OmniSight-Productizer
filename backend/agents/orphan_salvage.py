"""Recover runner feature-branch commits stranded before Gerrit push.

The JIRA runner normally pushes a successful CLI commit immediately after the
agent exits. If the runner crashes in that narrow window, the next tick's fresh
branch switch can abandon the local branch. This module scans those runner
branches at tick start and pushes real, unreviewed OP commits to Gerrit.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path

from backend.agents import jira_dispatch
from backend.agents.circuit_breaker import BREAKERS

log = logging.getLogger(__name__)

BRANCH_PATTERN = "feature/OP-*-runner-fresh"
MAX_ORPHAN_BRANCHES = 5
_TICKET_RE = re.compile(r"\[OP-(\d+)\]")


def salvage_orphan_commits(worktree_path: Path, agent_class: str) -> int:
    """Push orphaned runner branch commits to Gerrit.

    Returns the number of branches successfully salvaged. Branches are skipped
    when their OP ticket already appears in an open Gerrit change, their diff is
    empty, or their branch-local commits are not all for the same OP ticket.
    """
    branches = _runner_branches(worktree_path)
    if len(branches) > MAX_ORPHAN_BRANCHES:
        detail = (
            f"orphan salvage halted: {len(branches)} {BRANCH_PATTERN} branches "
            "detected; this suggests a systematic runner failure, not one crash."
        )
        log.critical("orphan_salvage_too_many_branches branches=%s", branches)
        jira_dispatch.notify_operator(
            channel="runner-alerts",
            severity="critical",
            detail=detail,
        )
        return 0

    if not branches:
        return 0

    open_change_subjects = fetch_open_change_subjects(agent_class)
    salvaged = 0
    jira_client = None

    for branch in branches:
        branch_info = _branch_ticket(worktree_path, branch)
        if branch_info is None:
            continue
        ticket, head_msg = branch_info

        if any(ticket in subject for subject in open_change_subjects):
            continue

        validation = _validate_branch_commits(worktree_path, branch, ticket)
        if validation is not None:
            log.error(
                "orphan_salvage_ambiguous_branch branch=%s ticket=%s reason=%s",
                branch,
                ticket,
                validation,
            )
            jira_dispatch.notify_operator(
                channel="runner-alerts",
                severity="high",
                detail=f"orphan salvage skipped {branch}: {validation}",
            )
            continue

        if _branch_diff_is_empty(worktree_path, branch):
            continue

        log.warning(
            "orphan_commit_detected branch=%s ticket=%s head_msg=%s",
            branch,
            ticket,
            head_msg[:80],
        )
        push_result = _push_branch_to_gerrit(worktree_path, agent_class, branch)
        if push_result.returncode == 0:
            log.info("orphan_salvaged branch=%s ticket=%s", branch, ticket)
            salvaged += 1
            try:
                if jira_client is None:
                    jira_client = jira_dispatch.make_client(agent_class)
                jira_dispatch.add_comment(
                    jira_client,
                    ticket,
                    (
                        "[runner-orphan-salvage] Recovered orphan commit from "
                        f"previous runner crash. Branch={branch}, "
                        f"head={head_msg[:80]}. Push result: "
                        f"{push_result.stdout[:200]}"
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - salvage already succeeded.
                log.error(
                    "orphan_salvage_comment_fail branch=%s ticket=%s err=%s",
                    branch,
                    ticket,
                    exc,
                )
        else:
            log.error(
                "orphan_salvage_push_fail branch=%s stderr=%s",
                branch,
                push_result.stderr[:200],
            )

    return salvaged


def fetch_open_change_subjects(agent_class: str) -> list[str]:
    """Return subjects for currently open Gerrit changes."""
    auth = jira_dispatch._GERRIT_AUTH_BY_CLASS.get(agent_class)
    if auth is None:
        raise ValueError(f"unknown agent_class for Gerrit auth: {agent_class}")
    user, ssh_key = auth
    result = BREAKERS["gerrit_ssh"].call(
        subprocess.run,
        [
            "ssh",
            "-i",
            str(ssh_key),
            "-p",
            str(jira_dispatch.GERRIT_SSH_PORT),
            f"{user}@{jira_dispatch.GERRIT_SSH_HOST}",
            "gerrit",
            "query",
            "--format=JSON",
            "is:open",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    result.check_returncode()

    subjects: list[str] = []
    for line in result.stdout.splitlines():
        try:
            change = json.loads(line)
        except json.JSONDecodeError:
            continue
        if change.get("type") == "stats":
            continue
        subject = change.get("subject")
        if subject:
            subjects.append(str(subject))
    return subjects


def _runner_branches(worktree_path: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(worktree_path), "branch", "--list", BRANCH_PATTERN],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip().lstrip("* ").strip() for line in result.stdout.splitlines() if line.strip()]


def _branch_ticket(worktree_path: Path, branch: str) -> tuple[str, str] | None:
    result = subprocess.run(
        ["git", "-C", str(worktree_path), "log", "-1", "--pretty=%B", branch],
        capture_output=True,
        text=True,
        check=True,
    )
    head_msg = result.stdout.strip()
    match = _TICKET_RE.search(head_msg)
    if not match:
        return None
    return f"OP-{match.group(1)}", head_msg


def _validate_branch_commits(worktree_path: Path, branch: str, ticket: str) -> str | None:
    base = _branch_base(worktree_path, branch)
    if base is None:
        return None

    result = subprocess.run(
        ["git", "-C", str(worktree_path), "log", "--format=%s", f"{base}..{branch}"],
        capture_output=True,
        text=True,
        check=True,
    )
    subjects = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not subjects:
        return None

    tickets = []
    missing = []
    for subject in subjects:
        match = _TICKET_RE.search(subject)
        if match is None:
            missing.append(subject)
        else:
            tickets.append(f"OP-{match.group(1)}")
    if missing:
        return "branch-local commit without [OP-XXX] subject"
    if set(tickets) != {ticket}:
        return f"branch-local commits span multiple tickets: {sorted(set(tickets))}"
    return None


def _branch_diff_is_empty(worktree_path: Path, branch: str) -> bool:
    base = _branch_base(worktree_path, branch) or f"{branch}~1"
    result = subprocess.run(
        ["git", "-C", str(worktree_path), "diff", "--quiet", base, branch],
        capture_output=True,
    )
    return result.returncode == 0


def _branch_base(worktree_path: Path, branch: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(worktree_path), "merge-base", branch, "develop"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _push_branch_to_gerrit(worktree_path: Path, agent_class: str, branch: str) -> subprocess.CompletedProcess:
    auth = jira_dispatch._GERRIT_AUTH_BY_CLASS.get(agent_class)
    if auth is None:
        raise ValueError(f"unknown agent_class for Gerrit auth: {agent_class}")
    _, ssh_key = auth
    if not ssh_key.exists():
        raise FileNotFoundError(f"SSH key not found at {ssh_key}")

    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key}"
    return BREAKERS["gerrit_ssh"].call(
        subprocess.run,
        [
            "git",
            "-C",
            str(worktree_path),
            "push",
            jira_dispatch._gerrit_ssh_url(agent_class),
            f"{branch}:refs/for/develop",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
