"""OP-1656 — dag_plans executor lease: compare-and-set claim primitive.

Covers the acceptance criteria for the single-writer lease over a
``dag_plans`` row:

  * atomic claim (no double-grant) — two racing claimers, exactly one wins;
  * lease expiry + heartbeat-renew — a live lease renews, a lapsed one
    cannot, and a reclaim fences out the stale owner's token;
  * a stale lease is reclaimable (lazily on contention + via the explicit
    ``expire_stale_leases`` sweeper);
  * a backend-a / backend-b co-tenant simulation shows no double-execution;
  * the lease is dag_plans-LOCAL — it never imports runner_coordination or
    touches the runner_claims table (MUST-NOT).

Schema bootstrap note
---------------------
The PG-backed tests build a *minimal* ``dag_plans`` table directly (id +
the original Phase-56 columns + the four alembic-0248 lease columns) rather
than depending on the ``pg_test_alembic_upgraded`` / ``pg_test_pool``
fixtures. Those run ``alembic upgrade head``, which includes a
``CREATE EXTENSION vector`` step that is unavailable on PG images without
pgvector (a db/devops concern out of this ticket's area). Bootstrapping just
the table under test keeps the CAS logic exercised against *real* asyncpg /
PostgreSQL — which is what the integration ACs require — without dragging in
that unrelated extension. The column set + types mirror
``0016_pg_schema_sync`` (table) and ``0248_dag_plans_executor_lease`` (lease
columns).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from backend import dag_executor as dx
from backend import dag_storage as ds


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Non-PG unit tests (always run)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_lease_token_is_dag_plans_local_namespace():
    tok = ds._mint_lease_token("dag-exec-a")
    # Distinct from runner_coordination's OP-977 "claim:" prefix.
    assert tok.startswith("dag-plan-lease:")
    assert not tok.startswith("claim:")
    assert ":dag-exec-a:" in tok


def test_lease_tokens_are_unique_per_mint():
    # Uniqueness per attempt is the fencing guarantee — a re-claim must mint
    # a strictly fresh token so a stale owner's old token stops matching.
    toks = {ds._mint_lease_token("owner") for _ in range(200)}
    assert len(toks) == 200


def test_lease_layer_does_not_import_runner_coordination():
    """MUST-NOT: the dag_plans lease never reuses the runner claim module.
    Inspect the import graph (not comments) so a future edit can't silently
    couple them."""
    import ast

    tree = ast.parse(Path(ds.__file__).read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [n.name for n in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
            imported += [f"{node.module}.{n.name}" for n in node.names]
    assert not any("runner_coordination" in name for name in imported), imported
    assert not any("runner_claims" in name for name in imported), imported


def test_lease_sql_does_not_touch_runner_claims_table():
    """MUST-NOT: no lease query reads/writes the runner_claims table. Scan
    only string literals (SQL), skipping comments/docstrings, for the table
    name as a SQL identifier."""
    import ast

    tree = ast.parse(Path(ds.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "runner_claims" not in node.value, node.value


async def test_claim_plan_rejects_empty_owner():
    # Validation fires before any DB access, so this needs no PG.
    with pytest.raises(ValueError, match="non-empty owner"):
        await ds.claim_plan(1, "")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PG-backed integration tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

try:
    import pytest_asyncio  # noqa: F401
    import asyncpg  # noqa: F401
    _ASYNCPG_AVAILABLE = True
except ImportError:  # pragma: no cover
    _ASYNCPG_AVAILABLE = False


_BOOTSTRAP_SQL = """
DROP TABLE IF EXISTS dag_plans;
CREATE TABLE dag_plans (
    id                BIGSERIAL PRIMARY KEY,
    dag_id            TEXT NOT NULL,
    run_id            TEXT,
    parent_plan_id    BIGINT,
    json_body         TEXT NOT NULL DEFAULT '{}',
    status            TEXT NOT NULL DEFAULT 'pending',
    mutation_round    INTEGER NOT NULL DEFAULT 0,
    validation_errors TEXT,
    created_at        DOUBLE PRECISION NOT NULL,
    updated_at        DOUBLE PRECISION NOT NULL,
    -- alembic 0248 lease columns (all nullable, additive)
    claim_owner       TEXT,
    claim_token       TEXT,
    claim_expires_at  DOUBLE PRECISION,
    heartbeat_at      DOUBLE PRECISION
);
"""


if _ASYNCPG_AVAILABLE:

    @pytest_asyncio.fixture
    async def lease_pool(pg_test_dsn):
        """Function-scoped asyncpg pool over a minimal bootstrapped
        ``dag_plans`` table, installed as the module-global pool so the
        ``dag_storage`` helpers (which call ``get_pool()``) see it.

        Depends on ``pg_test_dsn`` only (skips when ``OMNI_TEST_PG_URL`` is
        unset) — deliberately NOT on the alembic-head fixtures; see module
        docstring.
        """
        from backend import db_pool as _db_pool

        _db_pool._reset_for_tests()
        # max_size >= 2 so the two-claimer race can hold two real
        # connections concurrently.
        pool = await _db_pool.init_pool(
            pg_test_dsn, min_size=2, max_size=8, init=None,
        )
        async with pool.acquire() as conn:
            await conn.execute(_BOOTSTRAP_SQL)
        try:
            yield pool
        finally:
            async with pool.acquire() as conn:
                await conn.execute("DROP TABLE IF EXISTS dag_plans")
            await _db_pool.close_pool()

    async def _new_plan(pool, dag_id="REQ-1", status="executing") -> int:
        now = time.time()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO dag_plans (dag_id, status, created_at, updated_at) "
                "VALUES ($1, $2, $3, $3) RETURNING id",
                dag_id, status, now,
            )
        return row["id"]

    # ─── atomic claim ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_claim_unclaimed_succeeds(lease_pool):
        pid = await _new_plan(lease_pool)
        lease = await ds.claim_plan(pid, "backend-a")
        assert lease is not None
        assert lease.owner == "backend-a"
        assert lease.token.startswith("dag-plan-lease:backend-a:")
        assert lease.claim_expires_at > time.time()
        # readable back through get_lease
        got = await ds.get_lease(pid)
        assert got is not None and got.token == lease.token

    @pytest.mark.asyncio
    async def test_get_lease_none_when_unclaimed(lease_pool):
        pid = await _new_plan(lease_pool)
        assert await ds.get_lease(pid) is None

    @pytest.mark.asyncio
    async def test_second_distinct_owner_blocked(lease_pool):
        pid = await _new_plan(lease_pool)
        a = await ds.claim_plan(pid, "backend-a")
        assert a is not None
        b = await ds.claim_plan(pid, "backend-b")
        assert b is None  # live lease held by A → no double-grant
        # A still owns it
        got = await ds.get_lease(pid)
        assert got.owner == "backend-a"

    @pytest.mark.asyncio
    async def test_same_owner_reclaim_refreshes_token(lease_pool):
        pid = await _new_plan(lease_pool)
        a1 = await ds.claim_plan(pid, "backend-a")
        a2 = await ds.claim_plan(pid, "backend-a")
        assert a2 is not None
        assert a2.token != a1.token  # fresh token minted

    @pytest.mark.asyncio
    async def test_two_claimers_exactly_one_wins(lease_pool):
        """Integration AC: a two-claimer race → exactly one winner.

        Run many rounds, each with two genuinely-concurrent claims racing on
        the same fresh plan row; assert exactly one grant per round.
        """
        for _ in range(40):
            pid = await _new_plan(lease_pool)
            results = await asyncio.gather(
                ds.claim_plan(pid, "backend-a"),
                ds.claim_plan(pid, "backend-b"),
            )
            winners = [r for r in results if r is not None]
            assert len(winners) == 1, (
                f"expected exactly one winner, got {len(winners)}"
            )
            # the DB agrees on a single owner
            got = await ds.get_lease(pid)
            assert got is not None and got.owner == winners[0].owner

    # ─── expiry + reclaim ────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_stale_lease_is_reclaimed_on_contention(lease_pool):
        """Integration AC: a stale lease is reclaimable."""
        pid = await _new_plan(lease_pool)
        t0 = 1_000_000.0
        a = await ds.claim_plan(pid, "backend-a", lease_ttl_s=10.0, now=t0)
        assert a is not None
        # before expiry: B blocked
        assert await ds.claim_plan(pid, "backend-b", now=t0 + 5) is None
        # after expiry: B reclaims, gets a distinct token
        b = await ds.claim_plan(pid, "backend-b", lease_ttl_s=10.0, now=t0 + 11)
        assert b is not None
        assert b.owner == "backend-b"
        assert b.token != a.token

    @pytest.mark.asyncio
    async def test_expire_stale_leases_sweeps_only_lapsed(lease_pool):
        live = await _new_plan(lease_pool)
        stale = await _new_plan(lease_pool)
        t0 = 2_000_000.0
        await ds.claim_plan(live, "backend-a", lease_ttl_s=100.0, now=t0)
        await ds.claim_plan(stale, "backend-b", lease_ttl_s=10.0, now=t0)
        swept = await ds.expire_stale_leases(now=t0 + 50)
        assert swept == 1
        assert await ds.get_lease(stale) is None       # lapsed → cleared
        assert (await ds.get_lease(live)).owner == "backend-a"  # live → kept
        # idempotent: a second sweep finds nothing
        assert await ds.expire_stale_leases(now=t0 + 50) == 0

    # ─── heartbeat-renew + fencing ───────────────────────────────

    @pytest.mark.asyncio
    async def test_renew_extends_live_lease(lease_pool):
        pid = await _new_plan(lease_pool)
        t0 = 3_000_000.0
        a = await ds.claim_plan(pid, "backend-a", lease_ttl_s=30.0, now=t0)
        renewed = await ds.renew_lease(
            pid, a.owner, a.token, lease_ttl_s=30.0, now=t0 + 10,
        )
        assert renewed is not None
        assert renewed.heartbeat_at == t0 + 10
        assert renewed.claim_expires_at == t0 + 40  # extended past original

    @pytest.mark.asyncio
    async def test_renew_fails_after_lapse(lease_pool):
        pid = await _new_plan(lease_pool)
        t0 = 4_000_000.0
        a = await ds.claim_plan(pid, "backend-a", lease_ttl_s=10.0, now=t0)
        # renew attempted after the lease already expired → lost
        assert await ds.renew_lease(
            pid, a.owner, a.token, now=t0 + 20,
        ) is None

    @pytest.mark.asyncio
    async def test_renew_fenced_out_after_reclaim(lease_pool):
        """Fencing: once B reclaims a stale lease, A's old token can no
        longer renew — A is fenced out."""
        pid = await _new_plan(lease_pool)
        t0 = 5_000_000.0
        a = await ds.claim_plan(pid, "backend-a", lease_ttl_s=10.0, now=t0)
        b = await ds.claim_plan(pid, "backend-b", lease_ttl_s=10.0, now=t0 + 11)
        assert b is not None
        # A tries to renew with its stale token while B's lease is live
        assert await ds.renew_lease(
            pid, a.owner, a.token, now=t0 + 12,
        ) is None
        # B can still renew its own live lease
        assert await ds.renew_lease(
            pid, b.owner, b.token, now=t0 + 12,
        ) is not None

    # ─── release ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_release_clears_lease_and_is_idempotent(lease_pool):
        pid = await _new_plan(lease_pool)
        a = await ds.claim_plan(pid, "backend-a")
        assert await ds.release_lease(pid, a.owner, a.token) is True
        assert await ds.get_lease(pid) is None
        # idempotent: releasing again is a no-op returning False
        assert await ds.release_lease(pid, a.owner, a.token) is False

    @pytest.mark.asyncio
    async def test_release_wrong_token_does_not_steal(lease_pool):
        pid = await _new_plan(lease_pool)
        await ds.claim_plan(pid, "backend-a")
        # B (wrong owner/token) cannot release A's lease
        assert await ds.release_lease(pid, "backend-b", "bogus-token") is False
        assert (await ds.get_lease(pid)).owner == "backend-a"

    @pytest.mark.asyncio
    async def test_release_lets_next_owner_claim(lease_pool):
        pid = await _new_plan(lease_pool)
        a = await ds.claim_plan(pid, "backend-a")
        await ds.release_lease(pid, a.owner, a.token)
        b = await ds.claim_plan(pid, "backend-b")
        assert b is not None and b.owner == "backend-b"

    # ─── co-tenant simulation: no double-execution ───────────────

    @pytest.mark.asyncio
    async def test_co_tenant_no_double_execution(lease_pool):
        """Exercised AC: backend-a and backend-b both poll a shared set of
        plans; every plan is granted to at most one executor — no plan is
        ever simultaneously leased by both (= no double-execution)."""
        plan_ids = [await _new_plan(lease_pool, dag_id=f"REQ-{i}")
                    for i in range(12)]

        async def claimer(owner: str) -> list[int]:
            won = []
            for pid in plan_ids:
                lease = await ds.claim_plan(pid, owner)
                if lease is not None:
                    won.append(pid)
            return won

        won_a, won_b = await asyncio.gather(claimer("backend-a"),
                                            claimer("backend-b"))
        # No plan won by both tenants.
        assert set(won_a).isdisjoint(set(won_b))
        # Every plan ends with exactly one owner.
        for pid in plan_ids:
            assert (await ds.get_lease(pid)) is not None

    # ─── executor wiring (owner = instance id; loop stays inert) ──

    @pytest.mark.asyncio
    async def test_executor_claim_uses_instance_id_as_owner(lease_pool):
        pid = await _new_plan(lease_pool)
        ex = dx.DagExecutor(
            dx.DagExecutorConfig(instance_id="claimant"), enabled=False,
        )
        lease = await ex.claim_plan(pid)
        assert lease is not None
        # owner is the normalised dag-exec-* instance id
        assert lease.owner == "dag-exec-claimant"
        # renew + release round-trip through the executor wrappers
        renewed = await ex.renew_plan_lease(lease)
        assert renewed is not None
        assert await ex.release_plan_lease(renewed) is True
        assert await ds.get_lease(pid) is None

    @pytest.mark.asyncio
    async def test_inert_loop_claims_nothing(lease_pool):
        """Deploy/Go-Live AC: the executor ships but stays inert — an armed
        run never claims the plan (the SEAM is a no-op)."""
        pid = await _new_plan(lease_pool)
        ex = dx.DagExecutor(
            dx.DagExecutorConfig(
                instance_id="inert",
                poll_interval_s=0.0, heartbeat_interval_s=0.0, max_ticks=5,
            ),
            heartbeat=_NoopHeartbeat(), enabled=True,
        )
        result = await ex.run()
        assert result.enabled is True and result.ticks == 5
        # nothing was claimed by the inert loop
        assert await ds.get_lease(pid) is None


class _NoopHeartbeat:
    """Heartbeat double so the inert-loop test needs no Redis."""

    def beat(self, *a, **k):  # noqa: D401
        pass

    def clear(self, *a, **k):
        pass
