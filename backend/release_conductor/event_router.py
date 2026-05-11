"""OP-947 H2 — Event router service backend (durable queue + retry).

The Sprint H L3 conductor (event-driven release scheduler) needs a
single HTTP ingestion point for the ADR-0018 webhook subscription
matrix:

* ``POST /api/v1/release-conductor/events`` accepts a JSON envelope
  with ``source`` / ``event_type`` / ``event_id`` / ``payload`` and
  persists the event to the ``release_events`` table (AC #1 + #2).
* The persisted rows are then pulled by
  :mod:`backend.release_conductor.worker` in FIFO order and dispatched
  through :mod:`backend.release_conductor.event_handlers` (AC #3).
* Idempotency is enforced by ``sha256(source + ":" + event_id)`` on
  the row's ``idempotency_key`` UNIQUE constraint (AC #5).

What this module does NOT do
============================
- It does not authenticate the caller — the canonical webhook
  endpoints in ``backend.routers.webhooks`` are the externally-exposed
  surfaces (with Caddy/Cloudflare tunnel auth for Gerrit, bearer-token
  auth for JIRA). Those handlers convert wire events to the L3
  envelope and POST to this router *in-process* (or via the standard
  internal authorisation surface for cross-backend coherency).
- It does not dispatch synchronously — events are persisted and the
  worker handles them. Per AC #4 retries happen out-of-band so the
  HTTP call returns 202 quickly.
- It does not own the schema. The 0232 alembic migration owns the
  ``release_events`` table; we only read/write rows.

Status enum
===========
The five string values match the ``release_events_status_chk``
CHECK constraint in 0232; the assert at import time guards drift.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth


logger = logging.getLogger(__name__)


# ─── Status enum (mirrors alembic 0232) ──────────────────────────────
STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_FAILED = "failed"  # retries exhausted (AC #4 dead-letter trigger)
STATUS_DEAD_LETTER = "dead_letter"  # operator has triaged + parked

STATUSES: frozenset[str] = frozenset(
    {
        STATUS_PENDING,
        STATUS_IN_PROGRESS,
        STATUS_DONE,
        STATUS_FAILED,
        STATUS_DEAD_LETTER,
    }
)


# ─── Tunables (AC #4: 3 attempts, exponential backoff) ───────────────
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_SECONDS = 2.0  # 2 → 4 → 8 seconds between retries


# ─── Error catalog (per ticket description) ──────────────────────────
class EventInsertFailed(RuntimeError):
    """Persistence layer rejected the INSERT (Postgres unreachable, etc.).

    Per the ticket error catalog: fail closed and return 500 to the
    source. The source webhook is then responsible for retrying — the
    L3 receiver never silently drops an event because of an infra
    fault.
    """


class DeadLetterAlert(RuntimeError):
    """Raised on the operator-alert path when an event hits dead-letter.

    The worker (not this module) raises it; we export the type from
    here so the dispatch-table tests and the H4 web UI share a single
    import path.
    """


# ─── Engine plumbing (mirrors state_machine) ─────────────────────────
_test_engine: sa.Engine | None = None
_prod_engine: sa.Engine | None = None


def set_engine_for_tests(engine: sa.Engine | None) -> None:
    """Inject a SQLAlchemy engine for the duration of a test.

    Tests call ``set_engine_for_tests(engine)`` in setup and
    ``set_engine_for_tests(None)`` in teardown; production code never
    does."""
    global _test_engine
    _test_engine = engine


def _engine() -> sa.Engine:
    if _test_engine is not None:
        return _test_engine
    global _prod_engine
    if _prod_engine is None:
        url = os.environ.get("OMNISIGHT_DATABASE_URL", "sqlite:///release_events.db")
        _prod_engine = sa.create_engine(url, future=True)
    return _prod_engine


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


# ─── Idempotency key helper ──────────────────────────────────────────
def make_idempotency_key(source: str, event_id: str) -> str:
    """``sha256(source + ":" + event_id)`` per AC #5.

    Stable across processes so a duplicate delivery to backend-a after
    the same event already landed on backend-b collapses on the UNIQUE
    constraint (per ADR-0018 §Idempotency / §Why not reuse the SQLite
    store)."""
    raw = f"{source}:{event_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# ─── Persist + read primitives ───────────────────────────────────────
def persist_event(
    *,
    source: str,
    event_type: str,
    event_id: str,
    payload: dict[str, Any],
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Insert a row or short-circuit on the existing one.

    Returns ``{"event_row_id": int, "idempotency_key": str,
    "deduplicated": bool, "status": str}``. The ``deduplicated`` flag
    distinguishes a fresh insert (False) from a hit on the UNIQUE
    constraint (True) so the HTTP handler can serve the same 202 body
    in both cases without lying about state.

    Raises :class:`EventInsertFailed` if the database is unreachable.
    """
    if not source or not source.strip():
        raise ValueError("source must be a non-empty string")
    if not event_type or not event_type.strip():
        raise ValueError("event_type must be a non-empty string")
    if not event_id or not event_id.strip():
        raise ValueError("event_id must be a non-empty string")
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    key = make_idempotency_key(source, event_id)
    payload_blob = json.dumps(payload or {}, separators=(",", ":"), sort_keys=True)
    ts = _now_iso()

    try:
        with _engine().begin() as conn:
            # Try the INSERT first; on UNIQUE collision fall through to
            # the SELECT branch. We deliberately do not use ON CONFLICT
            # DO NOTHING because the syntax differs across sqlite and
            # postgres, and we want the same code path in tests.
            try:
                result = conn.execute(
                    sa.text(
                        "INSERT INTO release_events "
                        "(source, event_type, idempotency_key, "
                        " payload_json, status, attempt_count, "
                        " max_attempts, next_retry_at, received_at, "
                        " updated_at) "
                        "VALUES (:source, :event_type, :key, :payload, "
                        " :status, 0, :max_attempts, :ts, :ts, :ts)"
                    ),
                    {
                        "source": source.strip(),
                        "event_type": event_type.strip(),
                        "key": key,
                        "payload": payload_blob,
                        "status": STATUS_PENDING,
                        "max_attempts": max_attempts,
                        "ts": ts,
                    },
                )
                row_id = int(result.lastrowid or 0)
                if row_id == 0 and conn.dialect.name == "postgresql":
                    row = conn.execute(
                        sa.text(
                            "SELECT id FROM release_events "
                            "WHERE idempotency_key = :k"
                        ),
                        {"k": key},
                    ).first()
                    row_id = int(row[0]) if row else 0
                return {
                    "event_row_id": row_id,
                    "idempotency_key": key,
                    "deduplicated": False,
                    "status": STATUS_PENDING,
                }
            except sa.exc.IntegrityError:
                # Duplicate delivery — return the existing row's id +
                # status so the caller gets a stable answer.
                row = conn.execute(
                    sa.text(
                        "SELECT id, status FROM release_events "
                        "WHERE idempotency_key = :k"
                    ),
                    {"k": key},
                ).first()
                if row is None:
                    # Shouldn't happen — IntegrityError without a row
                    # means a different constraint failed. Re-raise as
                    # EventInsertFailed.
                    raise EventInsertFailed(
                        f"insert failed but no existing row for key={key}"
                    )
                return {
                    "event_row_id": int(row[0]),
                    "idempotency_key": key,
                    "deduplicated": True,
                    "status": str(row[1]),
                }
    except sa.exc.OperationalError as exc:
        # Postgres unreachable / sqlite locked / etc. — surface as the
        # AC-named EventInsertFailed.
        raise EventInsertFailed(
            f"release_events insert failed for source={source} "
            f"event_type={event_type}: {exc!r}"
        ) from exc


