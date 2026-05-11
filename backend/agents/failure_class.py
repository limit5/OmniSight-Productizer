"""OP-854 (C2) — Failure-class taxonomy + classifier helpers.

This module is the single source of truth for the runner's
``failure_class`` enum. Every runner-side incident that lands in
``runner_incidents`` carries one of these values; downstream consumers
(``incident_recorder``, ``failure_graph``, the C6 memory-recall filter)
treat the enum as a closed set.

The taxonomy comes from
``docs/audit/2026-05-11-sprint-abc-master-plan.md`` §3.3. New values
require a co-change to the alembic migration (``runner_incidents``
``failure_class`` CHECK constraint) and to ``classify_from_traceback``
below so the regex classifier can recognise them.

Why a separate module
---------------------
Before C2, ``FailureClass`` lived inside ``incident_recorder`` because
C6 only needed the ``MEMORY_RECALL_AUDIT`` audit slot. C2 expands the
taxonomy to 10 + ``OTHER`` runner-failure modes; ``incident_recorder``
keeps the audit-emit seam but defers the enum + classifier to this
file. Keeping these two concerns split is what lets ``failure_graph``
import the enum without dragging the audit-buffer machinery in.

Backwards compatibility
-----------------------
``incident_recorder.FailureClass`` is re-exported as an alias of this
module's enum, so older callers (the C6 test suite + the policy gate)
keep working without import-path churn.
"""

from __future__ import annotations

import logging
import re
from enum import Enum

log = logging.getLogger(__name__)


class FailureClass(str, Enum):
    """Closed enumeration of runner-incident failure classes.

    10 runner-failure modes + ``OTHER`` catch-all = 11 values (per the
    C2 AC). ``MEMORY_RECALL_AUDIT`` is a separate slot owned by C6 — it
    is **not** a runner failure but the same table records audit rows,
    so the enum carries it for the shared ``runner_incidents.failure_class``
    column.
    """

    LINT_FAILURE = "LINT_FAILURE"
    """checkpatch / ruff / mypy / static-analysis gate failed."""

    TEST_FAILURE = "TEST_FAILURE"
    """pytest / unit-test / integration-test red."""

    MERGE_CONFLICT = "MERGE_CONFLICT"
    """Auto-rebase or merger-agent could not resolve the conflict."""

    WORKTREE_DIRTY = "WORKTREE_DIRTY"
    """Workspace had unstaged / untracked files at pickup or push."""

    LLM_LOOP_DETECTED = "LLM_LOOP_DETECTED"
    """Reflection-loop / repeat-2-error guard fired."""

    UNKNOWN_AREA_LABEL = "UNKNOWN_AREA_LABEL"
    """Ticket area label did not map to a known scope-to-paths bucket."""

    BRIDGE_DESYNC = "BRIDGE_DESYNC"
    """Gerrit ↔ JIRA bridge missed an event / state drifted."""

    RUNNER_TIMEOUT = "RUNNER_TIMEOUT"
    """Per-ticket wall-clock budget exceeded."""

    MUTEX_CONTENTION = "MUTEX_CONTENTION"
    """File-coordinator mutex could not be acquired in time."""

    OUTCOMES_GRADER_REFUSED = "OUTCOMES_GRADER_REFUSED"
    """The outcomes-grader refused to score the run (insufficient signal)."""

    OTHER = "OTHER"
    """Catch-all — coerced from any value outside the closed enum."""

    # C6 (OP-856) — memory-recall audit row. Not a runner failure; the
    # column hosts both runner failures and the audit trail because the
    # operator dashboard joins them on ``ticket_key``. Kept here so the
    # enum drives a single CHECK constraint in Postgres.
    MEMORY_RECALL_AUDIT = "MEMORY_RECALL_AUDIT"

    @classmethod
    def coerce(cls, raw: "str | FailureClass | None") -> "FailureClass":
        """Resolve a free-form string to a registered enum value.

        Unknown values are coerced to :attr:`OTHER` with a logged warning
        (per error catalog ``FailureClassUnregistered``). The coercion
        path is intentionally non-raising so a misspelled class name in
        the runner does not block the incident write — the row still
        lands with ``failure_class=OTHER`` and the operator can re-classify
        from ``raw_traceback`` later.
        """
        if isinstance(raw, cls):
            return raw
        if raw is None:
            log.warning("failure_class.coerce.none → OTHER")
            return cls.OTHER
        token = str(raw).strip().upper()
        try:
            return cls(token)
        except ValueError:
            log.warning("failure_class.coerce.unregistered raw=%r → OTHER", raw)
            return cls.OTHER


