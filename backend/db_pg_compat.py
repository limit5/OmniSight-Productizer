"""Phase-3 Runtime (2026-04-20) — aiosqlite-compatible wrapper over asyncpg.

Why this module exists
──────────────────────
``backend/db.py`` is 1592 lines of aiosqlite-native code written against
the ``aiosqlite.Connection`` surface:

  * ``async with conn.execute(sql, params) as cur:``
  * ``cur.fetchone() / cur.fetchall()``
  * ``cur.rowcount``
  * ``row[0]`` positional indexing + ``row["col"]`` dict indexing
  * ``conn.commit()``

Porting every call site (~80 of them) to the neutral
:class:`backend.db_connection.AsyncDBConnection` protocol would be a
multi-session effort. This wrapper papers over the gap: it speaks the
aiosqlite surface ``db.py`` expects, but the bytes under it are asyncpg
talking to PG.

Scope-wise, the alternative (full port) is better long-term — the
neutral protocol makes future driver swaps trivial. But the constraint
this session is "complete Phase-3 cutover end-to-end in one maintenance
window", and the compat wrapper achieves that in ~300 LOC with zero
changes to the 80 call sites in ``db.py`` and ~10 call sites in other
modules that reach through ``db._conn()`` (``tenant_secrets``,
``audit``, ``bootstrap``, etc.).

Runtime translations performed
──────────────────────────────
1. **Placeholders.** SQLite's ``?`` → asyncpg's ``$N`` via a
   string-literal-aware state machine (no regex, no catastrophic
   backtracking). Mirrors the logic in
   :mod:`backend.alembic_pg_compat.translate_params_qmark_to_pyformat`.

2. **SQLite-specific SQL.** Four SQLite-isms survive into runtime code
   paths (application-level INSERTs, not just migrations):

     - ``INSERT OR IGNORE INTO t ...`` → ``INSERT INTO t ... ON CONFLICT DO NOTHING``
     - ``INSERT OR REPLACE INTO t (pk, ...)``  → ``INSERT INTO t (pk, ...) ON CONFLICT (pk) DO UPDATE SET col=EXCLUDED.col, ...``
     - ``datetime('now')`` → ``to_char(now(), 'YYYY-MM-DD HH24:MI:SS')``
     - ``strftime('%s','now')`` → ``EXTRACT(EPOCH FROM NOW())``

   Same rules ``alembic_pg_compat`` uses for DDL; we can't import that
   module directly here (different event-hook integration) but the
   transformations are textual and cheap to mirror.

3. **PRAGMA no-op.** ``db.py::init()`` and ``close()`` issue
   ``PRAGMA quick_check`` / ``PRAGMA wal_checkpoint`` / etc. PG has no
   PRAGMA — these become no-ops on the PG path (cursor with 0 rows).

4. **VACUUM no-op.** SQLite VACUUM has no PG equivalent that makes
   sense in this lifecycle; no-op on PG.

5. **Cursor emulation.** ``async with conn.execute(...) as cur`` is the
   dominant pattern in ``db.py``. asyncpg has no cursor-with-context
   semantics; we return a ``_PgCursor`` that pre-materialises the
   result set (for SELECT) or the status string (for DML) and exposes
   ``fetchone() / fetchall() / rowcount`` over it. Memory cost is
   bounded because no ``db.py`` query today returns millions of rows;
   if that changes we'd introduce a streaming variant.

Concurrency model
─────────────────
``db.py`` runs as a single-connection singleton — multiple coroutines
await ``_conn().execute()`` concurrently and serialise on aiosqlite's
internal queue. asyncpg's ``Connection`` does NOT support concurrent
``execute()`` (raises ``InterfaceError: another operation is in
progress``). We add an ``asyncio.Lock`` in this wrapper so the compat
surface behaves identically to aiosqlite under the same call pattern.

Transactions
────────────
asyncpg has no implicit transaction. ``db.py`` code expects aiosqlite's
"auto-begin on first DML, commit on conn.commit()" behaviour. We wrap
writes in a lazily-started transaction that ``.commit()`` finishes;
reads execute outside a transaction to avoid unnecessary locking.

This wrapper is deliberately NOT in
:mod:`backend.db_connection` because that module is the clean
long-term protocol; this is a scoped bridge. When ``db.py`` is
eventually ported to the neutral protocol directly, this file gets
deleted.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Sequence

logger = logging.getLogger(__name__)


# ── SQL dialect translations (runtime, PG only) ───────────────────────────

_INSERT_OR_IGNORE_RE = re.compile(
    r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE
)
_INSERT_OR_REPLACE_RE = re.compile(
    r"\bINSERT\s+OR\s+REPLACE\s+INTO\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(([^)]+)\)",
    re.IGNORECASE,
)
_DATETIME_NOW_RE = re.compile(r"datetime\s*\(\s*'now'\s*\)", re.IGNORECASE)
_STRFTIME_EPOCH_RE = re.compile(
    r"strftime\s*\(\s*'%{1,2}s'\s*,\s*'now'\s*\)", re.IGNORECASE
)


def _translate_insert_or_ignore(sql: str) -> str:
    """``INSERT OR IGNORE INTO t ...`` → ``INSERT INTO t ... ON CONFLICT DO NOTHING``.

    We append ``ON CONFLICT DO NOTHING`` only if one isn't already
    present (defence against double-translation from a caller that
    already did the work)."""
    replaced = _INSERT_OR_IGNORE_RE.sub("INSERT INTO", sql)
    if replaced is sql:
        return sql
    if "ON CONFLICT" in replaced.upper():
        return replaced
    trimmed = replaced.rstrip()
    if trimmed.endswith(";"):
        return trimmed[:-1].rstrip() + " ON CONFLICT DO NOTHING;"
    return trimmed + " ON CONFLICT DO NOTHING"


def _translate_insert_or_replace(sql: str) -> str:
    """``INSERT OR REPLACE INTO t (pk, c1, c2) VALUES (...)`` →
    ``INSERT INTO t (pk, c1, c2) VALUES (...) ON CONFLICT (pk) DO UPDATE SET c1=EXCLUDED.c1, c2=EXCLUDED.c2``.

    Convention: first column in the list is the conflict target (PK).
    Matches :func:`backend.alembic_pg_compat._translate_insert_or_replace`.
    """
    match = _INSERT_OR_REPLACE_RE.search(sql)
    if not match:
        return sql
    cols_text = match.group(2)
    cols = [c.strip() for c in cols_text.split(",") if c.strip()]
    if not cols:
        return sql
    conflict_target = cols[0]
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols[1:]) or \
        f"{conflict_target}=EXCLUDED.{conflict_target}"
    # Replace "INSERT OR REPLACE INTO" with "INSERT INTO" (preserving the
    # rest of the statement), then append ON CONFLICT.
    replaced = re.sub(
        r"\bINSERT\s+OR\s+REPLACE\s+INTO\b", "INSERT INTO", sql,
        flags=re.IGNORECASE,
    )
    trimmed = replaced.rstrip().rstrip(";").rstrip()
    return f"{trimmed} ON CONFLICT ({conflict_target}) DO UPDATE SET {updates}"


def _translate_datetime_now(sql: str) -> str:
    return _DATETIME_NOW_RE.sub("to_char(now(), 'YYYY-MM-DD HH24:MI:SS')", sql)


def _translate_strftime_epoch(sql: str) -> str:
    return _STRFTIME_EPOCH_RE.sub("EXTRACT(EPOCH FROM NOW())", sql)


def _qmark_to_dollar(sql: str) -> str:
    """Convert SQLite ``?`` placeholders to asyncpg ``$N``.

    String-literal-aware (single-quoted, with SQL doubled-quote escape).
    No regex → predictable, no catastrophic backtracking. Mirrors
    :func:`backend.db_connection._PostgresAsyncConnection._qmark_to_dollar`
    but kept local to avoid a tight coupling with that module.
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


