"""Shared fixtures for OmniSight backend tests.

A8/C9 note: several tests import modules that keep state in
module-level globals (decision_engine._pending, decision_rules._RULES,
pipeline._active_pipeline, etc.). The reset hooks (`_reset_for_tests`,
`clear`) exist so this file can put those singletons back in a known
state between runs — they are NOT a supported production API. A
future refactor pass should dependency-inject these stores so
pytest-xdist can run tests in parallel safely; until then the serial
runner plus these reset hooks is the contract.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

# ─── 2026-04-21: .env.test isolation (before ANY backend import) ─────
#
# Pydantic-settings' ``.env`` loader bled developer workstation
# credentials into pytest — a workstation with real
# ``OMNISIGHT_ANTHROPIC_API_KEY`` in ``.env`` made "fresh install"
# assertions (``_llm_provider_is_configured() == False``) fail
# non-deterministically based on where the test ran. Fix: point
# pydantic-settings at ``.env.test`` (safe defaults only) AND unset
# the live env vars the real ``.env`` may have already pushed into
# os.environ before pytest started.
#
# This block MUST run before any ``from backend import ...`` in this
# file or in test-module imports — Settings is instantiated at
# backend.config class-definition time, which fires on first import.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
os.environ["OMNISIGHT_DOTENV_FILE"] = str(_REPO_ROOT / ".env.test")
for _leaky in (
    # LLM provider + API keys — real values in .env break
    # _llm_provider_is_configured() / wizard "not configured" gates.
    "OMNISIGHT_LLM_PROVIDER",
    "OMNISIGHT_ANTHROPIC_API_KEY",
    "OMNISIGHT_GOOGLE_API_KEY",
    "OMNISIGHT_OPENAI_API_KEY",
    "OMNISIGHT_XAI_API_KEY",
    "OMNISIGHT_GROQ_API_KEY",
    "OMNISIGHT_DEEPSEEK_API_KEY",
    "OMNISIGHT_TOGETHER_API_KEY",
    "OMNISIGHT_OPENROUTER_API_KEY",
    # CF tunnel — real token in .env makes wizard think install is done.
    "OMNISIGHT_CLOUDFLARE_TUNNEL_TOKEN",
    "OMNISIGHT_PUBLIC_HOSTNAME",
    "OMNISIGHT_CF_TUNNEL_SKIP",
    # Frontend origin — real prod hostname breaks health-check URL stubs.
    "OMNISIGHT_FRONTEND_ORIGIN",
    "OMNISIGHT_NEXT_PUBLIC_API_URL",
    # Strict-mode gates — real admin pw / bearer leak through.
    "OMNISIGHT_ADMIN_PASSWORD",
    "OMNISIGHT_DECISION_BEARER",
):
    os.environ.pop(_leaky, None)

# Ensure workspace root points to a temp directory for all tool tests
_tmp = tempfile.mkdtemp(prefix="omnisight_test_")
os.environ["OMNISIGHT_WORKSPACE"] = _tmp


def pytest_sessionfinish(session, exitstatus):
    """Clean up the module-level temp directory after all tests."""
    shutil.rmtree(_tmp, ignore_errors=True)


# ─── Y-prep.1 #287 — ``p0`` CI gate: cannot be skipped ──────────────────
#
# The ``p0`` marker (registered in ``backend/pytest.ini``) tags a small
# set of production-critical contract tests — currently the three
# Gerrit dispatcher routing tests in
# ``backend/tests/test_webhooks.py::TestGerritEventRouting``. The policy
# bullet says "不准 skip" (must not be skipped). Two enforcement hooks:
#
# 1. ``pytest_collection_modifyitems`` — fails collection if a p0 test
#    is declared with a static skip marker. Covers:
#      - ``@pytest.mark.skip`` (unconditional skip — always a mistake on
#        a CI gate),
#      - ``@pytest.mark.skipif(True, ...)`` or ``@pytest.mark.skipif(
#        constant_truthy, ...)`` (condition that is provably true at
#        collection time).
#    ``@pytest.mark.skipif(False, ...)`` is benign and stays allowed —
#    pytest will execute the test normally; we only reject skips that
#    actually skip.
#
# 2. ``pytest_runtest_makereport`` — if a p0 test triggers a runtime
#    ``pytest.skip()`` / ``pytest.importorskip()`` during the ``call``
#    phase (i.e. the body skips instead of running), converts the
#    ``Skipped`` outcome into ``Failed``. Setup / teardown skips are
#    left alone — that is usually a legitimate fixture-level environment
#    gate (e.g. ``pg_test_dsn`` skipping when ``OMNI_TEST_PG_URL`` is
#    unset), and retrofitting a "no-skip" policy onto the setup path
#    would require a broader refactor than this CI-gate bullet asks
#    for. The authoritative signal is what the test body decides once
#    pytest actually starts running it.
#
# The policy is intentionally conservative — if somebody really, truly
# needs to silence a p0 test for an emergency, removing the marker is
# a single-line change that is visible in the diff and forces a
# discussion.


def _p0_marker_selects_skip(marker) -> tuple[bool, str]:
    """Return ``(should_reject, reason)`` for a skip-family marker on a
    p0 test.

    ``@pytest.mark.skip`` is always rejected.
    ``@pytest.mark.skipif(cond, ...)`` is rejected only if ``cond`` is
    a truthy literal at collection time. ``skipif(False, ...)`` /
    ``skipif(0, ...)`` / ``skipif(None, ...)`` are treated as no-ops and
    left alone — they do not actually cause a skip.
    """
    if marker.name == "skip":
        return True, "@pytest.mark.skip"
    if marker.name == "skipif":
        # skipif's first positional arg is the condition. Runtime-evaluated
        # expressions (``skipif(platform.system() == 'Windows', ...)``)
        # do reach here as whatever the expression evaluated to at
        # collection time — pytest has already resolved it before this
        # hook runs. A truthy value means the test would be skipped;
        # that is the case we reject.
        if marker.args and marker.args[0]:
            reason = marker.kwargs.get("reason") or repr(marker.args[0])
            return True, f"@pytest.mark.skipif({reason!r})"
        return False, ""
    return False, ""


def pytest_collection_modifyitems(config, items):
    """Fail collection if a ``p0`` test carries a static skip marker.

    Y-prep.1 #287 CI gate — see the block comment at the top of this
    section for the full policy rationale.
    """
    import pytest

    offenders = []
    for item in items:
        if item.get_closest_marker("p0") is None:
            continue
        for marker in item.iter_markers():
            rejected, reason = _p0_marker_selects_skip(marker)
            if rejected:
                offenders.append(f"{item.nodeid} ← {reason}")
                break
    if offenders:
        # Use UsageError so pytest exits with code 4 (usage error) not 1
        # (test failure) — the signal to CI is "test config broken", not
        # "code broken". The message lists every offender so the author
        # doesn't have to re-run to find the next one.
        raise pytest.UsageError(
            "p0 CI-gate violation: the following tests are marked "
            "``@pytest.mark.p0`` but ALSO carry a skip directive. "
            "p0 tests must not be skipped (Y-prep.1 #287). Remove "
            "the skip decorator, or remove the p0 marker if the test "
            "truly belongs in a different tier.\n  "
            + "\n  ".join(offenders)
        )


try:
    import pytest as _pytest

    @_pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_makereport(item, call):
        """Convert a runtime ``pytest.skip()`` in a p0 test's body into a
        failure.

        Only intercepts the ``call`` phase — a setup-phase skip (e.g.
        ``pg_test_dsn`` fixture skipping when the test DB is unavailable)
        is a legitimate environment gate and is left alone. The body
        skip is the case we care about: once the test is actually
        running, a ``pytest.skip("todo")`` is a silent CI-gate hole.
        """
        outcome = yield
        report = outcome.get_result()
        if report.when != "call":
            return
        if item.get_closest_marker("p0") is None:
            return
        if not report.skipped:
            return
        # Flip Skipped → Failed and surface a scannable reason. Keep the
        # original longrepr around as a breadcrumb — usually that is the
        # ``pytest.skip()`` message.
        original_longrepr = report.longrepr
        report.outcome = "failed"
        report.longrepr = (
            f"p0 CI-gate violation: {item.nodeid} called pytest.skip() "
            "during the test body. p0 tests must not be skipped "
            "(Y-prep.1 #287). Original skip reason: "
            f"{original_longrepr!r}"
        )
except ImportError:  # pragma: no cover — pytest is always available in tests
    pass


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Provide a fresh temporary workspace and activate it for tools."""
    from backend.agents.tools import set_active_workspace

    set_active_workspace(tmp_path)
    yield tmp_path
    set_active_workspace(None)


