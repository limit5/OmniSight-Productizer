"""Phase 63-E — episodic memory quality decay.

Step A.2 (2026-04-21): memory_decay is now pool-native. Fixture
migrated from SQLite tempfile + ``db.init()`` to pg_test_pool +
TRUNCATE. ``_seed`` helper rewrites the raw ``last_used_at`` UPDATE
via pool. Skip marker removed.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend import memory_decay as md


@pytest.fixture()
async def fresh_db(pg_test_pool, pg_test_dsn, monkeypatch):
    # Point compat path at PG too — some helpers (db.insert_episodic_
    # memory, used by _seed) still route through the compat wrapper
    # when called without an explicit conn; OMNISIGHT_DATABASE_URL
    # makes ``db.init()`` open a PgCompatConnection against the same
    # PG pool uses.
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE episodic_memory RESTART IDENTITY CASCADE"
        )
    from backend import db
    if db._db is not None:
        await db.close()
    await db.init()
    try:
        yield db
    finally:
        await db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE episodic_memory RESTART IDENTITY CASCADE"
            )


async def _seed(db, mid: str, *, quality: float = 1.0, last_used: str | None = None):
    # db.insert_episodic_memory is pool-native (takes a conn). Use
    # the pool directly here too so the seed + update share the
    # same code path as production memory_decay.touch does.
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        await db.insert_episodic_memory(conn, {
            "id": mid,
            "error_signature": f"sig-{mid}",
            "solution": "s",
            "quality_score": quality,
        })
        if last_used is not None:
            await conn.execute(
                "UPDATE episodic_memory SET last_used_at = $1 "
                "WHERE id = $2",
                last_used, mid,
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  is_enabled
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.parametrize("level,expected", [
    (None, False), ("", False), ("off", False),
    ("l1", False), ("l4", False),
    ("l3", True), ("L3", True), ("l1+l3", True),
    ("all", True),
])
def test_is_enabled(monkeypatch, level, expected):
    if level is None:
        monkeypatch.delenv("OMNISIGHT_SELF_IMPROVE_LEVEL", raising=False)
    else:
        monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", level)
    assert md.is_enabled() is expected


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  touch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_touch_updates_last_used_at(fresh_db):
    await _seed(fresh_db, "m1")
    ok = await md.touch("m1")
    assert ok
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_used_at FROM episodic_memory WHERE id = $1",
            "m1",
        )
    assert row["last_used_at"] is not None


@pytest.mark.asyncio
async def test_touch_missing_returns_false(fresh_db):
    assert await md.touch("ghost") is False
    assert await md.touch("") is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  decay_unused
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_decay_skips_recent_and_decays_stale(fresh_db):
    now = time.time()
    # fresh — touched 1h ago
    await _seed(fresh_db, "fresh", quality=1.0,
                last_used=md._ts_iso(now - 3600))
    # stale — touched 200 days ago
    await _seed(fresh_db, "stale", quality=1.0,
                last_used=md._ts_iso(now - 200 * 86400))
    # never-touched (NULL)
    await _seed(fresh_db, "null1", quality=0.8)

    res = await md.decay_unused(ttl_s=90 * 86400, factor=0.5, now=now)
    assert res.scanned == 3
    assert res.decayed == 2
    assert res.skipped_recent == 1

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, decayed_score FROM episodic_memory ORDER BY id"
        )
    by_id = {r["id"]: r["decayed_score"] for r in rows}
    assert by_id["fresh"] == pytest.approx(1.0)
    assert by_id["stale"] == pytest.approx(0.5)
    assert by_id["null1"] == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_decay_factor_clamped(fresh_db):
    await _seed(fresh_db, "m1", quality=1.0)
    # factor > 1 → clamped to 1 (no inflation)
    await md.decay_unused(ttl_s=1.0, factor=5.0, now=time.time())
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        score = await conn.fetchval(
            "SELECT decayed_score FROM episodic_memory WHERE id = $1",
            "m1",
        )
    assert score == pytest.approx(1.0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  restore
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_restore_copies_quality_back(fresh_db):
    await _seed(fresh_db, "m1", quality=0.9)
    await md.decay_unused(ttl_s=1.0, factor=0.1, now=time.time())
    got = await md.restore("m1")
    assert got == pytest.approx(0.9)
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT decayed_score, last_used_at FROM episodic_memory "
            "WHERE id = $1",
            "m1",
        )
    assert row["decayed_score"] == pytest.approx(0.9)
    assert row["last_used_at"] is not None


@pytest.mark.asyncio
async def test_restore_missing_returns_none(fresh_db):
    assert await md.restore("ghost") is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Loop singleton
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_loop_singleton_then_cancel_clears_flag(fresh_db, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", "l3")
    md._LOOP_RUNNING = False

    task = asyncio.create_task(md.run_decay_loop(interval_s=0.05))
    await asyncio.sleep(0.01)
    assert md._LOOP_RUNNING is True
    result = await asyncio.wait_for(md.run_decay_loop(interval_s=10), timeout=0.5)
    assert result is None

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert md._LOOP_RUNNING is False
