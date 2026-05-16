"""OP-909 runner telemetry recorder.

Records one row per runner CLI invocation into the ``runner_metrics``
Postgres table (created by alembic migration ``0231_runner_metrics``).
A pickup writes the start row and returns its id; the matching completion
update stamps the outcome and elapsed seconds on that same row.

Telemetry is deliberately fail-open: runner pickup/completion must keep
moving if Postgres is unavailable or the schema has not been deployed yet.
Every database call is wrapped in a broad ``except`` that logs
``MetricsInsertFailed`` and returns a sentinel so the caller never raises.
Losing a single telemetry row is acceptable; wedging a ticket pickup is not.

The module exposes both async coroutines (``record_pickup`` /
``record_completion``) for use from async runner code and thin
``*_sync`` wrappers (``record_pickup_sync`` / ``record_completion_sync``)
that drive a fresh ``asyncio.run`` loop — the latter are what the
synchronous ``auto-runner-jira.py`` entrypoint actually calls.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from backend.db_url import parse as parse_db_url

LOG = logging.getLogger(__name__)

ConnFactory = Callable[[], Any]


class MetricsInsertFailed(RuntimeError):
    """A ``runner_metrics`` write failed; callers must fail open.

    Raised only inside the private connection helpers when a precondition
    (missing DSN, non-Postgres URL) makes the write impossible. The public
    ``record_*`` functions catch this alongside any other ``Exception``,
    log it, and return ``None`` so pickup/completion is never blocked by
    telemetry.
    """


@dataclass(frozen=True)
class RunnerMetricStart:
    """Immutable payload describing a single runner pickup event.

    Built by the runner at pickup time and consumed by :func:`record_pickup`.
    Every field maps 1:1 to a column on the ``runner_metrics`` start-event row.

    Attributes:
        agent_class: Logical agent family executing the ticket
            (e.g. ``"claude-runner"``). Used for per-agent drift reporting.
        instance_id: Per-process runner identifier. Lets multi-worker
            deployments distinguish concurrent pickups of distinct tickets.
        ticket_key: JIRA issue key (e.g. ``"OP-1204"``).
        ticket_type: Ticket classifier — typically the JIRA component or
            ``"unknown"`` when the runner cannot resolve one.
        tier: Tier label (``"S"`` / ``"M"`` / ``"L"`` …) controlling
            scheduling weight; falls back to ``"M"`` upstream.
        area: Scope area label (``"backend"``, ``"docs"`` …) or
            ``"<none>"`` when no area was tagged.
        lessons_used_count: Number of lessons-learned entries injected into
            the agent prompt. Defaults to 0.
        mcp_calls_count: Number of MCP tool calls the agent issued.
            Defaults to 0; the runner may overwrite via a follow-up update.
        claude_model_used: Model identifier read from the ``ANTHROPIC_MODEL``
            / ``CLAUDE_MODEL`` env at pickup, or ``None`` if neither is set.
    """

    agent_class: str
    instance_id: str
    ticket_key: str
    ticket_type: str
    tier: str
    area: str
    lessons_used_count: int = 0
    mcp_calls_count: int = 0
    claude_model_used: str | None = None


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC ``datetime``.

    Centralised so tests can monkeypatch a single symbol and so every
    timestamp the recorder writes is unambiguously UTC (the
    ``runner_metrics`` columns are ``timestamptz``).
    """
    return datetime.now(timezone.utc)


def outcome_from_return_code(rc: int) -> str:
    """Map a CLI exit code to a ``runner_metrics.outcome`` label.

    Mapping:
        * ``0``   → ``"success"`` — the agent CLI exited cleanly.
        * ``99``  → ``"skipped"`` — runner convention for "agent abstained
          / nothing to do"; not a failure.
        * ``124`` → ``"timeout"`` — the coreutils ``timeout(1)`` exit code
          surfaced when the wrapping watchdog kills a runaway CLI.
        * anything else → ``"failure"``.

    The set of labels is fixed by the ``runner_metrics.outcome`` check
    constraint in migration ``0231_runner_metrics``; add new codes here
    only alongside a migration that widens that constraint.
    """
    if rc == 0:
        return "success"
    if rc == 99:
        return "skipped"
    if rc == 124:
        return "timeout"
    return "failure"


