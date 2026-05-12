"""OP-948 H3 — Postgres-backed release state machine.

Per-version state for the Sprint H event-driven release conductor. One
row per ``RELEASE-vX.Y.Z``; every successful ``transition`` bumps the
state, appends to ``transition_log_json``, and increments
``row_version`` for optimistic-locking detection of double writers.

Contract surface (called by H2 webhook dispatchers + the H4 cron
fallback):

* :func:`create` — insert a fresh ``pending`` row for a brand-new
  release. Raises if a row for ``version`` already exists.
* :func:`transition` — atomic ``UPDATE`` that refuses anything outside
  the declared transition graph (``IllegalStateTransition``) and
  refuses lost-update races (``RaceConditionDoubleTransition``).
* :func:`get` / :func:`get_history` — read paths used by the query API
  and by the H2 dispatcher to inspect the current state before
  dispatching.

State graph (AC #2)
-------------------
::

    pending ─── building ─── staging ─── canary_5 ─── canary_25 ─── canary_100 ─── done
       │           │             │           │            │              │
       └───────────┴─────────────┴───────────┴────────────┴──────────────┘── failed
                                             │            │              │
                                             └────────────┴──────────────┘── rolled_back
    rolled_back ─── pending  (single re-entry edge; AC #6)

* ``failed`` is terminal (no edges out).
* ``rolled_back`` is terminal *except* for a single re-entry edge to
  ``pending`` so the operator can re-instantiate after a rollback (AC
  #6 "rolled_back is terminal except for re-pending").
* ``done`` is terminal — a "done" release that needs to be undone goes
  through the rollback path on the canary stages, not by reverse-
  walking from ``done``.

Engine resolution mirrors :mod:`backend.deploy_audit`: prod reads
``OMNISIGHT_DATABASE_URL``; tests inject an in-memory sqlite engine via
:func:`set_engine_for_tests`. Both code paths use plain sync
SQLAlchemy because release state events are low volume (≤ tens of
transitions per release per day) and a sync API keeps the atomic-
UPDATE critical section easy to reason about.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa


logger = logging.getLogger(__name__)


# ─── State enum (AC #2) ──────────────────────────────────────────────
STATE_PENDING = "pending"
STATE_BUILDING = "building"
STATE_STAGING = "staging"
STATE_CANARY_5 = "canary_5"
STATE_CANARY_25 = "canary_25"
STATE_CANARY_100 = "canary_100"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_ROLLED_BACK = "rolled_back"

STATES: frozenset[str] = frozenset(
    {
        STATE_PENDING,
        STATE_BUILDING,
        STATE_STAGING,
        STATE_CANARY_5,
        STATE_CANARY_25,
        STATE_CANARY_100,
        STATE_DONE,
        STATE_FAILED,
        STATE_ROLLED_BACK,
    }
)


# Allowed transitions: (from_state -> set of to_states).
# Keep this table in lock-step with the diagram in the module docstring
# and with the alembic 0233 CHECK literal.
_HAPPY_PATH = (
    (STATE_PENDING, STATE_BUILDING),
    (STATE_BUILDING, STATE_STAGING),
    (STATE_STAGING, STATE_CANARY_5),
    (STATE_CANARY_5, STATE_CANARY_25),
    (STATE_CANARY_25, STATE_CANARY_100),
    (STATE_CANARY_100, STATE_DONE),
)

# Any non-terminal state can fail; canary stages can be rolled back.
_FAIL_EDGES = {
    STATE_PENDING,
    STATE_BUILDING,
    STATE_STAGING,
    STATE_CANARY_5,
    STATE_CANARY_25,
    STATE_CANARY_100,
}
_ROLLBACK_EDGES = {STATE_CANARY_5, STATE_CANARY_25, STATE_CANARY_100}

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {state: frozenset() for state in STATES}
for _src, _dst in _HAPPY_PATH:
    ALLOWED_TRANSITIONS[_src] = ALLOWED_TRANSITIONS[_src] | {_dst}
for _src in _FAIL_EDGES:
    ALLOWED_TRANSITIONS[_src] = ALLOWED_TRANSITIONS[_src] | {STATE_FAILED}
for _src in _ROLLBACK_EDGES:
    ALLOWED_TRANSITIONS[_src] = ALLOWED_TRANSITIONS[_src] | {STATE_ROLLED_BACK}
# AC #6: rolled_back is terminal except for re-pending (operator
# decides to re-instantiate the version after a rollback).
ALLOWED_TRANSITIONS[STATE_ROLLED_BACK] = ALLOWED_TRANSITIONS[STATE_ROLLED_BACK] | {
    STATE_PENDING
}
# Freeze the value sets — keys are already frozen at module load.
ALLOWED_TRANSITIONS = {k: frozenset(v) for k, v in ALLOWED_TRANSITIONS.items()}

# Genuinely terminal states — no outgoing edges. ``rolled_back`` is
# deliberately NOT listed: it keeps a single re-entry edge to
# ``pending`` (AC #6), so a rolled-back release may still be "open"
# pending an operator decision and stays visible on the G7 dashboard.
TERMINAL_STATES: frozenset[str] = frozenset({STATE_DONE, STATE_FAILED})


# ─── Error catalog (per ticket description) ──────────────────────────
class IllegalStateTransition(RuntimeError):
    """Refused because the (from, to) edge is not in the state graph.

    The H2 dispatcher must alert on this — it is a state-machine
    invariant violation, not a transient infrastructure failure.
    """


class RaceConditionDoubleTransition(RuntimeError):
    """Refused because another writer raced us for the same transition.

    Optimistic-locking version column detected a stale ``row_version``
    on the UPDATE. The caller should re-read state and decide whether
    to retry (idempotent) or surface the duplicate-delivery signal.
    """


class ReleaseNotFound(LookupError):
    """No ``release_state`` row exists for the given version."""


# ─── Engine plumbing (mirrors backend.deploy_audit) ──────────────────
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
        url = os.environ.get("OMNISIGHT_DATABASE_URL", "sqlite:///release_state.db")
        _prod_engine = sa.create_engine(url, future=True)
    return _prod_engine


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Public API ──────────────────────────────────────────────────────
def create(*, release_id: str, version: str) -> int:
    """Insert a fresh ``pending`` row for a release. Returns row id.

    Raises ``ValueError`` if ``release_id`` or ``version`` is empty;
    raises ``sqlalchemy.exc.IntegrityError`` (propagated as-is) if a
    row for ``version`` already exists — callers that want idempotent
    upsert semantics should catch the IntegrityError and call
    :func:`get` instead.
    """
    if not release_id or not release_id.strip():
        raise ValueError("release_id must be a non-empty string")
    if not version or not version.strip():
        raise ValueError("version must be a non-empty string")

    ts = _now_iso()
    # AC #4: transition_log_json is JSONL — one JSON object per line.
    # The genesis entry seeds the log so readers always see the
    # ``pending`` row appear as a real transition event.
    genesis_line = json.dumps(
        {"from": None, "to": STATE_PENDING, "reason": "created", "at": ts},
        separators=(",", ":"),
    )
    with _engine().begin() as conn:
        result = conn.execute(
            sa.text(
                "INSERT INTO release_state "
                "(release_id, version, state, row_version, "
                " last_transition_at, transition_log_json, created_at) "
                "VALUES (:release_id, :version, :state, 0, "
                ":ts, :log, :ts)"
            ),
            {
                "release_id": release_id.strip(),
                "version": version.strip(),
                "state": STATE_PENDING,
                "ts": ts,
                "log": genesis_line,
            },
        )
        if conn.dialect.name == "postgresql":
            row = conn.execute(
                sa.text(
                    "SELECT id FROM release_state WHERE version = :v"
                ),
                {"v": version.strip()},
            ).first()
            return int(row[0]) if row else 0
        return int(result.lastrowid or 0)


def transition(
    *,
    release_id: str,
    from_state: str,
    to_state: str,
    reason: str,
) -> dict[str, Any]:
    """Atomic state transition.

    Looks up the row by ``release_id``, refuses anything outside
    :data:`ALLOWED_TRANSITIONS`, then runs an UPDATE with a WHERE
    clause that matches both the expected ``state`` *and* the current
    ``row_version`` so a concurrent writer cannot lose-update us.

    Returns the post-transition row dict on success. Raises:

    * :class:`IllegalStateTransition` — (from, to) is not in the graph,
      or the current row state is not ``from_state``.
    * :class:`RaceConditionDoubleTransition` — UPDATE matched 0 rows
      due to a stale ``row_version`` (another writer beat us to it).
    * :class:`ReleaseNotFound` — no row for ``release_id``.
    """
    if from_state not in STATES:
        raise IllegalStateTransition(
            f"from_state must be one of {sorted(STATES)}; got {from_state!r}"
        )
    if to_state not in STATES:
        raise IllegalStateTransition(
            f"to_state must be one of {sorted(STATES)}; got {to_state!r}"
        )
    if to_state not in ALLOWED_TRANSITIONS[from_state]:
        raise IllegalStateTransition(
            f"transition {from_state!r} -> {to_state!r} is not allowed; "
            f"allowed targets from {from_state!r}: "
            f"{sorted(ALLOWED_TRANSITIONS[from_state])}"
        )
    if not reason or not reason.strip():
        raise ValueError("transition reason must be a non-empty string")

    ts = _now_iso()
    reason_clean = reason.strip()

    with _engine().begin() as conn:
        row = conn.execute(
            sa.text(
                "SELECT id, state, row_version, transition_log_json "
                "FROM release_state WHERE release_id = :rid"
            ),
            {"rid": release_id},
        ).first()
        if row is None:
            raise ReleaseNotFound(
                f"no release_state row for release_id={release_id!r}"
            )

        row_id, current_state, current_row_version, current_log = row
        if current_state != from_state:
            raise IllegalStateTransition(
                f"release {release_id!r} is in state {current_state!r}, "
                f"not {from_state!r}; refusing transition to {to_state!r}"
            )

        log_entries = _parse_jsonl(current_log)
        new_entry = {
            "from": from_state,
            "to": to_state,
            "reason": reason_clean,
            "at": ts,
        }
        new_line = json.dumps(new_entry, separators=(",", ":"))
        # JSONL append: existing blob + "\n" + new_line. If the existing
        # blob is empty (defensive — create() always seeds the genesis
        # line) we skip the leading newline so the column stays well-
        # formed.
        if current_log:
            new_log = f"{current_log}\n{new_line}"
        else:
            new_log = new_line
        log_entries.append(new_entry)
        next_row_version = current_row_version + 1

        update_result = conn.execute(
            sa.text(
                "UPDATE release_state SET "
                "  state = :to_state, "
                "  row_version = :next_rv, "
                "  last_transition_at = :ts, "
                "  transition_log_json = :log "
                "WHERE id = :rid_pk "
                "  AND state = :from_state "
                "  AND row_version = :prev_rv"
            ),
            {
                "to_state": to_state,
                "next_rv": next_row_version,
                "ts": ts,
                "log": new_log,
                "rid_pk": row_id,
                "from_state": from_state,
                "prev_rv": current_row_version,
            },
        )
        if update_result.rowcount != 1:
            raise RaceConditionDoubleTransition(
                f"race detected on release {release_id!r}: another "
                f"writer modified row_version away from "
                f"{current_row_version} before our UPDATE landed "
                f"(rowcount={update_result.rowcount})"
            )

    logger.info(
        "release_state.transition release_id=%s version_chain=%s -> %s reason=%s",
        release_id,
        from_state,
        to_state,
        reason_clean,
    )
    return {
        "release_id": release_id,
        "state": to_state,
        "row_version": next_row_version,
        "last_transition_at": ts,
        "transition_log": log_entries,
    }


def get(*, version: str) -> dict[str, Any]:
    """Return the current row for ``version``.

    Raises :class:`ReleaseNotFound` if no row exists.
    """
    if not version or not version.strip():
        raise ValueError("version must be a non-empty string")
    with _engine().connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT id, release_id, version, state, row_version, "
                "       last_transition_at, transition_log_json, "
                "       created_at "
                "FROM release_state WHERE version = :v"
            ),
            {"v": version.strip()},
        ).first()
    if row is None:
        raise ReleaseNotFound(f"no release_state row for version={version!r}")
    return _row_to_dict(row)


def get_by_release_id(*, release_id: str) -> dict[str, Any]:
    """Return the current row for ``release_id`` (the JIRA META key).

    Raises :class:`ReleaseNotFound` if no row exists.
    """
    if not release_id or not release_id.strip():
        raise ValueError("release_id must be a non-empty string")
    with _engine().connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT id, release_id, version, state, row_version, "
                "       last_transition_at, transition_log_json, "
                "       created_at "
                "FROM release_state WHERE release_id = :rid"
            ),
            {"rid": release_id.strip()},
        ).first()
    if row is None:
        raise ReleaseNotFound(
            f"no release_state row for release_id={release_id!r}"
        )
    return _row_to_dict(row)


def get_history(*, version: str) -> list[dict[str, Any]]:
    """Return the append-only transition log for ``version``.

    Raises :class:`ReleaseNotFound` if no row exists.
    """
    return list(get(version=version)["transition_log"])


# ─── OP-949 H4 — operator-approval sub-state ─────────────────────────
#
# The H3 state column is a closed enum pinned by the alembic 0233 CHECK
# constraint, so we model "release is awaiting an operator decision" as
# a *sub-state* recorded in ``transition_log_json``. The main ``state``
# column is left untouched; the H2 dispatcher records an "approval
# requested" log entry when a release reaches a gate, and the H4 web UI
# records the operator's verdict as a matching "approval granted" /
# "approval aborted" entry. Computing the current sub-state is a
# tail-scan over the log entries.
#
# Wire shape (all entries are dicts inside the JSONL blob):
#
#   {"kind": "approval_pending",
#    "state": "<current main state>",
#    "canary_percent": <int|None>,
#    "slo_snapshot": {...|None},
#    "reason": "...",
#    "at": "<iso8601>"}
#
#   {"kind": "approval_granted" | "approval_aborted",
#    "operator": "<email>",
#    "reason": "...",
#    "at": "<iso8601>"}
#
# This sub-state lives only in the JSONL column — no DB CHECK, no new
# column. That keeps the contract additive and avoids a schema change.

APPROVAL_KIND_PENDING = "approval_pending"
APPROVAL_KIND_GRANTED = "approval_granted"
APPROVAL_KIND_ABORTED = "approval_aborted"


class ApprovalAlreadyResolved(RuntimeError):
    """Two operators clicked at the same time — second click loses.

    Mirrors the ``RaceConditionApproval`` error from the H4 ticket's
    error catalog. The web UI surfaces this as a "already approved /
    already aborted by <operator>" toast so the second clicker doesn't
    think their click failed for an infrastructure reason.
    """


def _latest_approval_entry(
    log_entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the trailing approval-* log entry, or None.

    The tail-scan ignores the ordinary state-transition entries (which
    have ``from`` / ``to`` keys but no ``kind``) and only inspects the
    approval markers. A pending entry is the "current sub-state"; a
    granted/aborted entry after it means the sub-state is resolved.
    """
    for entry in reversed(log_entries):
        kind = entry.get("kind")
        if kind in (
            APPROVAL_KIND_PENDING,
            APPROVAL_KIND_GRANTED,
            APPROVAL_KIND_ABORTED,
        ):
            return entry
    return None


