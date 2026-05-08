"""Scope-label path predictions for runner file-level pickup mutex.

Values are advisory globs used only when a ticket lacks an explicit
``Files / Paths`` section. False negatives are acceptable; false positives
only delay pickup by one runner tick.
"""
from __future__ import annotations


# OP-795 Bug 1: previous value `{"docs/sop/lessons/*.md"}` matched any open PS
# touching any per-file lesson, blocking every other ticket as soon as one
# lesson PS was open. Per-file lessons are uniquely named L-OP-{ticket}-*.md
# so they never collide between different tickets — handled per-ticket in
# predict_target_files() via ALWAYS_TOUCHED_TEMPLATE rather than a wildcard
# glob shared across all tickets.
ALWAYS_TOUCHED: set[str] = set()

# Per-ticket lesson path template — predict_target_files() formats this with
# the ticket key so two tickets writing different lessons don't collide.
ALWAYS_TOUCHED_TEMPLATE: str = "docs/sop/lessons/L-{ticket}-*.md"


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