def translate_sql(sql: str) -> str:
    """Apply all SQLite → PG runtime rewrites in the right order.

    Order matters: placeholder conversion comes last because the earlier
    rewrites may inject literal ``?`` characters (they don't today, but
    keeping the order stable is cheap insurance).
    """
    out = sql
    out = _translate_insert_or_replace(out)  # must precede ignore
    out = _translate_insert_or_ignore(out)
    out = _translate_datetime_now(out)
    out = _translate_strftime_epoch(out)
    out = _qmark_to_dollar(out)
    return out


def _is_select_like(sql: str) -> bool:
    """Decide which asyncpg method to dispatch to.

    Returns True for statements that yield rows (SELECT / WITH / or
    anything with RETURNING); False for DML that only returns a status.
    """
    s = sql.lstrip().upper()
    if s.startswith("SELECT") or s.startswith("WITH") or s.startswith("VALUES"):
        return True
    # INSERT/UPDATE/DELETE ... RETURNING col — also row-producing.
    return " RETURNING " in f" {s} "


def _is_pragma_or_vacuum(sql: str) -> bool:
    s = sql.lstrip().upper()
    return s.startswith("PRAGMA") or s.startswith("VACUUM")


def _parse_rowcount(status: str | None) -> int:
    """Extract affected-row count from asyncpg's status string.

    asyncpg returns strings like ``'INSERT 0 1'`` / ``'UPDATE 3'`` /
    ``'DELETE 2'`` from ``conn.execute()``. The last integer token is
    the row count. Returns -1 if we can't parse (matches DB-API
    convention for 'unknown').
    """
    if not status:
        return -1
    parts = status.split()
    if parts and parts[-1].lstrip("-").isdigit():
        return int(parts[-1])
    return -1


