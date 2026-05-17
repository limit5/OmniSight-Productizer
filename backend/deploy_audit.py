"""OP-779 D18 -- change-management compliance audit log for deploys.

Append-only, hash-chained record of every deploy / rollback / SLO
breach / operator action. Backs the change-management compliance
surface required by the D18 spec:

  * every deploy/rollback/SLO-breach/operator-action is recorded
  * entries are immutable (no UPDATE / DELETE API exists; tampering is
    detectable via :func:`verify_chain`)
  * queryable for any change in the last 1y
  * exportable as CSV for compliance audits

Why this is separate from :mod:`backend.audit`
----------------------------------------------
``backend.audit`` is a per-tenant chain for tenant-scoped state
changes (membership / project / billing / etc.). Deploys are
*global* events that do not belong to a tenant -- mixing them into
a tenant chain would either pick a synthetic tenant id (polluting
that tenant's chain) or require a sentinel that compliance reviewers
would have to special-case. A dedicated table keeps the compliance
view free of cross-traffic and lets the schema be tighter
(``kind`` + ``status`` CHECK constraints rather than free-form
``action``).

Hash chain
----------
``curr_hash = sha256(prev_hash || canonical(row))`` where the genesis
row uses ``"0" * 64`` as ``prev_hash``. The chain is single-linear
(no per-tenant branching) so a tampered row breaks the chain from
that point onward; :func:`verify_chain` re-walks and reports the
first divergence.

Concurrency
-----------
Writers serialise on a PG advisory lock keyed to the table name.
SQLite (test mode) is single-process so the lock is a no-op there.
The ``record`` -> ``query`` ordering is read-after-write safe because
``record`` commits the INSERT before returning.

Engine resolution
-----------------
Production reads ``OMNISIGHT_DATABASE_URL``; tests inject a sqlite
engine via :func:`set_engine_for_tests`. Both code paths use plain
synchronous SQLAlchemy because:

  * the volume is low (handful of rows per day) -- no async needed
  * existing async callers (``approve_and_ship``) wrap the call in
    ``asyncio.to_thread`` so they don't block the event loop
  * keeping it sync simplifies the chain-write critical section
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import sqlalchemy as sa


logger = logging.getLogger(__name__)


KIND_DEPLOY = "deploy"
KIND_ROLLBACK = "rollback"
KIND_SLO_BREACH = "slo_breach"
KIND_OPERATOR_ACTION = "operator_action"
ALLOWED_KINDS = frozenset(
    {KIND_DEPLOY, KIND_ROLLBACK, KIND_SLO_BREACH, KIND_OPERATOR_ACTION}
)

STATUS_STARTED = "started"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
ALLOWED_STATUSES = frozenset({STATUS_STARTED, STATUS_SUCCEEDED, STATUS_FAILED})

# SHA-256 produces a 32-byte digest -> 64 hex chars.
SHA256_HEX_LEN = 64

# Default ``query`` window: matches the 1-year compliance retention
# claim in this module's docstring.
DEFAULT_QUERY_WINDOW_DAYS = 365

# ``query`` filters ``ts < :until`` (exclusive). Without a small buffer
# a row inserted at the same wall-clock instant as ``until=now()`` would
# be excluded; one second is well below the audit cadence.
QUERY_UNTIL_CLOCK_SKEW_SECONDS = 1

GENESIS_HASH = "0" * SHA256_HEX_LEN

# Operator-initiated kinds: a non-empty reason is required so the
# compliance trail explains why a human triggered the change.
OPERATOR_KINDS = frozenset({KIND_DEPLOY, KIND_ROLLBACK, KIND_OPERATOR_ACTION})

CSV_COLUMNS = (
    "id",
    "ts",
    "kind",
    "tag",
    "actor",
    "reason",
    "status",
    "elapsed_seconds",
    "context",
    "prev_hash",
    "curr_hash",
)


# Test-injected engine override. Production leaves this as None and
# falls back to a lazy ``OMNISIGHT_DATABASE_URL`` engine.
_test_engine: sa.Engine | None = None
_prod_engine: sa.Engine | None = None


def set_engine_for_tests(engine: sa.Engine | None) -> None:
    """Inject a SQLAlchemy engine for the duration of a test.

    Tests should call ``set_engine_for_tests(engine)`` in setup and
    ``set_engine_for_tests(None)`` in teardown. Production code never
    calls this.
    """
    global _test_engine
    _test_engine = engine


def _engine() -> sa.Engine:
    if _test_engine is not None:
        return _test_engine
    global _prod_engine
    if _prod_engine is None:
        url = os.environ.get("OMNISIGHT_DATABASE_URL", "sqlite:///deploy_audit.db")
        _prod_engine = sa.create_engine(url, future=True)
    return _prod_engine


def _canonical(row: dict[str, Any]) -> str:
    """Deterministic JSON for hashing: sorted keys, no whitespace."""
    return json.dumps(
        row,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _hash(prev_hash: str, payload_canon: str) -> str:
    return hashlib.sha256((prev_hash + payload_canon).encode("utf-8")).hexdigest()


class ImmutableAuditError(RuntimeError):
    """Raised when an UPDATE/DELETE attempt is detected on deploy_audit."""


def _validate_kind(kind: str) -> None:
    if kind not in ALLOWED_KINDS:
        raise ValueError(
            f"deploy_audit kind must be one of {sorted(ALLOWED_KINDS)}; got {kind!r}"
        )


def _validate_status(status: str) -> None:
    if status not in ALLOWED_STATUSES:
        raise ValueError(
            f"deploy_audit status must be one of {sorted(ALLOWED_STATUSES)};"
            f" got {status!r}"
        )


def _validate_reason(kind: str, reason: str | None) -> str | None:
    if kind in OPERATOR_KINDS:
        if reason is None or not reason.strip():
            raise ValueError(
                f"deploy_audit kind {kind!r} requires a non-empty reason"
            )
        return reason.strip()
    return reason.strip() if isinstance(reason, str) and reason.strip() else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _last_hash(conn: sa.Connection) -> str:
    row = conn.execute(
        sa.text(
            "SELECT curr_hash FROM deploy_audit "
            "ORDER BY id DESC LIMIT 1"
        )
    ).first()
    return row[0] if row else GENESIS_HASH


def record(
    *,
    kind: str,
    status: str = STATUS_STARTED,
    tag: str | None = None,
    actor: str | None = None,
    reason: str | None = None,
    elapsed_seconds: float | None = None,
    context: dict[str, Any] | None = None,
) -> int:
    """Append a row to ``deploy_audit`` and return the row id.

    Operator-initiated kinds (``deploy``, ``rollback``,
    ``operator_action``) require a non-empty ``reason``; system events
    (``slo_breach``) may pass ``reason=None``.
    """
    _validate_kind(kind)
    _validate_status(status)
    reason = _validate_reason(kind, reason)
    context_json = (
        json.dumps(context, sort_keys=True, separators=(",", ":"), default=str)
        if context
        else None
    )
    ts = _now_iso()

    engine = _engine()
    is_pg = engine.dialect.name == "postgresql"

    with engine.begin() as conn:
        if is_pg:
            # advisory lock on table name hash so chain writes serialise
            # across workers and processes.
            conn.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtext('deploy_audit'))")
            )
        prev_hash = _last_hash(conn)
        # Canonical row for hashing: include the application-controlled
        # fields, exclude the autoincrement id (unknown until INSERT
        # commits).
        payload = {
            "ts": ts,
            "kind": kind,
            "tag": tag,
            "actor": actor,
            "reason": reason,
            "status": status,
            "elapsed_seconds": elapsed_seconds,
            "context": context_json,
        }
        curr_hash = _hash(prev_hash, _canonical(payload))

        result = conn.execute(
            sa.text(
                "INSERT INTO deploy_audit "
                "(ts, kind, tag, actor, reason, status, elapsed_seconds, "
                " context, prev_hash, curr_hash) "
                "VALUES (:ts, :kind, :tag, :actor, :reason, :status, "
                ":elapsed, :context, :prev_hash, :curr_hash)"
            ),
            {
                "ts": ts,
                "kind": kind,
                "tag": tag,
                "actor": actor,
                "reason": reason,
                "status": status,
                "elapsed": elapsed_seconds,
                "context": context_json,
                "prev_hash": prev_hash,
                "curr_hash": curr_hash,
            },
        )
        # SQLite returns lastrowid; PG via RETURNING-less INSERT we
        # round-trip with a separate select on curr_hash (unique by
        # construction in a non-tampered chain).
        if is_pg:
            row = conn.execute(
                sa.text(
                    "SELECT id FROM deploy_audit WHERE curr_hash = :h "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"h": curr_hash},
            ).first()
            return int(row[0]) if row else 0
        return int(result.lastrowid or 0)


def query(
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    kind: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return rows in chronological order within ``[since, until)``.

    Defaults to the last 1 year (compliance retention window). Pass
    ``since=datetime.min`` for an unbounded scan.
    """
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=DEFAULT_QUERY_WINDOW_DAYS)
    if until is None:
        until = datetime.now(timezone.utc) + timedelta(
            seconds=QUERY_UNTIL_CLOCK_SKEW_SECONDS
        )
    if kind is not None:
        _validate_kind(kind)

    sql = (
        "SELECT id, ts, kind, tag, actor, reason, status, "
        "elapsed_seconds, context, prev_hash, curr_hash "
        "FROM deploy_audit "
        "WHERE ts >= :since AND ts < :until"
    )
    params: dict[str, Any] = {
        "since": since.isoformat(),
        "until": until.isoformat(),
    }
    if kind is not None:
        sql += " AND kind = :kind"
        params["kind"] = kind
    sql += " ORDER BY id ASC"
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = int(limit)

    with _engine().connect() as conn:
        rows = conn.execute(sa.text(sql), params).all()
    return [
        {
            "id": row[0],
            "ts": row[1],
            "kind": row[2],
            "tag": row[3],
            "actor": row[4],
            "reason": row[5],
            "status": row[6],
            "elapsed_seconds": row[7],
            "context": row[8],
            "prev_hash": row[9],
            "curr_hash": row[10],
        }
        for row in rows
    ]


