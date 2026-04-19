"""G4 #1 (HA-04) — SQLite ↔ PostgreSQL dialect compatibility shim for Alembic.

Existing migrations were written against SQLite and freely use
SQLite-specific syntax (``datetime('now')``, ``AUTOINCREMENT``,
``INSERT OR IGNORE``, ``PRAGMA table_info``, positional ``?`` placeholders).
Rewriting the migration files in-place would obliterate git history on the
authoritative audit trail; instead this module intercepts raw SQL at
execution time and rewrites SQLite-isms into their PostgreSQL equivalents
*only* when the connected dialect is Postgres. SQLite runs are untouched.

Public surface:

* :func:`translate_sql` — transform one SQL statement.
* :func:`translate_params` — convert ``?`` → ``%s`` (positional, psycopg2).
* :func:`scan_sqlite_isms` — static scan of a migration file for known
  SQLite-isms. Used by the :mod:`scripts.scan_sqlite_isms` CLI and the
  contract tests. This is the "sqlite-isms 掃描" deliverable of G4 #1.
* :func:`install_pg_compat` — register before-cursor-execute hook on an
  Alembic/SQLAlchemy connection. Called from ``env.py``.

The rewrite rules are intentionally narrow and *regex-based*. We resist
the urge to add a full SQL parser because (a) our migration DSL is a tiny
subset of ANSI SQL, (b) introducing ``sqlglot`` would be a new runtime
dependency on the hot migration path, and (c) each rule is covered by
contract tests so drift is caught locally.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


# ── Static SQLite-ism patterns (used by scanner + tests) ──────────────────

#: Map of human-readable label → compiled regex. The labels are the canonical
#: identifiers used by the TODO.md task description — do not rename without
#: updating the TODO/HANDOFF.
ISM_PATTERNS: dict[str, re.Pattern[str]] = {
    "AUTOINCREMENT": re.compile(r"\bAUTOINCREMENT\b", re.IGNORECASE),
    "WITHOUT ROWID": re.compile(r"\bWITHOUT\s+ROWID\b", re.IGNORECASE),
    # SQLite lets you declare a column with no type ("dynamic type" /
    # "type affinity"). Our migrations never do this, but the scan is
    # cheap and protects future additions. We flag a column definition
    # line inside a CREATE TABLE that has no type token after the name.
    "DYNAMIC_TYPE": re.compile(
        r"^\s*[a-zA-Z_][a-zA-Z0-9_]*\s*(?:,|\)|$)",
        re.MULTILINE | re.IGNORECASE,
    ),
    "datetime_now": re.compile(r"datetime\s*\(\s*'now'\s*\)", re.IGNORECASE),
    "strftime_epoch": re.compile(
        # Matches both the raw source form ``strftime('%s','now')`` and
        # the SA-compiled wire form ``strftime('%%s','now')`` that
        # op.execute() emits when it percent-escapes for psycopg2.
        r"strftime\s*\(\s*'%{1,2}s'\s*,\s*'now'\s*\)", re.IGNORECASE
    ),
    "INSERT_OR_IGNORE": re.compile(r"\bINSERT\s+OR\s+IGNORE\b", re.IGNORECASE),
    "INSERT_OR_REPLACE": re.compile(r"\bINSERT\s+OR\s+REPLACE\b", re.IGNORECASE),
    "PRAGMA_TABLE_INFO": re.compile(
        r"PRAGMA\s+table_info\s*\(\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\)",
        re.IGNORECASE,
    ),
}


@dataclass(frozen=True)
class IsmHit:
    """One occurrence of a SQLite-ism in a migration file."""

    ism: str
    lineno: int
    snippet: str


def scan_sqlite_isms(
    source: str,
    *,
    ignore: Iterable[str] = (),
) -> list[IsmHit]:
    """Return all SQLite-isms found in ``source``.

    The scanner is used in two places: the CLI in
    ``scripts/scan_sqlite_isms.py`` prints a report, and the contract
    tests assert that every ism we know about is handled by at least
    one :func:`translate_sql` rule.

    ``ignore`` lets callers skip known patterns (mostly ``DYNAMIC_TYPE``,
    which has a high false-positive rate on non-column-definition lines).
    """
    ignore_set = {label.upper() for label in ignore}
    hits: list[IsmHit] = []
    lines = source.splitlines()
    for label, pattern in ISM_PATTERNS.items():
        if label.upper() in ignore_set:
            continue
        for match in pattern.finditer(source):
            start = match.start()
            lineno = source.count("\n", 0, start) + 1
            snippet = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
            hits.append(IsmHit(ism=label, lineno=lineno, snippet=snippet))
    hits.sort(key=lambda h: (h.lineno, h.ism))
    return hits


# ── Regex-based SQL translation (SQLite → PostgreSQL) ──────────────────────

_AUTOINCREMENT_RE = re.compile(
    r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
    re.IGNORECASE,
)
_DATETIME_NOW_RE = re.compile(r"datetime\s*\(\s*'now'\s*\)", re.IGNORECASE)
_STRFTIME_EPOCH_RE = re.compile(
    r"strftime\s*\(\s*'%{1,2}s'\s*,\s*'now'\s*\)", re.IGNORECASE
)
_INSERT_OR_IGNORE_RE = re.compile(
    r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE
)
_INSERT_OR_REPLACE_RE = re.compile(
    r"\bINSERT\s+OR\s+REPLACE\s+INTO\b", re.IGNORECASE
)
_PRAGMA_TABLE_INFO_RE = re.compile(
    r"PRAGMA\s+table_info\s*\(\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\)",
    re.IGNORECASE,
)
_REAL_COL_RE = re.compile(r"\bREAL\b", re.IGNORECASE)
# Guard: only rewrite REAL when the SQL statement is a DDL (CREATE TABLE /
# ALTER TABLE / ADD COLUMN). That keeps the rewrite well clear of any
# parameter strings / JSON bodies that may legitimately contain the token.
_DDL_GUARD_RE = re.compile(
    r"\b(CREATE\s+TABLE|ALTER\s+TABLE|ADD\s+COLUMN)\b", re.IGNORECASE
)


def _translate_autoincrement(sql: str) -> str:
    # PG 10+ IDENTITY — matches SQLite's "allocate next rowid" semantics
    # more faithfully than SERIAL (which deprecates in newer PG).
    return _AUTOINCREMENT_RE.sub(
        "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
        sql,
    )


def _translate_datetime_now(sql: str) -> str:
    # SQLite's ``datetime('now')`` returns a string like
    # '2026-04-18 12:34:56'. Postgres has no function named ``datetime``.
    # ``to_char(now(), 'YYYY-MM-DD HH24:MI:SS')`` produces an identical
    # shape, and because the columns that use this default are declared
    # TEXT the implicit cast is a no-op.
    return _DATETIME_NOW_RE.sub(
        "to_char(now(), 'YYYY-MM-DD HH24:MI:SS')",
        sql,
    )


def _translate_strftime_epoch(sql: str) -> str:
    # ``strftime('%s','now')`` returns unix epoch seconds as a string.
    # ``EXTRACT(EPOCH FROM NOW())`` returns it as a numeric — the single
    # known caller stores it in a ``REAL`` column so the implicit
    # conversion is safe.
    return _STRFTIME_EPOCH_RE.sub(
        "EXTRACT(EPOCH FROM NOW())",
        sql,
    )


def _translate_insert_or_ignore(sql: str) -> str:
    # Append ``ON CONFLICT DO NOTHING`` unless one is already present.
    replaced = _INSERT_OR_IGNORE_RE.sub("INSERT INTO", sql)
    if replaced is sql:  # nothing changed
        return sql
    if "ON CONFLICT" in replaced.upper():
        return replaced
    # Put the ON CONFLICT clause right before the trailing semicolon (if any).
    trimmed = replaced.rstrip()
    if trimmed.endswith(";"):
        return trimmed[:-1].rstrip() + " ON CONFLICT DO NOTHING;"
    return trimmed + " ON CONFLICT DO NOTHING"


def _translate_insert_or_replace(sql: str) -> str:
    # ``INSERT OR REPLACE`` has no drop-in PG equivalent without a
    # conflict target. The one known caller (0015) targets a PK column
    # (``tenant_id``) and is immediately preceded by a table CREATE, so
    # ``ON CONFLICT DO NOTHING`` would be wrong — we need an UPDATE.
    #
    # We can't know the conflict target from the INSERT alone, so we
    # rewrite to a DELETE + INSERT pair wrapped so the net effect is the
    # same "upsert" as SQLite's OR REPLACE. The rewrite only fires if
    # a single row is being inserted (VALUES (…)) which covers 0015 and
    # is the only known call site.
    match = _INSERT_OR_REPLACE_RE.search(sql)
    if not match:
        return sql
    # Strategy: replace OR REPLACE with plain INSERT then append
    # ON CONFLICT … DO UPDATE SET col=EXCLUDED.col, … using the column list
    # parsed from the statement.
    head_match = re.search(
        r"INSERT\s+OR\s+REPLACE\s+INTO\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(([^)]+)\)",
        sql,
        re.IGNORECASE,
    )
    if not head_match:
        return _INSERT_OR_REPLACE_RE.sub("INSERT INTO", sql)
    head_match.group(1)
    cols_text = head_match.group(2)
    cols = [c.strip() for c in cols_text.split(",") if c.strip()]
    # First column is the conflict target (convention: PK first).
    conflict_target = cols[0]
    updates = ", ".join(
        f"{c}=EXCLUDED.{c}" for c in cols[1:]
    ) or f"{conflict_target}=EXCLUDED.{conflict_target}"
    replaced = _INSERT_OR_REPLACE_RE.sub("INSERT INTO", sql)
    trimmed = replaced.rstrip().rstrip(";").rstrip()
    return (
        f"{trimmed} ON CONFLICT ({conflict_target}) DO UPDATE SET {updates}"
    )


def _translate_real(sql: str) -> str:
    """Rewrite ``REAL`` → ``DOUBLE PRECISION`` in DDL.

    SQLite's ``REAL`` is IEEE-754 binary64 (8 bytes). PostgreSQL's
    ``REAL`` is binary32 (4 bytes) — half the precision. The audit_log
    hash chain depends on byte-exact round-tripping of ``ts`` (a
    sub-second float stored with 6-digit precision); a REAL→REAL
    migration truncates the fractional seconds and breaks the chain
    on every row. PG's ``DOUBLE PRECISION`` is the byte-exact match
    for SQLite's REAL.

    Scoped to DDL statements (``CREATE TABLE`` / ``ALTER TABLE`` /
    ``ADD COLUMN``) via the ``_DDL_GUARD_RE`` preflight so the
    rewrite can't accidentally mangle JSON payloads or parameter
    strings — neither of which reach this hook as DDL anyway, but the
    belt-and-braces check costs nothing.

    Phase-3 P1 root cause: ``audit_log.ts REAL NOT NULL`` became
    ``REAL`` on PG too, truncating 1776547569.684335 → 1776547584.0
    and breaking ``curr_hash`` verification on the first row.
    """
    if not _DDL_GUARD_RE.search(sql):
        return sql
    return _REAL_COL_RE.sub("DOUBLE PRECISION", sql)


def _translate_pragma_table_info(sql: str) -> str:
    # The migrations call PRAGMA table_info(T) and read row[1] for the
    # column name. Postgres has no PRAGMA; we emit a SELECT that yields
    # compatible columns: (cid, name, type, notnull, dflt_value, pk).
    def repl(match: re.Match[str]) -> str:
        table = match.group(1).lower()
        return (
            "SELECT ordinal_position AS cid, column_name AS name, "
            "data_type AS type, "
            "CASE WHEN is_nullable='NO' THEN 1 ELSE 0 END AS notnull, "
            "column_default AS dflt_value, "
            "0 AS pk "
            "FROM information_schema.columns "
            f"WHERE table_schema='public' AND table_name='{table}' "
            "ORDER BY ordinal_position"
        )

    return _PRAGMA_TABLE_INFO_RE.sub(repl, sql)


def translate_params_qmark_to_pyformat(sql: str) -> str:
    """Convert ``?`` positional placeholders to ``%s`` for psycopg2.

    Skips ``?`` inside single-quoted strings so we don't mangle JSON
    literals like ``'{"x": "?"}'``. We *don't* handle dollar-quoted
    strings because none of the migrations use them.
    """
    out: list[str] = []
    i = 0
    in_single = False
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            # Handle SQL '' escape inside a single-quoted string.
            if in_single and i + 1 < len(sql) and sql[i + 1] == "'":
                out.append("''")
                i += 2
                continue
            in_single = not in_single
            out.append(ch)
            i += 1
            continue
        if ch == "?" and not in_single:
            out.append("%s")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def translate_sql(sql: str, dialect: str) -> str:
    """Return SQL rewritten for the given dialect name.

    ``dialect`` is the SQLAlchemy dialect identifier (``sqlite``,
    ``postgresql``). Unknown dialects pass through untouched.
    """
    if not dialect:
        return sql
    dialect = dialect.lower()
    if dialect != "postgresql":
        return sql
    out = sql
    out = _translate_autoincrement(out)
    out = _translate_real(out)
    out = _translate_datetime_now(out)
    out = _translate_strftime_epoch(out)
    out = _translate_insert_or_replace(out)
    out = _translate_insert_or_ignore(out)
    out = _translate_pragma_table_info(out)
    out = translate_params_qmark_to_pyformat(out)
    return out


# ── SQLAlchemy event-hook installer ───────────────────────────────────────


def install_pg_compat(bind) -> None:
    """Attach a before_cursor_execute listener that rewrites SQLite-isms
    when the connected engine is Postgres.

    Called from ``backend/alembic/env.py`` right after we open the
    connection. A no-op for SQLite binds.
    """
    from sqlalchemy import event

    dialect_name = getattr(bind.dialect, "name", "").lower()
    if dialect_name != "postgresql":
        return

    @event.listens_for(bind, "before_cursor_execute", retval=True)
    def _translate(conn, cursor, statement, parameters, context, executemany):
        new_sql = translate_sql(statement, "postgresql")
        return new_sql, parameters


__all__ = [
    "IsmHit",
    "ISM_PATTERNS",
    "install_pg_compat",
    "scan_sqlite_isms",
    "translate_params_qmark_to_pyformat",
    "translate_sql",
]