def _append_log_entry(
    *,
    release_id: str,
    entry: dict[str, Any],
    expect_unresolved_pending: bool = False,
) -> dict[str, Any]:
    """Append ``entry`` to the row's JSONL log with optimistic locking.

    When ``expect_unresolved_pending`` is True the call refuses unless
    the trailing approval-* entry is ``approval_pending`` — that's how
    ``record_decision`` enforces "the approval must not already be
    resolved by another operator". The same row_version bump that
    protects ``transition`` from lost-update races also protects this
    write path.

    Returns the full post-write row dict.
    """
    if not release_id or not release_id.strip():
        raise ValueError("release_id must be a non-empty string")
    ts = _now_iso()
    enriched = {**entry, "at": ts}

    with _engine().begin() as conn:
        row = conn.execute(
            sa.text(
                "SELECT id, state, row_version, transition_log_json, "
                "       version "
                "FROM release_state WHERE release_id = :rid"
            ),
            {"rid": release_id},
        ).first()
        if row is None:
            raise ReleaseNotFound(
                f"no release_state row for release_id={release_id!r}"
            )
        row_id, current_state, current_rv, current_log, version = row
        log_entries = _parse_jsonl(current_log)
        latest = _latest_approval_entry(log_entries)
        if expect_unresolved_pending and (
            latest is None or latest.get("kind") != APPROVAL_KIND_PENDING
        ):
            raise ApprovalAlreadyResolved(
                f"release {release_id!r} has no unresolved approval "
                f"(latest kind={latest.get('kind') if latest else None!r})"
            )
        # ``request_approval`` is idempotent — re-requesting on an
        # already-pending row is a no-op. The dispatcher may replay
        # webhooks, so a duplicate request must not produce a duplicate
        # log entry.
        if (
            entry.get("kind") == APPROVAL_KIND_PENDING
            and latest is not None
            and latest.get("kind") == APPROVAL_KIND_PENDING
        ):
            return {
                "release_id": release_id,
                "version": version,
                "state": current_state,
                "row_version": int(current_rv),
                "transition_log": log_entries,
                "approval": latest,
                "deduplicated": True,
            }

        new_line = json.dumps(enriched, separators=(",", ":"))
        new_log = f"{current_log}\n{new_line}" if current_log else new_line
        next_rv = int(current_rv) + 1
        update = conn.execute(
            sa.text(
                "UPDATE release_state SET "
                "  row_version = :next_rv, "
                "  last_transition_at = :ts, "
                "  transition_log_json = :log "
                "WHERE id = :rid_pk AND row_version = :prev_rv"
            ),
            {
                "next_rv": next_rv,
                "ts": ts,
                "log": new_log,
                "rid_pk": row_id,
                "prev_rv": current_rv,
            },
        )
        if update.rowcount != 1:
            raise RaceConditionDoubleTransition(
                f"race detected on release {release_id!r}: another writer "
                f"modified row_version away from {current_rv} before our "
                f"approval-log UPDATE landed (rowcount={update.rowcount})"
            )
        log_entries.append(enriched)
        return {
            "release_id": release_id,
            "version": version,
            "state": current_state,
            "row_version": next_rv,
            "transition_log": log_entries,
            "approval": enriched,
            "deduplicated": False,
        }