# ── Cursor emulation ──────────────────────────────────────────────────────


class _ExecuteResult:
    """Dual-mode return object from ``PgCompatConnection.execute(...)``.

    aiosqlite's ``execute()`` returns an object that is BOTH awaitable
    (yielding a cursor) and an async context manager (entering yields
    the same cursor, exiting closes it). db.py uses both call shapes:

        cur = await _conn().execute(...)        # awaitable
        async with _conn().execute(...) as cur: # context manager

    We can't replicate this with a plain ``async def execute()`` — that
    returns a coroutine which is NOT a context manager. Instead we
    return this deferred-execution proxy: the actual asyncpg fetch/
    execute runs inside ``__await__`` or ``__aenter__``, whichever the
    caller uses first.

    Why not ``contextlib.asynccontextmanager``? That yields an
    async-context-manager but not an awaitable. We need both on the
    same object.
    """

    __slots__ = ("_conn", "_sql", "_params", "_cursor")

    def __init__(self, conn: "PgCompatConnection", sql: str, params: Any) -> None:
        self._conn = conn
        self._sql = sql
        self._params = params
        self._cursor: _PgCursor | None = None

    def __await__(self):
        return self._conn._do_execute(self._sql, self._params).__await__()

    async def __aenter__(self) -> "_PgCursor":
        self._cursor = await self._conn._do_execute(self._sql, self._params)
        return self._cursor

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ARG002
        if self._cursor is not None:
            await self._cursor.close()
            self._cursor = None


class _PgCursor:
    """aiosqlite-cursor-like wrapper over a pre-materialised result set.

    ``async with conn.execute(...) as cur`` is the dominant pattern in
    ``db.py``. asyncpg's ``fetch()`` returns a ``list[Record]`` eagerly —
    we keep that in the cursor and expose aiosqlite's fetchone/fetchall
    semantics over it.

    For DML (non-SELECT) we hold the status string and expose
    ``rowcount`` without a row list.

    Supports async-context-manager but ``__aexit__`` is a no-op —
    asyncpg has nothing to close at cursor granularity.
    """

    __slots__ = ("_records", "_status", "_idx", "_closed")

    def __init__(
        self,
        records: list[Any] | None = None,
        status: str | None = None,
    ) -> None:
        self._records = records
        self._status = status
        self._idx = 0
        self._closed = False

    async def __aenter__(self) -> "_PgCursor":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ARG002
        self._closed = True

    async def fetchone(self) -> Any:
        if self._records is None:
            return None
        if self._idx >= len(self._records):
            return None
        r = self._records[self._idx]
        self._idx += 1
        return r

    async def fetchall(self) -> list[Any]:
        if self._records is None:
            return []
        remaining = self._records[self._idx:]
        self._idx = len(self._records)
        return list(remaining)

    @property
    def rowcount(self) -> int:
        # For SELECT-like queries, rowcount is the length of the full
        # result set (aiosqlite returns -1 for SELECT but many db.py
        # call sites check `.rowcount` after DML only, so this is safe).
        if self._records is not None:
            return len(self._records)
        return _parse_rowcount(self._status)

    async def close(self) -> None:
        self._closed = True


# ── Connection wrapper ────────────────────────────────────────────────────