def _env_dsn() -> str | None:
    """Return the Postgres DSN from env, preferring the OmniSight override.

    ``OMNISIGHT_DATABASE_URL`` wins over the generic ``DATABASE_URL`` so a
    deployment can point telemetry at a dedicated database without
    rerouting unrelated services. Returns ``None`` if neither is set —
    callers treat that as "telemetry disabled".
    """
    return os.environ.get("OMNISIGHT_DATABASE_URL") or os.environ.get("DATABASE_URL")


@asynccontextmanager
async def _connect_from_env() -> AsyncIterator[Any]:
    """Yield an ``asyncpg`` connection built from the env DSN.

    Raises :class:`MetricsInsertFailed` if no DSN is configured or the
    configured DSN is not a Postgres URL (``runner_metrics`` is
    Postgres-only because it relies on ``RETURNING`` and ``timestamptz``).
    The connection is closed unconditionally on exit. ``asyncpg`` is
    imported lazily so the runner does not require the dependency in
    deployments that do not record telemetry.
    """
    dsn = _env_dsn()
    if not dsn:
        raise MetricsInsertFailed("OMNISIGHT_DATABASE_URL or DATABASE_URL is not set")
    parsed = parse_db_url(dsn)
    if not parsed.is_postgres:
        raise MetricsInsertFailed("runner_metrics requires a Postgres database URL")
    import asyncpg  # type: ignore[import-not-found]

    conn = await asyncpg.connect(**parsed.asyncpg_connect_kwargs())
    try:
        yield conn
    finally:
        await conn.close()


@asynccontextmanager
async def _acquire(factory: ConnFactory | None) -> AsyncIterator[Any]:
    """Yield a connection from ``factory`` if given, else from env.

    The factory seam exists primarily for tests, which inject a
    ``FakeConn`` async context manager so the SQL can be inspected
    without standing up a Postgres instance. In production both
    ``record_pickup`` and ``record_completion`` pass ``factory=None``
    and the env-driven path is used.
    """
    if factory is None:
        async with _connect_from_env() as conn:
            yield conn
        return
    cm = factory()
    async with cm as conn:
        yield conn


async def record_pickup(
    metric: RunnerMetricStart,
    *,
    conn_factory: ConnFactory | None = None,
    now: datetime | None = None,
) -> int | None:
    """Insert the start-event row for a runner pickup and return its id.

    Writes a fresh ``runner_metrics`` row with both ``ts`` and
    ``started_at`` set to ``now`` (defaulting to :func:`utcnow`) so the
    row is immediately discoverable by drift reports even before its
    matching :func:`record_completion` update lands.

    Args:
        metric: Pickup payload — see :class:`RunnerMetricStart`.
        conn_factory: Optional async context manager factory yielding a
            connection. ``None`` (the production default) routes through
            the env DSN; tests inject a fake.
        now: Override timestamp; defaults to :func:`utcnow`. Pass the
            same value you store as ``started_at`` for
            :func:`record_completion` so elapsed math stays exact.

    Returns:
        The new row id on success, or ``None`` on any failure (missing
        DSN, connection error, schema not deployed, etc.). The typed
        exception is logged at WARNING with prefix ``MetricsInsertFailed``
        so operators can grep it; the caller is expected to treat
        ``None`` as "telemetry skipped" and continue.
    """
    stamp = now or utcnow()
    try:
        async with _acquire(conn_factory) as conn:
            row_id = await conn.fetchval(
                """
                INSERT INTO runner_metrics (
                    agent_class, instance_id, ticket_key, ticket_type, tier, area,
                    lessons_used_count, mcp_calls_count, claude_model_used,
                    ts, started_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $10)
                RETURNING id
                """,
                metric.agent_class,
                metric.instance_id,
                metric.ticket_key,
                metric.ticket_type,
                metric.tier,
                metric.area,
                metric.lessons_used_count,
                metric.mcp_calls_count,
                metric.claude_model_used,
                stamp,
            )
            return int(row_id) if row_id is not None else None
    except Exception as exc:  # noqa: BLE001 - telemetry is fire-and-forget
        LOG.warning("MetricsInsertFailed pickup ticket=%s err=%s", metric.ticket_key, exc)
        return None


