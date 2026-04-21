"""Phase 53 / I8 tests — audit chain integrity + per-tenant chain isolation.

Phase-3-Runtime-v2 SP-4.1 (2026-04-20): migrated from SQLite tempfile
fixture to ``pg_test_pool``. audit.py is now asyncpg-native with
``pg_advisory_xact_lock`` per-tenant for concurrent-append safety;
tests exercise that via the pool.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
async def _audit_db(pg_test_pool, monkeypatch):
    # Clean slate per test — audit_log is NOT savepoint-isolated
    # because audit.log commits via its own pool-scoped transaction.
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE audit_log RESTART IDENTITY CASCADE"
        )
    from backend import audit
    try:
        yield audit
    finally:
        from backend.db_context import set_tenant_id
        set_tenant_id(None)
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE audit_log RESTART IDENTITY CASCADE"
            )


@pytest.mark.asyncio
async def test_log_appends_row(_audit_db):
    audit = _audit_db
    rid = await audit.log("mode_change", "operation_mode", "global",
                          before={"mode": "supervised"},
                          after={"mode": "full_auto"})
    assert isinstance(rid, int) and rid > 0
    rows = await audit.query(limit=10)
    assert len(rows) == 1
    assert rows[0]["action"] == "mode_change"
    assert rows[0]["after"] == {"mode": "full_auto"}


@pytest.mark.asyncio
async def test_chain_intact_after_many_writes(_audit_db):
    audit = _audit_db
    for i in range(20):
        await audit.log(f"act_{i % 3}", "thing", f"id_{i}",
                        before={"v": i}, after={"v": i + 1})
    ok, bad = await audit.verify_chain()
    assert ok and bad is None


@pytest.mark.asyncio
async def test_chain_detects_tampering(_audit_db):
    audit = _audit_db
    from backend.db_pool import get_pool
    for i in range(5):
        await audit.log("set_strategy", "budget_strategy", "global",
                        before={"s": "balanced"}, after={"s": "sprint"})
    # Find the 3rd row's actual id (autoincrement may not start at 1
    # on a shared PG; use offset 2 for the third insert).
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM audit_log ORDER BY id ASC OFFSET 2 LIMIT 1"
        )
        tampered_id = row["id"]
        await conn.execute(
            "UPDATE audit_log SET after_json = '{\"s\":\"FORGED\"}' "
            "WHERE id = $1",
            tampered_id,
        )
    ok, bad = await audit.verify_chain()
    assert not ok
    assert bad == tampered_id, f"first bad should be the tampered row, got {bad}"


@pytest.mark.asyncio
async def test_query_filters(_audit_db):
    audit = _audit_db
    await audit.log("a", "decision", "d1", actor="user")
    await audit.log("b", "operation_mode", "global", actor="system")
    await audit.log("a", "decision", "d2", actor="user")

    by_actor = await audit.query(actor="user")
    assert len(by_actor) == 2
    by_kind = await audit.query(entity_kind="operation_mode")
    assert len(by_kind) == 1
    assert by_kind[0]["entity_id"] == "global"


@pytest.mark.asyncio
async def test_query_session_id_filter(_audit_db):
    audit = _audit_db
    await audit.log("a", "decision", "d1", actor="user", session_id="sess-aaa")
    await audit.log("b", "operation_mode", "global", actor="user", session_id="sess-bbb")
    await audit.log("c", "decision", "d2", actor="user", session_id="sess-aaa")

    by_sess = await audit.query(session_id="sess-aaa")
    assert len(by_sess) == 2
    assert all(r["session_id"] == "sess-aaa" for r in by_sess)

    by_sess_b = await audit.query(session_id="sess-bbb")
    assert len(by_sess_b) == 1
    assert by_sess_b[0]["action"] == "b"

    no_match = await audit.query(session_id="sess-zzz")
    assert len(no_match) == 0


@pytest.mark.asyncio
async def test_log_failure_does_not_raise(_audit_db, monkeypatch):
    # SP-4.1: confirm the outer try/except in audit.log still swallows
    # errors + returns None rather than bubbling them to the caller.
    # Simulate failure by monkeypatching get_pool to raise — avoids
    # the "close the shared module pool and break the fixture"
    # anti-pattern the original SQLite test used.
    audit = _audit_db

    def _broken_pool(*a, **kw):
        raise RuntimeError("simulated pool-unavailable")

    monkeypatch.setattr("backend.db_pool.get_pool", _broken_pool)
    rid = await audit.log("a", "x", None)
    assert rid is None


# ─── I8: Per-tenant hash chain tests ───


async def _create_test_tenants(*tids):
    """Insert test tenant rows so FK constraints pass."""
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        for tid in tids:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan) VALUES ($1, $2, 'free') "
                "ON CONFLICT (id) DO NOTHING",
                tid, f"Test {tid}",
            )


@pytest.mark.asyncio
async def test_per_tenant_independent_chains(_audit_db):
    """Each tenant has its own genesis (empty prev_hash) and independent chain."""
    audit = _audit_db
    from backend.db_context import set_tenant_id
    await _create_test_tenants("t-alpha", "t-beta")

    set_tenant_id("t-alpha")
    for i in range(5):
        await audit.log(f"alpha_{i}", "thing", f"a{i}")

    set_tenant_id("t-beta")
    for i in range(3):
        await audit.log(f"beta_{i}", "thing", f"b{i}")

    ok_a, bad_a = await audit.verify_chain(tenant_id="t-alpha")
    assert ok_a and bad_a is None

    ok_b, bad_b = await audit.verify_chain(tenant_id="t-beta")
    assert ok_b and bad_b is None


@pytest.mark.asyncio
async def test_per_tenant_genesis_starts_empty(_audit_db):
    """First row of each tenant's chain should have empty prev_hash."""
    audit = _audit_db
    from backend import db
    from backend.db_context import set_tenant_id
    await _create_test_tenants("t-one", "t-two")

    set_tenant_id("t-one")
    await audit.log("first", "thing", "x1")

    set_tenant_id("t-two")
    await audit.log("first", "thing", "x2")

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT tenant_id, prev_hash FROM audit_log ORDER BY id ASC"
        )
    assert len(rows) == 2
    assert rows[0]["prev_hash"] == ""
    assert rows[1]["prev_hash"] == ""


