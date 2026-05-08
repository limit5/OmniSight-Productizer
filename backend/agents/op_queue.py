"""Durable operation queue (SQLite WAL) — META OP-757 / H10 of OP-747.

Goal: Make every external-system mutation (JIRA transition / comment /
label, Gerrit push, …) crash-safe by recording the *intent* in a local
SQLite database BEFORE invoking the remote API. A worker drains the
queue serially; on process crash the next startup resets any
``in_progress`` row back to ``pending`` and resumes — re-execution is
safe because every op carries a stable ``idempotency_key`` that the
underlying handler honours (JIRA: ``X-Idempotency-Key`` header +
``IdempotencyStore`` cache).

Design decisions worth flagging:

* **Single SQLite WAL file.** WAL mode lets concurrent readers (e.g. a
  monitor / dashboard) inspect the queue while a worker writes. The
  worker stays single-threaded by contract (enforced via ``SELECT ...
  WHERE status='pending' ... LIMIT 1`` + immediate ``UPDATE ... SET
  status='in_progress' WHERE id=? AND status='pending'`` —
  SQLite's ``BEGIN IMMEDIATE`` prevents two workers from claiming the
  same row).

* **Ordering by ``ordering_key`` + ``seq``.** The spec requires
  "transition → push → comment must run in order for the same ticket".
  The producer assigns a monotonic per-key ``seq`` at insert time;
  the claim query refuses to dequeue an op while a sibling op with
  the same ``ordering_key`` is still ``in_progress``, and prefers the
  lowest ``seq`` per group. Default ``ordering_key`` is the
  ``idempotency_key`` itself, which gives every op its own group
  (i.e. no ordering constraint — the common case).

* **Retry with exponential backoff up to 5 attempts.** The 6th failure
  marks the op ``failed`` and dispatches an operator alert via
  :func:`backend.agents.operator_notifier.notify` (DEGRADED severity)
  so an operator can investigate and manually replay or drop the op.
  Backoff schedule: 2, 4, 8, 16, 32 seconds (exponential, base 2).

* **Recovery semantics.** ``OpQueue.recover()`` (called from
  ``__init__``) resets every ``in_progress`` row back to ``pending``.
  The worker may have crashed mid-call; idempotency keys make retry
  safe even if the remote API saw the previous attempt.

* **Idempotency on insert.** ``idempotency_key`` is ``UNIQUE``;
  ``enqueue`` uses ``INSERT OR IGNORE`` so retried inserts (same key)
  are a no-op. The matching existing row's id is returned.

* **Handler registry.** Handlers register via :func:`register_handler`
  and live in this module — the queue does not care what an action
  does, only that the registered callable is idempotent. Each handler
  takes ``args: dict`` and the original ``idempotency_key``; it MUST
  return a JSON-serialisable dict (or ``None``).

Scope of this ticket (META OP-757):

* **Phase A** ✓: JIRA transitions + comments + labels + Gerrit-push
  handlers. Producer helper ``enqueue_jira_transition`` /
  ``enqueue_jira_comment`` / ``enqueue_gerrit_push`` lets callers
  durably record intent without restructuring the runner main loop.
* **Phase B** ✓: ``gerrit.push`` handler shipped alongside Phase A so
  the AC's "transition → push → comment ordered per ticket" can be
  exercised end-to-end.
* **Phase C** (audit log writes): out of scope — audit log already
  goes through Postgres with a different durability story.
* **Phase D** (bridge daemon events): out of scope — gerrit-jira
  bridge consumes Gerrit's stream-events which is itself a durable
  source; wrapping its outbound JIRA transitions through the same
  queue is a follow-up.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger("omnisight.op_queue")

# Default location — overridable via ``OpQueue(path=...)`` for tests.
DEFAULT_DB_PATH = Path("~/.config/omnisight/op-queue.db").expanduser()

MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 2.0  # 2, 4, 8, 16, 32

STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


# ── Schema + connection helpers ──────────────────────────────────────


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    args_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    ordering_key TEXT NOT NULL,
    seq INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    completed_at REAL,
    last_error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    not_before REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ops_pending_idx
    ON ops (status, ordering_key, seq);
CREATE INDEX IF NOT EXISTS ops_status_idx ON ops (status);
"""


