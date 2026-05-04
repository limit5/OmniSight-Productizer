"""H4a row 2582 — last-known-good budget persistence tests.

Two layers of coverage:

1. **Dirty-flag contract** (sync, no DB): ``tick()`` must mark the
   state dirty exactly when the budget actually changes. HOLD / CAP /
   no-op FLOOR must NOT set the flag — otherwise an idle host burns
   a DB write every 5 s forever.
2. **Load / save flow** (mocked pool): ``prime_from_db`` must
   silently fall back to :data:`INIT_BUDGET` when the pool is
   unavailable or the row is missing, and clamp out-of-envelope
   stored values into ``[FLOOR_BUDGET, CAPACITY_MAX]`` (operator
   moved the DB to a smaller host, or CAPACITY_MAX shrunk).

A third layer — real PG round-trip via ``pg_test_pool`` — lives in
``test_adaptive_budget_persistence_pg.py``-style suites gated on
``OMNI_TEST_PG_URL``; the harness available here swaps it out for a
minimal async mock so the contract runs unconditionally in CI.
"""

from __future__ import annotations


import pytest

from backend import adaptive_budget as ab
from backend.sandbox_capacity import CAPACITY_MAX


# ─────────────────────────────────────────────────────────────────────
#  Fixture: reset controller state between tests
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset():
    ab._reset_for_tests()
    yield
    ab._reset_for_tests()


# ─────────────────────────────────────────────────────────────────────
#  Async mock pool (mirrors ``db_pool.get_pool`` surface)
# ─────────────────────────────────────────────────────────────────────

class _FakeConn:
    """Minimal asyncpg.Connection stand-in covering the two methods
    :mod:`backend.db` calls on the adaptive-budget path."""

    def __init__(self, rows: dict | None = None, *, raise_on_execute: bool = False):
        self._rows = rows if rows is not None else {}
        self._raise_on_execute = raise_on_execute
        self.exec_calls: list[tuple] = []  # (sql, args)
        self.fetch_calls: list[tuple] = []

    async def fetchrow(self, sql, *args):
        self.fetch_calls.append((sql, args))
        # Support only the single SELECT we issue.
        if "SELECT budget" not in sql:
            raise AssertionError(f"unexpected fetchrow: {sql!r}")
        key = args[0] if args else None
        row = self._rows.get(key)
        return row

    async def execute(self, sql, *args):
        self.exec_calls.append((sql, args))
        if self._raise_on_execute:
            raise RuntimeError("simulated DB failure")
        # Support the INSERT ... ON CONFLICT upsert used by
        # save_adaptive_budget_state.
        if "INSERT INTO adaptive_budget_state" in sql:
            ident, budget, reason, updated = args
            self._rows[ident] = {
                "budget": budget,
                "last_reason": reason,
                "updated_at": updated,
            }


class _FakePoolCtx:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    def acquire(self):
        return _FakePoolCtx(self._conn)


def _install_fake_pool(monkeypatch, conn: _FakeConn) -> _FakePool:
    pool = _FakePool(conn)
    monkeypatch.setattr(
        "backend.db_pool.get_pool",
        lambda: pool,
        raising=False,
    )
    return pool


def _break_pool(monkeypatch) -> None:
    """Make ``db_pool.get_pool()`` raise — simulates SQLite dev mode
    or a lifespan that hasn't opened the pool yet."""
    def _boom():
        raise RuntimeError("pool not initialised")
    monkeypatch.setattr("backend.db_pool.get_pool", _boom, raising=False)


# ─────────────────────────────────────────────────────────────────────
#  Dirty-flag contract (sync, no DB)
# ─────────────────────────────────────────────────────────────────────