@pytest.mark.asyncio
async def test_tampering_one_tenant_does_not_affect_other(_audit_db):
    """Tampering in tenant A should not break tenant B's chain."""
    audit = _audit_db
    from backend import db
    from backend.db_context import set_tenant_id
    await _create_test_tenants("t-alpha", "t-beta")

    set_tenant_id("t-alpha")
    for i in range(3):
        await audit.log(f"a_{i}", "thing", f"a{i}")

    set_tenant_id("t-beta")
    for i in range(3):
        await audit.log(f"b_{i}", "thing", f"b{i}")

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM audit_log WHERE tenant_id = $1 "
            "ORDER BY id ASC LIMIT 1 OFFSET 1",
            "t-alpha",
        )
        tampered_id = row["id"]
        await conn.execute(
            "UPDATE audit_log SET after_json = '{\"forged\":true}' "
            "WHERE id = $1",
            tampered_id,
        )

    ok_a, bad_a = await audit.verify_chain(tenant_id="t-alpha")
    assert not ok_a
    assert bad_a == tampered_id

    ok_b, bad_b = await audit.verify_chain(tenant_id="t-beta")
    assert ok_b and bad_b is None


@pytest.mark.asyncio
async def test_verify_all_chains(_audit_db):
    """verify_all_chains returns per-tenant results."""
    audit = _audit_db
    from backend.db_context import set_tenant_id
    await _create_test_tenants("t-alpha", "t-beta")

    set_tenant_id("t-alpha")
    for i in range(3):
        await audit.log(f"a_{i}", "thing", f"a{i}")

    set_tenant_id("t-beta")
    for i in range(2):
        await audit.log(f"b_{i}", "thing", f"b{i}")

    results = await audit.verify_all_chains()
    assert "t-alpha" in results
    assert "t-beta" in results
    assert results["t-alpha"] == (True, None)
    assert results["t-beta"] == (True, None)


