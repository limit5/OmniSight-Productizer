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

import hashlib
import logging
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Mapping

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
    "LIVE_INCIDENT_ID_PREFIX",
    "RunnerIncidentRecord",
    "get_recorded_events",
    "get_runner_incidents",
    "record_incident_durable",
    "record_memory_recall_audit",
    "record_runner_incident",
    "recall_similar_incidents",
    "reset_for_tests",
]


# ── Durable-writer configuration (OP-2537 / R3.1+R3.4) ─────────────

DATABASE_URL_ENV = "OMNISIGHT_DATABASE_URL"
AUDIT_DB_ENV_FILE_ENV = "OMNISIGHT_AUDIT_DB_ENV_FILE"
DEFAULT_AUDIT_DB_ENV_FILE = Path("~/.config/omnisight/audit-db.env")
WRITE_FAIL_COUNTER_ENV = "OMNISIGHT_INCIDENT_WRITE_FAIL_COUNTER"
DEFAULT_WRITE_FAIL_COUNTER = Path(
    "~/.local/state/omnisight/incident_write_failures.count"
)
LIVE_INCIDENT_ID_PREFIX = "live-v1-"


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

        raise MemoryAuditWriteFailed(
            f"incident_recorder._persist_audit: failed to append IncidentRecord("
            f"failure_class={record.failure_class.value}, "
            f"query_fleet={record.query_fleet!r}, "
            f"target_fleet={record.target_fleet!r}, "
            f"tier={record.tier!r}) to in-memory buffer: {exc}"
        ) from exc


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


# ── Durable writer (OP-2537 R3.1+R3.4) ─────────────────────────────


_engine_lock = threading.Lock()
_engine_cache: dict[str, Any] = {}


# Mirrors scripts/backfill_runner_incidents.py's statement shape, minus
# ``created_at`` which is left to the DB default (NOW() / CURRENT_TIMESTAMP).
_DURABLE_INSERT_SQL = """
    INSERT INTO runner_incidents (
        incident_id, ticket_key, failure_class, summary, raw_traceback,
        runner_class, mutex_label, area
    )
    VALUES (
        :incident_id, :ticket_key, :failure_class, :summary, :raw_traceback,
        :runner_class, :mutex_label, :area
    )
    ON CONFLICT (incident_id) DO NOTHING
"""


def _get_engine(dsn: str):
    """Module-cached SQLAlchemy engine per DSN."""
    from sqlalchemy import create_engine

    with _engine_lock:
        engine = _engine_cache.get(dsn)
        if engine is None:
            engine = create_engine(dsn, future=True)
            _engine_cache[dsn] = engine
        return engine


def _resolve_dsn() -> str:
    """DSN precedence: ``OMNISIGHT_DATABASE_URL`` env (literal, only this
    name), else the ``OMNISIGHT_DATABASE_URL`` key from the KEY=VALUE env
    file at ``OMNISIGHT_AUDIT_DB_ENV_FILE``.
    """
    url = os.environ.get(DATABASE_URL_ENV)
    if url:
        return url
    env_file = Path(
        os.environ.get(AUDIT_DB_ENV_FILE_ENV) or DEFAULT_AUDIT_DB_ENV_FILE
    ).expanduser()
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == DATABASE_URL_ENV:
            value = value.strip().strip('"').strip("'")
            if value:
                return value
    raise LookupError(
        f"no {DATABASE_URL_ENV} entry in env file {env_file}"
    )


