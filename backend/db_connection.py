"""G4 #2 (HA-04) — Async connection factory for the DATABASE_URL abstraction.

This module turns a parsed :class:`backend.db_url.DatabaseURL` into an
open async connection, picking the right driver at runtime:

* SQLite URLs → :mod:`aiosqlite`   (current default, zero install cost)
* Postgres URLs → :mod:`asyncpg`   (HA-04 primary/replica target)

Design goals:

* **Lazy imports.** We only import ``asyncpg`` when a Postgres URL is
  actually opened. This keeps the SQLite CI track from needing asyncpg
  installed; the Postgres CI matrix job installs it explicitly.
* **Uniform interface.** Both backends implement
  :class:`AsyncDBConnection`, a tiny protocol that covers just the
  operations the rest of the system needs (``execute``,
  ``executescript``, ``fetchone``, ``fetchall``, ``commit``, ``close``).
  This is intentionally narrower than aiosqlite's full surface — we're
  not trying to be a full ORM, we're trying to paper over the minimum
  set of calls so that db.py's existing flow can be ported table-by-table
  without a big-bang rewrite.
* **No global state.** Opening a connection returns an object; the caller
  owns its lifecycle. ``backend.db`` keeps a module-level singleton as
  before for backwards compatibility, but the abstraction itself is
  purely functional.

What this module does NOT do:

* It does not rewrite SQL dialects at runtime. Migrations use
  :mod:`backend.alembic_pg_compat` for that; application code should be
  written in a cross-dialect subset (see ``scripts/scan_sqlite_isms.py``
  for the lint that enforces it).
* It does not pool connections. For the single-process FastAPI app the
  legacy aiosqlite singleton is sufficient; Postgres pooling will be
  added in a later subtask once the migration script lands.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from backend.db_url import DatabaseURL, parse, resolve_from_env

logger = logging.getLogger(__name__)


# ── Uniform async connection protocol ─────────────────────────────────────


@runtime_checkable
class AsyncDBConnection(Protocol):
    """The minimum surface every backend must implement.

    We keep this deliberately small. Anything richer (streaming
    cursors, LISTEN/NOTIFY, advisory locks) belongs in a backend-specific
    helper, not this protocol.
    """

    dialect: str   # "sqlite" | "postgresql"
    driver: str    # "aiosqlite" | "asyncpg"

    async def execute(self, sql: str, params: tuple | list | None = ...) -> Any: ...
    async def executescript(self, sql: str) -> None: ...
    async def fetchone(self, sql: str, params: tuple | list | None = ...) -> Any: ...
    async def fetchall(self, sql: str, params: tuple | list | None = ...) -> list[Any]: ...
    async def commit(self) -> None: ...
    async def close(self) -> None: ...


# ── aiosqlite-backed implementation ──────────────────────────────────────


class _SqliteAsyncConnection:
    """Thin wrapper over aiosqlite.Connection that matches the protocol."""

    dialect = "sqlite"
    driver = "aiosqlite"

    def __init__(self, conn: Any) -> None:  # aiosqlite.Connection
        self._conn = conn

    @classmethod
    async def open(cls, url: DatabaseURL) -> "_SqliteAsyncConnection":
        import aiosqlite  # lazy

        if url.is_memory_sqlite:
            target = ":memory:"
        else:
            path = url.sqlite_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            target = str(path)
        conn = await aiosqlite.connect(target)
        conn.row_factory = aiosqlite.Row
        return cls(conn)

    # ── protocol methods ──
    async def execute(self, sql: str, params: tuple | list | None = None) -> Any:
        if params is None:
            return await self._conn.execute(sql)
        return await self._conn.execute(sql, params)

    async def executescript(self, sql: str) -> None:
        await self._conn.executescript(sql)

    async def fetchone(self, sql: str, params: tuple | list | None = None) -> Any:
        cur = await self.execute(sql, params)
        try:
            return await cur.fetchone()
        finally:
            await cur.close()

    async def fetchall(self, sql: str, params: tuple | list | None = None) -> list[Any]:
        cur = await self.execute(sql, params)
        try:
            return list(await cur.fetchall())
        finally:
            await cur.close()

    async def commit(self) -> None:
        await self._conn.commit()

    async def close(self) -> None:
        await self._conn.close()

    # Expose the raw aiosqlite connection for legacy callers during the
    # migration window. This is intentionally underscore-prefixed to
    # discourage new call sites from reaching through — new code should
    # use the protocol methods.
    @property
    def _raw(self) -> Any:
        return self._conn


# ── asyncpg-backed implementation ────────────────────────────────────────


class _PostgresAsyncConnection:
    """Thin wrapper over asyncpg.Connection that matches the protocol.

    Translates the SQLite-flavoured ``?`` placeholders many call sites
    emit into asyncpg's ``$1, $2, ...`` form on the fly. This mirrors
    what :mod:`backend.alembic_pg_compat` does for migrations but is
    cheaper because runtime SQL doesn't contain string literals with
    embedded quotes as frequently — we can use a simple state-machine
    replacement instead of regex.
    """

    dialect = "postgresql"
    driver = "asyncpg"

    def __init__(self, conn: Any) -> None:  # asyncpg.Connection
        self._conn = conn
        # asyncpg has no implicit transaction; to match aiosqlite's
        # "commit when I say" ergonomics we open a transaction lazily.
        self._tx: Any = None

    @classmethod
    async def open(cls, url: DatabaseURL) -> "_PostgresAsyncConnection":
        try:
            import asyncpg  # lazy
        except ImportError as exc:  # pragma: no cover — env-specific
            raise RuntimeError(
                "asyncpg is not installed. Install with "
                "`pip install asyncpg` or use the sqlite:// DATABASE_URL."
            ) from exc
        kw = url.asyncpg_connect_kwargs()
        conn = await asyncpg.connect(**kw)
        return cls(conn)

    # ── placeholder translation ──
    @staticmethod
    def _qmark_to_dollar(sql: str) -> str:
        """Convert SQLite ``?`` placeholders to asyncpg ``$N``.

        String-literal aware (single-quoted, with SQL doubled-quote
        escape). No regex → predictable, no catastrophic backtracking.
        """
        out: list[str] = []
        i = 0
        n = 0
        in_str = False
        while i < len(sql):
            c = sql[i]
            if in_str:
                out.append(c)
                if c == "'":
                    # Doubled '' escapes a quote; peek ahead.
                    if i + 1 < len(sql) and sql[i + 1] == "'":
                        out.append("'")
                        i += 2
                        continue
                    in_str = False
                i += 1
                continue
            if c == "'":
                in_str = True
                out.append(c)
                i += 1
                continue
            if c == "?":
                n += 1
                out.append(f"${n}")
                i += 1
                continue
            out.append(c)
            i += 1
        return "".join(out)

    # ── protocol methods ──
    async def _ensure_tx(self) -> None:
        if self._tx is None:
            self._tx = self._conn.transaction()
            await self._tx.start()

    async def execute(self, sql: str, params: tuple | list | None = None) -> Any:
        await self._ensure_tx()
        pg_sql = self._qmark_to_dollar(sql)
        args = tuple(params) if params else ()
        return await self._conn.execute(pg_sql, *args)

    async def executescript(self, sql: str) -> None:
        # asyncpg's `execute()` handles multi-statement strings when no
        # parameters are bound. No implicit transaction so callers can
        # issue BEGIN/COMMIT themselves if they need to.
        await self._conn.execute(sql)

    async def fetchone(self, sql: str, params: tuple | list | None = None) -> Any:
        await self._ensure_tx()
        pg_sql = self._qmark_to_dollar(sql)
        args = tuple(params) if params else ()
        return await self._conn.fetchrow(pg_sql, *args)

    async def fetchall(self, sql: str, params: tuple | list | None = None) -> list[Any]:
        await self._ensure_tx()
        pg_sql = self._qmark_to_dollar(sql)
        args = tuple(params) if params else ()
        rows = await self._conn.fetch(pg_sql, *args)
        return list(rows)

    async def commit(self) -> None:
        if self._tx is not None:
            await self._tx.commit()
            self._tx = None

    async def close(self) -> None:
        # Best-effort: commit any in-flight tx so callers don't lose
        # writes that relied on aiosqlite's auto-commit-on-close.
        if self._tx is not None:
            try:
                await self._tx.commit()
            except Exception as exc:
                logger.warning("asyncpg tx commit on close failed: %s", exc)
            self._tx = None
        await self._conn.close()

    @property
    def _raw(self) -> Any:
        return self._conn


# ── Factory ──────────────────────────────────────────────────────────────


async def open_connection(
    url: DatabaseURL | str | None = None,
    *,
    default_sqlite_path: Path | str | None = None,
) -> AsyncDBConnection:
    """Open an async connection to ``url``.

    Accepts a :class:`DatabaseURL`, a raw string (parsed first), or
    ``None`` (resolves from the environment). Lazy-imports the driver
    module so callers that only ever use SQLite don't need asyncpg in
    their lockfile.
    """
    if url is None:
        resolved = resolve_from_env(default_sqlite_path=default_sqlite_path)
    elif isinstance(url, str):
        resolved = parse(url)
    else:
        resolved = url

    if resolved.is_sqlite:
        return await _SqliteAsyncConnection.open(resolved)
    if resolved.is_postgres:
        if resolved.driver != "asyncpg":
            raise RuntimeError(
                f"Postgres URL uses driver {resolved.driver!r}, but "
                "backend.db_connection only supports the asyncpg driver "
                "for runtime connections. Use postgresql+asyncpg:// "
                "(psycopg2 URLs are accepted by Alembic only)."
            )
        return await _PostgresAsyncConnection.open(resolved)

    raise RuntimeError(f"Unsupported dialect {resolved.dialect!r}")


__all__ = [
    "AsyncDBConnection",
    "open_connection",
]