@pytest.mark.asyncio
async def test_verify_all_chains_detects_partial_tampering(_audit_db):
    """verify_all_chains should detect tampering in one tenant while others pass."""
    audit = _audit_db
    from backend import db
    from backend.db_context import set_tenant_id
    await _create_test_tenants("t-good", "t-bad")

    set_tenant_id("t-good")
    for i in range(3):
        await audit.log(f"g_{i}", "thing", f"g{i}")

    set_tenant_id("t-bad")
    for i in range(3):
        await audit.log(f"b_{i}", "thing", f"b{i}")

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM audit_log WHERE tenant_id = $1 "
            "ORDER BY id ASC LIMIT 1 OFFSET 1",
            "t-bad",
        )
        tampered_id = row["id"]
        await conn.execute(
            "UPDATE audit_log SET after_json = '{\"forged\":true}' "
            "WHERE id = $1",
            tampered_id,
        )

    results = await audit.verify_all_chains()
    assert results["t-good"] == (True, None)
    ok_bad, bad_id = results["t-bad"]
    assert not ok_bad
    assert bad_id == tampered_id


@pytest.mark.asyncio
async def test_cross_tenant_query_isolation(_audit_db):
    """Queries with tenant context only return that tenant's rows."""
    audit = _audit_db
    from backend.db_context import set_tenant_id
    await _create_test_tenants("t-alpha", "t-beta")

    set_tenant_id("t-alpha")
    await audit.log("a1", "thing", "x1")
    await audit.log("a2", "thing", "x2")

    set_tenant_id("t-beta")
    await audit.log("b1", "thing", "y1")

    set_tenant_id("t-alpha")
    rows_alpha = await audit.query()
    assert len(rows_alpha) == 2
    assert all(r["action"].startswith("a") for r in rows_alpha)

    set_tenant_id("t-beta")
    rows_beta = await audit.query()
    assert len(rows_beta) == 1
    assert rows_beta[0]["action"] == "b1"


@pytest.mark.asyncio
async def test_interleaved_writes_maintain_separate_chains(_audit_db):
    """Alternating writes between tenants should maintain correct chains."""
    audit = _audit_db
    from backend.db_context import set_tenant_id
    await _create_test_tenants("t-even", "t-odd")

    for i in range(6):
        tid = "t-even" if i % 2 == 0 else "t-odd"
        set_tenant_id(tid)
        await audit.log(f"act_{i}", "thing", f"id_{i}")

    ok_even, _ = await audit.verify_chain(tenant_id="t-even")
    ok_odd, _ = await audit.verify_chain(tenant_id="t-odd")
    assert ok_even
    assert ok_odd


# ─── SP-4.1 concurrent-append contract (load-bearing) ──────────────


@pytest.mark.asyncio
async def test_concurrent_appends_preserve_chain(_audit_db):
    """LOAD-BEARING: multiple simultaneous audit.log() calls on the
    same tenant must NOT create chain forks.

    Without pg_advisory_xact_lock, two tasks running on different pool
    connections can both read the same prev_hash (the SELECT for the
    previous row's curr_hash isn't under SELECT FOR UPDATE on the
    tenant's tail), compute the same curr_hash, and INSERT two rows
    with identical prev_hash + curr_hash → chain forks and verify_chain
    fails from the second row onward.

    SP-4.1 adds ``SELECT pg_advisory_xact_lock(hashtext('audit-chain-'||tenant))``
    as the first statement inside each append's transaction. PG
    serializes writers on the lock key; different tenants hold
    different keys and still append in parallel. Regression guard:
    if that advisory lock is dropped or the key becomes wrong, this
    test fails within a few runs.
    """
    import asyncio
    audit = _audit_db
    await _create_test_tenants("t-concurrent")
    from backend.db_context import set_tenant_id
    set_tenant_id("t-concurrent")

    async def _one(i: int) -> None:
        await audit.log(f"concurrent_{i}", "thing", f"id_{i}",
                        before={"v": i}, after={"v": i + 1})

    # Fan out 20 concurrent appends — with the advisory lock they
    # serialise at the DB level; without it, the race window is
    # large enough that several will collide.
    await asyncio.gather(*(_one(i) for i in range(20)))

    ok, bad = await audit.verify_chain(tenant_id="t-concurrent")
    assert ok, (
        f"Chain broke at row {bad} under concurrent appends — advisory "
        f"lock missing or keyed incorrectly?"
    )

    # 20 rows total, chain intact, no forks.
    rows = await audit.query(limit=100)
    assert len(rows) == 20


