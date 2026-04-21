"""Phase-3-Runtime-v2 SP-1.3 — asyncpg.Pool wrapper.

This module owns the single process-global ``asyncpg.Pool`` that the
rest of the backend uses for all PG access. It replaced the original
single-connection-plus-asyncio-Lock architecture of the compat shim
(``backend.db_pg_compat.PgCompatConnection``) that was retired in
Phase-3 Step C.2 (2026-04-21).

Lifecycle contract
------------------
The FastAPI lifespan handler in ``backend.main`` calls
:func:`init_pool` on startup (before any request hits a router) and
:func:`close_pool` on shutdown (after all in-flight requests drain).

Every request-scoped handler that needs PG access depends on
:func:`get_conn`, which yields one ``asyncpg.Connection`` borrowed
from the pool for the lifetime of the request. The connection is
automatically released back to the pool when the handler returns or
raises — the ``async with pool.acquire()`` block inside
:func:`get_conn` guarantees the release even on exception paths.

Background tasks / SSE stream handlers / startup hooks that are NOT
part of a request lifecycle call :func:`get_pool` directly and use
``async with pool.acquire() as conn:`` to borrow a connection scoped
to the work they're about to do.

Connection-level defaults
-------------------------
Every connection is initialised (via the pool's ``init`` callback) with
session parameters that are safer than asyncpg's bare defaults:

* ``timezone = 'UTC'`` — all DB-side timestamps normalised
* ``statement_timeout = '30s'`` — kill runaway queries before user
  timeout cascades
* ``lock_timeout = '10s'`` — fail fast on lock contention rather than
  wedging the request
* ``idle_in_transaction_session_timeout = '60s'`` — PG kills the
  session if a tx is opened and never committed/rolled back, preventing
  indefinite holds on row locks by stuck workers

These are documented in the design doc
(``docs/phase-3-runtime-v2/01-design-decisions.md`` §2.3).

Pool sizing
-----------
Defaults: ``min_size=5, max_size=20``. Per worker. At 2 replicas × 2
workers × 20 = 80 peak connections system-wide, sized against
``max_connections=200`` on the primary (raised in SP-1.1 with 105
connections of headroom for ops/alembic/burst).

Callers that want to override (tests especially) pass explicit
``min_size`` / ``max_size`` to :func:`init_pool`.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator, Optional

import asyncpg

logger = logging.getLogger(__name__)


# ─── Module-global singleton ──────────────────────────────────────────
# One pool per Python process. asyncpg.Pool is safe to share across all
# coroutines in the same event loop (its internal queue serialises
# connection acquisition). We expose it as a private module variable
# rather than a class to match the style of `backend.db._db` — routers
# access the pool exclusively via the public helpers below.


_pool: Optional[asyncpg.Pool] = None


# ─── Default session-parameter init callback ──────────────────────────


async def _set_connection_defaults(conn: asyncpg.Connection) -> None:
    """Pool ``init`` callback — runs on every newly created connection.

    Sets the session-level guardrails documented in the module header.
    Any SET here fails-fast if the DB doesn't accept the parameter (e.g.
    an older PG without ``idle_in_transaction_session_timeout``), which
    is the behaviour we want — better to fail pool init than to run
    production with a silently-missing guardrail.
    """
    await conn.execute("SET timezone = 'UTC'")
    await conn.execute("SET statement_timeout = '30s'")
    await conn.execute("SET lock_timeout = '10s'")
    await conn.execute("SET idle_in_transaction_session_timeout = '60s'")


# ─── Lifecycle ────────────────────────────────────────────────────────


async def init_pool(
    dsn: str,
    *,
    min_size: int = 5,
    max_size: int = 20,
    statement_cache_size: int = 512,
    command_timeout: float = 30.0,
    max_inactive_connection_lifetime: float = 300.0,
    init: Optional[Any] = None,
) -> asyncpg.Pool:
    """Create the process-global pool.

    Raises :class:`RuntimeError` if a pool already exists (callers must
    explicitly ``close_pool()`` first). Returns the created pool so
    callers that want to inspect it (tests, health probes) have a
    handle without calling :func:`get_pool` immediately after.

    The ``init`` kwarg defaults to :func:`_set_connection_defaults` —
    tests that want to bypass session-parameter setup (e.g. to test
    the pool primitive without those SET commands) can pass
    ``init=None`` explicitly.

    Raises
    ------
    RuntimeError
        If a pool is already initialised. The caller must call
        :func:`close_pool` before creating a new one.
    asyncpg.PostgresError
        On any DB-side failure during pool creation or the init callback.
    """
    global _pool
    if _pool is not None:
        raise RuntimeError(
            "db_pool.init_pool called while a pool is already active. "
            "Call db_pool.close_pool() first if you intend to re-create "
            "the pool (e.g. switching DSN)."
        )

    init_cb = init if init is not None else _set_connection_defaults

    logger.info(
        "db_pool.init_pool: creating pool min=%d max=%d stmt_cache=%d "
        "cmd_timeout=%.1fs idle_lifetime=%.1fs",
        min_size, max_size, statement_cache_size,
        command_timeout, max_inactive_connection_lifetime,
    )

    pool = await asyncpg.create_pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        statement_cache_size=statement_cache_size,
        command_timeout=command_timeout,
        max_inactive_connection_lifetime=max_inactive_connection_lifetime,
        init=init_cb,
    )
    _pool = pool
    return pool


async def close_pool() -> None:
    """Close the process-global pool.

    Idempotent — calling twice is safe (second call is a no-op).
    Callers must have drained all in-flight borrows first; the
    underlying ``asyncpg.Pool.close()`` waits indefinitely for
    outstanding connections to be released, so it's the lifespan
    handler's responsibility to only call this after the app has
    stopped accepting new requests.
    """
    global _pool
    if _pool is None:
        return
    logger.info("db_pool.close_pool: closing pool")
    pool = _pool
    _pool = None  # null the global BEFORE awaiting close so concurrent
                  # callers of get_pool() see "not initialised" rather
                  # than a pool in teardown state.
    await pool.close()


def get_pool() -> asyncpg.Pool:
    """Return the global pool or raise if not initialised.

    Code paths that hit this before lifespan startup (or after shutdown)
    should treat the RuntimeError as a bug — the pool must be ready
    before any request enters a router that depends on it, and the
    lifespan handler is responsible for that ordering.

    Raises
    ------
    RuntimeError
        If :func:`init_pool` has not been called yet, or
        :func:`close_pool` has already been called.
    """
    if _pool is None:
        raise RuntimeError(
            "db_pool.get_pool called before init_pool — check lifespan "
            "ordering in backend/main.py"
        )
    return _pool


# ─── FastAPI dependency ──────────────────────────────────────────────


async def get_conn() -> AsyncGenerator[asyncpg.Connection, None]:
    """FastAPI dependency: yield a request-scoped connection.

    Usage in a router:

        from fastapi import Depends
        from backend.db_pool import get_conn
        import asyncpg

        @router.get("/things")
        async def list_things(
            conn: asyncpg.Connection = Depends(get_conn),
        ):
            return await conn.fetch("SELECT ...")

    The connection is held for the duration of the handler (including
    any response streaming) and released to the pool on return or
    exception. Tests that want to bypass the dependency can override
    it via FastAPI's dependency_overrides machinery.

    Note: asyncpg auto-commits each statement by default when no
    explicit transaction is open. Multi-statement atomicity requires
    ``async with conn.transaction(): ...`` inside the handler.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        yield conn


