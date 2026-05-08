"""Scope-label path predictions for runner file-level pickup mutex.

Values are advisory globs used only when a ticket lacks an explicit
``Files / Paths`` section. False negatives are acceptable; false positives
only delay pickup by one runner tick.
"""
from __future__ import annotations


SCOPE_TO_PATHS: dict[str, set[str]] = {
    "runner-pipeline": {
        "auto-runner-jira.py",
        "backend/agents/jira_*.py",
        "backend/agents/scheduler.py",
        "backend/tests/test_auto_runner_jira.py",
        "backend/tests/test_jira_dispatch.py",
    },
    "jira-dispatch": {
        "backend/agents/jira_*.py",
        "backend/tests/test_jira_dispatch.py",
    },
    "docs-sop": {
        "docs/sop/*.md",
    },
}