@pytest.fixture()
def sample_files(workspace: Path) -> Path:
    """Create a small tree of sample files inside the workspace."""
    (workspace / "src").mkdir()
    (workspace / "src" / "main.c").write_text('#include "driver.h"\nint main() { return 0; }\n')
    (workspace / "src" / "driver.h").write_text("#pragma once\nvoid init_sensor(void);\n")
    (workspace / "README.md").write_text("# Test project\n")
    (workspace / "config.yaml").write_text("sensor:\n  model: IMX335\n  bus: i2c\n")
    return workspace


@pytest.fixture()
async def client(tmp_path, monkeypatch):
    """Provide an async HTTP test client against the FastAPI app.

    Each test gets a fresh per-test sqlite file so state never leaks
    across tests. Previously every test hit the real `data/omnisight.db`
    and rows accumulated forever — the audit flagged this as the root
    cause of `test_list_plan_chain` seeing 8+ plans when it expected 2.

    L1 #2 note: the bootstrap gate middleware would normally 307 every
    non-exempt request on a fresh install (nothing configured). For the
    shared client fixture we pin the gate to "finalized" so existing
    tests don't suddenly have to care about bootstrap state. Tests that
    explicitly exercise the gate reset the cache themselves.
    """
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", str(db_path))
    # Re-resolve path on the module (loaded at import time from the real
    # data/ dir) so `init()` opens the fresh tmp file.
    from backend import config as _cfg
    _cfg.settings.database_path = str(db_path)
    from backend import db
    db._DB_PATH = db._resolve_db_path()

    from backend.main import app
    from httpx import ASGITransport, AsyncClient
    from backend import bootstrap as _boot

    # Pin bootstrap to finalized so non-gate tests see a normal app.
    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )
    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    await db.init()

    # Phase-3-Runtime-v2 SP-3.1/3.2/3.4 (2026-04-20): routes and worker
    # paths that need PG access both go through the module-global
    # ``db_pool``. SP-3.4 moved pool ownership to the ``pg_test_pool``
    # fixture (below) so a single code path initialises it; we only
    # need to init here for tests that use ``client`` WITHOUT
    # depending on pg_test_pool. ``_reset_for_tests()`` + idempotent
    # init covers both paths without double-init errors.
    _dsn = _omni_test_pg_dsn_normalised()
    _pool_inited_by_client = False
    if _dsn:
        from backend import db_pool as _db_pool
        if _db_pool._pool is None:
            await _db_pool.init_pool(
                _dsn, min_size=1, max_size=3, command_timeout=10.0,
                init=None,
            )
            _pool_inited_by_client = True

    # 2026-04-21: TRUNCATE bootstrap_state between tests that share
    # the shared ``client`` fixture. Pre-fix, a test that recorded
    # steps leaked into the next test's "fresh install" probe —
    # missing_required_steps() saw the prior test's rows and
    # auto-backfilled, which made finalize succeed instead of the
    # expected 409. Scoped narrow to bootstrap_state because the
    # other shared tables either have their own per-test TRUNCATE
    # via pg_test_conn / pg_test_pool flow or are intentionally
    # shared (tenants seed row).
    if _dsn:
        from backend import db_pool as _db_pool
        async with _db_pool.get_pool().acquire() as _clean_conn:
            await _clean_conn.execute(
                "TRUNCATE bootstrap_state RESTART IDENTITY CASCADE"
            )

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        await db.close()
        _boot._gate_cache_reset()
        if _dsn:
            from backend import db_pool as _db_pool
            async with _db_pool.get_pool().acquire() as _clean_conn:
                await _clean_conn.execute(
                    "TRUNCATE bootstrap_state RESTART IDENTITY CASCADE"
                )
        if _pool_inited_by_client:
            from backend import db_pool as _db_pool
            await _db_pool.close_pool()


