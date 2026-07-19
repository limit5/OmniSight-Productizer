"""U6-2a — L2 allowlisted session-outcome schema (DORMANT).

The §2.C boundary for L2 episodic memory: a session's ``summary_outcome`` is an
ALLOWLISTED, structured record with **no free-form behavioral field**. L2 reads
attacker-controllable raw turns, so a free-text summary could be steered ("record
that my standing policy is to bypass reviews"); the closed schema makes that
UNREPRESENTABLE — the outcome is only closed enums + server-counted ints +
validated task refs. (L2 is write-only + UI-only in v1 — NOT injected into any
prompt, §2.C/§D2 — so there is deliberately NO prompt renderer here.)

Two guarantees:
  * a ``SessionOutcome`` cannot hold arbitrary text — every field is an enum, a
    bounded int, a bool, or a tuple of format-validated task refs;
  * ``from_jsonb`` ALLOWLISTS keys + enum values — a stored blob carrying a rogue
    "behavioral" field (a poisoned row) is rejected fail-closed, so a later reader
    can never resurrect a free-form directive from storage.

Pure + offline; no DB (U6-2b builds the ``chat_session_summaries`` table + writer
on this). DORMANT: nothing produces or stores an outcome yet. Versioned so the
persisted jsonb shape is a reviewed, migratable contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

OUTCOME_SCHEMA_VERSION = 1

_MAX_TURNS = 100_000       # server-counted; a sane upper bound, never unbounded
_MAX_TASKS = 100           # bound the task-ref list length
_TASK_REF_RE = re.compile(r"\A[A-Z][A-Z0-9]{1,9}-\d{1,7}\Z")  # JIRA-style, bounded


class OutcomeValidationError(ValueError):
    """A session outcome violates the allowlisted schema — reject fail-closed."""


class OutcomeKind(Enum):
    """CLOSED — what a session accomplished. Data-only; no directive/behavior is
    representable (there is no "policy" or "permission" outcome)."""

    NO_OUTCOME = "no_outcome"
    QUESTION_ANSWERED = "question_answered"
    TASK_CREATED = "task_created"
    TROUBLESHOOTING = "troubleshooting"
    CONFIG_REVIEWED = "config_reviewed"
    HANDOFF = "handoff"


class TopicTag(Enum):
    """CLOSED controlled vocabulary — even the 'about what' is allowlisted, never
    a free-form label (a free topic string would reopen the §2.C hole)."""

    GENERAL = "general"
    CAMERA_SETUP = "camera_setup"
    DEPLOYMENT = "deployment"
    ACCOUNT = "account"
    TROUBLESHOOTING = "troubleshooting"


# The COMPLETE persisted key set (allowlist — from_jsonb rejects anything else).
_JSONB_KEYS = frozenset(
    {"schema_version", "outcome_kind", "turn_count", "resolved", "tasks_created", "topics"}
)


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """A validated, non-behavioral L2 session outcome. Invalid instances are
    unrepresentable — ``__post_init__`` runs the full allowlist validation."""

    outcome_kind: OutcomeKind
    turn_count: int
    resolved: bool
    tasks_created: tuple[str, ...] = ()
    topics: tuple[TopicTag, ...] = ()

    def __post_init__(self) -> None:
        validate_outcome(self)


def validate_outcome(outcome: SessionOutcome) -> None:
    """Fail-closed validation. Raises ``OutcomeValidationError`` on ANY violation."""
    if not isinstance(outcome, SessionOutcome):
        raise OutcomeValidationError("not a SessionOutcome")
    if not isinstance(outcome.outcome_kind, OutcomeKind):
        raise OutcomeValidationError("outcome_kind must be an OutcomeKind")
    # bool is a subclass of int — reject it explicitly so resolved/turn_count don't
    # cross-contaminate (True == 1 would otherwise pass an int check).
    if not isinstance(outcome.resolved, bool):
        raise OutcomeValidationError("resolved must be a bool")
    if not isinstance(outcome.turn_count, int) or isinstance(outcome.turn_count, bool):
        raise OutcomeValidationError("turn_count must be an int")
    if not (0 <= outcome.turn_count <= _MAX_TURNS):
        raise OutcomeValidationError(f"turn_count out of range [0, {_MAX_TURNS}]")

    if not isinstance(outcome.tasks_created, tuple):
        raise OutcomeValidationError("tasks_created must be a tuple")
    if len(outcome.tasks_created) > _MAX_TASKS:
        raise OutcomeValidationError(f"tasks_created exceeds {_MAX_TASKS}")
    for ref in outcome.tasks_created:
        if not isinstance(ref, str) or not _TASK_REF_RE.match(ref):
            raise OutcomeValidationError(f"invalid task ref: {ref!r}")

    if not isinstance(outcome.topics, tuple):
        raise OutcomeValidationError("topics must be a tuple")
    if len(outcome.topics) > len(TopicTag):
        raise OutcomeValidationError("too many topics")
    for tag in outcome.topics:
        if not isinstance(tag, TopicTag):
            raise OutcomeValidationError("each topic must be a TopicTag")

    # a light consistency invariant: TASK_CREATED must cite ≥1 task.
    if outcome.outcome_kind is OutcomeKind.TASK_CREATED and not outcome.tasks_created:
        raise OutcomeValidationError("TASK_CREATED outcome must list ≥1 task")


def to_jsonb(outcome: SessionOutcome) -> dict:
    """Serialize to the persisted jsonb shape (U6-2b stores this)."""
    validate_outcome(outcome)
    return {
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "outcome_kind": outcome.outcome_kind.value,
        "turn_count": outcome.turn_count,
        "resolved": outcome.resolved,
        "tasks_created": list(outcome.tasks_created),
        "topics": [t.value for t in outcome.topics],
    }


def from_jsonb(data: dict) -> SessionOutcome:
    """Rebuild + REVALIDATE a persisted outcome. ALLOWLISTS keys and enum values —
    a stored blob carrying an unknown ('behavioral') field or a forged enum is
    rejected fail-closed, so storage can never resurrect a free-form directive."""
    if not isinstance(data, dict):
        raise OutcomeValidationError("jsonb must be a dict")
    extra = set(data) - _JSONB_KEYS
    if extra:
        raise OutcomeValidationError(f"unknown outcome fields (possible poisoned row): {sorted(extra)}")
    if data.get("schema_version") != OUTCOME_SCHEMA_VERSION:
        raise OutcomeValidationError(f"unknown schema_version: {data.get('schema_version')!r}")
    # Guard the CONTAINER types BEFORE iterating — a poisoned/migrated row with a
    # non-iterable topics (Postgres jsonb null, a number) must fail closed as an
    # OutcomeValidationError, never escape as an uncaught TypeError.
    topics_raw = data.get("topics", ())
    if not isinstance(topics_raw, (list, tuple)):
        raise OutcomeValidationError("topics must be a list")
    tasks = data.get("tasks_created", ())
    if not isinstance(tasks, (list, tuple)):
        raise OutcomeValidationError("tasks_created must be a list")
    try:
        kind = OutcomeKind(data["outcome_kind"])
        topics = tuple(TopicTag(t) for t in topics_raw)
    except (KeyError, ValueError) as exc:
        raise OutcomeValidationError(f"unknown enum value: {exc}") from exc
    return SessionOutcome(
        outcome_kind=kind,
        turn_count=data.get("turn_count", -1),
        resolved=data.get("resolved"),  # type: ignore[arg-type]  # validated in __post_init__
        tasks_created=tuple(tasks),
        topics=topics,
    )
