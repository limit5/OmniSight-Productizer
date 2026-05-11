"""OP-909 runner telemetry recorder.

Telemetry is deliberately fail-open: runner pickup/completion must keep
moving if Postgres is unavailable or the schema has not been deployed yet.
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
    """A runner_metrics write failed; callers must fail open."""


@dataclass(frozen=True)
class RunnerMetricStart:
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
    return datetime.now(timezone.utc)


def outcome_from_return_code(rc: int) -> str:
    if rc == 0:
        return "success"
    if rc == 99:
        return "skipped"
    if rc == 124:
        return "timeout"
    return "failure"


def _env_dsn() -> str | None:
    return os.environ.get("OMNISIGHT_DATABASE_URL") or os.environ.get("DATABASE_URL")


@asynccontextmanager
async def _connect_from_env() -> AsyncIterator[Any]:
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
    """Insert the start_event row and return its id.

    Returns ``None`` on any failure. The typed exception is logged so
    operators can grep ``MetricsInsertFailed`` without blocking pickup.
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
    """Update the start row with outcome + elapsed seconds.

    If the pickup insert failed, this is a no-op. That matches the
    recovery contract: losing one telemetry row is acceptable.
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