@pytest.mark.asyncio
async def test_concurrent_appends_different_tenants_dont_block(
    _audit_db,
):
    """Different tenants' advisory locks use different keys → their
    appends can proceed in parallel (regression guard against using
    a single global lock key).
    """
    import asyncio
    audit = _audit_db
    await _create_test_tenants("t-par-A", "t-par-B")
    from backend.db_context import set_tenant_id

    async def _append(tid: str, n: int) -> None:
        set_tenant_id(tid)
        for i in range(n):
            await audit.log(f"{tid}_{i}", "thing", f"id_{i}")

    await asyncio.gather(
        _append("t-par-A", 10),
        _append("t-par-B", 10),
    )

    ok_a, _ = await audit.verify_chain(tenant_id="t-par-A")
    ok_b, _ = await audit.verify_chain(tenant_id="t-par-B")
    assert ok_a
    assert ok_b


# ── Coverage gap-fill (task #83, 2026-04-21) ─────────────────────
#
# Baseline was 56% on the targeted run. Quick-win fills below
# target the polymorphic-conn arms, the query ``since`` filter,
# and the ``write_audit`` request-state wrapper. The CLI block
# (``_cli_main``, lines 364-420) is intentionally out of scope —
# subprocess-level CLI tests belong in a separate commit.


@pytest.mark.asyncio
async def test_log_accepts_explicit_conn_with_nested_tx(_audit_db, pg_test_pool):
    """Covers the ``conn is not None`` arm of ``log`` which wraps the
    caller-supplied connection in a nested PG savepoint (lines
    165-166). The advisory lock is still scoped to the outer
    transaction — we assert the row lands and the chain stays
    intact when reading back."""
    audit = _audit_db
    async with pg_test_pool.acquire() as conn:
        async with conn.transaction():
            rid = await audit.log(
                "explicit_conn", "thing", "x",
                before={"v": 0}, after={"v": 1},
                conn=conn,
            )
            assert isinstance(rid, int) and rid > 0
    ok, bad = await audit.verify_chain()
    assert ok and bad is None


@pytest.mark.asyncio
async def test_query_filters_by_since_timestamp(_audit_db):
    """Covers ``_query_impl``'s ``since`` filter (lines 205-206) —
    rows before the cutoff must be excluded. Exercises the
    timestamp-based audit window that the operator UI uses for
    "last N minutes"-style displays."""
    import time
    audit = _audit_db
    # Write one row, pin the cutoff, then write a second row.
    await audit.log("old", "thing", "a")
    cutoff = time.time()
    # Sub-millisecond-precision sleep so the second row's ts > cutoff.
    import asyncio
    await asyncio.sleep(0.01)
    await audit.log("new", "thing", "b")
    rows = await audit.query(since=cutoff)
    actions = {r["action"] for r in rows}
    assert "new" in actions
    assert "old" not in actions


@pytest.mark.asyncio
async def test_query_accepts_explicit_conn(_audit_db, pg_test_pool):
    """Covers the ``conn is not None`` arm of ``query`` (line 261).
    The polymorphic helper lets callers reuse an acquired pool
    connection instead of borrowing a new one."""
    audit = _audit_db
    await audit.log("direct_conn_q", "thing", "z")
    async with pg_test_pool.acquire() as conn:
        rows = await audit.query(limit=10, conn=conn)
    assert any(r["action"] == "direct_conn_q" for r in rows)


@pytest.mark.asyncio
async def test_verify_chain_accepts_explicit_conn(_audit_db, pg_test_pool):
    """Covers the ``conn is not None`` arm of ``verify_chain`` (line
    328)."""
    audit = _audit_db
    await audit.log("vc_explicit", "thing", "a")
    async with pg_test_pool.acquire() as conn:
        ok, bad = await audit.verify_chain(conn=conn)
    assert ok and bad is None


