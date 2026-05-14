"""OP-1106 / v2-Ⅹ-1bc — Runner coordination substrate (shadow-only at land time).

Backwards-compat: safe (additive module + table; no consumer reads it yet).

Provides a 4-function API for runner claim coordination backed by the
``runner_claims`` Postgres/SQLite table (alembic 0234). Replaces the
JIRA-label-based mutex (OP-977's ``find_mutex_holders`` +
``claim_ticket_atomic`` in :mod:`backend.agents.jira_dispatch`) over
the next Atlas tickets:

* :func:`acquire_claim` — atomic INSERT; raises :class:`ClaimBlocked`
  on resource collision. Idempotent for same-owner re-acquire.
* :func:`release_claim` — soft delete (``state='released'``); fencing
  token must match the active row. Idempotent: releasing an
  already-released or never-existed lease is a no-op.
* :func:`record_phase` — heartbeat + phase tracker on active claim.
  Used by long-running tickets to signal liveness.
* :func:`find_active_holders` — read-side query that replaces the JQL
  ``find_mutex_holders`` once OP-1108 cuts the read path over.

Fencing token format extends OP-977's
``claim:{instance}:{epoch_us}-{uuid}`` so callers can reuse the
existing label-parsing helpers during the strangler-pattern migration.

Schema reference: see docstring of
``backend/alembic/versions/0234_runner_claims.py`` §"Schema rationale".

Why sqlite3 (sync) and not asyncpg/aiosqlite
--------------------------------------------
The three runner entry points
(``auto-runner-jira.py`` / ``auto-runner-multi.py`` /
``auto-runner-codex.py``) are sync code paths. Wrapping them in async
just for the claim layer would force every caller to bridge contexts;
the claim path is also short-lived (single INSERT/UPDATE per
transition) so the sync ergonomics win. When the consumer is the
async webhook handler (Sprint H), a thin async adapter can be added
without changing the public API.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional, Sequence

__all__ = [
    "ClaimLease",
    "CoordinationError",
    "ClaimBlocked",
    "ClaimNotFound",
    "FencingTokenMismatch",
    "acquire_claim",
    "release_claim",
    "record_phase",
    "find_active_holders",
    "expire_stale_active_claims",
]


# ── Exceptions ────────────────────────────────────────────────────────


class CoordinationError(Exception):
    """Base for runner_coordination errors."""


class ClaimBlocked(CoordinationError):
    """Resource is already actively claimed by another lease."""

    def __init__(self, resource_key: str, existing_lease: "Optional[ClaimLease]" = None):
        super().__init__(f"resource {resource_key!r} actively claimed")
        self.resource_key = resource_key
        self.existing_lease = existing_lease


class ClaimNotFound(CoordinationError):
    """No active claim row for the given lease_id."""

    def __init__(self, lease_id: str):
        super().__init__(f"no active claim for lease_id {lease_id!r}")
        self.lease_id = lease_id


class FencingTokenMismatch(CoordinationError):
    """Fencing token does not match the active claim row — wrong owner."""

    def __init__(self, lease_id: str):
        super().__init__(f"fencing token mismatch for lease_id {lease_id!r}")
        self.lease_id = lease_id


# ── Data types ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ClaimLease:
    """Snapshot of a row in ``runner_claims``."""

    lease_id: str
    ticket_key: str
    resource_key: str
    owner_agent_class: str
    owner_instance_id: str
    fencing_token: str
    state: str  # 'active' | 'released'
    phase: str
    heartbeat_at: str  # ISO8601
    acquired_at: str
    released_at: Optional[str]
    release_reason: Optional[str]
    external_refs: dict


# ── Internals ─────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _make_fencing_token(instance_id: str) -> str:
    """Per OP-977: ``claim:{instance}:{epoch_us}-{uuid}``."""
    epoch_us = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
    return f"claim:{instance_id}:{epoch_us}-{uuid.uuid4()}"


def _db_path() -> Path:
    """Return SQLite DB path (matches alembic env.py convention)."""
    env_path = os.getenv("OMNISIGHT_DATABASE_PATH")
    if env_path:
        return Path(env_path)
    return Path("data/omnisight.db")


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """Open a sqlite3 connection with explicit transaction control.

    ``isolation_level=None`` puts the connection in autocommit mode at
    the driver layer; the caller manages BEGIN/COMMIT/ROLLBACK
    explicitly via :meth:`sqlite3.Connection.execute`. This is the
    pattern recommended by the sqlite3 stdlib docs for code that needs
    explicit transaction boundaries.
    """
    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _row_to_lease(row: sqlite3.Row) -> ClaimLease:
    refs_raw = row["external_refs"]
    try:
        refs = json.loads(refs_raw) if isinstance(refs_raw, str) else (refs_raw or {})
    except (json.JSONDecodeError, TypeError):
        refs = {}
    return ClaimLease(
        lease_id=row["lease_id"],
        ticket_key=row["ticket_key"],
        resource_key=row["resource_key"],
        owner_agent_class=row["owner_agent_class"],
        owner_instance_id=row["owner_instance_id"],
        fencing_token=row["fencing_token"],
        state=row["state"],
        phase=row["phase"],
        heartbeat_at=row["heartbeat_at"],
        acquired_at=row["acquired_at"],
        released_at=row["released_at"],
        release_reason=row["release_reason"],
        external_refs=refs,
    )


# ── Public API ────────────────────────────────────────────────────────


def acquire_claim(
    *,
    ticket_key: str,
    resource_key: str,
    owner_agent_class: str,
    owner_instance_id: str,
    phase: str = "pickup",
    external_refs: Optional[dict] = None,
) -> ClaimLease:
    """Atomically claim ``resource_key`` for ``ticket_key``.

    Returns the resulting :class:`ClaimLease`. Raises
    :class:`ClaimBlocked` (with ``existing_lease`` populated when
    readable) if a different active lease holds ``resource_key``.

    **Idempotency**: re-acquiring an existing active claim by the same
    ``(ticket_key, resource_key, owner_agent_class, owner_instance_id)``
    returns the existing lease unchanged — matching the OP-977
    same-bot fast-fail behaviour. Different owner on a held resource
    → :class:`ClaimBlocked`.
    """
    lease_id = str(uuid.uuid4())
    fencing_token = _make_fencing_token(owner_instance_id)
    now = _now_iso()
    refs_json = json.dumps(external_refs or {})

    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM runner_claims "
                "WHERE resource_key = ? AND state = 'active' LIMIT 1",
                (resource_key,),
            ).fetchone()

            if row is not None:
                if (
                    row["ticket_key"] == ticket_key
                    and row["owner_agent_class"] == owner_agent_class
                    and row["owner_instance_id"] == owner_instance_id
                ):
                    conn.execute("COMMIT")
                    return _row_to_lease(row)
                conn.execute("ROLLBACK")
                raise ClaimBlocked(resource_key, _row_to_lease(row))

            try:
                conn.execute(
                    "INSERT INTO runner_claims ("
                    "lease_id, ticket_key, resource_key, owner_agent_class, "
                    "owner_instance_id, fencing_token, state, phase, "
                    "heartbeat_at, acquired_at, released_at, release_reason, "
                    "external_refs) VALUES ("
                    "?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, NULL, NULL, ?)",
                    (
                        lease_id,
                        ticket_key,
                        resource_key,
                        owner_agent_class,
                        owner_instance_id,
                        fencing_token,
                        phase,
                        now,
                        now,
                        refs_json,
                    ),
                )
            except sqlite3.IntegrityError:
                # Partial unique index hit: another acquirer raced ahead
                # between our SELECT and INSERT. Re-read to surface the
                # winning lease.
                conn.execute("ROLLBACK")
                row = conn.execute(
                    "SELECT * FROM runner_claims "
                    "WHERE resource_key = ? AND state = 'active' LIMIT 1",
                    (resource_key,),
                ).fetchone()
                raise ClaimBlocked(resource_key, _row_to_lease(row) if row else None)

            conn.execute("COMMIT")
        except CoordinationError:
            raise
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    return ClaimLease(
        lease_id=lease_id,
        ticket_key=ticket_key,
        resource_key=resource_key,
        owner_agent_class=owner_agent_class,
        owner_instance_id=owner_instance_id,
        fencing_token=fencing_token,
        state="active",
        phase=phase,
        heartbeat_at=now,
        acquired_at=now,
        released_at=None,
        release_reason=None,
        external_refs=external_refs or {},
    )


def release_claim(
    *,
    lease_id: str,
    fencing_token: str,
    release_reason: str,
) -> None:
    """Release an active claim.

    **Idempotency**: releasing an already-released lease (or one that
    was never acquired) is a no-op — supports the "release in
    ``finally``" runner pattern where the finally may fire on a path
    that already released earlier.

    Raises :class:`FencingTokenMismatch` if the active row exists but
    its fencing_token does not match — protects against a stale lease
    holder overwriting a fresh lease.
    """
    now = _now_iso()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT fencing_token, state FROM runner_claims "
                "WHERE lease_id = ? LIMIT 1",
                (lease_id,),
            ).fetchone()

            if row is None:
                conn.execute("COMMIT")
                return
            if row["state"] == "released":
                conn.execute("COMMIT")
                return
            if row["fencing_token"] != fencing_token:
                conn.execute("ROLLBACK")
                raise FencingTokenMismatch(lease_id)

            conn.execute(
                "UPDATE runner_claims SET state = 'released', "
                "released_at = ?, release_reason = ? "
                "WHERE lease_id = ? AND state = 'active'",
                (now, release_reason, lease_id),
            )
            conn.execute("COMMIT")
        except CoordinationError:
            raise
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise


def record_phase(
    *,
    lease_id: str,
    fencing_token: str,
    phase: str,
) -> None:
    """Update ``phase`` and bump ``heartbeat_at`` on an active claim.

    Raises :class:`ClaimNotFound` if no active claim exists.
    Raises :class:`FencingTokenMismatch` if the fencing token doesn't
    match the active row.
    """
    now = _now_iso()
    with _conn() as conn:
        row = conn.execute(
            "SELECT fencing_token, state FROM runner_claims "
            "WHERE lease_id = ? LIMIT 1",
            (lease_id,),
        ).fetchone()
        if row is None or row["state"] != "active":
            raise ClaimNotFound(lease_id)
        if row["fencing_token"] != fencing_token:
            raise FencingTokenMismatch(lease_id)
        conn.execute(
            "UPDATE runner_claims SET phase = ?, heartbeat_at = ? "
            "WHERE lease_id = ? AND state = 'active'",
            (phase, now, lease_id),
        )


def find_active_holders(
    *,
    resource_keys: Optional[Sequence[str]] = None,
    exclude_ticket: Optional[str] = None,
) -> list[ClaimLease]:
    """Return active claim rows, optionally filtered.

    Replaces the JIRA-JQL :func:`backend.agents.jira_dispatch.find_mutex_holders`
    once OP-1108 (v2-Ⅹ-2bc) cuts the read path over. Until then this
    is read-by-tests-only (shadow mode).
    """
    sql = "SELECT * FROM runner_claims WHERE state = 'active'"
    params: list = []
    if resource_keys:
        placeholders = ",".join("?" * len(resource_keys))
        sql += f" AND resource_key IN ({placeholders})"
        params.extend(resource_keys)
    if exclude_ticket:
        sql += " AND ticket_key != ?"
        params.append(exclude_ticket)
    sql += " ORDER BY acquired_at ASC"

    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_lease(r) for r in rows]


def expire_stale_active_claims(*, max_age_seconds: int = 300) -> int:
    """Mark active claims whose heartbeat is older than ``max_age_seconds``
    as ``released`` with reason ``"ttl-expired"``.

    Returns the number of claims expired. Idempotent: a second call with
    the same threshold is a no-op because the just-expired rows have
    ``state='released'`` and are no longer matched by the WHERE clause.

    OP-1109 Integration AC: SIGKILL of a runner mid-pickup must result
    in the orphaned claim being collectable within 5 min. This function
    is the collector; a periodic caller (cron / systemd timer / runner
    pre-pickup helper) is the trigger. The default 300-second TTL aligns
    with the 5-min target; the per-claim heartbeat written by
    :func:`record_phase` is the freshness signal.

    Note: this collects claims by *heartbeat age*, not by *acquired age*.
    A long-running ticket that calls :func:`record_phase` regularly will
    not be expired. A frozen / killed runner stops heartbeating and gets
    swept on the first run after ``max_age_seconds`` elapses.
    """
    now = _now_iso()
    threshold = (
        datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE runner_claims SET state = 'released', "
            "released_at = ?, release_reason = 'ttl-expired' "
            "WHERE state = 'active' AND heartbeat_at < ?",
            (now, threshold),
        )
        return cur.rowcount or 0
