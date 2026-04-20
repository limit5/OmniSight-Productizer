"""Task #106 regression guard — api_keys.migrate_legacy_bearer under multi-worker.

Surfaced by the SP-4.6 close-out smoke: with ``OMNISIGHT_WORKERS=2``
and ``OMNISIGHT_DECISION_BEARER`` set, two workers each ran
``migrate_legacy_bearer`` on startup and each created a separate
``ak-legacy-<random-uuid-hex>`` row (different ids → no UNIQUE
collision), ending up with two rows for one logical secret.

Fix: key the row id by ``sha256(legacy_secret)[:12]`` so all workers
compute the same id → ``INSERT ... ON CONFLICT (id) DO NOTHING``
collapses concurrent migrations to a single row.

Two tests:

  1. Single-process idempotency — calls ``migrate_legacy_bearer()``
     three times sequentially in the same process. With the old code
     this would produce three rows (UUID freshens per call); with the
     fix, exactly one. Fast.

  2. Multi-worker race — spawns 3 subprocesses (real OS processes,
     not asyncio tasks), each calling ``migrate_legacy_bearer()``
     against the same PG with the same env. The new harness from
     task #82 (backend/tests/multi_worker.py) gives us real worker-
     parallel semantics. Without the determinstic id this would
     produce 3 rows; with the fix, exactly one.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.tests.multi_worker import run_workers


LEGACY_SECRET = "smoke-test-bearer-wJ6zkXkqU1iKq8QA"


async def _worker_migrate_once(pool, worker_id: int, legacy_secret: str):
    """Top-level worker — each subprocess sets the legacy env + calls
    migrate_legacy_bearer once. Must be module-level so
    multiprocessing.spawn can re-import it in each child.
    """
    import os
    os.environ["OMNISIGHT_DECISION_BEARER"] = legacy_secret
    # Point the compat wrapper at the same PG the harness connected to.
    # The harness builds a pool from a DSN but the wrapper reads the
    # env directly, so propagate.
    dsn = pool._connect_args[0] if hasattr(pool, "_connect_args") else None
    if dsn:
        os.environ["OMNISIGHT_DATABASE_URL"] = dsn
    # More robust: walk known asyncpg.Pool attrs for the DSN.
    if not os.environ.get("OMNISIGHT_DATABASE_URL"):
        os.environ["OMNISIGHT_DATABASE_URL"] = os.environ.get(
            "OMNI_TEST_PG_URL", "",
        ).replace("postgresql+psycopg2://", "postgresql://")

    from backend import db, api_keys
    if db._db is None:
        await db.init()
    try:
        result = await api_keys.migrate_legacy_bearer()
    finally:
        await db.close()
    return {
        "did_migrate": result is not None,
        "key_id": result.id if result else None,
    }


@pytest.mark.asyncio
async def test_migrate_legacy_bearer_idempotent_sequential(
    pg_test_pool, pg_test_dsn, monkeypatch,
):
    """Calling migrate_legacy_bearer three times in one process
    produces exactly one row — the deterministic id means the second
    and third calls hit ON CONFLICT DO NOTHING and return None."""
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    monkeypatch.setenv("OMNISIGHT_DECISION_BEARER", LEGACY_SECRET)
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM api_keys WHERE name = 'legacy-bearer'"
        )

    from backend import db, api_keys
    if db._db is not None:
        await db.close()
    await db.init()
    try:
        first = await api_keys.migrate_legacy_bearer()
        second = await api_keys.migrate_legacy_bearer()
        third = await api_keys.migrate_legacy_bearer()
    finally:
        await db.close()

    assert first is not None, "first call must actually migrate"
    assert second is None, "second call must see existing row → return None"
    assert third is None, "third call must see existing row → return None"

    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM api_keys WHERE name = 'legacy-bearer'"
        )
    assert len(rows) == 1, (
        f"expected exactly one legacy-bearer row after 3 migrate calls, "
        f"got {len(rows)}: {[dict(r) for r in rows]}"
    )
    # Cleanup — this test writes a real row (no tx rollback since
    # migrate commits).
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM api_keys WHERE name = 'legacy-bearer'"
        )


def test_migrate_legacy_bearer_multi_worker_single_winner(
    pg_test_dsn,
):
    """**The real regression guard** — reproduces the smoke scenario.

    3 OS-process workers, all call migrate_legacy_bearer() with the
    same legacy secret. Without the deterministic id fix, each
    generated its own uuid → 3 rows. With the fix, same id → 1 row,
    exactly one worker reports ``did_migrate=True`` (the rest see
    the conflict and return None).
    """
    import os
    import asyncio

    # Seed env so the children inherit it (they also set it themselves
    # at worker entry, but setting here makes the pipeline explicit).
    os.environ["OMNISIGHT_DECISION_BEARER"] = LEGACY_SECRET

    # Clean slate.
    async def _reset():
        import asyncpg
        c = await asyncpg.connect(pg_test_dsn)
        await c.execute("DELETE FROM api_keys WHERE name = 'legacy-bearer'")
        await c.close()

    asyncio.run(_reset())

    try:
        results = run_workers(
            "backend.tests.test_api_keys_legacy_migration",
            "_worker_migrate_once",
            n=3,
            dsn=pg_test_dsn,
            args=(LEGACY_SECRET,),
            timeout_s=60.0,
        )
        # Exactly one worker landed the row.
        migrators = [r for r in results if r["did_migrate"]]
        assert len(migrators) == 1, (
            f"expected exactly one worker to migrate; got {len(migrators)}: "
            f"{results}. Without the deterministic id fix, all 3 workers "
            f"would have created their own row with different uuids."
        )
        # All non-migrators return None (no row object) — but the key_id
        # of the winning migrator is the deterministic sha-prefix.
        winner_id = migrators[0]["key_id"]
        assert winner_id.startswith("ak-legacy-"), winner_id

        # And the DB confirms: exactly one row.
        async def _count():
            import asyncpg
            c = await asyncpg.connect(pg_test_dsn)
            n = await c.fetchval(
                "SELECT COUNT(*) FROM api_keys WHERE name = 'legacy-bearer'"
            )
            await c.close()
            return n

        count = asyncio.run(_count())
        assert count == 1, (
            f"multi-worker migration must collapse to 1 row, got {count}"
        )
    finally:
        asyncio.run(_reset())
