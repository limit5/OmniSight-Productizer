"""Scope-label path predictions for runner file-level pickup mutex.

Values are advisory globs used only when a ticket lacks an explicit
``Files / Paths`` section. False negatives are acceptable; false positives
only delay pickup by one runner tick.
"""
from __future__ import annotations

import re


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


# Files that have demonstrated repeat-conflict pattern (multiple tickets
# touching same file within hours). Pre-PS claim-level mutex is extra
# aggressive on these; see file_mutex_check's hot-file branch.
HOT_FILES: frozenset[str] = frozenset({
    "backend/agents/reflection_rag.py",       # OP-142 vs OP-143 conflict 2026-05-16
    "docs/operations/agent-rpg-system.md",    # OP-130 vs OP-137 vs others 2026-05-16
    "backend/metrics.py",                     # OP-1148/1165/1167/1168 codex pool storm 2026-05-16
    "backend/agents/capability_matrix.py",    # OP-1148/1165 pair
})


# OP-800: Files / Paths section parser, lifted out of jira_dispatch so it can
# be reused by predict_target_files and any future cross-PS overlap callers
# without an import-cycle. The token regex requires a literal `.` in the file
# name, which structurally rejects bare globs like ``backend/**/*.py`` (the
# Pattern 12 risk in OP-800 spec) — broad scope labels stay in
# :data:`SCOPE_TO_PATHS`, never in a ticket's declared Files section.
FILES_SECTION_RE = re.compile(
    r"(?ims)^#{0,6}\s*Files\s*/\s*Paths\s*$\n(?P<body>.*?)(?=^#{1,6}\s+\S|\Z)"
)
PATH_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+"
)


def parse_files_section(description: str) -> set[str]:
    """Extract concrete file paths from a ticket's ``Files / Paths`` section.

    Captures paths with explicit extensions (incl. ones annotated ``(NEW)`` /
    ``(new, ~250 LOC)`` — the annotation is just trailing text and does not
    interfere with extraction). Bare globs lacking a literal ``.`` (e.g.
    ``backend/**/*.py``) are rejected by ``PATH_TOKEN_RE`` to avoid the
    Pattern 12 false-positive cited in OP-800's Risk section.
    """
    match = FILES_SECTION_RE.search(description or "")
    if not match:
        return set()
    paths: set[str] = set()
    for raw in PATH_TOKEN_RE.findall(match.group("body")):
        token = raw.strip("`'\".,;:()[]{}<>")
        if token and "://" not in token:
            paths.add(token)
    return paths