def _increment_write_fail_counter() -> None:
    """Bump the single-int counter file. Best-effort — never raises."""
    try:
        path = Path(
            os.environ.get(WRITE_FAIL_COUNTER_ENV) or DEFAULT_WRITE_FAIL_COUNTER
        ).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            current = int(path.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            current = 0
        path.write_text(f"{current + 1}\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — counter is best-effort too
        log.debug("incident_recorder.write_fail_counter_bump_failed err=%s", exc)


def _note_durable_write_failure(
    record: RunnerIncidentRecord, stage: str, exc: Exception
) -> None:
    """ONE structured log line + counter bump per failed durable write."""
    log.warning(
        "incident_recorder.durable_write_failed stage=%s incident_id=%s "
        "ticket=%s failure_class=%s err=%s",
        stage,
        record.incident_id,
        record.ticket_key,
        record.failure_class.value,
        exc,
    )
    _increment_write_fail_counter()


def _insert_durable(record: RunnerIncidentRecord) -> None:
    """Best-effort INSERT into ``runner_incidents``. Never raises."""
    try:
        dsn = _resolve_dsn()
    except Exception as exc:  # noqa: BLE001 — no-DSN is a soft failure
        _note_durable_write_failure(record, "resolve_dsn", exc)
        return
    try:
        from sqlalchemy import text

        engine = _get_engine(dsn)
        with engine.begin() as conn:
            conn.execute(
                text(_DURABLE_INSERT_SQL),
                {
                    "incident_id": record.incident_id,
                    "ticket_key": record.ticket_key,
                    "failure_class": record.failure_class.value,
                    "summary": record.summary,
                    "raw_traceback": record.raw_traceback,
                    "runner_class": record.runner_class,
                    "mutex_label": record.mutex_label,
                    "area": record.area,
                },
            )
    except Exception as exc:  # noqa: BLE001 — insert is best-effort
        _note_durable_write_failure(record, "insert", exc)


def record_incident_durable(
    ticket_key: str,
    failure_class: FailureClass | str,
    *,
    claim_token: str | None = None,
    summary: str = "",
    raw_traceback: str = "",
    runner_class: str = "",
    mutex_label: str | None = "",
    area: str | None = "",
) -> RunnerIncidentRecord:
    """Durable incident writer (OP-2537 pinned contract).

    * ``failure_class`` is coerced like :func:`record_runner_incident`;
      the dedup hash uses the COERCED ``.value``.
    * ``claim_token`` present → deterministic
      ``live-v1-<sha256(f"{ticket_key}|{failure_class.value}|{claim_token}")>``
      id, so the same failure reported through multiple seams dedups to
      one row via ``ON CONFLICT (incident_id) DO NOTHING``.
    * ``claim_token=None`` → fresh ``uuid4().hex`` id, no dedup.
    * Empty-string kwargs normalize to NULL at write for the nullable
      columns (``mutex_label``, ``area``); the NOT NULL columns keep
      their DB-default empty-string semantics per the 0206 DDL.
    * ``created_at`` is left to the DB default.
    * The record ALWAYS lands in the in-memory deque (recall read-cache,
      dedup by ``incident_id``) even when the DB write fails; the DB
      write is best-effort — this helper NEVER raises.
    """
    if isinstance(failure_class, FailureClass):
        klass = failure_class
    else:
        klass = FailureClass.coerce(failure_class)

    if claim_token is not None:
        digest = hashlib.sha256(
            f"{ticket_key}|{klass.value}|{claim_token}".encode("utf-8")
        ).hexdigest()
        incident_id = LIVE_INCIDENT_ID_PREFIX + digest
    else:
        incident_id = uuid.uuid4().hex

    record = RunnerIncidentRecord(
        incident_id=incident_id,
        ticket_key=ticket_key,
        failure_class=klass,
        summary=summary or "",
        raw_traceback=raw_traceback or "",
        runner_class=runner_class or "",
        mutex_label=mutex_label or None,
        area=area or None,
    )
    with _buffer_lock:
        if not any(r.incident_id == incident_id for r in _runner_buffer):
            _runner_buffer.append(record)
    _insert_durable(record)
    return record


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
            f"incident_recorder.recall_similar_incidents: no prior incidents "
            f"for ticket={ticket_key!r} area={area!r} "
            f"failure_class={failure_class.value} tier={tier!r} "
            f"top_k={top_k}"
        )
    return priors


# ── Test seam ──────────────────────────────────────────────────────


def reset_for_tests() -> None:
    """Clear in-memory buffers + engine cache. Test-only seam."""
    with _buffer_lock:
        _buffer.clear()
        _runner_buffer.clear()
    with _engine_lock:
        for engine in _engine_cache.values():
            try:
                engine.dispose()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        _engine_cache.clear()