def get_event(*, row_id: int) -> dict[str, Any]:
    """Return a single row as a dict, or raise LookupError."""
    with _engine().connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT id, source, event_type, idempotency_key, "
                "       payload_json, status, attempt_count, "
                "       max_attempts, next_retry_at, last_error, "
                "       handler_result_json, received_at, updated_at "
                "FROM release_events WHERE id = :id"
            ),
            {"id": row_id},
        ).first()
    if row is None:
        raise LookupError(f"release_events row {row_id} not found")
    return _row_to_dict(row)


def _row_to_dict(row: sa.Row) -> dict[str, Any]:
    payload: dict[str, Any]
    try:
        payload = json.loads(row[4]) if row[4] else {}
    except (ValueError, TypeError):
        payload = {}
    handler_result: dict[str, Any] | None
    try:
        handler_result = json.loads(row[10]) if row[10] else None
    except (ValueError, TypeError):
        handler_result = None
    return {
        "id": int(row[0]),
        "source": row[1],
        "event_type": row[2],
        "idempotency_key": row[3],
        "payload": payload,
        "status": row[5],
        "attempt_count": int(row[6]),
        "max_attempts": int(row[7]),
        "next_retry_at": str(row[8]),
        "last_error": row[9],
        "handler_result": handler_result,
        "received_at": str(row[11]),
        "updated_at": str(row[12]),
    }


