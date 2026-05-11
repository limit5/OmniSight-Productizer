"""C2/C6 incident recorder — write + recall seam for ``runner_incidents``.

This module is the audit/write/recall seam for the ``runner_incidents``
Postgres table introduced by C2 (OP-854). The table itself is owned by
alembic migration ``0206_runner_incidents``; this module is the Python
side that:

* writes audit rows for C6 memory-recall decisions (existing seam),
* writes runner-incident rows for C2 failure tagging (new in OP-854),
* recalls similar prior incidents by ``failure_class`` + ``area``,
  with the C6 S/M/L/X tier policy applied per AC #5.

Audit-write contract
--------------------
* Always fail-open. Memory-recall callers must see a successful
  recall even if the audit row cannot persist.
* Raise :class:`backend.agents.memory_tool_handler.MemoryAuditWriteFailed`
  on persistence failure so the caller can log + carry on (the
  policy gate already handles this).

Recall contract (C2 AC #3)
--------------------------
* ``recall_similar_incidents(ticket_key, area, failure_class, tier=...)``
  returns up to ``top_k`` prior :class:`RunnerIncidentRecord` rows that
  share recognised ``area`` (from JIRA labels) AND ``failure_class``.
* The recall is tier-gated through :func:`memory_tool_handler.enforce_recall`
  so tier:X requests are refused and tier:L requests require opt-in.
* When the C1 Memory Tool backend is unavailable, callers see
  :class:`backend.agents.failure_class.MemoryToolUnavailableForC2`;
  this module's direct in-memory fallback always answers, so the error
  is only raised by the higher-level integration in
  ``scripts/run_s1_via_anthropic_sdk.py``.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Mapping

from backend.agents.failure_class import (
    FailureClass,
    FailureClassRecallEmpty,
    FailureClassUnregistered,
    classify_from_traceback,
)

log = logging.getLogger(__name__)


__all__ = [
    "FailureClass",
    "IncidentRecord",
    "RunnerIncidentRecord",
    "get_recorded_events",
    "get_runner_incidents",
    "record_memory_recall_audit",
    "record_runner_incident",
    "recall_similar_incidents",
    "reset_for_tests",
]


# ── Data shapes ────────────────────────────────────────────────────


@dataclass(frozen=True)
class IncidentRecord:
    """One C6 audit row destined for ``runner_incidents``.

    Carries the memory-recall policy decision shape. Distinct from
    :class:`RunnerIncidentRecord` (which captures runner failures);
    both ride the same Postgres table but populate different columns.
    """

    failure_class: FailureClass
    summary: str
    escalate: bool
    permitted: bool
    tier: str
    query_fleet: str
    target_fleet: str
    recorded_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class RunnerIncidentRecord:
    """One C2 runner-incident row.

    Mirrors the ``runner_incidents`` table columns introduced by
    migration 0206. ``area`` is the recognised JIRA area label
    (e.g. ``backend``) — derived at write time from the ticket's
    label list; absent means the runner did not have a labelled
    area at incident time (e.g. ``UNKNOWN_AREA_LABEL``).
    """

    incident_id: str
    ticket_key: str
    failure_class: FailureClass
    summary: str
    raw_traceback: str
    runner_class: str
    mutex_label: str | None
    area: str | None
    created_at: float = field(default_factory=time.time)


# ── In-memory buffers (durable surface until Postgres lands) ───────


_BUFFER_MAX = 1024
_buffer_lock = threading.Lock()
_buffer: Deque[IncidentRecord] = deque(maxlen=_BUFFER_MAX)
_runner_buffer: Deque[RunnerIncidentRecord] = deque(maxlen=_BUFFER_MAX)


# ── C6 audit-write (existing seam) ─────────────────────────────────


def record_memory_recall_audit(request, decision) -> None:
    """C6 audit-row entry point invoked by the policy gate.

    Signature is intentionally untyped at the parameter level (the
    types live in ``memory_tool_handler``) to avoid an import cycle:
    ``memory_tool_handler`` already imports this module lazily.
    """
    record = IncidentRecord(
        failure_class=FailureClass.MEMORY_RECALL_AUDIT,
        summary=decision.audit_summary,
        escalate=decision.escalate,
        permitted=decision.permitted,
        tier=decision.tier.value,
        query_fleet=request.query_fleet,
        target_fleet=request.target_fleet,
    )
    _persist_audit(record)


def _persist_audit(record: IncidentRecord) -> None:
    """Write to the durable buffer (and, when 0206 lands, to Postgres)."""
    try:
        with _buffer_lock:
            _buffer.append(record)
    except Exception as exc:  # noqa: BLE001 — fail-open contract
        from backend.agents.memory_tool_handler import MemoryAuditWriteFailed

        raise MemoryAuditWriteFailed(str(exc)) from exc


def get_recorded_events(
    failure_class: FailureClass | None = None,
) -> list[IncidentRecord]:
    """Snapshot of the C6 audit buffer, oldest first."""
    with _buffer_lock:
        snapshot = list(_buffer)
    if failure_class is None:
        return snapshot
    return [r for r in snapshot if r.failure_class is failure_class]


# ── C2 runner-incident write ───────────────────────────────────────


def record_runner_incident(
    *,
    ticket_key: str,
    failure_class: FailureClass | str | None,
    summary: str,
    raw_traceback: str = "",
    runner_class: str = "unknown",
    mutex_label: str | None = None,
    area: str | None = None,
    incident_id: str | None = None,
    strict: bool = False,
) -> RunnerIncidentRecord:
    """Tag a runner failure with a :class:`FailureClass` and persist it.

    Behaviour:

    * If ``failure_class`` is unset, ``classify_from_traceback`` infers
      one from ``raw_traceback`` (best-effort; falls back to ``OTHER``).
    * If ``failure_class`` is a string outside the enum, it is coerced
      to :attr:`FailureClass.OTHER` (per error catalog
      ``FailureClassUnregistered``). Strict callers can opt in to a
      hard failure with ``strict=True``.
    * ``incident_id`` defaults to a fresh ``uuid4`` hex so the row PK
      is unique without coordinating with Postgres.
    """
    if failure_class is None:
        klass = classify_from_traceback(raw_traceback)
    elif isinstance(failure_class, FailureClass):
        klass = failure_class
    else:
        if strict:
            try:
                klass = FailureClass(str(failure_class).strip().upper())
            except ValueError as exc:
                raise FailureClassUnregistered(failure_class) from exc
        else:
            klass = FailureClass.coerce(failure_class)

    record = RunnerIncidentRecord(
        incident_id=incident_id or uuid.uuid4().hex,
        ticket_key=ticket_key,
        failure_class=klass,
        summary=summary,
        raw_traceback=raw_traceback,
        runner_class=runner_class,
        mutex_label=mutex_label,
        area=area,
    )
    _persist_runner_incident(record)
    return record


def _persist_runner_incident(record: RunnerIncidentRecord) -> None:
    """Write to the durable buffer (and, when 0206 lands, to Postgres)."""
    with _buffer_lock:
        _runner_buffer.append(record)


def get_runner_incidents(
    *,
    failure_class: FailureClass | None = None,
    area: str | None = None,
    ticket_key: str | None = None,
) -> list[RunnerIncidentRecord]:
    """Snapshot of the C2 runner-incident buffer, oldest first.

    Supports filtering by ``failure_class``, ``area``, and ``ticket_key``
    so the recall path can do a single in-memory scan. When the Postgres
    surface lands (0206), this becomes a SELECT with the same filters.
    """
    with _buffer_lock:
        snapshot = list(_runner_buffer)
    if failure_class is not None:
        snapshot = [r for r in snapshot if r.failure_class is failure_class]
    if area is not None:
        snapshot = [r for r in snapshot if r.area == area]
    if ticket_key is not None:
        snapshot = [r for r in snapshot if r.ticket_key == ticket_key]
    return snapshot


# ── C2 recall (AC #3) — tier-gated lookup ──────────────────────────


def recall_similar_incidents(
    *,
    ticket_key: str,
    area: str | None,
    failure_class: FailureClass,
    tier: str = "S",
    query_fleet: str = "fleet-prod",
    target_fleet: str | None = None,
    env: Mapping[str, str] | None = None,
    top_k: int = 3,
    audit_emitter=None,
) -> list[RunnerIncidentRecord]:
    """Return up to ``top_k`` prior incidents matching ``area`` + ``failure_class``.

    The recall is tier-gated (AC #5): the policy gate from C6 decides
    whether the lookup may execute. ``tier:X`` raises; ``tier:L`` needs
    the existing opt-in env. Permitted recalls return rows from the
    runner-incident buffer, ordered newest-first, capped at ``top_k``.

    Raises:
      * :class:`FailureClassRecallEmpty` — recall permitted but no
        priors match. Informational; the runner skips the
        system-message preamble in that case.
      * :class:`TierViolationUnauthorizedRecall` / its subclass — recall
        refused by tier policy. The caller must surface this to the
        operator (escalate=True for tier:X).
    """
    # Local import keeps this module importable when the policy gate
    # is unavailable (early-boot or a narrowly-scoped unit test).
    from backend.agents.memory_tool_handler import (
        MemoryTier,
        RecallRequest,
        enforce_recall,
    )

    request = RecallRequest(
        query=f"failure_class={failure_class.value} area={area or '?'} ticket={ticket_key}",
        tier=MemoryTier.parse(tier),
        query_fleet=query_fleet,
        target_fleet=target_fleet or query_fleet,
    )
    enforce_recall(request, env=env, audit_emitter=audit_emitter)

    # Permitted — scan the buffer. The C2 spec says "share recognised
    # area + failure_class"; we honour that filter when ``area`` is
    # provided. Unrecognised area (None) widens to "any area" so the
    # ``UNKNOWN_AREA_LABEL`` ticket itself can still recall priors.
    candidates = get_runner_incidents(
        failure_class=failure_class,
        area=area,
    )
    # Newest-first, top_k cap.
    candidates.sort(key=lambda r: r.created_at, reverse=True)
    priors = candidates[: max(0, top_k)]
    if not priors:
        raise FailureClassRecallEmpty(
            f"no prior incidents for ticket={ticket_key} "
            f"area={area} failure_class={failure_class.value}"
        )
    return priors


# ── Test seam ──────────────────────────────────────────────────────


def reset_for_tests() -> None:
    """Clear both in-memory buffers. Test-only seam (see conftest.py)."""
    with _buffer_lock:
        _buffer.clear()
        _runner_buffer.clear()
