"""Skeleton tests for the multi-worker harness (task #82).

Two purposes:

  1. **Harness smoke** — prove the subprocess plumbing actually runs:
     spawn 3 workers, each returns its worker_id, assert we get back
     {0, 1, 2}. Catches "your fixture silently runs zero workers" bugs
     before any real regression test relies on it.
  2. **Real multi-worker regression guard for SP-4.4** — the
     ``_record_login_failure`` atomic-increment guarantee is supposed
     to hold across true OS-process workers (uvicorn --workers N),
     not just asyncio.gather. We reproduce that here.

Worker functions MUST be top-level so ``multiprocessing.spawn`` can
re-import them inside each child. See module docstring of
``multi_worker.py`` for the contract.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.tests.multi_worker import run_workers


# ── Top-level worker functions (picklable / re-importable) ───────


async def _worker_echo_id(pool, worker_id: int):
    """Smoke worker — just returns its id via the pool, proving the
    subprocess is live and has a working PG connection."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT $1::int AS wid", worker_id)
    return {"wid": row["wid"]}


async def _worker_wrong_password_burst(pool, worker_id: int, email: str, attempts: int):
    """Each worker pounds ``authenticate_password`` with wrong creds.

    Runs inside a fresh Python process — module-globals are re-
    initialised, so this is the closest we get to ``uvicorn --workers N``
    behaviour without a full compose stack.
    """
    # Re-import backend.auth inside the worker; it will bring up its own
    # db_pool (per-process) but we pass in an explicit pool reference
    # too for tests that want to bypass the module's lazy init path.
    from backend import auth
    from backend import db_pool as _db_pool
    # Ensure the module-global pool in this worker points at the same DSN.
    # init_pool is idempotent-ish; if already init, skip.
    if _db_pool._pool is None:
        # Reuse the per-worker pool the harness already created.
        _db_pool._pool = pool

    results = []
    for _ in range(attempts):
        u = await auth.authenticate_password(email, "wrong")
        results.append(u is None)
    return {"attempts": attempts, "all_rejected": all(results)}


# ── Tests ─────────────────────────────────────────────────────────


def test_multi_worker_smoke(pg_test_dsn):
    """3 workers, each does a trivial SELECT, we collect their ids.

    Failure modes this catches: spawn misconfigured, PYTHONPATH not
    propagating to children, asyncpg not available in the child env,
    the subprocess returning non-JSON.
    """
    results = run_workers(
        "backend.tests.test_multi_worker_skeleton",
        "_worker_echo_id",
        n=3,
        dsn=pg_test_dsn,
    )
    # Each worker returns {"wid": <worker_id>}.
    wids = sorted(r["wid"] for r in results)
    assert wids == [0, 1, 2], (
        f"expected workers 0/1/2 to each execute once, got {wids}. "
        f"If the list is short, some worker is silently failing."
    )


@pytest.mark.asyncio
async def test_multi_worker_failed_login_counter_atomic(pg_test_pool, pg_test_dsn):
    """**The real reason this harness exists.**

    SP-4.4's atomic ``failed_login_count = failed_login_count + 1
    RETURNING`` was proven race-free under asyncio.gather in
    backend/tests/test_auth.py. That test runs a single event loop
    with cooperative interleaving. Prod runs multiple OS processes
    (uvicorn --workers N). OS processes have preemptive scheduling
    and no shared Python state — the atomicity must hold at the PG
    row-lock level, not just asyncio-level.

    Fire 4 workers × 5 wrong-password attempts each. If the atomic
    increment is truly correct, counter = 20. If there's any
    process-level race the DB layer doesn't catch, counter < 20.
    """
    # Seed the victim user in the outer test process.
    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")
    from backend import auth
    u = await auth.create_user(
        "mw@test.com", "MW", role="viewer", password="correct-password",
    )

    # Stay strictly below LOCKOUT_THRESHOLD (10) so we test the
    # atomic-increment primitive itself and NOT the lockout short-
    # circuit (which deliberately skips the increment branch once
    # locked_until is set). 3 workers × 2 attempts = 6 increments
    # expected — all land on the increment path.
    N_WORKERS = 3
    ATTEMPTS_PER_WORKER = 2
    results = await asyncio.to_thread(
        run_workers,
        "backend.tests.test_multi_worker_skeleton",
        "_worker_wrong_password_burst",
        N_WORKERS,
        dsn=pg_test_dsn,
        args=("mw@test.com", ATTEMPTS_PER_WORKER),
        timeout_s=60.0,
    )
    # Every attempt across every worker must have been rejected.
    for r in results:
        assert r["all_rejected"], f"worker returned acceptance: {r}"

    expected = N_WORKERS * ATTEMPTS_PER_WORKER
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT failed_login_count FROM users WHERE id = $1", u.id,
        )
    assert row["failed_login_count"] == expected, (
        f"multi-worker atomic-increment regression: expected "
        f"{expected} recorded failures ({N_WORKERS} workers × "
        f"{ATTEMPTS_PER_WORKER} attempts), got "
        f"{row['failed_login_count']}. Lost updates suggest the PG "
        f"row lock is insufficient under true OS-process concurrency "
        f"— SP-4.4's atomic UPDATE RETURNING did NOT hold at this "
        f"scale."
    )