# The 10 runner-failure members (excludes OTHER catch-all and the C6
# audit slot). Exposed for tests and for the docs renderer.
RUNNER_FAILURE_MEMBERS: tuple[FailureClass, ...] = (
    FailureClass.LINT_FAILURE,
    FailureClass.TEST_FAILURE,
    FailureClass.MERGE_CONFLICT,
    FailureClass.WORKTREE_DIRTY,
    FailureClass.LLM_LOOP_DETECTED,
    FailureClass.UNKNOWN_AREA_LABEL,
    FailureClass.BRIDGE_DESYNC,
    FailureClass.RUNNER_TIMEOUT,
    FailureClass.MUTEX_CONTENTION,
    FailureClass.OUTCOMES_GRADER_REFUSED,
)


# ── Error catalog (AC §error catalog) ──────────────────────────────


class FailureClassUnregistered(ValueError):
    """Raised by strict callers when an incident carries an unknown class.

    The default code path uses :meth:`FailureClass.coerce` which silently
    downgrades to :attr:`FailureClass.OTHER`; this exception exists for
    operator tooling that wants a hard failure (e.g. a CI lint that
    forbids unregistered classes in fixtures).
    """

    def __init__(self, raw: object) -> None:
        super().__init__(f"failure class {raw!r} is not registered")
        self.raw = raw


class FailureClassRecallEmpty(LookupError):
    """Raised when ``recall_similar_incidents`` finds no priors.

    Informational — the runner treats this as "fresh ticket, no prior
    failure to inject" and continues without a system-message preamble.
    """


class FailureClassMigrationFailed(RuntimeError):
    """Raised when the alembic apply for ``runner_incidents`` errors out.

    The migration is forward-only; on failure the caller MUST rollback
    via ``alembic downgrade -1`` before attempting another upgrade so
    the table is left in a known-empty state.
    """


class MemoryToolUnavailableForC2(RuntimeError):
    """Raised when the C1 Memory Tool backend is unreachable.

    C2 recall falls back to a direct Postgres query on
    ``runner_incidents`` so the runner can still inject prior-failure
    context even when the Anthropic Memory Tool API is down.
    """


# ── Classifier (regex → FailureClass) ──────────────────────────────


# Ordered: first match wins. Regexes are case-insensitive. The patterns
# stay deliberately narrow — a noisy classifier produces misleading
# recall priors, so when in doubt we route to ``OTHER`` rather than
# guess.
_CLASSIFY_PATTERNS: tuple[tuple[re.Pattern[str], FailureClass], ...] = (
    (re.compile(r"checkpatch|ruff (?:check )?failed|mypy: error|flake8", re.I), FailureClass.LINT_FAILURE),
    (re.compile(r"pytest.*FAILED|assert(?:ion)? error|test.*failed", re.I), FailureClass.TEST_FAILURE),
    (re.compile(r"merge conflict|conflicts:|unmerged paths|<<<<<<<", re.I), FailureClass.MERGE_CONFLICT),
    (re.compile(r"untracked working tree files|workspace dirty|uncommitted changes", re.I), FailureClass.WORKTREE_DIRTY),
    (re.compile(r"reflection loop|repeat[- ]?2[- ]?error|llm loop", re.I), FailureClass.LLM_LOOP_DETECTED),
    (re.compile(r"unknown area label|scope[- ]to[- ]paths.*not found", re.I), FailureClass.UNKNOWN_AREA_LABEL),
    (re.compile(r"bridge desync|gerrit.*jira.*drift|webhook miss", re.I), FailureClass.BRIDGE_DESYNC),
    (re.compile(r"runner timeout|wall[- ]clock budget|deadline exceeded", re.I), FailureClass.RUNNER_TIMEOUT),
    (re.compile(r"mutex contention|file coordinator.*lock|could not acquire", re.I), FailureClass.MUTEX_CONTENTION),
    (re.compile(r"outcomes[- ]?grader (?:refused|abstained)|insufficient signal", re.I), FailureClass.OUTCOMES_GRADER_REFUSED),
)


def classify_from_traceback(raw: str) -> FailureClass:
    """Best-effort regex classification of a raw traceback / stderr.

    Returns the first match; falls back to :attr:`FailureClass.OTHER`
    when no pattern fires. The classifier is intentionally simple — we
    want operators to be able to predict the routing decision from the
    pattern table alone, without re-running the runner.
    """
    text = str(raw or "")
    if not text.strip():
        return FailureClass.OTHER
    for pattern, klass in _CLASSIFY_PATTERNS:
        if pattern.search(text):
            return klass
    return FailureClass.OTHER


__all__ = [
    "FailureClass",
    "FailureClassMigrationFailed",
    "FailureClassRecallEmpty",
    "FailureClassUnregistered",
    "MemoryToolUnavailableForC2",
    "RUNNER_FAILURE_MEMBERS",
    "classify_from_traceback",
]