def _connect(path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode + reasonable defaults.

    ``isolation_level=None`` puts us in autocommit mode so we can issue
    ``BEGIN IMMEDIATE`` explicitly when we need a write lock for the
    claim transaction. WAL is set per-database (sticky) but the PRAGMA
    is idempotent and cheap, so we run it on every connect.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")  # WAL + NORMAL = durable on commit, fast
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    return conn


# ── Op record ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Op:
    """Snapshot of one ``ops`` row. Frozen so consumers cannot mutate
    fields and accidentally desync from the database."""

    id: int
    action: str
    args: dict[str, Any]
    idempotency_key: str
    ordering_key: str
    seq: int
    status: str
    created_at: float
    completed_at: float | None
    last_error: str | None
    retry_count: int
    not_before: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Op":
        return cls(
            id=row["id"],
            action=row["action"],
            args=json.loads(row["args_json"]),
            idempotency_key=row["idempotency_key"],
            ordering_key=row["ordering_key"],
            seq=row["seq"],
            status=row["status"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            last_error=row["last_error"],
            retry_count=row["retry_count"],
            not_before=row["not_before"],
        )


# ── Handler registry ──────────────────────────────────────────────────


HandlerFn = Callable[[dict[str, Any], str], Any]
_HANDLERS: dict[str, HandlerFn] = {}


def register_handler(action: str, fn: HandlerFn) -> None:
    """Register a handler for ``action``. Re-registration is allowed
    (last write wins) so test fixtures can swap in fakes."""
    _HANDLERS[action] = fn


def get_handler(action: str) -> HandlerFn:
    try:
        return _HANDLERS[action]
    except KeyError as exc:
        raise KeyError(f"no handler registered for action={action!r}") from exc


def registered_actions() -> list[str]:
    return sorted(_HANDLERS)


# ── Operator escalation hook ──────────────────────────────────────────


def _default_escalate(op: Op) -> None:
    """Fired when an op exhausts its retry budget. Goes through the
    operator-notifier bridge so the alert lands on JIRA + email + Slack
    + LINE per severity matrix.

    Imported lazily because the notifier wires up channels via env vars
    at first use; importing it at module load time would also pull in
    the JIRA dispatch client, which the test suite stubs out per-case.
    """
    try:
        from backend.agents.operator_notifier import notify
        notify(
            severity="DEGRADED",
            code=f"op_queue.exhausted:{op.action}",
            message=(
                f"Op {op.id} ({op.action}) failed after {op.retry_count} attempts; "
                f"marked status=failed. Last error: {op.last_error or '(none)'}"
            ),
            context={
                "op_id": op.id,
                "action": op.action,
                "idempotency_key": op.idempotency_key,
                "ordering_key": op.ordering_key,
                "retry_count": op.retry_count,
            },
            scope="op_queue",
            root_cause_key=op.action,
        )
    except Exception:  # noqa: BLE001 — escalation must not crash the worker
        logger.exception("op_queue: failed to escalate op id=%s", op.id)


# ── OpQueue ───────────────────────────────────────────────────────────


class OpQueue:
    """Durable SQLite-backed FIFO operation queue.

    Thread-safe: the worker is expected to be single-threaded but
    ``enqueue`` is safe to call from many producer threads thanks to
    SQLite's per-connection write serialisation. We open one connection
    per call (cheap on local FS) so there is no shared-conn locking
    headache.
    """

    def __init__(
        self,
        path: Path | str = DEFAULT_DB_PATH,
        clock: Callable[[], float] | None = None,
        escalate_fn: Callable[[Op], None] | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        backoff_base: float = BACKOFF_BASE_SECONDS,
    ) -> None:
        self.path = Path(path)
        self._clock = clock or time.time
        self._escalate = escalate_fn or _default_escalate
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._lock = threading.Lock()
        # Initialise schema + recover any in-flight ops from a prior crash.
        with self._connect() as conn:
            self._init_schema(conn)
        self.recover()

    def _connect(self) -> sqlite3.Connection:
        return _connect(self.path)

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        # Schema creation lives in _connect; this hook stays for future
        # migrations (additive ALTER TABLE statements).
        return None

    # Producer ─────────────────────────────────────────────────────────

    def enqueue(
        self,
        action: str,
        args: dict[str, Any],
        idempotency_key: str,
        ordering_key: str | None = None,
        not_before: float | None = None,
    ) -> int:
        """Insert a new op and return its row id.

        If ``idempotency_key`` already exists, returns the existing row's
        id without inserting (no-op). This makes producer retries safe.

        ``ordering_key`` defaults to ``idempotency_key`` (i.e. the op
        is its own ordering group, no implicit serialisation against
        other ops). Pass an explicit key (e.g. JIRA ticket key) to
        serialise multiple ops per ticket.
        """
        ordering = ordering_key or idempotency_key
        nb = not_before if not_before is not None else 0.0
        now = self._clock()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Compute next seq for this ordering group. Holding the
            # immediate write lock guarantees uniqueness.
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM ops WHERE ordering_key = ?",
                (ordering,),
            ).fetchone()
            next_seq = int(row["s"]) + 1
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO ops
                    (action, args_json, idempotency_key, ordering_key, seq,
                     status, created_at, retry_count, not_before)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    action,
                    json.dumps(args, sort_keys=True, default=str),
                    idempotency_key,
                    ordering,
                    next_seq,
                    STATUS_PENDING,
                    now,
                    nb,
                ),
            )
            if cur.rowcount == 0:
                # Row already existed — return the existing id.
                existing = conn.execute(
                    "SELECT id FROM ops WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                conn.execute("COMMIT")
                return int(existing["id"])
            new_id = int(cur.lastrowid)
            conn.execute("COMMIT")
            return new_id

    # Worker ───────────────────────────────────────────────────────────

    def claim_next(self, now: float | None = None) -> Op | None:
        """Atomically claim the next runnable op.

        Selection rules (in priority order):
        1. ``status='pending'`` AND ``not_before <= now``;
        2. no other op with the same ``ordering_key`` is currently
           ``in_progress`` (in-order constraint);
        3. lowest ``seq`` per ordering group, oldest ``created_at`` first.

        Returns the claimed :class:`Op` (now ``in_progress``) or
        ``None`` if nothing is runnable.
        """
        t = now if now is not None else self._clock()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM ops
                WHERE status = ?
                  AND not_before <= ?
                  AND ordering_key NOT IN (
                      SELECT ordering_key FROM ops WHERE status = ?
                  )
                ORDER BY seq ASC, created_at ASC, id ASC
                LIMIT 1
                """,
                (STATUS_PENDING, t, STATUS_IN_PROGRESS),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            op = Op.from_row(row)
            # Optimistic update — refuse if status changed under us.
            cur = conn.execute(
                "UPDATE ops SET status = ? WHERE id = ? AND status = ?",
                (STATUS_IN_PROGRESS, op.id, STATUS_PENDING),
            )
            if cur.rowcount == 0:
                conn.execute("COMMIT")
                return None
            conn.execute("COMMIT")
            return Op(
                id=op.id,
                action=op.action,
                args=op.args,
                idempotency_key=op.idempotency_key,
                ordering_key=op.ordering_key,
                seq=op.seq,
                status=STATUS_IN_PROGRESS,
                created_at=op.created_at,
                completed_at=op.completed_at,
                last_error=op.last_error,
                retry_count=op.retry_count,
                not_before=op.not_before,
            )

    def mark_completed(self, op_id: int) -> None:
        now = self._clock()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE ops SET status = ?, completed_at = ?, last_error = NULL "
                "WHERE id = ?",
                (STATUS_COMPLETED, now, op_id),
            )

    def mark_failed(self, op: Op, error: str) -> Op:
        """Record a failed attempt. Returns the updated Op row.

        Behaviour:
        * If ``retry_count + 1 < max_attempts`` → status=pending, schedule
          ``not_before = now + backoff``;
        * Else → status=failed, escalate to operator-notifier.
        """
        now = self._clock()
        attempt = op.retry_count + 1
        with self._lock, self._connect() as conn:
            if attempt < self._max_attempts:
                backoff = self._backoff_base ** attempt
                conn.execute(
                    """UPDATE ops SET status = ?, retry_count = ?,
                       last_error = ?, not_before = ? WHERE id = ?""",
                    (STATUS_PENDING, attempt, error[:2000], now + backoff, op.id),
                )
                row = conn.execute("SELECT * FROM ops WHERE id = ?", (op.id,)).fetchone()
                return Op.from_row(row)
            # Exhausted: terminal failure.
            conn.execute(
                """UPDATE ops SET status = ?, retry_count = ?,
                   last_error = ?, completed_at = ? WHERE id = ?""",
                (STATUS_FAILED, attempt, error[:2000], now, op.id),
            )
            row = conn.execute("SELECT * FROM ops WHERE id = ?", (op.id,)).fetchone()
        terminal = Op.from_row(row)
        # Escalation outside the lock — avoid blocking other producers
        # while the notifier (which talks to JIRA) runs.
        self._escalate(terminal)
        return terminal

    def recover(self) -> int:
        """Reset every ``in_progress`` op back to ``pending``.

        Called automatically on construction; safe to call repeatedly.
        Returns the number of rows recovered. Idempotency keys ensure
        any half-completed remote work is replayed safely.
        """
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """UPDATE ops SET status = ?, not_before = 0
                   WHERE status = ?""",
                (STATUS_PENDING, STATUS_IN_PROGRESS),
            )
            n = int(cur.rowcount or 0)
        if n:
            logger.info("op_queue.recover: reset %d in_progress ops to pending", n)
        return n

    # Introspection ────────────────────────────────────────────────────

    def get(self, op_id: int) -> Op | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM ops WHERE id = ?", (op_id,)).fetchone()
        return Op.from_row(row) if row is not None else None

    def by_idempotency_key(self, key: str) -> Op | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ops WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return Op.from_row(row) if row is not None else None

    def list_by_status(self, status: str, limit: int = 100) -> list[Op]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ops WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        return [Op.from_row(r) for r in rows]

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM ops GROUP BY status"
            ).fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}


# ── Worker ────────────────────────────────────────────────────────────


class Worker:
    """Single-threaded queue drainer.

    ``drain_once()`` claims at most one op and runs its handler.
    Returns the executed :class:`Op` or ``None`` if nothing was ready.

    ``run_forever(poll_interval)`` loops until ``stop()`` is called or
    ``stop_event`` fires. Suitable for a daemon process; tests use
    ``drain_once`` in a controlled loop.
    """

    def __init__(
        self,
        queue: OpQueue,
        handlers: dict[str, HandlerFn] | None = None,
    ) -> None:
        self.queue = queue
        # Local override map; falls back to the module-global registry
        # so tests can pass per-instance fakes without leaking globals.
        self._handlers: dict[str, HandlerFn] = handlers or {}
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def _resolve_handler(self, action: str) -> HandlerFn:
        if action in self._handlers:
            return self._handlers[action]
        return get_handler(action)

    def drain_once(self) -> Op | None:
        op = self.queue.claim_next()
        if op is None:
            return None
        try:
            handler = self._resolve_handler(op.action)
            handler(op.args, op.idempotency_key)
        except Exception as exc:  # noqa: BLE001 — every handler error is recoverable
            logger.warning(
                "op_queue: action=%s op_id=%s attempt=%s failed: %s",
                op.action, op.id, op.retry_count + 1, exc,
            )
            return self.queue.mark_failed(op, f"{type(exc).__name__}: {exc}")
        self.queue.mark_completed(op.id)
        return op

    def drain_until_empty(self, max_ops: int = 1000) -> list[Op]:
        """Run ops until ``claim_next`` returns ``None`` or ``max_ops``
        is reached (defensive cap so a runaway producer can't hold the
        worker forever in one drain call)."""
        done: list[Op] = []
        for _ in range(max_ops):
            op = self.drain_once()
            if op is None:
                break
            done.append(op)
        return done

    def run_forever(self, poll_interval: float = 1.0) -> None:
        while not self._stop_event.is_set():
            try:
                op = self.drain_once()
            except Exception:  # noqa: BLE001 — keep the worker alive across surprises
                logger.exception("op_queue.worker: drain_once raised")
                op = None
            if op is None:
                self._stop_event.wait(poll_interval)


# ── Default handlers (Phase A + B) ────────────────────────────────────
#
# Handlers stay thin so the queue file does not become a god-module —
# they delegate to the existing ``jira_dispatch`` API and rely on the
# ``X-Idempotency-Key`` header that ``_request_idempotent`` already
# sends. The args dict shape mirrors the public function signature.


def _make_jira_client(agent_class: str) -> Any:
    from backend.agents.jira_dispatch import make_client
    return make_client(agent_class)


def _handle_jira_transition_to_in_progress(args: dict[str, Any], idem_key: str) -> None:
    from backend.agents.jira_dispatch import transition_to_in_progress
    client = args.get("__client") or _make_jira_client(args["agent_class"])
    transition_to_in_progress(client, args["key"], idem_key=idem_key)


def _handle_jira_transition_back_to_todo(args: dict[str, Any], idem_key: str) -> None:
    from backend.agents.jira_dispatch import transition_back_to_todo
    client = args.get("__client") or _make_jira_client(args["agent_class"])
    transition_back_to_todo(client, args["key"], args["reason"], idem_key=idem_key)


def _handle_jira_transition_to_under_review(args: dict[str, Any], idem_key: str) -> None:
    from backend.agents.jira_dispatch import transition_to_under_review_if_needed
    client = args.get("__client") or _make_jira_client(args["agent_class"])
    transition_to_under_review_if_needed(client, args["key"], idem_key=idem_key)


def _handle_jira_add_comment(args: dict[str, Any], idem_key: str) -> None:
    from backend.agents.jira_dispatch import add_comment
    client = args.get("__client") or _make_jira_client(args["agent_class"])
    add_comment(client, args["key"], args["text"], idem_key=idem_key)


def _handle_jira_add_label(args: dict[str, Any], idem_key: str) -> None:
    from backend.agents.jira_dispatch import add_label
    client = args.get("__client") or _make_jira_client(args["agent_class"])
    add_label(client, args["key"], args["label"], idem_key=idem_key)


def _handle_jira_remove_label(args: dict[str, Any], idem_key: str) -> None:
    from backend.agents.jira_dispatch import remove_label
    client = args.get("__client") or _make_jira_client(args["agent_class"])
    remove_label(client, args["key"], args["label"], idem_key=idem_key)


def _handle_jira_clear_assignee(args: dict[str, Any], idem_key: str) -> None:
    from backend.agents.jira_dispatch import clear_assignee
    client = args.get("__client") or _make_jira_client(args["agent_class"])
    clear_assignee(client, args["key"], idem_key=idem_key)


def _handle_gerrit_push(args: dict[str, Any], idem_key: str) -> dict[str, Any]:
    """Push a worktree HEAD to ``refs/for/<target>``.

    Gerrit dedups by Change-Id (commit-msg hook), so re-pushing after a
    crash creates a new patchset on the same change rather than a
    duplicate change — the desired outcome.
    """
    from backend.agents.jira_dispatch import push_to_gerrit_for_review
    result = push_to_gerrit_for_review(
        Path(args["worktree_path"]),
        args["agent_class"],
        target=args.get("target", "develop"),
    )
    if not result.success:
        raise RuntimeError(f"gerrit push failed: {result.detail}")
    return {
        "change_number": result.change_number,
        "change_url": result.change_url,
    }


def register_default_handlers() -> None:
    """Wire the in-tree handlers into the module registry.

    Called from :func:`get_default_queue` so production paths get the
    handlers without an extra import; tests that want a clean registry
    can call :func:`reset_handlers_for_tests` between cases.
    """
    register_handler("jira.transition_to_in_progress", _handle_jira_transition_to_in_progress)
    register_handler("jira.transition_back_to_todo", _handle_jira_transition_back_to_todo)
    register_handler("jira.transition_to_under_review", _handle_jira_transition_to_under_review)
    register_handler("jira.add_comment", _handle_jira_add_comment)
    register_handler("jira.add_label", _handle_jira_add_label)
    register_handler("jira.remove_label", _handle_jira_remove_label)
    register_handler("jira.clear_assignee", _handle_jira_clear_assignee)
    register_handler("gerrit.push", _handle_gerrit_push)


def reset_handlers_for_tests() -> None:
    """Clear the registry. Tests use this to assert no leakage."""
    _HANDLERS.clear()


# ── Producer helpers (callers prefer these over raw enqueue) ─────────


def _new_idem_subkey(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def enqueue_jira_transition(
    queue: OpQueue,
    *,
    agent_class: str,
    key: str,
    target: str,
    reason: str | None = None,
    idempotency_key: str | None = None,
    ordering_key: str | None = None,
) -> int:
    """Convenience: enqueue a JIRA transition op for ticket ``key``.

    ``target`` is one of ``"in_progress"``, ``"back_to_todo"``,
    ``"under_review"``. ``reason`` is required for ``back_to_todo``.

    ``ordering_key`` defaults to the ticket key — that pins the
    "transition → push → comment must run in order" invariant for the
    same ticket.
    """
    if target == "in_progress":
        action = "jira.transition_to_in_progress"
        args: dict[str, Any] = {"agent_class": agent_class, "key": key}
        idem = idempotency_key or _new_idem_subkey(f"transition-{key}-in-progress")
    elif target == "back_to_todo":
        if reason is None:
            raise ValueError("reason= is required when target='back_to_todo'")
        action = "jira.transition_back_to_todo"
        args = {"agent_class": agent_class, "key": key, "reason": reason}
        idem = idempotency_key or _new_idem_subkey(f"transition-{key}-back-to-todo")
    elif target == "under_review":
        action = "jira.transition_to_under_review"
        args = {"agent_class": agent_class, "key": key}
        idem = idempotency_key or _new_idem_subkey(f"transition-{key}-under-review")
    else:
        raise ValueError(f"unknown transition target: {target!r}")
    return queue.enqueue(action, args, idem, ordering_key=ordering_key or key)


def enqueue_jira_comment(
    queue: OpQueue,
    *,
    agent_class: str,
    key: str,
    text: str,
    idempotency_key: str | None = None,
    ordering_key: str | None = None,
) -> int:
    idem = idempotency_key or _new_idem_subkey(f"comment-{key}")
    return queue.enqueue(
        "jira.add_comment",
        {"agent_class": agent_class, "key": key, "text": text},
        idem,
        ordering_key=ordering_key or key,
    )


def enqueue_jira_label(
    queue: OpQueue,
    *,
    agent_class: str,
    key: str,
    label: str,
    add: bool,
    idempotency_key: str | None = None,
    ordering_key: str | None = None,
) -> int:
    action = "jira.add_label" if add else "jira.remove_label"
    verb = "label-add" if add else "label-remove"
    idem = idempotency_key or _new_idem_subkey(f"{verb}-{key}-{label}")
    return queue.enqueue(
        action,
        {"agent_class": agent_class, "key": key, "label": label},
        idem,
        ordering_key=ordering_key or key,
    )


def enqueue_gerrit_push(
    queue: OpQueue,
    *,
    agent_class: str,
    worktree_path: Path | str,
    ticket_key: str,
    target: str = "develop",
    idempotency_key: str | None = None,
) -> int:
    """Enqueue a Gerrit push for ``ticket_key``.

    The push is grouped under the ticket key so it cannot run before a
    pending ``jira.transition_to_in_progress`` on the same ticket.
    """
    idem = idempotency_key or _new_idem_subkey(f"gerrit-push-{ticket_key}")
    return queue.enqueue(
        "gerrit.push",
        {
            "agent_class": agent_class,
            "worktree_path": str(worktree_path),
            "target": target,
        },
        idem,
        ordering_key=ticket_key,
    )


# ── Module-level default queue ────────────────────────────────────────


_default_queue: OpQueue | None = None
_default_lock = threading.Lock()


def get_default_queue() -> OpQueue:
    """Return the process-wide :class:`OpQueue` instance.

    Lazily registers the default handlers on first use so simple
    consumers (the runner) can ``from backend.agents.op_queue import
    get_default_queue, enqueue_jira_transition`` without an extra
    bootstrap call.
    """
    global _default_queue
    with _default_lock:
        if _default_queue is None:
            register_default_handlers()
            _default_queue = OpQueue()
        return _default_queue


def reset_default_queue_for_tests() -> None:
    """Drop the module-level singleton. Tests call this between cases
    so each gets a fresh DB path injected via ``OpQueue(path=tmp)``."""
    global _default_queue
    with _default_lock:
        _default_queue = None


__all__ = [
    "DEFAULT_DB_PATH",
    "MAX_ATTEMPTS",
    "BACKOFF_BASE_SECONDS",
    "STATUS_PENDING",
    "STATUS_IN_PROGRESS",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "Op",
    "OpQueue",
    "Worker",
    "HandlerFn",
    "register_handler",
    "register_default_handlers",
    "reset_handlers_for_tests",
    "get_handler",
    "registered_actions",
    "enqueue_jira_transition",
    "enqueue_jira_comment",
    "enqueue_jira_label",
    "enqueue_gerrit_push",
    "get_default_queue",
    "reset_default_queue_for_tests",
]


# Used by ``Iterable`` re-exports in __all__-style typing.
_ = Iterable