@pytest.mark.asyncio
async def test_verify_all_chains_accepts_explicit_conn(_audit_db, pg_test_pool):
    """Covers the explicit-conn block in ``verify_all_chains`` (lines
    347-355) where the caller hands in a pool connection and the
    function iterates tenants sharing it."""
    audit = _audit_db
    from backend.db_context import set_tenant_id
    set_tenant_id("t-par-X")
    await audit.log("vac_x", "thing", "a")
    set_tenant_id("t-par-Y")
    await audit.log("vac_y", "thing", "b")
    set_tenant_id(None)
    async with pg_test_pool.acquire() as conn:
        results = await audit.verify_all_chains(conn=conn)
    assert results.get("t-par-X", (False, None))[0] is True
    assert results.get("t-par-Y", (False, None))[0] is True


@pytest.mark.asyncio
async def test_write_audit_wrapper_pulls_session_and_actor(_audit_db):
    """Covers ``write_audit`` (lines 277-281) — the convenience
    wrapper that auto-extracts ``session_id`` and ``actor`` from a
    request's ``state`` attributes. Feeds it a minimal fake request
    and asserts both branches of the actor-resolution logic: (a)
    user present → email used, (b) user missing → "system" fallback."""
    audit = _audit_db

    class _FakeState:
        def __init__(self, user=None, session=None):
            self.user = user
            self.session = session

    class _FakeRequest:
        def __init__(self, state):
            self.state = state

    class _FakeSession:
        token = "tok-write-audit"

    class _FakeUser:
        email = "operator@test"

    # Branch (a): user present, session present — actor pulled from
    # user.email; session_id pulled from session.token.
    req = _FakeRequest(_FakeState(user=_FakeUser(), session=_FakeSession()))
    rid_a = await audit.write_audit(
        req, "write_audit_a", "thing", "x",
    )
    assert isinstance(rid_a, int)

    # Branch (b): no user, no session → actor defaults to "system",
    # session_id is None.
    req_bare = _FakeRequest(_FakeState())
    rid_b = await audit.write_audit(
        req_bare, "write_audit_b", "thing", "y",
    )
    assert isinstance(rid_b, int)

    rows = await audit.query(limit=5)
    by_action = {r["action"]: r for r in rows}
    assert by_action["write_audit_a"]["actor"] == "operator@test"
    assert by_action["write_audit_b"]["actor"] == "system"


@pytest.mark.asyncio
async def test_write_audit_respects_explicit_actor(_audit_db):
    """Covers the 276→282 partial branch — when ``actor`` is passed
    explicitly the wrapper must NOT fall into the
    state-introspection block."""
    audit = _audit_db

    class _FakeRequest:
        state = None  # no state at all; would crash the introspection

    # Passing an actor short-circuits the state walk (line 276 False).
    rid = await audit.write_audit(
        _FakeRequest(), "explicit_actor", "thing", "z",
        actor="ci-bot@test",
    )
    assert isinstance(rid, int)
    rows = await audit.query(limit=3)
    by_action = {r["action"]: r for r in rows}
    assert by_action["explicit_actor"]["actor"] == "ci-bot@test"


@pytest.mark.asyncio
async def test_log_sync_schedules_task_on_running_loop(_audit_db):
    """Covers ``log_sync``'s happy path (lines 184-189) — we're
    inside an async test so ``asyncio.get_running_loop`` succeeds,
    the coroutine is scheduled via ``create_task``, and we await a
    round-trip to confirm the row landed."""
    audit = _audit_db
    audit.log_sync("sync_ok", "thing", "a")
    # log_sync is fire-and-forget; give the scheduled task a tick
    # to drain before we query.
    import asyncio
    await asyncio.sleep(0.1)
    rows = await audit.query(limit=3)
    assert any(r["action"] == "sync_ok" for r in rows)


def test_log_sync_drops_silently_without_running_loop():
    """Covers the ``except RuntimeError`` arm of ``log_sync``. Called
    from a plain synchronous test (no asyncio loop running), the
    helper must log-and-return instead of crashing — that's the
    contract for sync callers like ``decision_engine.set_mode``."""
    from backend import audit
    # Must not raise; no loop to schedule on.
    audit.log_sync("no_loop", "thing", "x")


