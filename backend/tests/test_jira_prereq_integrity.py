"""CI contract test for JIRA Prerequisites integrity.

Per ``docs/sop/jira-ticket-conventions.md`` §12. Calls
``scripts/jira_prereq_audit.py --json`` and fails CI on cycles or
broken refs. Stale schema_locks are warnings (during transition
period), not failures.

This test is the gate that prevents a PR from introducing a
``blocks_on`` cycle into the dependency graph (which would
permanently lock all tickets on the cycle).

Network access required: hits soraapp.atlassian.net via Claude bot
credentials at ~/.config/omnisight/jira-claude.env. Skip when those
files are absent (CI runners without secrets injected).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_SCRIPT = REPO_ROOT / "scripts" / "jira_prereq_audit.py"
JIRA_TOKEN_PATH = Path("~/.config/omnisight/jira-claude-token").expanduser()


def _has_jira_credentials() -> bool:
    return JIRA_TOKEN_PATH.is_file()


def _run_audit_json() -> dict:
    """Invoke audit script in JSON mode, return parsed report."""
    result = subprocess.run(
        ["python3", str(AUDIT_SCRIPT), "--json"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    if result.returncode == 2:
        pytest.skip(
            "jira_prereq_audit.py is a skeleton — implement before this test enforces"
        )
    return json.loads(result.stdout)


@pytest.mark.skipif(
    not _has_jira_credentials(),
    reason="JIRA Claude bot credentials not present (CI runner without secrets)",
)
def test_no_blocks_on_cycles() -> None:
    """No cycles in the blocks_on dependency graph.

    A cycle locks all tickets on the cycle permanently — this is fatal
    and must be caught before merge.
    """
    report = _run_audit_json()
    assert report["cycles"] == [], (
        f"blocks_on cycles detected:\n"
        + "\n".join(" → ".join(c) for c in report["cycles"])
    )


@pytest.mark.skipif(
    not _has_jira_credentials(),
    reason="JIRA Claude bot credentials not present (CI runner without secrets)",
)
def test_no_broken_blocks_on_refs() -> None:
    """All blocks_on / soft_prereqs target keys must exist + not Archived."""
    report = _run_audit_json()
    assert report["broken_refs"] == [], (
        f"Broken Prerequisites references:\n"
        + "\n".join(f"{src} blocks_on {tgt}" for src, tgt in report["broken_refs"])
    )


@pytest.mark.skipif(
    not _has_jira_credentials(),
    reason="JIRA Claude bot credentials not present (CI runner without secrets)",
)
def test_stale_schema_locks_warn_only() -> None:
    """Stale schema_locks log warning but don't fail (transition period).

    Once all schema_locks point to Accepted (not Superseded) ADRs,
    this test can be tightened to assert empty.
    """
    report = _run_audit_json()
    if report["stale_schema_locks"]:
        # WARN level — print but don't fail
        for ticket, adr in report["stale_schema_locks"]:
            print(f"WARN: {ticket} references stale {adr}")