class TestDirtyFlag:
    def test_initial_state_not_dirty(self):
        # Cold start (reset by fixture) must not schedule a DB write.
        assert ab._state.dirty is False

    def test_ai_marks_dirty(self):
        ab.reset(initial_budget=6, now=0.0)
        assert ab._state.dirty is False
        r = ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=30.0)
        assert r == ab.AdjustReason.AI
        assert ab._state.dirty is True

    def test_md_marks_dirty(self):
        ab.reset(initial_budget=8, now=0.0)
        assert ab._state.dirty is False
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=0.0)
        r = ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=10.0)
        assert r == ab.AdjustReason.MD
        assert ab._state.dirty is True

    def test_hold_does_not_mark_dirty(self):
        # Cool but 30 s not elapsed → HOLD → no persist required.
        ab.reset(initial_budget=6, now=0.0)
        ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=15.0)
        assert ab._state.dirty is False

    def test_cap_does_not_mark_dirty(self):
        # Already at CAPACITY_MAX — an "AI" eligibility tick turns
        # into CAP with budget unchanged → no persist needed.
        ab.reset(initial_budget=CAPACITY_MAX, now=0.0)
        r = ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=30.0)
        assert r == ab.AdjustReason.CAP
        assert ab._state.dirty is False

    def test_floor_noop_does_not_mark_dirty(self):
        # Already at FLOOR_BUDGET — a sustained-pressure tick returns
        # FLOOR with budget unchanged → no persist needed.
        ab.reset(initial_budget=ab.FLOOR_BUDGET, now=0.0)
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=0.0)
        r = ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=10.0)
        assert r == ab.AdjustReason.FLOOR
        assert ab._state.dirty is False

    def test_md_that_shrinks_to_floor_still_marks_dirty(self):
        # 3 // 2 = 1, clamped to 2. Reason is MD (did shrink 3→2) so
        # this counts as a real change — must persist.
        ab.reset(initial_budget=3, now=0.0)
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=0.0)
        r = ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=10.0)
        assert r == ab.AdjustReason.MD
        assert ab.current_budget() == 2
        assert ab._state.dirty is True

    def test_reset_clears_dirty(self):
        # After an AI, budget is dirty; reset() must clear it so the
        # next startup doesn't re-persist the same value.
        ab.reset(initial_budget=6, now=0.0)
        ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=30.0)
        assert ab._state.dirty is True
        ab.reset(initial_budget=6, now=100.0)
        assert ab._state.dirty is False


# ─────────────────────────────────────────────────────────────────────
#  load_last_known_good — best-effort with mock pool
# ─────────────────────────────────────────────────────────────────────

class TestLoadLastKnownGood:
    @pytest.mark.asyncio
    async def test_returns_none_when_pool_unavailable(self, monkeypatch):
        _break_pool(monkeypatch)
        assert await ab.load_last_known_good() is None

    @pytest.mark.asyncio
    async def test_returns_none_when_row_missing(self, monkeypatch):
        conn = _FakeConn(rows={})  # no row with id='global'
        _install_fake_pool(monkeypatch, conn)
        assert await ab.load_last_known_good() is None

    @pytest.mark.asyncio
    async def test_returns_stored_budget(self, monkeypatch):
        conn = _FakeConn(rows={
            "global": {"budget": 9, "last_reason": "additive_increase", "updated_at": 123.0},
        })
        _install_fake_pool(monkeypatch, conn)
        loaded = await ab.load_last_known_good()
        assert loaded == 9  # in-envelope → passes through

    @pytest.mark.asyncio
    async def test_clamps_above_cap(self, monkeypatch):
        # Operator migrated DB from a bigger host → row has 100 but
        # current CAPACITY_MAX is smaller. Loader must clamp, not reject.
        conn = _FakeConn(rows={
            "global": {"budget": 9999, "last_reason": "additive_increase", "updated_at": 0.0},
        })
        _install_fake_pool(monkeypatch, conn)
        assert await ab.load_last_known_good() == CAPACITY_MAX

    @pytest.mark.asyncio
    async def test_clamps_below_floor(self, monkeypatch):
        # Row corrupted to 0 or somehow < FLOOR — still safe to seed.
        conn = _FakeConn(rows={
            "global": {"budget": 0, "last_reason": "init", "updated_at": 0.0},
        })
        _install_fake_pool(monkeypatch, conn)
        assert await ab.load_last_known_good() == ab.FLOOR_BUDGET


# ─────────────────────────────────────────────────────────────────────
#  persist_current_budget_if_dirty — best-effort with mock pool
# ─────────────────────────────────────────────────────────────────────

class TestPersistCurrentBudget:
    @pytest.mark.asyncio
    async def test_noop_when_not_dirty(self, monkeypatch):
        conn = _FakeConn()
        _install_fake_pool(monkeypatch, conn)
        ab.reset(initial_budget=6, now=0.0)  # clears dirty
        result = await ab.persist_current_budget_if_dirty()
        assert result is False
        assert conn.exec_calls == []

    @pytest.mark.asyncio
    async def test_writes_when_dirty_and_clears_flag(self, monkeypatch):
        conn = _FakeConn()
        _install_fake_pool(monkeypatch, conn)
        ab.reset(initial_budget=6, now=0.0)
        ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=30.0)
        assert ab._state.dirty is True
        assert ab.current_budget() == 7
        result = await ab.persist_current_budget_if_dirty()
        assert result is True
        assert ab._state.dirty is False
        # Upsert was fired with the new budget.
        assert len(conn.exec_calls) == 1
        sql, args = conn.exec_calls[0]
        assert "INSERT INTO adaptive_budget_state" in sql
        assert args[0] == "global"
        assert args[1] == 7
        assert args[2] == ab.AdjustReason.AI.value

    @pytest.mark.asyncio
    async def test_pool_unavailable_returns_false_without_raising(self, monkeypatch):
        # Dirty but pool broken — must not crash the host sampling loop.
        ab.reset(initial_budget=6, now=0.0)
        ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=30.0)
        _break_pool(monkeypatch)
        result = await ab.persist_current_budget_if_dirty()
        assert result is False

    @pytest.mark.asyncio
    async def test_db_error_returns_false_without_raising(self, monkeypatch):
        ab.reset(initial_budget=6, now=0.0)
        ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=30.0)
        conn = _FakeConn(raise_on_execute=True)
        _install_fake_pool(monkeypatch, conn)
        result = await ab.persist_current_budget_if_dirty()
        assert result is False
        # Flag is cleared even on DB failure — next AI/MD re-arms
        # it. This is the documented contract (best-effort, stale
        # writes during HOLD-only windows are acceptable).
        assert ab._state.dirty is False