def export_csv(
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    kind: str | None = None,
) -> str:
    """Return the audit window as a CSV string.

    The header is the canonical column order (``CSV_COLUMNS``) so
    compliance tooling can round-trip the export.
    """
    rows = query(since=since, until=until, kind=kind)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([_csv_cell(row[col]) for col in CSV_COLUMNS])
    return buf.getvalue()


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def verify_chain() -> dict[str, Any]:
    """Re-walk the chain and report tampering.

    Returns ``{"ok": True, "rows": N}`` on success, or
    ``{"ok": False, "rows": N, "first_bad_id": id}`` when a row's
    ``curr_hash`` does not match the recomputed hash.
    """
    rows = _query_full_chain()
    expected_prev = GENESIS_HASH
    for row in rows:
        payload = {
            "ts": row["ts"],
            "kind": row["kind"],
            "tag": row["tag"],
            "actor": row["actor"],
            "reason": row["reason"],
            "status": row["status"],
            "elapsed_seconds": row["elapsed_seconds"],
            "context": row["context"],
        }
        expected = _hash(expected_prev, _canonical(payload))
        if row["prev_hash"] != expected_prev or row["curr_hash"] != expected:
            return {
                "ok": False,
                "rows": len(rows),
                "first_bad_id": row["id"],
            }
        expected_prev = row["curr_hash"]
    return {"ok": True, "rows": len(rows)}


def _query_full_chain() -> list[dict[str, Any]]:
    return query(since=datetime(1970, 1, 1, tzinfo=timezone.utc))


def reset_engine_cache() -> None:
    """Test helper -- drop the cached production engine."""
    global _prod_engine
    _prod_engine = None


__all__: Iterable[str] = (
    "KIND_DEPLOY",
    "KIND_ROLLBACK",
    "KIND_SLO_BREACH",
    "KIND_OPERATOR_ACTION",
    "STATUS_STARTED",
    "STATUS_SUCCEEDED",
    "STATUS_FAILED",
    "ALLOWED_KINDS",
    "ALLOWED_STATUSES",
    "OPERATOR_KINDS",
    "CSV_COLUMNS",
    "GENESIS_HASH",
    "ImmutableAuditError",
    "record",
    "query",
    "export_csv",
    "verify_chain",
    "set_engine_for_tests",
    "reset_engine_cache",
)
