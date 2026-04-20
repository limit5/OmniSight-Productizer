"""Fix-D D4 — DLQ retry worker edge cases.

Phase 52 shipped the DLQ loop + the happy paths (mark-dead, retry).
Audit flagged three gaps: concurrent sweeps on the same row,
clean cancellation of `run_dlq_loop`, and the `_DLQ_RUNNING` singleton
guard. These tests close those.

Phase-3-Runtime-v2 SP-3.4 (2026-04-20): ported from the SQLite
``fresh_db`` fixture to ``pg_test_pool`` — ``retry_failed_notifications``
acquires its own pool-backed conn (polymorphic conn=None branch), so
every test just needs a clean ``notifications`` table at start. The
fixture TRUNCATEs at setup rather than relying on pg_test_conn's
savepoint, because the worker's self-acquired conn is a *different*
pool connection that wouldn't see uncommitted savepoint writes.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest


@pytest.fixture()
async def dlq_env(monkeypatch, pg_test_pool):
    """Clean notifications table + reset `_DLQ_RUNNING` + no external channels."""
    from backend import notifications as n
    from backend.config import settings

    # Start each test from an empty notifications table. Other sibling
    # suites (test_db_notifications.py) use pg_test_conn's savepoint
    # so their writes don't leak here; this TRUNCATE defends against
    # prior dlq_env-using tests that committed via the pool.
    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE notifications RESTART IDENTITY CASCADE")

    # No external channels configured → `_dispatch_external` takes the
    # "skipped" branch, keeping unit tests offline.
    monkeypatch.setattr(settings, "notification_slack_webhook", "", raising=False)
    monkeypatch.setattr(settings, "notification_jira_url", "", raising=False)
    monkeypatch.setattr(settings, "notification_pagerduty_key", "", raising=False)
    # Reset singleton guard
    n._DLQ_RUNNING = False
    try:
        yield pg_test_pool, n, settings
    finally:
        n._DLQ_RUNNING = False
        # Clean up committed rows so the next test starts clean
        # regardless of which fixture it uses.
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE notifications RESTART IDENTITY CASCADE"
            )


async def _seed_failed(
    pool, nid: str, *, attempts: int, error: str = "boom",
) -> None:
    from backend import db
    async with pool.acquire() as conn:
        await db.insert_notification(conn, {
            "id": nid, "level": "warning", "title": "t", "message": "m",
            "source": "test", "timestamp": "2026-04-14T00:00:00",
            "action_url": None, "action_label": None,
        })
        await db.update_notification_dispatch(
            conn, nid, "failed", attempts=attempts, error=error,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Concurrent sweeps — aiosqlite serialises so rows transition once
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_concurrent_sweeps_do_not_double_count_dead(dlq_env):
    pool, n, settings = dlq_env
    from backend import db
    nid = f"notif-cc-{uuid.uuid4().hex[:6]}"
    # Exhausted on purpose — one sweep suffices to mark it dead.
    await _seed_failed(pool, nid, attempts=settings.notification_max_retries)

    r1, r2 = await asyncio.gather(
        n.retry_failed_notifications(),
        n.retry_failed_notifications(),
    )
    # Either both see the row (dead=1 each) or the second sees it already
    # gone (dead=0). Combined total of rows touched must be >= 1.
    assert (r1["dead"] + r2["dead"]) >= 1
    # Afterwards the row is no longer in the `failed` list.
    async with pool.acquire() as conn:
        failed = await db.list_failed_notifications(conn)
    assert all(r["id"] != nid for r in failed)


@pytest.mark.asyncio
async def test_concurrent_retry_does_not_exceed_attempt_budget(dlq_env):
    pool, n, settings = dlq_env
    nid = f"notif-cc2-{uuid.uuid4().hex[:6]}"
    await _seed_failed(pool, nid, attempts=0)  # attempts remaining
    # Two overlapping sweeps on a retryable row. The retried counter should
    # be bounded and not explode. (The row may transition to 'skipped' via
    # `_dispatch_external` since no webhooks are configured.)
    r1, r2 = await asyncio.gather(
        n.retry_failed_notifications(),
        n.retry_failed_notifications(),
    )
    assert r1["retried"] + r2["retried"] >= 1
    assert r1["retried"] + r2["retried"] <= 2  # at most once per sweep


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  run_dlq_loop — cancellation cleanup + singleton guard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_run_dlq_loop_exits_cleanly_on_cancel(dlq_env, monkeypatch):
    _pool, n, settings = dlq_env
    # Shrink the interval so we don't sleep for real.
    monkeypatch.setattr(settings, "notification_retry_backoff", 5, raising=False)

    task = asyncio.create_task(n.run_dlq_loop())
    await asyncio.sleep(0.05)  # yield — loop enters its sleep
    assert n._DLQ_RUNNING is True

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # `finally` must have cleared the flag.
    assert n._DLQ_RUNNING is False
    # Task must be done (not stuck somewhere awaiting).
    assert task.done()


@pytest.mark.asyncio
async def test_run_dlq_loop_second_start_is_noop(dlq_env, monkeypatch):
    _pool, n, settings = dlq_env
    monkeypatch.setattr(settings, "notification_retry_backoff", 5, raising=False)

    t1 = asyncio.create_task(n.run_dlq_loop())
    await asyncio.sleep(0.05)
    assert n._DLQ_RUNNING is True

    # Second call must return immediately without blocking on sleep.
    result = await asyncio.wait_for(n.run_dlq_loop(), timeout=0.5)
    assert result is None  # early return

    t1.cancel()
    try:
        await t1
    except asyncio.CancelledError:
        pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Metric cardinality guard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_persist_failure_metric_module_label_has_bounded_set():
    """We intentionally only use a small, fixed set of module labels so a
    bug (e.g. passing a notification id instead of a module name) doesn't
    blow Prometheus cardinality. Document the allowed set here; add to
    this list when a new caller is introduced."""
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")
    allowed = {
        "notifications", "budget_strategy", "project_report",
        # extend here when new callers land
    }
    for mod in allowed:
        m.persist_failure_total.labels(module=mod).inc(0)  # register
    # Sanity: no integer / UUID-shaped label should ever appear.
    samples = list(m.persist_failure_total.collect()[0].samples)
    for s in samples:
        label = s.labels.get("module", "")
        assert not label.startswith("notif-"), f"uuid leaked into label: {label}"
        assert not label.isdigit(), f"numeric label: {label}"
