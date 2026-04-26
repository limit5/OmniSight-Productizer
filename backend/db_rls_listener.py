"""Y5 row 3 (#281) — SQLAlchemy event listener: auto-inject tenant + project filters.

Sits between the application and the database.  Reads the request-scoped
``(tenant_id, project_id, user_role)`` triple set by
:mod:`backend.auth.require_project_member` (Y5 row 2) plus the original
``set_tenant_id`` from I2, and rewrites outgoing SQL so every consumer of
project-scoped tables transparently sees only its own tenant's project's
rows.

The two transformations
───────────────────────

* **SELECT** — append (or AND-merge into) ``WHERE``::

      WHERE tenant_id = '<t>' AND (project_id = '<p>' OR project_id IS NULL)

  The ``OR project_id IS NULL`` arm is the *Y5-row-3 backward-compat
  clause for legacy rows*: alembic 0038 backfilled
  ``project_id`` for every row that already had a ``tenant_id``, but in
  the (deliberately observed) ``release_window`` between 0038 landing
  and the future ``ALTER … SET NOT NULL`` revision, a row inserted by an
  un-ported writer might still hold a NULL.  We let those rows fall
  through so a project-scoped reader does not silently lose pre-cutover
  data.  The contract is documented in alembic 0038's "Schema decisions
  → NULLable column" section.

* **INSERT** — auto-fill ``(tenant_id, project_id)`` columns when the
  caller did not name them in the INSERT column list.  This means
  business-table writers can keep on writing ``INSERT INTO artifacts
  (id, payload) VALUES (?, ?)`` without thinking about the multi-tenant
  layer; the listener attaches the current tenant + project from
  context.  When the writer DID name them (intentional cross-tenant
  copy, admin tools, alembic backfills) we leave the INSERT alone — no
  silent overwrite.

Both transformations are scoped to a *closed allowlist* of tables — the
union of I1 ``TABLES_NEEDING_TENANT_ID`` (alembic 0012) and Y1
``_TABLES_NEEDING_PROJECT_ID`` (alembic 0038).  Tables outside the list
(``tenants``, ``projects``, ``project_members``, ``audit_log``,
``users``, ``api_keys``, internal alembic tables, …) pass through
untouched.  This is deliberate:

* ``audit_log`` / ``users`` / ``api_keys`` are tenant-scoped at row
  level but live outside the project-scope contract (per alembic 0038
  "Two TODO entries are deliberately NOT covered here" — audit_log is
  tenant-scoped, never project-scoped).
* Catalog tables (``tenants`` / ``projects`` / ``project_members``)
  are *the source of truth* for cross-tenant lookups; injecting a
  tenant filter into them would break every admin REST surface that
  looks up "which projects exist for this tenant" before the
  contextvar is set.

Bypass paths
────────────

The listener short-circuits to "no rewrite" in three cases.  Each
case populates ``RewrittenQuery.bypass_reason`` so callers / tests can
assert on intent:

1. ``"super_admin"`` — the request was authenticated as a platform
   super-admin (``current_user_role() == "super_admin"``).  This
   matches the contract documented on
   ``backend.auth.require_project_member`` step 1.  The listener
   leaves the query alone but emits a structured-log line with the
   actor + table + intended cross-project access; production routes
   that allow cross-project reads also require the
   ``X-Admin-Cross-Project: 1`` header (enforced by the router) and
   then write an ``audit_log`` row from the request layer.
2. ``"no_tenant_set"`` — no tenant in context (e.g. anonymous health
   probe, a CLI cron task that runs before ``set_tenant_id`` is
   called, or an admin REST surface that is *intentionally* tenant-
   wide).  Rewriting in this case would inject ``tenant_id = 'None'``
   which silently empties every result set; we'd rather not rewrite
   than silently mask a missing-context bug.  The contract for
   request-path code is "set the contextvar before any DB call" —
   this fall-through exists for boot / cron / admin paths that
   deliberately operate without one.
3. ``"table_unscoped"`` — the SQL targets a table outside the closed
   allowlist (catalog table, internal alembic, an unmigrated table).
   No rewrite, no warning.

Module-global / cross-worker state audit (per implement_phase_step.md
Step 1, case-1 answer):

* ``PROJECT_SCOPED_TABLES`` and ``TENANT_ONLY_SCOPED_TABLES`` are
  module-level frozensets — every uvicorn worker derives the same
  values from the same ``alembic 0012`` + ``alembic 0038`` source of
  truth, so cross-worker drift is impossible by construction.
* The listener reads ContextVars (per-asyncio-Task state, NOT
  module-global) — see ``backend/db_context.py`` for the matching
  audit note.
* No mutable module-level dict / cache / counter; no Redis / DB
  coordination needed because there is no shared state to coordinate.

Read-after-write timing audit:
the listener is purely a SQL string rewriter — it neither writes nor
reads database state itself.  It does not change the serialisation
properties of any downstream tx.  No new timing-visible behaviour
relative to today's helpers (``tenant_where`` / ``tenant_where_pg``)
which are also pure functions over the contextvar.

Production readiness gate
─────────────────────────

* No new wheel / OS package — uses :mod:`sqlalchemy` (already in
  ``backend/requirements.txt`` for alembic) and stdlib :mod:`re`.
* No schema migration — operates only on tables alembic 0012 +
  alembic 0038 already created.
* No new env knob — behaviour comes entirely from
  :mod:`backend.db_context` ContextVars set by the auth dependency.
* No new endpoint exposed — :func:`install_project_rls_listener`
  is library-only; it has to be explicitly attached to a SQLAlchemy
  bind by the caller.  Today the caller list is empty (runtime path
  is asyncpg-direct, alembic doesn't need request-scoped RLS), so
  this row ships as **deployed-inactive** — code is in main but no
  prod path calls it yet.  The first activation is the future
  SQLAlchemy ORM port (or the Y5 row 4 frontend-middleware test
  harness's in-memory SQLite engine).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from backend.db_context import (
    current_project_id,
    current_tenant_id,
    current_user_role,
)

logger = logging.getLogger(__name__)


# ─── Allowlist constants ────────────────────────────────────────────

# Tables carrying ``project_id`` per alembic 0038 (Y1 row 7).  Matches
# ``backend.alembic.versions._038_y1_project_id_on_business_tables.
# _TABLES_NEEDING_PROJECT_ID`` exactly — drift between the two is what
# the ``test_project_scoped_tables_match_alembic_0038`` guard catches.
PROJECT_SCOPED_TABLES: frozenset[str] = frozenset({
    "workflow_runs",
    "debug_findings",
    "decision_rules",
    "event_log",
    "artifacts",
    "user_preferences",
})

# Tables carrying ``tenant_id`` per alembic 0012 but NOT ``project_id``
# (deliberately, per alembic 0038's "Two TODO entries are deliberately
# NOT covered here").  These get the tenant filter only.
TENANT_ONLY_SCOPED_TABLES: frozenset[str] = frozenset({
    "audit_log",
    "users",
})

# Union — every table the listener will rewrite for at all.
ALL_TENANT_SCOPED_TABLES: frozenset[str] = (
    PROJECT_SCOPED_TABLES | TENANT_ONLY_SCOPED_TABLES
)


# Bypass-reason tokens.  Module-level constants so tests can assert
# on identity rather than string-spelling.
BYPASS_SUPER_ADMIN = "super_admin"
BYPASS_NO_TENANT = "no_tenant_set"
BYPASS_TABLE_UNSCOPED = "table_unscoped"
BYPASS_NOT_REWRITABLE = "not_rewritable"   # SELECT/INSERT pattern not detected


# Roles that bypass the rewrite entirely.  Today only ``super_admin``;
# kept as a frozenset so future additions (``platform_auditor`` etc)
# are a one-line change.
BYPASS_ROLES: frozenset[str] = frozenset({"super_admin"})


# ─── Statement classification ───────────────────────────────────────

# Detect ``SELECT … FROM <table>``.  Matches the *first* FROM target —
# we don't try to rewrite multi-table joins or subqueries.  A future
# version can compose with sqlglot for full AST awareness; today the
# project's actual SQL is single-target reads + INSERTs.
# ``[\w.]`` covers bare and schema-qualified identifiers, plus we
# accept bracket / backtick / double-quote wrappers so PG ``"foo"``,
# MySQL ``` `foo` ``` and MSSQL ``[foo]`` round-trip cleanly.  Single
# quotes are NOT in the class — those are literal strings, never
# table identifiers.
_IDENT_CHARS = r"\w.\"`\[\]"
_SELECT_FROM_RE = re.compile(
    r"\s*SELECT\b.*?\bFROM\s+(?P<table>[" + _IDENT_CHARS + r"]+)",
    re.IGNORECASE | re.DOTALL,
)

# Detect ``INSERT [OR <verb>] INTO <table> (<col1>, <col2>, …) VALUES …``.
# We capture the column list because INSERT auto-fill is column-aware:
# if the caller already named ``tenant_id`` we leave the row alone.
_INSERT_INTO_RE = re.compile(
    r"\s*INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(?P<table>[" + _IDENT_CHARS + r"]+)\s*"
    r"\((?P<columns>[^)]*)\)\s*VALUES",
    re.IGNORECASE | re.DOTALL,
)

# Cut points for "where to insert WHERE" when the original query had
# none.  We must put the new clause BEFORE ORDER BY / GROUP BY / HAVING
# / LIMIT / OFFSET / ``;`` so the parser still sees a well-formed query.
_WHERE_INSERT_CUT_RE = re.compile(
    r"\b(GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|OFFSET)\b|;\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RewrittenQuery:
    """Result of :func:`apply_project_rls`.

    ``original_query`` and ``rewritten_query`` are equal when
    ``applied`` is False; tests should assert on ``applied`` /
    ``bypass_reason`` rather than string-comparing the queries.
    """
    original_query: str
    rewritten_query: str
    tenant_id: Optional[str]
    project_id: Optional[str]
    user_role: Optional[str]
    target_table: Optional[str]
    statement_kind: Optional[str]   # "select" | "insert" | None
    applied: bool
    bypass_reason: Optional[str]


# ─── Helpers ────────────────────────────────────────────────────────


def _normalise_identifier(raw: str) -> str:
    """Strip schema qualifier and quote chars so ``"public"."artifacts"``
    matches the bare allowlist token ``artifacts``.

    Lowercases the result because PG identifiers are case-insensitive
    by default; SQLite is case-insensitive for table names regardless.
    """
    s = raw.strip()
    if "." in s:
        s = s.rsplit(".", 1)[-1]
    s = s.strip('"`[]')
    return s.lower()


def _split_columns(raw: str) -> list[str]:
    """Split the comma-separated column-list capture into normalised
    identifiers.  Same normalisation rules as :func:`_normalise_identifier`.
    """
    if not raw.strip():
        return []
    return [_normalise_identifier(c) for c in raw.split(",") if c.strip()]


def _quote_literal(value: str) -> str:
    """Return a single-quoted SQL literal with embedded single quotes
    doubled.  We rewrite into literals (rather than parameter binds)
    because the listener cannot grow the cursor's parameter tuple
    without re-encoding the entire driver-specific parameter style;
    the input is system-issued (UUIDs from
    :func:`backend.db_context.set_tenant_id` which is fed by validated
    auth claims), not user-controlled, so literal injection is safe.
    Defence-in-depth still doubles the quotes per the SQL standard.
    """
    return "'" + str(value).replace("'", "''") + "'"


def _detect_target(query: str) -> tuple[Optional[str], Optional[str], Optional[re.Match]]:
    """Return ``(statement_kind, target_table, match)`` or ``(None, None, None)``.

    ``statement_kind`` is ``"select"`` or ``"insert"`` — the listener
    only rewrites these two statement kinds.  UPDATE / DELETE need
    their own filter logic (they're sketched on Y5 row 4 / Y6) and
    fall through here as ``not_rewritable``.
    """
    m = _INSERT_INTO_RE.match(query)
    if m:
        return "insert", _normalise_identifier(m.group("table")), m
    m = _SELECT_FROM_RE.match(query)
    if m:
        return "select", _normalise_identifier(m.group("table")), m
    return None, None, None


def _build_where_predicate(
    *,
    table: str,
    tenant_id: str,
    project_id: Optional[str],
) -> str:
    """Compose the WHERE-clause body to AND-merge into the query.

    For project-scoped tables we always emit the tenant filter and,
    when a project is in context, the ``(project_id = X OR project_id
    IS NULL)`` clause.  When project is *not* in context (admin REST
    on a tenant-wide route, e.g. listing every project of a tenant)
    we omit the project clause — the contextvar is the operator's
    intent signal.
    """
    parts = [f"tenant_id = {_quote_literal(tenant_id)}"]
    if table in PROJECT_SCOPED_TABLES and project_id is not None:
        parts.append(
            f"(project_id = {_quote_literal(project_id)} "
            f"OR project_id IS NULL)"
        )
    return " AND ".join(parts)


def _splice_where(query: str, predicate: str) -> str:
    """Insert ``predicate`` into ``query`` AND-merged with any existing
    WHERE, otherwise prepended with WHERE before the first
    GROUP BY / ORDER BY / LIMIT / OFFSET / trailing ``;``.
    """
    where_match = re.search(r"\bWHERE\b", query, re.IGNORECASE)
    if where_match:
        # AND-merge: keep existing WHERE intact, append "AND <predicate>"
        end = where_match.end()
        return f"{query[:end]} {predicate} AND{query[end:]}"

    cut = _WHERE_INSERT_CUT_RE.search(query)
    if cut:
        i = cut.start()
        head, tail = query[:i].rstrip(), query[i:]
        return f"{head} WHERE {predicate} {tail}"

    return f"{query.rstrip().rstrip(';')} WHERE {predicate}"


# ─── Public API ─────────────────────────────────────────────────────


def apply_project_rls(
    query: str,
    *,
    tenant_id: Optional[str] = None,
    project_id: Optional[str] = None,
    user_role: Optional[str] = None,
) -> RewrittenQuery:
    """Rewrite ``query`` in-place per the Y5 row 3 contract.

    All three kwargs default to ``None``; when *any* is ``None`` the
    listener reads it from the matching ContextVar in
    :mod:`backend.db_context`.  This dual signature lets unit tests
    pass explicit values while production callers (the SQLAlchemy
    listener installed by :func:`install_project_rls_listener`) can
    rely on the request-scoped context.

    The ``query`` is returned verbatim — bypassed, not rejected — when
    no rewrite applies.  The ``RewrittenQuery.bypass_reason`` field
    documents the why for tests / structured logs.
    """
    if tenant_id is None:
        tenant_id = current_tenant_id()
    if project_id is None:
        project_id = current_project_id()
    if user_role is None:
        user_role = current_user_role()

    kind, table, match = _detect_target(query)

    base = dict(
        original_query=query,
        rewritten_query=query,
        tenant_id=tenant_id,
        project_id=project_id,
        user_role=user_role,
        target_table=table,
        statement_kind=kind,
    )

    if kind is None:
        return RewrittenQuery(
            **base, applied=False, bypass_reason=BYPASS_NOT_REWRITABLE,
        )

    if table not in ALL_TENANT_SCOPED_TABLES:
        return RewrittenQuery(
            **base, applied=False, bypass_reason=BYPASS_TABLE_UNSCOPED,
        )

    if user_role in BYPASS_ROLES:
        logger.info(
            "db_rls_listener: super_admin bypass — actor_role=%s "
            "kind=%s table=%s tenant_in_ctx=%s project_in_ctx=%s",
            user_role, kind, table, tenant_id, project_id,
        )
        return RewrittenQuery(
            **base, applied=False, bypass_reason=BYPASS_SUPER_ADMIN,
        )

    if tenant_id is None:
        return RewrittenQuery(
            **base, applied=False, bypass_reason=BYPASS_NO_TENANT,
        )

    if kind == "select":
        predicate = _build_where_predicate(
            table=table, tenant_id=tenant_id, project_id=project_id,
        )
        rewritten = _splice_where(query, predicate)
        return RewrittenQuery(
            **{**base, "rewritten_query": rewritten},
            applied=True, bypass_reason=None,
        )

    # kind == "insert"
    rewritten = _auto_fill_insert(
        query=query,
        match=match,
        table=table,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    if rewritten == query:
        # Caller already named tenant_id (and project_id when scoped)
        # — leave the row alone so intentional cross-tenant copies and
        # alembic backfills aren't silently rewritten.
        return RewrittenQuery(
            **base, applied=False, bypass_reason="caller_named_columns",
        )
    return RewrittenQuery(
        **{**base, "rewritten_query": rewritten},
        applied=True, bypass_reason=None,
    )


def _auto_fill_insert(
    *,
    query: str,
    match: re.Match,
    table: str,
    tenant_id: str,
    project_id: Optional[str],
) -> str:
    """Splice ``tenant_id`` (and ``project_id`` when scoped) into the
    column list + the first VALUES tuple of an INSERT.

    Behaviour matrix:

    * Caller named neither column → both appended.
    * Caller named ``tenant_id`` but not ``project_id`` (project-scoped
      table) → only ``project_id`` appended.
    * Caller named both → noop, return ``query`` unchanged so
      :func:`apply_project_rls` reports ``"caller_named_columns"``.

    Multi-row INSERT (``VALUES (...), (...), (...)``) is supported —
    we walk the VALUES tail tuple-by-tuple and append the literals to
    each tuple.  This matches asyncpg / aiosqlite ``executemany``
    semantics where every row in a single statement shares the same
    column list.
    """
    existing_columns = _split_columns(match.group("columns"))
    project_scoped = table in PROJECT_SCOPED_TABLES

    extra_cols: list[str] = []
    extra_vals: list[str] = []
    if "tenant_id" not in existing_columns:
        extra_cols.append("tenant_id")
        extra_vals.append(_quote_literal(tenant_id))
    if (
        project_scoped
        and project_id is not None
        and "project_id" not in existing_columns
    ):
        extra_cols.append("project_id")
        extra_vals.append(_quote_literal(project_id))

    if not extra_cols:
        return query

    cols_start, cols_end = match.span("columns")
    new_columns_clause = match.group("columns").rstrip()
    if new_columns_clause and not new_columns_clause.endswith(","):
        new_columns_clause += ", "
    else:
        new_columns_clause += " "
    new_columns_clause += ", ".join(extra_cols)

    head = query[:cols_start] + new_columns_clause + query[cols_end:]

    return _append_to_value_tuples(head, extra_vals)


def _append_to_value_tuples(query: str, extra_vals: Iterable[str]) -> str:
    """Walk every ``(...)`` tuple after the ``VALUES`` keyword and
    append the literals from ``extra_vals``.

    The walk is paren-depth aware (so nested parens inside e.g.
    ``COALESCE(...)`` don't fool us) and string-literal aware (single
    quotes with the SQL standard ``''`` escape) — same shape as the
    ``_qmark_to_dollar`` walk in
    :mod:`backend.db_connection._PostgresAsyncConnection`.
    """
    extra = ", ".join(extra_vals)
    # Locate the VALUES keyword (case-insensitive).
    m = re.search(r"\bVALUES\b", query, re.IGNORECASE)
    if not m:
        return query
    head = query[:m.end()]
    tail = query[m.end():]

    out: list[str] = [head]
    i = 0
    n = len(tail)
    in_str = False
    depth = 0
    tuple_start = -1
    while i < n:
        c = tail[i]
        if in_str:
            out.append(c)
            if c == "'":
                if i + 1 < n and tail[i + 1] == "'":
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
        if c == "(":
            if depth == 0:
                tuple_start = len(out)   # position in `out`
            depth += 1
            out.append(c)
            i += 1
            continue
        if c == ")":
            depth -= 1
            if depth == 0:
                # Splice ``, <extra>`` immediately before the closing
                # paren.  ``out`` mirrors ``query`` up to here so we
                # can pop the latest character (the ``)``), append our
                # extras, then re-emit it.
                out.append(", ")
                out.append(extra)
                out.append(c)
                i += 1
                continue
            out.append(c)
            i += 1
            continue
        out.append(c)
        i += 1

    return "".join(out)


# ─── SQLAlchemy event hook installer ────────────────────────────────


def install_project_rls_listener(bind: Any) -> None:
    """Attach a ``before_cursor_execute`` listener to the SQLAlchemy
    bind so every executed statement is fed through
    :func:`apply_project_rls`.

    Library-only — callers (alembic env, future SQLAlchemy ORM,
    test harness) opt in by calling this from their own bind setup.
    Safe to call multiple times only if SQLAlchemy's
    ``event.contains`` is checked first; we don't double-register
    automatically because tests want to attach + detach freely.

    Parameters
    ----------
    bind
        Anything ``sqlalchemy.event.listens_for`` accepts — typically
        a ``sqlalchemy.engine.Engine`` or a ``Connection``.
    """
    from sqlalchemy import event   # lazy import — listener module is
                                    # importable in environments
                                    # without sqlalchemy installed
                                    # (ContextVar-only callers).

    @event.listens_for(bind, "before_cursor_execute", retval=True)
    def _rewrite(conn, cursor, statement, parameters, context, executemany):
        result = apply_project_rls(statement)
        return result.rewritten_query, parameters


__all__ = [
    "ALL_TENANT_SCOPED_TABLES",
    "BYPASS_NO_TENANT",
    "BYPASS_NOT_REWRITABLE",
    "BYPASS_ROLES",
    "BYPASS_SUPER_ADMIN",
    "BYPASS_TABLE_UNSCOPED",
    "PROJECT_SCOPED_TABLES",
    "RewrittenQuery",
    "TENANT_ONLY_SCOPED_TABLES",
    "apply_project_rls",
    "install_project_rls_listener",
]