def request_approval(
    *,
    release_id: str,
    canary_percent: int | None = None,
    slo_snapshot: dict[str, Any] | None = None,
    reason: str = "operator_gate",
) -> dict[str, Any]:
    """Mark a release as awaiting operator approval.

    Called by the H2 dispatcher when a release reaches a gate that
    needs human sign-off (replacing the JIRA +2 gate per R8 / L1 R8).
    Idempotent: replaying the same request on an already-pending row
    is a no-op so duplicate webhook deliveries don't bloat the log.

    Raises:
    * :class:`ReleaseNotFound` — no row for ``release_id``.
    * :class:`RaceConditionDoubleTransition` — concurrent log writer.
    """
    return _append_log_entry(
        release_id=release_id,
        entry={
            "kind": APPROVAL_KIND_PENDING,
            "canary_percent": canary_percent,
            "slo_snapshot": slo_snapshot,
            "reason": reason,
        },
    )


def record_decision(
    *,
    release_id: str,
    decision: str,
    operator: str,
    reason: str = "",
) -> dict[str, Any]:
    """Record an operator's approve / abort verdict.

    ``decision`` must be ``"approve"`` or ``"abort"``. Refuses if the
    trailing approval-* entry isn't a pending request (i.e. nothing to
    decide, or another operator already decided).

    Raises:
    * :class:`ReleaseNotFound` — no row for ``release_id``.
    * :class:`ApprovalAlreadyResolved` — pending sub-state is missing
      or has already been resolved by another operator (AC error
      ``RaceConditionApproval``).
    * :class:`RaceConditionDoubleTransition` — concurrent log writer
      bumped the row_version under us.
    """
    if decision not in ("approve", "abort"):
        raise ValueError(f"decision must be 'approve' or 'abort'; got {decision!r}")
    if not operator or not operator.strip():
        raise ValueError("operator must be a non-empty string")
    kind = (
        APPROVAL_KIND_GRANTED
        if decision == "approve"
        else APPROVAL_KIND_ABORTED
    )
    return _append_log_entry(
        release_id=release_id,
        entry={
            "kind": kind,
            "operator": operator.strip(),
            "reason": reason.strip(),
        },
        expect_unresolved_pending=True,
    )