# ─── Introspection / observability ───────────────────────────────────


def get_pool_stats() -> dict[str, Any]:
    """Return pool metrics suitable for /readyz or Prometheus export.

    If the pool isn't initialised, returns a sentinel shape rather
    than raising — /readyz can reasonably call this during startup
    before the pool exists.
    """
    if _pool is None:
        return {
            "initialised": False,
            "min_size": None,
            "max_size": None,
            "size": None,
            "free_size": None,
            "used_size": None,
        }
    # asyncpg.Pool exposes these as methods (not properties in older
    # versions). Use getattr to stay robust across 0.29 / 0.30+.
    min_size = _pool.get_min_size()
    max_size = _pool.get_max_size()
    size = _pool.get_size()
    free_size = _pool.get_idle_size()
    return {
        "initialised": True,
        "min_size": min_size,
        "max_size": max_size,
        "size": size,
        "free_size": free_size,
        "used_size": size - free_size,
    }


# ─── Test support ────────────────────────────────────────────────────


def _reset_for_tests() -> None:
    """Force-reset the module-global pool reference WITHOUT closing.

    Intended ONLY for test fixtures that want to re-create the pool
    inside a single pytest process. NEVER call this from production
    code — it leaks the existing pool's connections if one is open.

    Tests that borrow connections and want clean teardown should use
    the regular ``close_pool() + init_pool()`` flow; this escape hatch
    exists for unit tests of ``init_pool`` itself that need to exercise
    the "already initialised" branch without setting up a real PG.
    """
    global _pool
    _pool = None