async def record_completion(
    *,
    metric_id: int | None,
    ticket_key: str,
    agent_class: str,
    instance_id: str,
    outcome: str,
    started_at: datetime,
    conn_factory: ConnFactory | None = None,
    now: datetime | None = None,
) -> None:
    """Stamp outcome and elapsed seconds onto a previously inserted row.

    Updates the row identified by ``metric_id``, additionally matching
    on ``ticket_key``, ``agent_class``, and ``instance_id`` as a defensive
    cross-check so a stale id from a different runner cannot accidentally
    overwrite an unrelated row. ``ts`` and ``completed_at`` are both set
    to ``now`` (defaulting to :func:`utcnow`).

    Args:
        metric_id: Row id returned by :func:`record_pickup`. ``None``
            (the pickup-failed sentinel) makes this call a no-op.
        ticket_key: Same key passed at pickup; part of the WHERE clause.
        agent_class: Same agent class passed at pickup.
        instance_id: Same instance id passed at pickup.
        outcome: One of the labels produced by
            :func:`outcome_from_return_code`.
        started_at: Timestamp captured at pickup. ``time_to_complete_seconds``
            is computed as ``max(0, now - started_at)`` to defend against
            clock skew.
        conn_factory: See :func:`record_pickup`.
        now: Override timestamp; defaults to :func:`utcnow`.

    Returns:
        ``None``. Failures are logged at WARNING with prefix
        ``MetricsInsertFailed`` and otherwise swallowed — losing a
        completion-update telemetry row must never block ticket
        progression.
    """
    if metric_id is None:
        return
    completed_at = now or utcnow()
    elapsed = max(0.0, (completed_at - started_at).total_seconds())
    try:
        async with _acquire(conn_factory) as conn:
            await conn.execute(
                """
                UPDATE runner_metrics
                SET outcome = $1,
                    time_to_complete_seconds = $2,
                    completed_at = $3,
                    ts = $3
                WHERE id = $4
                  AND ticket_key = $5
                  AND agent_class = $6
                  AND instance_id = $7
                """,
                outcome,
                elapsed,
                completed_at,
                metric_id,
                ticket_key,
                agent_class,
                instance_id,
            )
    except Exception as exc:  # noqa: BLE001 - telemetry is fire-and-forget
        LOG.warning("MetricsInsertFailed completion ticket=%s err=%s", ticket_key, exc)


def record_pickup_sync(metric: RunnerMetricStart) -> tuple[int | None, datetime]:
    """Synchronous wrapper around :func:`record_pickup` for blocking callers.

    Captures ``started`` once and threads it into both the async call (as
    ``now``) and the return tuple, so the caller can later pass the same
    value as ``started_at`` to :func:`record_completion_sync` and get an
    accurate elapsed-time delta with no clock drift between the two calls.

    Drives a fresh ``asyncio.run`` loop, so it MUST NOT be invoked from
    inside an already-running event loop — that is why ``auto-runner-jira.py``
    (synchronous) uses this variant while async callers stick to the
    coroutine form.

    Returns:
        ``(metric_id, started_at)`` where ``metric_id`` is ``None`` on
        any telemetry failure (see :func:`record_pickup`).
    """
    started = utcnow()
    return asyncio.run(record_pickup(metric, now=started)), started


def record_completion_sync(
    *,
    metric_id: int | None,
    ticket_key: str,
    agent_class: str,
    instance_id: str,
    outcome: str,
    started_at: datetime,
) -> None:
    """Synchronous wrapper around :func:`record_completion`.

    Pairs with :func:`record_pickup_sync`: pass the ``metric_id`` and
    ``started_at`` returned from that call. Same event-loop caveat
    applies — do not invoke from inside a running event loop.

    All keyword arguments forward unchanged to :func:`record_completion`;
    see that function for failure semantics.
    """
    asyncio.run(
        record_completion(
            metric_id=metric_id,
            ticket_key=ticket_key,
            agent_class=agent_class,
            instance_id=instance_id,
            outcome=outcome,
            started_at=started_at,
        )
    )