@pytest.fixture(autouse=True)
def _reset_bootstrap_gate_between_tests():
    """Reset the bootstrap gate cache between every test.

    Without this, a test that sets _gate_cache["finalized"] = True
    (e.g. via the shared `client` fixture) leaks that state into
    subsequent tests — causing flaky failures where bootstrap_gate
    tests expect the gate to be "red" but find it "green" due to
    a prior test's leftover cache. The in-process cache has no TTL
    short enough to prevent this in a fast test run.
    """
    from backend import bootstrap as _boot
    _boot._gate_cache_reset()
    yield
    _boot._gate_cache_reset()


# ─── Phase-3-Runtime-v2 SP-1.2 — PostgreSQL test fixtures ───────────────
#
# These fixtures back the asyncpg-native test suite we'll build out in
# Epics 3-6. Every test that needs a real PG connection uses `pg_test_conn`
# (savepoint-wrapped, auto-rollback on exit), or `pg_test_pool` if it
# specifically needs to exercise pool semantics.
#
# Contract:
#   - Tests that use these fixtures require `OMNI_TEST_PG_URL` to be set.
#     If unset, the fixture SKIPS the test (not fails) so CI runs without
#     a PG service still pass the non-PG suite.
#   - Schema is brought up to alembic HEAD once per test session. Tests
#     can freely INSERT/UPDATE/DELETE; the savepoint in `pg_test_conn`
#     rolls back on teardown so no inter-test bleed.
#   - Readers who want the raw DSN can depend on `pg_test_dsn` directly.
#
# Production gap this plugs:
#   Before SP-1.2, the project had `OMNI_TEST_PG_URL` as a convention but
#   no standardised pool+tx-scoped fixture — every PG-aware test wrote
#   its own psycopg2 bootstrapping boilerplate. This fixture set is the
#   canonical entry point from SP-1.2 onward.