# ─────────────────────────────────────────────────────────────────────
#  prime_from_db — end-to-end bootstrap
# ─────────────────────────────────────────────────────────────────────

class TestPrimeFromDB:
    @pytest.mark.asyncio
    async def test_falls_back_silently_when_pool_unavailable(self, monkeypatch):
        _break_pool(monkeypatch)
        result = await ab.prime_from_db()
        assert result is None
        # Cold-start INIT_BUDGET default remains in place.
        expected = max(ab.FLOOR_BUDGET, min(CAPACITY_MAX, ab.INIT_BUDGET))
        assert ab.current_budget() == expected

    @pytest.mark.asyncio
    async def test_falls_back_silently_when_row_missing(self, monkeypatch):
        conn = _FakeConn(rows={})
        _install_fake_pool(monkeypatch, conn)
        result = await ab.prime_from_db()
        assert result is None
        expected = max(ab.FLOOR_BUDGET, min(CAPACITY_MAX, ab.INIT_BUDGET))
        assert ab.current_budget() == expected

    @pytest.mark.asyncio
    async def test_seeds_from_persisted_row(self, monkeypatch):
        # Previous process converged to 9 → new process wakes at 9,
        # not the static INIT_BUDGET=6.
        conn = _FakeConn(rows={
            "global": {"budget": 9, "last_reason": "additive_increase", "updated_at": 123.0},
        })
        _install_fake_pool(monkeypatch, conn)
        result = await ab.prime_from_db()
        assert result == 9
        assert ab.current_budget() == 9

    @pytest.mark.asyncio
    async def test_prime_clears_dirty_flag(self, monkeypatch):
        # After priming from DB, the dirty flag must be False —
        # otherwise the very next persist cycle would write the just-
        # loaded value back to the same row (no harm, but wasted I/O).
        conn = _FakeConn(rows={
            "global": {"budget": 9, "last_reason": "additive_increase", "updated_at": 0.0},
        })
        _install_fake_pool(monkeypatch, conn)
        await ab.prime_from_db()
        assert ab._state.dirty is False

    @pytest.mark.asyncio
    async def test_prime_replaces_cold_start_trace(self, monkeypatch):
        # After prime, the trace should have a fresh INIT entry at
        # the loaded budget — not a stale 6-seed entry from module
        # import. Operator looking at /ops/summary immediately after
        # restart sees the right starting budget.
        conn = _FakeConn(rows={
            "global": {"budget": 8, "last_reason": "additive_increase", "updated_at": 0.0},
        })
        _install_fake_pool(monkeypatch, conn)
        await ab.prime_from_db()
        entries = ab.trace()
        assert len(entries) == 1
        assert entries[0].reason == ab.AdjustReason.INIT
        assert entries[0].budget == 8


# ─────────────────────────────────────────────────────────────────────
#  End-to-end: save then reload via the same fake pool
# ─────────────────────────────────────────────────────────────────────

