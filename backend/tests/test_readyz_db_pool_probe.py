"""Phase-3-Runtime-v2 SP-1.5 — /readyz pool probe unit tests.

Focused on the ``_check_db_pool`` helper in ``backend/routers/health.py``.
We unit-test the helper in isolation (rather than hitting /readyz with
a full HTTP client) because /readyz also invokes _check_db which needs
a real compat connection. The helper is the unit of behaviour we care
about for SP-1.5; its integration into /readyz is a 2-line call that
the structural test in test_db_pool_lifespan.py-style could cover, but
observation-only probes are cheap to verify directly.
"""

from __future__ import annotations

import pytest

from backend import db_pool
from backend.routers.health import _check_db_pool


# Reset module-global pool between tests so one test doesn't leak into
# another. Tests that want a live pool call init_pool themselves and
# clean up in a finally.


@pytest.fixture(autouse=True)
def _db_pool_reset():
    db_pool._reset_for_tests()
    yield
    db_pool._reset_for_tests()


class TestCheckDbPoolUninitialised:
    """When the pool has never been started, the probe should return
    ok=True with a clear message — this is the SQLite-dev-mode path."""

    def test_returns_ok_true_when_pool_uninit(self) -> None:
        ok, detail = _check_db_pool()
        assert ok is True, (
            "Uninit pool is a legitimate state (SQLite dev mode / pre-"
            "lifespan-init) — /readyz must NOT fail on this."
        )
        # Detail should be human-readable and mention "not-initialised"
        # so operators eyeballing /readyz know what they're looking at.
        assert "not-initialised" in detail

    def test_detail_mentions_dev_mode(self) -> None:
        _, detail = _check_db_pool()
        # Operator signal: makes it clear this isn't an error state.
        assert "dev mode" in detail or "SQLite" in detail


class TestCheckDbPoolInitialised:
    """When the pool is live, the probe reports stats without borrowing
    a connection (cheap — /readyz is hit ~1/2s per replica)."""

    @pytest.mark.asyncio
    async def test_reports_pool_stats(
        self, pg_test_alembic_upgraded: str,
    ) -> None:
        await db_pool.init_pool(
            pg_test_alembic_upgraded, min_size=1, max_size=3,
        )
        try:
            ok, detail = _check_db_pool()
            assert ok is True
            # Detail should include the numeric breakdown so alerts
            # + dashboards can scrape it.
            assert "min=1" in detail
            assert "max=3" in detail
            assert "size=" in detail
            assert "free=" in detail
            assert "used=" in detail
        finally:
            await db_pool.close_pool()

    @pytest.mark.asyncio
    async def test_probe_does_not_acquire_connection(
        self, pg_test_alembic_upgraded: str,
    ) -> None:
        # Contract: the probe reads stats only, never calls .acquire().
        # Proof: used_size stays 0 before and after the probe — if the
        # probe had borrowed a conn, used_size would transiently hit 1.
        # (We can't catch the transient from outside, so we use a
        # side-channel check: stats.used_size must be 0 across the call.)
        await db_pool.init_pool(
            pg_test_alembic_upgraded, min_size=1, max_size=3,
        )
        try:
            stats_before = db_pool.get_pool_stats()
            _check_db_pool()
            stats_after = db_pool.get_pool_stats()
            # min_size=1 warm conns stay warm, used_size stays at 0.
            assert stats_before["used_size"] == 0
            assert stats_after["used_size"] == 0
        finally:
            await db_pool.close_pool()


class TestCheckDbPoolErrorPath:
    """If get_pool_stats raises for any reason, the probe degrades to
    ok=False with a sanitised detail (type + message, no traceback)."""

    def test_ok_false_when_stats_raises(self, monkeypatch) -> None:
        def _boom():
            raise RuntimeError("synthetic: stats unreachable")

        monkeypatch.setattr(db_pool, "get_pool_stats", _boom)
        ok, detail = _check_db_pool()
        assert ok is False
        assert "pool_stats_failed" in detail
        # Error detail should name the exception type for triage.
        assert "RuntimeError" in detail
        assert "synthetic: stats unreachable" in detail


class TestReadyzIntegration:
    """Structural check — /readyz handler must include db_pool in its
    checks dict. Same discipline as test_db_pool_lifespan: we assert on
    source shape, not runtime behaviour, to avoid the mocking rabbit
    hole. Runtime coverage is via the unit tests above."""

    def test_readyz_includes_db_pool_check(self) -> None:
        from pathlib import Path
        src = (
            Path(__file__).resolve().parents[1]
            / "routers" / "health.py"
        ).read_text()
        assert 'checks["db_pool"]' in src, (
            "/readyz must surface the db_pool probe in its JSON payload "
            "so operators + monitoring can see pool state. Add "
            'checks["db_pool"] = {"ok": ..., "detail": ...} alongside '
            "the existing db / migrations / provider_chain checks."
        )
        assert "_check_db_pool" in src, (
            "/readyz handler must call _check_db_pool() to populate the "
            "checks['db_pool'] entry."
        )

    def test_db_pool_is_not_in_ready_gate(self) -> None:
        # Observational only — must NOT be in the AND-ed `ready` expr
        # until Epic 7 (compat wrapper deletion) swaps the primary
        # DB probe. Catching this now prevents accidentally gating
        # readyz on pool state while the compat path is still live.
        from pathlib import Path
        src = (
            Path(__file__).resolve().parents[1]
            / "routers" / "health.py"
        ).read_text()
        # The ready = ... line must not reference pool_ok until Epic 7.
        # We look for the canonical pattern.
        assert "ready = db_ok and mig_ok and prov_ok" in src, (
            "/readyz's `ready` gate has drifted. Until Epic 7 the gate "
            "must be exactly `db_ok and mig_ok and prov_ok` — the pool "
            "probe is observational, not gating, during Epics 1-6."
        )