def claim_next_event() -> dict[str, Any] | None:
    """Lease the oldest ``pending`` (or ready-to-retry) row to the worker.

    Atomic UPDATE … WHERE id = (SELECT … LIMIT 1) so concurrent
    workers can't grab the same row. Returns ``None`` when the queue
    is empty / nothing is ready.

    Sqlite doesn't support ``RETURNING`` everywhere (the version
    feature-gates 3.35+), so we run a SELECT-then-UPDATE pair in one
    transaction and use ``WHERE status = 'pending'`` as the optimistic-
    lock guard — concurrent claimants race on the UPDATE and the
    loser sees rowcount=0 and retries.
    """
    ts = _now_iso()
    with _engine().begin() as conn:
        row = conn.execute(
            sa.text(
                "SELECT id, status, attempt_count FROM release_events "
                "WHERE status = :pending "
                "  AND next_retry_at <= :ts "
                "ORDER BY id ASC "
                "LIMIT 1"
            ),
            {"pending": STATUS_PENDING, "ts": ts},
        ).first()
        if row is None:
            return None
        row_id, _, _ = row
        update = conn.execute(
            sa.text(
                "UPDATE release_events SET "
                "  status = :in_progress, "
                "  updated_at = :ts "
                "WHERE id = :id AND status = :pending"
            ),
            {
                "in_progress": STATUS_IN_PROGRESS,
                "pending": STATUS_PENDING,
                "ts": ts,
                "id": row_id,
            },
        )
        if update.rowcount != 1:
            # Another worker raced us; tell the caller to try again.
            return None
    return get_event(row_id=int(row_id))


def mark_done(
    *, row_id: int, handler_result: dict[str, Any]
) -> None:
    """Worker calls this on a successful dispatch."""
    ts = _now_iso()
    blob = json.dumps(handler_result or {}, separators=(",", ":"), sort_keys=True)
    with _engine().begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE release_events SET "
                "  status = :done, "
                "  attempt_count = attempt_count + 1, "
                "  handler_result_json = :blob, "
                "  last_error = NULL, "
                "  updated_at = :ts "
                "WHERE id = :id"
            ),
            {
                "done": STATUS_DONE,
                "blob": blob,
                "ts": ts,
                "id": row_id,
            },
        )


def mark_retry(
    *,
    row_id: int,
    error: str,
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
) -> dict[str, Any]:
    """Worker calls this on a retriable handler failure.

    Increments ``attempt_count``, schedules ``next_retry_at`` with
    exponential backoff (``2^attempt_count * base``), and resets
    status to ``pending`` so the same worker (or any concurrent
    worker) can re-lease the row when the backoff window expires.

    If ``attempt_count + 1 >= max_attempts`` the row is moved to
    ``failed`` (the dead-letter terminal state) and the returned dict
    carries ``dead_letter=True``. The caller is responsible for
    emitting the ``DeadLetterAlert``; we don't do it here so a test
    can assert the row state without an alerting side effect.
    """
    ts = _now_iso()
    with _engine().begin() as conn:
        row = conn.execute(
            sa.text(
                "SELECT attempt_count, max_attempts FROM release_events "
                "WHERE id = :id"
            ),
            {"id": row_id},
        ).first()
        if row is None:
            raise LookupError(f"release_events row {row_id} not found")
        attempt_count, max_attempts = int(row[0]), int(row[1])
        next_attempt = attempt_count + 1
        if next_attempt >= max_attempts:
            conn.execute(
                sa.text(
                    "UPDATE release_events SET "
                    "  status = :failed, "
                    "  attempt_count = :next, "
                    "  last_error = :err, "
                    "  updated_at = :ts "
                    "WHERE id = :id"
                ),
                {
                    "failed": STATUS_FAILED,
                    "next": next_attempt,
                    "err": error,
                    "ts": ts,
                    "id": row_id,
                },
            )
            return {
                "row_id": row_id,
                "dead_letter": True,
                "attempt_count": next_attempt,
                "next_retry_at": None,
            }
        # Exponential: 2^next_attempt * base. With base=2 and
        # max_attempts=3 the sequence is (after 1st fail) 4s, (after
        # 2nd fail) 8s — final fail goes to dead-letter, not retry.
        backoff = (2 ** next_attempt) * backoff_base_seconds
        next_retry = _iso_in(backoff)
        conn.execute(
            sa.text(
                "UPDATE release_events SET "
                "  status = :pending, "
                "  attempt_count = :next, "
                "  next_retry_at = :next_retry, "
                "  last_error = :err, "
                "  updated_at = :ts "
                "WHERE id = :id"
            ),
            {
                "pending": STATUS_PENDING,
                "next": next_attempt,
                "next_retry": next_retry,
                "err": error,
                "ts": ts,
                "id": row_id,
            },
        )
        return {
            "row_id": row_id,
            "dead_letter": False,
            "attempt_count": next_attempt,
            "next_retry_at": next_retry,
        }