class PgCompatConnection:
    """aiosqlite-compatible wrapper over asyncpg.

    Matches the ``aiosqlite.Connection`` surface that ``backend/db.py``
    actually uses (not the full API — aiosqlite has features ``db.py``
    never touches). Install it by making ``db._conn()`` return an
    instance when ``OMNISIGHT_DATABASE_URL`` resolves to a PG URL.
    """

    # aiosqlite.Connection stores row_factory as a plain attribute; db.py
    # does ``_db.row_factory = aiosqlite.Row`` once at open time and
    # never reads it back. asyncpg.Record already supports both
    # positional (``r[0]``) and dict (``r["col"]``) access, so we just
    # accept the assignment and ignore it.
    row_factory: Any = None

    def __init__(self, pg_conn: Any) -> None:  # asyncpg.Connection
        self._conn = pg_conn
        self._tx: Any = None
        # Serialise execute() on the single connection (asyncpg forbids
        # concurrent operations on one Connection object). Matches
        # aiosqlite's internal queue so call-site concurrency semantics
        # are preserved.
        self._lock = asyncio.Lock()

    # ── lifecycle ──
    @classmethod
    async def open(cls, dsn: str) -> "PgCompatConnection":
        import asyncpg  # lazy
        conn = await asyncpg.connect(dsn=dsn)
        return cls(conn)

    async def close(self) -> None:
        # Commit any open tx before closing (matches aiosqlite's
        # implicit commit-on-close behaviour — none in strict mode but
        # close-while-dirty causes data loss on SQLite too, so matching
        # is a reasonable default).
        if self._tx is not None:
            try:
                await self._tx.commit()
            except Exception as exc:
                logger.warning("asyncpg tx commit on close failed: %s", exc)
            self._tx = None
        await self._conn.close()

    async def _ensure_tx(self) -> None:
        """Lazily begin a transaction. asyncpg has no auto-begin;
        aiosqlite does. We emulate by starting on first DML."""
        if self._tx is None:
            self._tx = self._conn.transaction()
            await self._tx.start()

    async def commit(self) -> None:
        if self._tx is not None:
            await self._tx.commit()
            self._tx = None

    async def rollback(self) -> None:
        if self._tx is not None:
            try:
                await self._tx.rollback()
            finally:
                self._tx = None

    # ── SQL execution ──
    def execute(
        self, sql: str, params: Sequence[Any] | None = None,
    ) -> _ExecuteResult:
        """Return a dual-mode result that is BOTH awaitable AND a
        valid ``async with ... as cur:`` target — matching aiosqlite's
        ``execute()`` calling contract so db.py's 80 call sites work
        unchanged. Actual SQL dispatch happens lazily inside
        :meth:`_do_execute` when the caller awaits or enters."""
        return _ExecuteResult(self, sql, params)

    async def _do_execute(
        self, sql: str, params: Sequence[Any] | None,
    ) -> _PgCursor:
        """The actual asyncpg dispatch. Called by ``_ExecuteResult``
        in either ``__await__`` or ``__aenter__`` path."""
        # PRAGMA / VACUUM: silently no-op on PG. Returns an empty
        # cursor so ``async with ... as cur:`` still works.
        if _is_pragma_or_vacuum(sql):
            return _PgCursor(records=[])

        pg_sql = translate_sql(sql)
        args = tuple(params) if params is not None else ()

        async with self._lock:
            upper = pg_sql.lstrip().upper()
            is_dml_with_returning = (
                " RETURNING " in f" {upper} "
                and (upper.startswith("INSERT")
                     or upper.startswith("UPDATE")
                     or upper.startswith("DELETE"))
            )
            if is_dml_with_returning:
                # INSERT/UPDATE/DELETE ... RETURNING — row-producing DML.
                # Must run inside the lazy transaction so the write
                # commits when ``.commit()`` lands (matches aiosqlite's
                # auto-begin-on-first-DML behaviour that the non-
                # returning branch below relies on).
                await self._ensure_tx()
                records = await self._conn.fetch(pg_sql, *args)
                return _PgCursor(records=list(records))
            if _is_select_like(pg_sql):
                # Pure SELECT / WITH / VALUES — read-only, no tx.
                records = await self._conn.fetch(pg_sql, *args)
                return _PgCursor(records=list(records))
            # DML without RETURNING — starts a tx if one isn't open,
            # returns status.
            await self._ensure_tx()
            status = await self._conn.execute(pg_sql, *args)
            return _PgCursor(status=status)

    async def executescript(self, sql: str) -> None:
        """aiosqlite's ``executescript`` handles multi-statement strings
        with no parameters. asyncpg's ``Connection.execute`` accepts
        multi-statement SQL too. We apply the SQLite-ism translator to
        every statement in the script — cheap and catches CREATE TABLE
        ``datetime('now')`` defaults in migrations run at boot."""
        translated = translate_sql(sql)
        async with self._lock:
            await self._conn.execute(translated)

    # Some callers use the connection as an async context manager
    # (``async with _db.execute(...) as cur`` — handled above — but also
    # ``async with db as conn``). aiosqlite supports the latter; we do
    # too for parity.
    async def __aenter__(self) -> "PgCompatConnection":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ARG002
        # aiosqlite's Connection __aexit__ does NOT close the
        # connection (it leaves it open, matching sqlite3's Connection
        # context-manager behaviour). Mirror that.
        if exc is not None and self._tx is not None:
            await self.rollback()


__all__ = ["PgCompatConnection", "translate_sql"]