def list_pending_approvals() -> list[dict[str, Any]]:
    """Return every release whose latest approval-* entry is pending.

    Scans the ``release_state`` table and tail-scans each row's log.
    The H4 admin panel calls this on first paint + on every SSE
    ``release.dashboard.updated`` tick. The release set is small (≤
    handful active at a time), so a naive scan beats maintaining a
    secondary index.
    """
    with _engine().connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT id, release_id, version, state, row_version, "
                "       last_transition_at, transition_log_json, "
                "       created_at "
                "FROM release_state "
                "ORDER BY last_transition_at DESC"
            )
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = _row_to_dict(row)
        latest = _latest_approval_entry(d["transition_log"])
        if latest is None or latest.get("kind") != APPROVAL_KIND_PENDING:
            continue
        out.append(
            {
                "release_id": d["release_id"],
                "version": d["version"],
                "state": d["state"],
                "row_version": d["row_version"],
                "last_transition_at": d["last_transition_at"],
                "canary_percent": latest.get("canary_percent"),
                "slo_snapshot": latest.get("slo_snapshot"),
                "reason": latest.get("reason"),
                "requested_at": latest.get("at"),
            }
        )
    return out


def list_releases(*, include_terminal: bool = False) -> list[dict[str, Any]]:
    """Return every ``release_state`` row, newest transition first.

    Each dict is the :func:`_row_to_dict` shape plus an ``approval``
    key carrying the trailing approval-* log entry (or ``None``). When
    ``include_terminal`` is False (the default) rows whose ``state`` is
    in :data:`TERMINAL_STATES` (``done`` / ``failed``) are omitted —
    the G7 "Pending releases" dashboard (OP-943) only cares about
    in-flight work. ``rolled_back`` rows are *kept*: they retain the
    AC #6 re-entry edge and may still be awaiting an operator decision.
    """
    with _engine().connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT id, release_id, version, state, row_version, "
                "       last_transition_at, transition_log_json, "
                "       created_at "
                "FROM release_state "
                "ORDER BY last_transition_at DESC"
            )
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = _row_to_dict(row)
        if not include_terminal and d["state"] in TERMINAL_STATES:
            continue
        d["approval"] = _latest_approval_entry(d["transition_log"])
        out.append(d)
    return out


def get_approval_status(*, release_id: str) -> dict[str, Any] | None:
    """Return the trailing approval-* entry for ``release_id``, or None.

    Used by the H4 API to answer "is this release still in
    pending-approval?" after the operator double-clicks. Returns
    ``None`` if no approval entry has ever been written.
    """
    row = get_by_release_id(release_id=release_id)
    return _latest_approval_entry(row["transition_log"])


def _parse_jsonl(blob: str | None) -> list[dict[str, Any]]:
    """Parse a JSONL string into a list of dicts.

    Tolerates trailing whitespace / blank lines. Skips lines that
    don't parse — JSONL is meant to degrade gracefully when the tail
    of the blob is truncated. A line that parses but isn't a dict is
    also skipped (defensive — the writer only ever produces dicts).
    """
    if not blob:
        return []
    out: list[dict[str, Any]] = []
    for line in blob.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def _row_to_dict(row: sa.Row) -> dict[str, Any]:
    log = _parse_jsonl(row[6])
    return {
        "id": int(row[0]),
        "release_id": row[1],
        "version": row[2],
        "state": row[3],
        "row_version": int(row[4]),
        "last_transition_at": str(row[5]),
        "transition_log": log,
        "created_at": str(row[7]),
    }
