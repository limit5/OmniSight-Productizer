"""AUDIT-26b / OP-981 — ``backend.audit.log()`` lazy-inits the asyncpg pool
when invoked from a standalone-script context (D5 ``auto_promote_main`` and
future cron entry points run outside the FastAPI lifespan, so
``db_pool.init_pool`` was never called).

Spec source: OP-925 R3 incident comment 2026-05-12 18:21 —
``release_audit`` stopped gaining rows because the audit sink failed with
``RuntimeError: db_pool.get_pool called before init_pool``.

These tests mock the pool primitives (``db_pool.init_pool`` /
``audit._log_impl``) so they need no live PostgreSQL — they exercise the
lazy-init *control flow* in ``audit.py``, not the chain-write SQL (that is
covered by ``test_audit.py`` against ``pg_test_pool``).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from backend import audit
from backend import db_pool


# ─── Minimal fake asyncpg pool ────────────────────────────────────────
#
# ``audit.log`` (conn=None path) does:
#     async with pool.acquire() as conn:
#         async with conn.transaction():
#             return await _log_impl(conn, ...)
# We mock ``_log_impl`` itself, so the fakes below only need to satisfy
# the two ``async with`` shapes around it.


class _FakeTxn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def transaction(self):
        return _FakeTxn()


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self) -> None:
        self.acquired = 0

    def acquire(self) -> _FakeAcquireCM:
        self.acquired += 1
        return _FakeAcquireCM(_FakeConn())


@pytest.fixture(autouse=True)
def _isolate_pool_global(monkeypatch):
    """Every test in this module starts from a pristine "pool not
    initialised" state and never mutates the real process-global pool
    permanently (monkeypatch restores it)."""
    monkeypatch.setattr(db_pool, "_pool", None, raising=False)
    # Don't let an env DSN leaked from CI config influence the no-DSN test;
    # individual tests opt back in via monkeypatch.setenv.
    monkeypatch.delenv("OMNISIGHT_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    yield


async def test_audit_log_lazy_inits_pool_when_used_in_script_context(monkeypatch):
    """No pre-init'd pool + a PG DSN in the env ⇒ ``log()`` calls
    ``db_pool.init_pool(dsn)`` itself and writes through the result."""
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", "postgresql://u:pw@db.local:5432/omni")

    fake_pool = _FakePool()

    async def _fake_init(dsn, **kwargs):
        assert dsn == "postgresql://u:pw@db.local:5432/omni"
        db_pool._pool = fake_pool
        return fake_pool

    init_mock = AsyncMock(side_effect=_fake_init)
    monkeypatch.setattr(db_pool, "init_pool", init_mock)
    log_impl_mock = AsyncMock(return_value=4242)
    monkeypatch.setattr(audit, "_log_impl", log_impl_mock)

    rid = await audit.log("milestone_force_promoted", "release_branch", "main",
                          after={"outcome": "force_promoted"}, actor="auto_promote_main")

    assert rid == 4242
    init_mock.assert_awaited_once()
    assert fake_pool.acquired == 1
    log_impl_mock.assert_awaited_once()


async def test_audit_log_falls_back_to_stderr_when_no_dsn(monkeypatch, capsys, caplog):
    """No pre-init'd pool and no env DSN ⇒ ``log()`` must NOT raise; it
    emits a visible stderr breadcrumb + a warning and returns None."""
    # _isolate_pool_global already cleared both DSN env vars.
    # init_pool must never be reached on this path.
    monkeypatch.setattr(db_pool, "init_pool", AsyncMock(side_effect=AssertionError("init_pool must not be called when no DSN")))
    monkeypatch.setattr(audit, "_log_impl", AsyncMock(side_effect=AssertionError("_log_impl must not be reached without a pool")))

    with caplog.at_level("WARNING", logger="backend.audit"):
        rid = await audit.log("milestone_ready", "release_branch", "main",
                              after={"outcome": "promoted"}, actor="auto_promote_main")

    assert rid is None
    err = capsys.readouterr().err
    assert "AUDIT-FALLBACK" in err
    assert "milestone_ready" in err
    assert "release_branch" in err
    assert "no PG DSN configured" in err
    assert any("no PG DSN configured" in r.message for r in caplog.records)


async def test_audit_log_uses_existing_pool_when_initialized(monkeypatch):
    """A pool is already installed (the FastAPI runtime case) ⇒ ``log()``
    uses it verbatim and never calls ``init_pool``."""
    fake_pool = _FakePool()
    monkeypatch.setattr(db_pool, "_pool", fake_pool, raising=False)
    # Even if a DSN is in the env, the pre-init'd pool wins.
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", "postgresql://u:pw@db.local:5432/omni")
    init_mock = AsyncMock(side_effect=AssertionError("init_pool must not be called when a pool already exists"))
    monkeypatch.setattr(db_pool, "init_pool", init_mock)
    monkeypatch.setattr(audit, "_log_impl", AsyncMock(return_value=7))

    rid = await audit.log("mode_change", "operation_mode", "global", after={"mode": "full_auto"})

    assert rid == 7
    assert fake_pool.acquired == 1
    init_mock.assert_not_awaited()


async def test_audit_log_does_not_double_init_pool(monkeypatch):
    """Two concurrent script-context ``log()`` calls (RaceOnFirstInit) ⇒
    exactly one ``init_pool`` call; the loser re-checks under the lock and
    reuses the freshly-created pool."""
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", "postgresql://u:pw@db.local:5432/omni")

    fake_pool = _FakePool()
    init_calls = 0

    async def _fake_init(dsn, **kwargs):
        nonlocal init_calls
        init_calls += 1
        await asyncio.sleep(0)  # yield so the racer reaches the lock
        db_pool._pool = fake_pool
        return fake_pool

    init_mock = AsyncMock(side_effect=_fake_init)
    monkeypatch.setattr(db_pool, "init_pool", init_mock)
    monkeypatch.setattr(audit, "_log_impl", AsyncMock(return_value=11))

    results = await asyncio.gather(
        audit.log("a", "release_branch", "main"),
        audit.log("b", "release_branch", "main"),
    )

    assert results == [11, 11]
    assert init_calls == 1
    init_mock.assert_awaited_once()
    # Both writes went through the single lazy-init'd pool.
    assert fake_pool.acquired == 2


async def test_audit_log_stderr_fallback_when_dsn_unreachable(monkeypatch, capsys, caplog):
    """Env DSN is set but ``init_pool`` raises (DSNUnreachable) ⇒ ``log()``
    catches it, warns + stderr-falls-back, and returns None — the script
    does not crash."""
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", "postgresql://u:pw@db.local:5432/omni")
    monkeypatch.setattr(
        db_pool, "init_pool",
        AsyncMock(side_effect=OSError("connection refused: db.local:5432")),
    )
    monkeypatch.setattr(audit, "_log_impl", AsyncMock(side_effect=AssertionError("unreachable without a pool")))

    with caplog.at_level("WARNING", logger="backend.audit"):
        rid = await audit.log("milestone_ready", "release_branch", "main")

    assert rid is None
    err = capsys.readouterr().err
    assert "AUDIT-FALLBACK" in err
    assert "pool lazy-init failed" in err
    assert "connection refused" in err
    assert any("lazy pool init failed" in r.message for r in caplog.records)