# ── CLI entry-point coverage (lines 363-420) ─────────────────────
#
# ``_cli_main`` is the ``python -m backend.audit verify|verify-all|tail``
# dispatcher that operators use during incident triage. It wraps
# everything in its own ``asyncio.run(...)``, which means calling it
# from inside pytest-asyncio's event loop crashes with "another
# operation in progress" on the shared pool. Tests exercise it via
# subprocess instead — slower but honest (covers the actual prod
# invocation shape) and avoids nested-loop pain. Coverage collection
# relies on the ``.coveragerc`` ``source = backend`` directive + the
# ``COVERAGE_PROCESS_START`` env so the subprocess writes its own
# ``.coverage.<pid>`` file that ``coverage combine`` merges.


def _run_audit_cli(argv: list[str]) -> tuple[int, str, str]:
    """Run ``python -m backend.audit <argv>`` as a subprocess,
    inheriting the OMNI_TEST_PG_URL env so the CLI's ``db.init`` no-op
    sees the same PG. Returns (returncode, stdout, stderr)."""
    import os
    import subprocess
    import sys
    env = os.environ.copy()
    # The CLI uses db.init() which is a no-op on PG (post-C.2), and
    # reaches for get_pool() via verify_chain / query. Point the
    # subprocess at the shared test PG.
    dsn = os.environ.get("OMNI_TEST_PG_URL", "").strip()
    if dsn:
        env["OMNISIGHT_DATABASE_URL"] = dsn
    # Coverage subprocess wiring: pytest-cov sets COVERAGE_PROCESS_START
    # already when --cov is active; we just propagate it. The
    # ``.coveragerc`` (here in pytest.ini's [coverage:run]) has
    # ``source = backend`` so the subprocess writes a .coverage.<pid>
    # that combine picks up.
    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    )
    # We need the CLI to boot db_pool — but the subprocess doesn't go
    # through the FastAPI lifespan. Instead, write a tiny bootstrap
    # wrapper that init_pool()s then calls _cli_main directly.
    bootstrap = (
        "import asyncio, sys\n"
        "from backend import db_pool, audit\n"
        "async def _boot():\n"
        "    await db_pool.init_pool(sys.argv[1], init=None)\n"
        "    try:\n"
        f"        rc = audit._cli_main({argv!r})\n"
        "        return rc\n"
        "    finally:\n"
        "        await db_pool.close_pool()\n"
        "# _cli_main already wraps in asyncio.run so we can't re-enter;\n"
        "# call _run inline via its captured coroutine instead.\n"
        "sys.exit(asyncio.run(_boot()))\n"
    )
    # Simpler path: just invoke _cli_main directly via -c, no pool
    # init — the CLI's ``await db.init()`` is a no-op and verify_*
    # reaches for get_pool() which will fail with "init_pool not
    # called". So we DO need the bootstrap.
    #
    # But _cli_main itself calls asyncio.run internally. We can't
    # nest — so patch the module: drop the asyncio.run wrapper and
    # expose the inner _run as async. Or call _cli_main's internals.
    # Simplest honest approach: let _cli_main run (it opens its own
    # loop), but set up the pool before via a monkey-patched db.init.
    bootstrap_v2 = (
        "import asyncio, os, sys\n"
        "from backend import audit, db, db_pool\n"
        "_orig_init = db.init\n"
        "async def _init_with_pool():\n"
        "    await _orig_init()\n"
        "    dsn = os.environ.get('OMNISIGHT_DATABASE_URL', '')\n"
        "    if dsn:\n"
        "        try:\n"
        "            await db_pool.init_pool(dsn, init=None)\n"
        "        except RuntimeError:\n"
        "            pass  # already initialised\n"
        "db.init = _init_with_pool\n"
        f"sys.exit(audit._cli_main({argv!r}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", bootstrap_v2],
        capture_output=True, text=True, env=env, cwd=repo_root,
        timeout=30,
    )
    return result.returncode, result.stdout, result.stderr


