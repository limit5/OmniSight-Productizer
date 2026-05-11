#!/usr/bin/env python3
"""OP-874 — flag raw ``POST /issueLink`` calls outside ``file_coordinator``.

Background
----------
``POST /issueLink`` is the Atlassian REST endpoint for creating Blocks /
Relates / Cloners links, and its ``inwardIssue`` / ``outwardIssue``
parameter names are ambiguous enough to invite the silent direction
inversion that produced Gerrit #387 (OP-858, 2026-05-11). The intent-named
``backend.agents.file_coordinator.add_blocked_by`` helper exists so
operator code never speaks the raw schema; this lint rule enforces
that contract.

What this script flags
----------------------
Any line in the repo that:

* contains the literal substring ``/issueLink`` *and*
* shows POST intent (``"POST"``, ``method="POST"`` or the helper name
  ``_request_idempotent``/``_request`` adjacent on the same expression),
* and lives outside the allowlisted callers
  (``backend/agents/file_coordinator.py`` + this script's own
  docstring/comments).

It deliberately does NOT flag ``DELETE /issueLink/<id>`` (used by the
audit ``--fix`` path) or docstring mentions of the endpoint.

Exit codes
----------
0 — clean. 1 — at least one new caller flagged.

Usage
-----
::

  python3 scripts/check_issuelink_post_callers.py [--verbose]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Files allowed to speak the raw POST /issueLink schema. ``file_coordinator``
# owns both the low-level ``jira_create_issue_link`` and the intent-named
# ``add_blocked_by`` wrapper; the audit script's ``--fix`` restore path
# uses the same low-level helper via ``fc.jira_create_issue_link``, so it
# never hits this regex itself (it doesn't call ``_request`` directly).
ALLOWED_FILES = {
    "backend/agents/file_coordinator.py",
}

SKIP_DIRS = {
    ".git", "node_modules", ".next", "dist", "build", "__pycache__",
    ".venv", "venv", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "coverage", "playwright-report", "test-results", ".turbo",
}

# Match a Python expression that combines POST intent + the /issueLink path
# on the same line. Examples flagged:
#   _request_idempotent(client, "POST", "/issueLink", ...)
#   urllib.request.Request(url + "/issueLink", method="POST")
# Examples NOT flagged:
#   "DELETE", "/issueLink/123"   (audit --fix path)
#   docstring "calling POST /issueLink with inward/outward..."  (markdown prose)
RAW_POST_RE = re.compile(
    r'("POST"\s*,\s*"\/issueLink"|\/issueLink".*method\s*=\s*"POST")'
)

# Allow-marker token: a test fixture or doc that needs to demonstrate the
# anti-pattern in literal code must include this token to suppress the check.
ALLOW_MARKER_TOKEN = "op-874-allow-raw-issuelink-post"


def _is_in_skip_dir(rel: Path) -> bool:
    parts = set(rel.parts)
    return bool(parts & SKIP_DIRS)


def scan(verbose: bool = False) -> list[tuple[Path, int, str]]:
    hits: list[tuple[Path, int, str]] = []
    self_path = Path(__file__).resolve()
    for path in REPO_ROOT.rglob("*.py"):
        if path.resolve() == self_path:
            continue
        rel = path.relative_to(REPO_ROOT)
        if _is_in_skip_dir(rel):
            continue
        if rel.as_posix() in ALLOWED_FILES:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if ALLOW_MARKER_TOKEN in content:
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            if RAW_POST_RE.search(line):
                hits.append((rel, lineno, line.rstrip()))
    if verbose:
        print(f"check_issuelink_post_callers: scanned {sum(1 for _ in REPO_ROOT.rglob('*.py'))} files")
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    hits = scan(verbose=args.verbose)
    if not hits:
        if args.verbose:
            print("OK — no raw POST /issueLink callers outside file_coordinator.py")
        return 0
    print(
        "FAIL — raw POST /issueLink call detected outside file_coordinator.py:",
        file=sys.stderr,
    )
    for rel, lineno, line in hits:
        snippet = line if len(line) <= 140 else line[:137] + "..."
        print(f"  {rel}:{lineno}: {snippet}", file=sys.stderr)
    print(
        "\nUse backend.agents.file_coordinator.add_blocked_by(blocked_key, "
        "blocker_key) instead. See docs/sop/jira-ticket-conventions.md §19.\n"
        f"If this is a deliberate fixture or doc, add '{ALLOW_MARKER_TOKEN}' "
        "anywhere in the file to suppress.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
