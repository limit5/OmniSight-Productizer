"""Live-state check engine — generic dispatcher for Prerequisites
``live_state_requires`` checks.

Per ``docs/sop/jira-ticket-conventions.md`` §13. Replaces the
hardcoded ``_current_alembic_head()`` probe in ``auto-runner-codex.py``
with a pluggable check registry.

Each check kind is a callable ``(argument) -> CheckResult``. Runner
calls :func:`evaluate` with the parsed YAML list before transitioning
a ticket from TODO to In Progress.

Adding a new check kind:

1. Implement handler ``def _check_<kind>(arg): ...`` returning CheckResult.
2. Register in ``CHECK_KINDS`` dict.
3. Add unit test in ``backend/tests/test_live_state_check.py``.
4. Document the kind in jira-ticket-conventions.md §13 table.

The handler argument shape comes straight from the YAML — runner
passes whatever value the operator wrote. Handlers must validate
shape and return CheckResult(False, "<reason>") on malformed input.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single live-state check.

    Attributes:
        passed: True iff the check matched expected state.
        kind: The check kind name (e.g. "alembic_head") for reporting.
        detail: Human-readable explanation. On fail, must include both
            expected and actual values so the runner-comment template
            in §13 produces actionable diagnostics.
    """

    passed: bool
    kind: str
    detail: str


# ── Built-in check kinds (handlers below) ─────────────────────────


def _check_alembic_head(expected: Any) -> CheckResult:
    """Pass iff ``alembic heads`` returns exactly the expected revision."""
    raise NotImplementedError("skeleton — implement by parsing alembic heads output")


def _check_feature_flag(expected: Any) -> CheckResult:
    """Pass iff env / DB feature flag matches.

    Argument shape: "OMNISIGHT_FOO_ENABLED=true" or {"name": "...", "value": "..."}.
    """
    raise NotImplementedError("skeleton — env first, DB feature_flags table fallback")


def _check_file_exists(expected: Any) -> CheckResult:
    """Pass iff path exists relative to repo root."""
    raise NotImplementedError("skeleton — Path(REPO_ROOT / expected).exists()")


def _check_command_succeeds(expected: Any) -> CheckResult:
    """Pass iff shell command returns exit code 0.

    Runs in REPO_ROOT cwd. 30s timeout. Output captured but not used
    for pass/fail (only exit code). For verbose checks, prefer
    file_exists or db_row_count which have richer detail.
    """
    raise NotImplementedError("skeleton — subprocess.run with shell=True, timeout=30")


def _check_db_row_count(expected: Any) -> CheckResult:
    """Pass iff ``SELECT COUNT(*) FROM <table>`` satisfies range.

    Argument shape: {"table": "users", "min": 1, "max": 100} (max optional).
    """
    raise NotImplementedError("skeleton — read DATABASE_URL, run query")


def _check_deployed_version(expected: Any) -> CheckResult:
    """Pass iff running service reports matching version string.

    Argument: "v0.4.0". Probes localhost:8000/healthz JSON response.
    """
    raise NotImplementedError("skeleton — httpx.get health endpoint")


CHECK_KINDS: dict[str, Callable[[Any], CheckResult]] = {
    "alembic_head": _check_alembic_head,
    "feature_flag": _check_feature_flag,
    "file_exists": _check_file_exists,
    "command_succeeds": _check_command_succeeds,
    "db_row_count": _check_db_row_count,
    "deployed_version": _check_deployed_version,
}


# ── Public API ─────────────────────────────────────────────────────


def evaluate(requirements: list[dict[str, Any]]) -> list[CheckResult]:
    """Dispatch each requirement to its handler. Order-independent.

    A requirement is a single-key dict ``{"kind": argument}``. Unknown
    kinds return CheckResult(False, "<kind>", "unknown check kind").
    Multiple keys in one dict raise ValueError (operator error).

    Returns one CheckResult per requirement. Caller decides whether
    any-fail = abort, or only specific kinds gate pickup.
    """
    raise NotImplementedError("skeleton — iterate requirements, dispatch via CHECK_KINDS")


def all_passed(results: list[CheckResult]) -> bool:
    """Convenience: True iff every result is .passed."""
    return all(r.passed for r in results)


def format_failures(results: list[CheckResult]) -> str:
    """Format failed checks for ticket-comment usage.

    Output matches the §13 comment template body — one line per fail
    with kind: detail.
    """
    raise NotImplementedError("skeleton — format failed results into multi-line string")
