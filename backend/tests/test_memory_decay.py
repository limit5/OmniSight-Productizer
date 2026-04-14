"""Phase 63-E — episodic memory quality decay."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time

import pytest

from backend import memory_decay as md


@pytest.fixture()
async def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.db")
        monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", path)
        from backend import config as cfg
        cfg.settings.database_path = path
        from backend import db
        db._DB_PATH = db._resolve_db_path()
        await db.init()
        try:
            yield db
        finally:
            await db.close()


async def _seed(db, mid: str, *, quality: float = 1.0, last_used: str | None = None):
    await db.insert_episodic_memory({
        "id": mid,
        "error_signature": f"sig-{mid}",
        "solution": "s",
        "quality_score": quality,
    })
    if last_used is not None:
        await db._conn().execute(
            "UPDATE episodic_memory SET last_used_at = ? WHERE id = ?",
            (last_used, mid),
        )
        await db._conn().commit()


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
    async with fresh_db._conn().execute(
        "SELECT last_used_at FROM episodic_memory WHERE id = ?", ("m1",),
    ) as cur:
        row = await cur.fetchone()
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

    async with fresh_db._conn().execute(
        "SELECT id, decayed_score FROM episodic_memory ORDER BY id",
    ) as cur:
        rows = {r["id"]: r["decayed_score"] for r in await cur.fetchall()}
    assert rows["fresh"] == pytest.approx(1.0)
    assert rows["stale"] == pytest.approx(0.5)
    assert rows["null1"] == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_decay_factor_clamped(fresh_db):
    await _seed(fresh_db, "m1", quality=1.0)
    # factor > 1 → clamped to 1 (no inflation)
    await md.decay_unused(ttl_s=1.0, factor=5.0, now=time.time())
    async with fresh_db._conn().execute(
        "SELECT decayed_score FROM episodic_memory WHERE id = ?", ("m1",),
    ) as cur:
        row = await cur.fetchone()
    assert row["decayed_score"] == pytest.approx(1.0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  restore
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_restore_copies_quality_back(fresh_db):
    await _seed(fresh_db, "m1", quality=0.9)
    await md.decay_unused(ttl_s=1.0, factor=0.1, now=time.time())
    got = await md.restore("m1")
    assert got == pytest.approx(0.9)
    async with fresh_db._conn().execute(
        "SELECT decayed_score, last_used_at FROM episodic_memory WHERE id = ?",
        ("m1",),
    ) as cur:
        row = await cur.fetchone()
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