def test_cli_usage_on_bad_argv():
    """Covers the usage-print branch at lines 365-368. Any unknown
    verb exits 2 with the usage line on stderr."""
    rc, _out, err = _run_audit_cli(["bogus"])
    assert rc == 2
    assert "usage:" in err


@pytest.mark.asyncio
async def test_cli_verify_default_tenant(_audit_db):
    """Covers the ``verify`` arm without ``--tenant`` — sets
    ``t-default`` via context and reports OK on an empty chain."""
    rc, out, _err = _run_audit_cli(["verify"])
    assert rc == 0
    assert "audit chain" in out and "OK" in out


@pytest.mark.asyncio
async def test_cli_verify_explicit_tenant_ok_and_broken(
    _audit_db, pg_test_pool,
):
    """Covers the ``--tenant`` branch plus the broken-chain reporter
    (lines 376-395). First seed one tenant with a clean chain → OK;
    then tamper it → BROKEN with the row id on stderr."""
    audit = _audit_db
    from backend.db_context import set_tenant_id
    set_tenant_id("t-cli-vrf")
    rid = await audit.log(
        "cli_verify", "thing", "a",
        before={"v": 0}, after={"v": 1},
    )
    assert isinstance(rid, int)
    set_tenant_id(None)

    rc, out, _err = _run_audit_cli(["verify", "--tenant", "t-cli-vrf"])
    assert rc == 0
    assert "OK" in out

    # Tamper: rewrite the after_json so the hash no longer matches.
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "UPDATE audit_log SET after_json = '{\"v\":999}' "
            "WHERE id = $1",
            rid,
        )

    rc, _out, err = _run_audit_cli(["verify", "--tenant", "t-cli-vrf"])
    assert rc == 1
    assert "BROKEN" in err and str(rid) in err


def test_cli_verify_tenant_flag_missing_value():
    """Covers the ``--tenant requires a value`` branch (lines 380-382)
    — ``--tenant`` at the end of argv with nothing after it exits 2."""
    rc, _out, err = _run_audit_cli(["verify", "--tenant"])
    assert rc == 2
    assert "--tenant requires a value" in err


@pytest.mark.asyncio
async def test_cli_verify_all_empty(_audit_db):
    """Covers the ``verify-all`` → no-entries branch (lines 399-401)
    on an empty ``audit_log``."""
    rc, out, _err = _run_audit_cli(["verify-all"])
    assert rc == 0
    assert "no audit entries found" in out


@pytest.mark.asyncio
async def test_cli_verify_all_mixed_ok_and_broken(
    _audit_db, pg_test_pool,
):
    """Covers the ``verify-all`` iteration loop with both ok + broken
    arms (lines 402-409)."""
    audit = _audit_db
    from backend.db_context import set_tenant_id

    set_tenant_id("t-cli-va-ok")
    await audit.log("va_ok", "thing", "a")
    set_tenant_id("t-cli-va-bad")
    rid_bad = await audit.log("va_bad", "thing", "b")
    set_tenant_id(None)

    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "UPDATE audit_log SET after_json = '{\"forged\":true}' "
            "WHERE id = $1",
            rid_bad,
        )

    rc, out, err = _run_audit_cli(["verify-all"])
    assert rc == 1  # at least one tenant broken → exit 1
    assert "t-cli-va-ok" in out
    assert "t-cli-va-bad" in err
    assert "BROKEN" in err


@pytest.mark.asyncio
async def test_cli_tail_prints_rows(_audit_db):
    """Covers the ``tail`` arm (lines 411-416). Seeds a few rows then
    runs ``tail 5`` — output should include formatted audit lines."""
    audit = _audit_db
    for i in range(3):
        await audit.log(f"tail_{i}", "thing", f"id_{i}")
    rc, out, _err = _run_audit_cli(["tail", "5"])
    assert rc == 0
    assert any(f"tail_{i}" in out for i in range(3))


@pytest.mark.asyncio
async def test_cli_tail_default_n(_audit_db):
    """Covers the ``len(argv) > 1 else 20`` default (line 411)."""
    rc, _out, _err = _run_audit_cli(["tail"])
    # No rows seeded beyond the fixture's default state → still 0.
    assert rc == 0