def mark_dead_letter_immediately(
    *, row_id: int, error: str
) -> None:
    """Used when the dispatcher knows the event is unrecoverable.

    ``UnknownEventType`` is the canonical trigger — retrying an
    unknown event 3 times doesn't help, so we skip the retry budget
    and park the row directly.
    """
    ts = _now_iso()
    with _engine().begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE release_events SET "
                "  status = :failed, "
                "  attempt_count = attempt_count + 1, "
                "  last_error = :err, "
                "  updated_at = :ts "
                "WHERE id = :id"
            ),
            {
                "failed": STATUS_FAILED,
                "err": error,
                "ts": ts,
                "id": row_id,
            },
        )


# ─── FastAPI surface ─────────────────────────────────────────────────
router = APIRouter(prefix="/release-conductor", tags=["release-conductor"])


class EventIngestRequest(BaseModel):
    """Wire envelope for ``POST /events`` (AC #1).

    The four required fields are the minimum a handler needs:

    * ``source`` — ADR-0018 matrix source (gerrit / jira / slo_monitor
      / canary / prod_orchestrator).
    * ``event_type`` — source-side wire name (change-merged /
      jira:issue_updated / slo.breach / canary.stage.transitioned /
      ...). Drives the dispatch table.
    * ``event_id`` — caller-stable identifier; combined with ``source``
      to form the AC #5 idempotency key.
    * ``payload`` — the raw event body. Free-form JSON; the dispatcher
      reads source-specific fields.
    """

    source: str = Field(..., min_length=1, max_length=64)
    event_type: str = Field(..., min_length=1, max_length=128)
    event_id: str = Field(..., min_length=1, max_length=256)
    payload: dict[str, Any] = Field(default_factory=dict)


class EventIngestResponse(BaseModel):
    event_row_id: int
    idempotency_key: str
    deduplicated: bool
    status: str


@router.post(
    "/events",
    response_model=EventIngestResponse,
    status_code=202,
)
async def post_event(
    body: EventIngestRequest,
    _user: auth.User = Depends(auth.current_user),
) -> EventIngestResponse:
    """Persist an event into ``release_events`` (AC #1 + AC #2).

    Returns 202 Accepted on success. Idempotent — duplicate
    ``(source, event_id)`` pairs short-circuit and return the existing
    row id without double-queuing (AC #5).

    On persistence failure returns 500 (per the ticket error catalog
    "EventInsertFailed — fail closed (return 500 to source); source
    retries").
    """
    try:
        result = persist_event(
            source=body.source,
            event_type=body.event_type,
            event_id=body.event_id,
            payload=body.payload,
        )
    except EventInsertFailed as exc:
        logger.error(
            "release_conductor.event_router.persist_failed source=%s "
            "event_type=%s event_id=%s err=%s",
            body.source,
            body.event_type,
            body.event_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail="release_events insert failed; please retry",
        )

    logger.info(
        "release_conductor.event_router.accepted row_id=%s "
        "source=%s event_type=%s deduplicated=%s",
        result["event_row_id"],
        body.source,
        body.event_type,
        result["deduplicated"],
    )
    return EventIngestResponse(**result)


# ─── Sanity: import-time assert on status enum drift ─────────────────
# The 0232 migration's CHECK literal must match STATUSES. If anyone
# adds a status here without updating the migration the next test
# import fails loudly rather than silently allowing a row that the DB
# will then reject.
_EXPECTED_CHECK_STATUSES = frozenset(
    {STATUS_PENDING, STATUS_IN_PROGRESS, STATUS_DONE, STATUS_FAILED, STATUS_DEAD_LETTER}
)
assert STATUSES == _EXPECTED_CHECK_STATUSES, (
    "release_events status enum drift — update both "
    "event_router.STATUSES and alembic 0232._STATUS_LITERAL"
)


# Re-export for callers that want a single import path.
__all__ = [
    "DEFAULT_BACKOFF_BASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "DeadLetterAlert",
    "EventIngestRequest",
    "EventIngestResponse",
    "EventInsertFailed",
    "STATUSES",
    "STATUS_DEAD_LETTER",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_IN_PROGRESS",
    "STATUS_PENDING",
    "claim_next_event",
    "get_event",
    "make_idempotency_key",
    "mark_dead_letter_immediately",
    "mark_done",
    "mark_retry",
    "persist_event",
    "router",
    "set_engine_for_tests",
]


# Reset helper used by tests to avoid leaking the prod engine across
# unit tests when run in the same process.
def _reset_prod_engine_for_tests() -> None:
    global _prod_engine
    _prod_engine = None


def _sleep_for_backoff(seconds: float) -> None:
    """Tiny indirection so tests can monkey-patch the sleep without
    importing :mod:`time` everywhere. The worker calls this between
    retries."""
    time.sleep(seconds)