def _omni_test_pg_dsn_normalised() -> str:
    """Return `OMNI_TEST_PG_URL` as a libpq DSN (no driver suffix).

    asyncpg refuses SQLAlchemy-style `postgresql+psycopg2://` or
    `postgresql+asyncpg://` — it wants plain `postgresql://`. Existing
    tests (``test_alembic_pg_live_upgrade.py``) set the env in the
    SQLAlchemy form for alembic's benefit, so we normalise here for
    asyncpg callers.
    """
    raw = os.environ.get("OMNI_TEST_PG_URL", "").strip()
    if not raw:
        return ""
    for prefix in ("postgresql+psycopg2://", "postgresql+asyncpg://"):
        if raw.startswith(prefix):
            return "postgresql://" + raw[len(prefix):]
    return raw


@pytest.fixture(scope="session")
def pg_test_dsn() -> str:
    """Session-scoped: the normalised libpq DSN, or skip the test.

    Any async fixture that borrows from asyncpg should depend on this
    rather than reading the env directly — keeps the skip behaviour
    consistent across the suite.
    """
    dsn = _omni_test_pg_dsn_normalised()
    if not dsn:
        pytest.skip(
            "OMNI_TEST_PG_URL not set — PG-backed test skipped. "
            "See backend/tests/README.md for how to start the test "
            "PG container."
        )
    return dsn


@pytest.fixture(scope="session")
def pg_test_alembic_upgraded(pg_test_dsn: str) -> str:
    """Session-scoped: run ``alembic upgrade head`` once so every
    PG-backed test sees a HEAD schema.

    Returns the same DSN (for chaining). Idempotent — safe to re-run
    against an already-upgraded DB (no-op per alembic).

    Subprocess invocation mirrors ``test_alembic_pg_live_upgrade.py`` to
    avoid importing backend.config at collection time (which has its
    own env-var drift issues).
    """
    import subprocess
    from pathlib import Path

    sqlalchemy_url = pg_test_dsn.replace("postgresql://", "postgresql+psycopg2://", 1)
    env = os.environ.copy()
    env["SQLALCHEMY_URL"] = sqlalchemy_url
    env["OMNISIGHT_SKIP_FS_MIGRATIONS"] = "1"

    # Two overlapping stdlib-shadow hazards we defend against here:
    #
    # (1) `PYTHONPATH=.` in the pytest parent inherits into the child,
    #     which puts the repo root on sys.path. The repo has a W0
    #     `./platform.py` module that shadows stdlib `platform`; any
    #     transitive `import platform` (uuid, sqlalchemy util, etc.)
    #     then raises AttributeError on `.system()` / `.python_
    #     implementation()`. Dropping PYTHONPATH breaks this chain.
    #
    # (2) `python -m alembic` sets sys.path[0] = '' (cwd), which with
    #     cwd=backend/ surfaces ANOTHER copy of `platform.py` — the real
    #     project module at backend/platform.py that legitimately lives
    #     there as `from backend import platform`. Running via the
    #     `alembic` console-script binary instead uses its shebang's
    #     sys.path (no cwd injection), side-stepping the shadow.
    #
    # These are pre-existing project hazards — migration-v2 just happens
    # to be the first test that invokes alembic from a pytest subprocess
    # and thus the first to surface them.
    env.pop("PYTHONPATH", None)

    backend_dir = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=backend_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        # Truncate both streams so the skip reason stays scannable; the
        # full output goes to pytest's captured log.
        pytest.skip(
            f"alembic upgrade head failed against OMNI_TEST_PG_URL: "
            f"exit={result.returncode} "
            f"stdout={result.stdout[-400:]!r} "
            f"stderr={result.stderr[-800:]!r}"
        )
    return pg_test_dsn