class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_ai_then_prime_recovers_value(self, monkeypatch):
        # Simulates: worker A grows to 7 → persists → process dies →
        # worker B boots and primes from DB → starts at 7, not 6.
        conn = _FakeConn()
        _install_fake_pool(monkeypatch, conn)
        ab.reset(initial_budget=6, now=0.0)
        ab.tick(cpu_percent=10, mem_percent=10, deferred_count=0, now=30.0)
        assert ab.current_budget() == 7
        assert await ab.persist_current_budget_if_dirty() is True

        # Simulate fresh process: module-state reset, then prime.
        ab._reset_for_tests()
        assert ab.current_budget() == max(ab.FLOOR_BUDGET, min(CAPACITY_MAX, ab.INIT_BUDGET))
        loaded = await ab.prime_from_db()
        assert loaded == 7
        assert ab.current_budget() == 7

    @pytest.mark.asyncio
    async def test_md_then_prime_recovers_reduced_value(self, monkeypatch):
        # Stress scenario: host spike halves budget 8→4, process
        # dies before it can recover. Next boot starts at the
        # throttled 4, not the default 6 (defensive — if the host
        # couldn't sustain 8, seeding at 6 would just invite another
        # MD within seconds).
        conn = _FakeConn()
        _install_fake_pool(monkeypatch, conn)
        ab.reset(initial_budget=8, now=0.0)
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=0.0)
        ab.tick(cpu_percent=90, mem_percent=10, deferred_count=0, now=10.0)
        assert ab.current_budget() == 4
        assert await ab.persist_current_budget_if_dirty() is True

        ab._reset_for_tests()
        loaded = await ab.prime_from_db()
        assert loaded == 4


# ─────────────────────────────────────────────────────────────────────
#  evaluate_and_persist_from_host_snapshot wiring
# ─────────────────────────────────────────────────────────────────────

class TestEvaluateAndPersist:
    @pytest.mark.asyncio
    async def test_persists_after_ai(self, monkeypatch):
        # Drive a cool host through an AI-eligible tick via the
        # convenience wrapper the sampling loop will call.
        conn = _FakeConn()
        _install_fake_pool(monkeypatch, conn)

        class _StubHost:
            cpu_percent = 10.0
            mem_percent = 10.0
            sampled_at = 0.0

        class _StubSnap:
            host = _StubHost()
            sampled_at = 0.0

        from backend import host_metrics, sandbox_capacity
        monkeypatch.setattr(host_metrics, "get_latest_host_snapshot", lambda: _StubSnap())
        monkeypatch.setattr(sandbox_capacity, "deferred_count_recent", lambda: 0)

        ab.reset(initial_budget=6, now=0.0)
        reason = await ab.evaluate_and_persist_from_host_snapshot(now=30.0)
        assert reason == ab.AdjustReason.AI
        assert ab.current_budget() == 7
        # Persistence kicked in automatically.
        assert len(conn.exec_calls) == 1
        assert conn.exec_calls[0][1][1] == 7

    @pytest.mark.asyncio
    async def test_no_persist_on_hold(self, monkeypatch):
        conn = _FakeConn()
        _install_fake_pool(monkeypatch, conn)

        class _StubHost:
            cpu_percent = 10.0
            mem_percent = 10.0
            sampled_at = 0.0

        class _StubSnap:
            host = _StubHost()
            sampled_at = 0.0

        from backend import host_metrics, sandbox_capacity
        monkeypatch.setattr(host_metrics, "get_latest_host_snapshot", lambda: _StubSnap())
        monkeypatch.setattr(sandbox_capacity, "deferred_count_recent", lambda: 0)

        ab.reset(initial_budget=6, now=0.0)
        reason = await ab.evaluate_and_persist_from_host_snapshot(now=15.0)
        assert reason == ab.AdjustReason.HOLD
        assert conn.exec_calls == []  # idle host ≠ DB traffic


# ─────────────────────────────────────────────────────────────────────
#  Drift guard — DB helpers call a shared singleton id
# ─────────────────────────────────────────────────────────────────────

class TestDriftGuards:
    def test_singleton_id_constant(self):
        from backend import db
        # Renaming the constant requires a schema migration (existing
        # rows would orphan). Lock it here.
        assert db.ADAPTIVE_BUDGET_SINGLETON_ID == "global"

    def test_migrator_covers_adaptive_budget_state(self):
        # The table must be in TABLES_IN_ORDER so cutover doesn't
        # silently drop the calibration row. Matches the pattern F3
        # drift guard enforces in test_migrator_schema_coverage.py.
        import importlib.util
        import sys as _sys
        from pathlib import Path
        migrator = (
            Path(__file__).resolve().parents[2]
            / "scripts" / "migrate_sqlite_to_pg.py"
        )
        spec = importlib.util.spec_from_file_location(
            "migrator_probe_adaptive_budget", migrator,
        )
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        # ``@dataclass`` resolves ``cls.__module__`` via sys.modules,
        # so the module must be registered before ``exec_module`` runs.
        _sys.modules[spec.name] = mod
        try:
            spec.loader.exec_module(mod)
            assert "adaptive_budget_state" in mod.TABLES_IN_ORDER
            # TEXT PK → must NOT be in the identity-reset list.
            assert "adaptive_budget_state" not in mod.TABLES_WITH_IDENTITY_ID
        finally:
            _sys.modules.pop(spec.name, None)
