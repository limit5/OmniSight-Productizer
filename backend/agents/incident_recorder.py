"""C2 incident recorder — fail-open audit shim with C6 hook.

C2 (proprietary differentiation child, master-plan §3.3) introduces a
``runner_incidents`` Postgres table that records runner-side failure
classes. C6 (this row, OP-856) adds the ``MEMORY_RECALL_AUDIT``
failure-class slot so memory-recall audit rows persist to the same
table.

This module is the **emit seam**, not the schema. The Postgres table
itself is owned by C2's alembic migration and is out of scope for the
C6 backend/docs/tests area boundary. Until that migration lands the
recorder uses the in-memory ring buffer below as the durable surface
— operators can still observe events via ``get_recorded_events`` and
the policy gate stays exercise-able from tests.

Audit-write contract
--------------------
* Always fail-open. Memory-recall callers must see a successful
  recall even if the audit row cannot persist.
* Raise :class:`backend.agents.memory_tool_handler.MemoryAuditWriteFailed`
  on persistence failure so the caller can log + carry on (the
  policy gate already handles this).
* When C2's table lands, swap the in-memory buffer for a SQLAlchemy
  insert — the public function signatures stay stable.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque

log = logging.getLogger(__name__)


class FailureClass(str, Enum):
    """Closed enumeration of incident classes recorded by the runner.

    Mirrors the planned ``runner_incidents.failure_class`` column
    constraint. New values must be co-added to the alembic migration
    when C2 lands; until then the recorder accepts only the values
    defined here so tests can pin the contract.
    """

    MEMORY_RECALL_AUDIT = "MEMORY_RECALL_AUDIT"
    """C6 audit row — one per recall attempt (permitted or refused)."""


@dataclass(frozen=True)
class IncidentRecord:
    """One row destined for ``runner_incidents``."""

    failure_class: FailureClass
    summary: str
    escalate: bool
    permitted: bool
    tier: str
    query_fleet: str
    target_fleet: str
    recorded_at: float = field(default_factory=time.time)


# In-memory ring buffer — bounded so a wedged DB cannot blow runner
# RSS. ``maxlen`` matches the operator-facing dashboard target of
# "last hour at ~10 recalls/min" with headroom.
_BUFFER_MAX = 1024
_buffer_lock = threading.Lock()
_buffer: Deque[IncidentRecord] = deque(maxlen=_BUFFER_MAX)


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
    _persist(record)


def _persist(record: IncidentRecord) -> None:
    """Write to the durable buffer (and, when C2 lands, to Postgres)."""
    try:
        with _buffer_lock:
            _buffer.append(record)
    except Exception as exc:  # noqa: BLE001 — fail-open contract
        # Local import dodges the import-cycle with memory_tool_handler.
        from backend.agents.memory_tool_handler import MemoryAuditWriteFailed

        raise MemoryAuditWriteFailed(str(exc)) from exc


def get_recorded_events(
    failure_class: FailureClass | None = None,
) -> list[IncidentRecord]:
    """Snapshot of the in-memory buffer, oldest first.

    Used by tests and the runner status endpoint until C2's Postgres
    surface lands. Filtering by ``failure_class`` is a convenience for
    the C6 ``test_memory_tier_policy`` suite.
    """
    with _buffer_lock:
        snapshot = list(_buffer)
    if failure_class is None:
        return snapshot
    return [r for r in snapshot if r.failure_class is failure_class]


def reset_for_tests() -> None:
    """Clear the in-memory buffer. Test-only seam (see conftest.py)."""
    with _buffer_lock:
        _buffer.clear()


__all__ = [
    "FailureClass",
    "IncidentRecord",
    "get_recorded_events",
    "record_memory_recall_audit",
    "reset_for_tests",
]