# ─── Async fixtures ────────────────────────────────────────────────
# These use `pytest_asyncio.fixture` (not `pytest.fixture`) so
# pytest-asyncio drives the event loop. asyncio_default_fixture_loop_scope
# is `function` in pytest.ini, which matches the default scope below.
#
# We intentionally keep the pool fixture function-scoped: pool creation
# is ~20 ms on PG 16, which is cheaper than debugging cross-test event-loop
# contamination. If profile shows this as a hotspot later, we can move to
# module or session scope by bumping asyncio_default_fixture_loop_scope.


try:
    import pytest_asyncio
    import asyncpg
    _ASYNCPG_AVAILABLE = True
except ImportError:  # pragma: no cover — asyncpg is required in prod
    _ASYNCPG_AVAILABLE = False


if _ASYNCPG_AVAILABLE:

    @pytest_asyncio.fixture
    async def pg_test_pool(pg_test_alembic_upgraded: str):
        """Function-scoped asyncpg pool against the test DB.

        Small pool (min=1, max=5) — tests exercise pool semantics at this
        scale; we're not load-testing here. If a test needs higher
        concurrency, it can override by creating its own pool inline.

        Phase-3-Runtime-v2 SP-3.4 (2026-04-20): the pool is also
        installed as the module-global via ``db_pool.init_pool`` so
        polymorphic worker helpers (notifications.notify,
        handoff.save_handoff, routers/{tasks,agents}._persist etc.)
        that borrow a conn via ``get_pool().acquire()`` when called
        without an explicit conn see the same pool this fixture
        yields. Without this, worker-path code hit during a test that
        uses pg_test_pool (but not the client fixture) would raise
        ``RuntimeError: db_pool.get_pool called before init_pool``.
        """
        from backend import db_pool as _db_pool
        # Defensive reset in case a prior crashed test skipped
        # close_pool() teardown.
        _db_pool._reset_for_tests()
        pool = await _db_pool.init_pool(
            pg_test_alembic_upgraded,
            min_size=1,
            max_size=5,
            command_timeout=10.0,
            statement_cache_size=256,
            init=None,  # skip connection-level SET commands — they're
                        # a production-safety concern, not a correctness
                        # one, and skipping shaves test setup time.
        )
        try:
            yield pool
        finally:
            await _db_pool.close_pool()


    @pytest_asyncio.fixture
    async def pg_test_conn(pg_test_pool):
        """Borrow a connection wrapped in an outer transaction; roll back
        on teardown so tests never pollute each other.

        Usage:
            async def test_something(pg_test_conn):
                await pg_test_conn.execute("INSERT INTO t VALUES (1)")
                # ... row is visible inside this test
                # ... but gone after the fixture teardown rolls back

        Nested transactions inside the test body use savepoints
        automatically (asyncpg detects outer tx). This is the canonical
        isolation mechanism for the v2 test suite.
        """
        async with pg_test_pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            try:
                # Phase-3-Runtime-v2 SP-3.2 (2026-04-20): TRUNCATE the
                # ported-domain tables inside the outer tx so each test
                # starts from an empty slate regardless of committed
                # pollution from prior ``pg_test_pool`` (non-tx) tests
                # or crashed fixtures. The TRUNCATE itself is part of
                # the savepoint and rolls back on teardown, so any
                # pre-existing committed rows come back intact.
                # Phase-3-Runtime-v2 ported tables. Epic 3 covered
                # agents/tasks/task_comments/handoffs/notifications/
                # token_usage/artifacts/npi_state/simulations/
                # debug_findings/event_log/decision_rules/
                # episodic_memory; Epic 4 covers ``audit_log``
                # (SP-4.1), ``users`` (SP-4.2), ``sessions`` (SP-4.3),
                # the password-flow tables (SP-4.4 —
                # ``password_history`` is cleared via CASCADE from
                # ``users``, no explicit TRUNCATE needed), and
                # ``tenant_secrets`` (SP-4.6). CASCADE handles the
                # user-referencing FKs.
                await conn.execute(
                    "TRUNCATE agents, tasks, task_comments, handoffs, "
                    "notifications, token_usage, artifacts, npi_state, "
                    "simulations, debug_findings, event_log, "
                    "decision_rules, episodic_memory, audit_log, "
                    "users, sessions, tenant_secrets "
                    "RESTART IDENTITY CASCADE"
                )
                yield conn
            finally:
                await tx.rollback()
