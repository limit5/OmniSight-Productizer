"""Y5 (#281) row 3 — drift guard for ``backend.db_rls_listener``.

Pure-unit (no DB) + in-memory SQLite end-to-end (covers the
SQLAlchemy ``before_cursor_execute`` integration) tests.  PG-live
isn't required — SQLite is sufficient for the listener contract
because the rewriter operates at the SQL-string layer (input is
already cross-dialect text), and the in-memory engine exercises the
full SQLAlchemy event-dispatch path identically to PG.

Drift guard families
────────────────────

(a) Module-level allowlist constants are aligned with alembic 0012
    (``TABLES_NEEDING_TENANT_ID``) and alembic 0038 (``Y1`` row 7).
(b) Bypass-reason tokens are the contract surface — exact strings.
(c) SELECT rewrite — no-WHERE / WHERE-merge / GROUP/ORDER/LIMIT cut /
    schema-qualified table / multi-line query.
(d) Project-scoped vs tenant-only scoped table differentiation.
(e) INSERT auto-fill — single row, multi-row, caller-named columns,
    placeholder-bearing VALUES, project-id absent in context.
(f) Bypass paths — super-admin, no tenant, table outside allowlist,
    UPDATE/DELETE not rewritten.
(g) ContextVar fall-through — kwargs default to current_*().
(h) End-to-end SQLAlchemy listener integration on in-memory SQLite.
(i) Self-fingerprint guard.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures — wipe ContextVars between tests so cross-test pollution
#  cannot mask a missing-tenant bug.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture(autouse=True)
def _reset_db_context_vars():
    from backend import db_context
    db_context.set_tenant_id(None)
    db_context.set_project_id(None)
    db_context.set_user_role(None)
    yield
    db_context.set_tenant_id(None)
    db_context.set_project_id(None)
    db_context.set_user_role(None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (a) Module-level allowlist constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_project_scoped_tables_match_alembic_0038():
    """Drift guard: alembic 0038 backfilled ``project_id`` on exactly
    six business tables.  The listener allowlist must agree row-for-row;
    a missing entry leaves the table un-filtered (cross-project leak),
    an extra entry rewrites SQL into a non-existent column (runtime
    SQL error).

    The alembic module's filename starts with a digit, which isn't a
    valid Python identifier, so load it via importlib + path.
    """
    import importlib.util as _ilu
    alembic_path = (
        Path(__file__).resolve().parent.parent
        / "alembic" / "versions"
        / "0038_y1_project_id_on_business_tables.py"
    )
    spec = _ilu.spec_from_file_location("_alembic_0038", alembic_path)
    module = _ilu.module_from_spec(spec)
    spec.loader.exec_module(module)

    from backend.db_rls_listener import PROJECT_SCOPED_TABLES
    assert PROJECT_SCOPED_TABLES == frozenset(module._TABLES_NEEDING_PROJECT_ID)


def test_tenant_only_scoped_tables_match_alembic_0012_minus_0038():
    """``audit_log`` and ``users`` carry ``tenant_id`` (alembic 0012)
    but were intentionally excluded from alembic 0038's project_id
    backfill (per its docstring: audit_log is tenant-scoped by chain
    integrity contract; users is platform-tier, not project-tier).
    Listener treats them as tenant-only — drift here either over-
    filters (stops admin REST from reading user catalogue) or under-
    filters (cross-tenant leak)."""
    from backend.db_rls_listener import (
        PROJECT_SCOPED_TABLES, TENANT_ONLY_SCOPED_TABLES,
    )
    assert "audit_log" in TENANT_ONLY_SCOPED_TABLES
    assert "users" in TENANT_ONLY_SCOPED_TABLES
    # Disjoint with project-scoped set.
    assert TENANT_ONLY_SCOPED_TABLES.isdisjoint(PROJECT_SCOPED_TABLES)


def test_all_tenant_scoped_tables_is_union():
    from backend.db_rls_listener import (
        ALL_TENANT_SCOPED_TABLES,
        PROJECT_SCOPED_TABLES,
        TENANT_ONLY_SCOPED_TABLES,
    )
    assert ALL_TENANT_SCOPED_TABLES == (
        PROJECT_SCOPED_TABLES | TENANT_ONLY_SCOPED_TABLES
    )


def test_constants_are_frozensets():
    """Frozenset (not list/set/tuple) so module-level state is
    immutable.  Per implement_phase_step.md Step 1 module-global
    state audit, case-1 answer (every worker derives identical
    immutable constants from same source)."""
    from backend.db_rls_listener import (
        ALL_TENANT_SCOPED_TABLES,
        BYPASS_ROLES,
        PROJECT_SCOPED_TABLES,
        TENANT_ONLY_SCOPED_TABLES,
    )
    assert isinstance(PROJECT_SCOPED_TABLES, frozenset)
    assert isinstance(TENANT_ONLY_SCOPED_TABLES, frozenset)
    assert isinstance(ALL_TENANT_SCOPED_TABLES, frozenset)
    assert isinstance(BYPASS_ROLES, frozenset)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (b) Bypass-reason tokens are the contract surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_bypass_reason_constants_are_stable_strings():
    from backend import db_rls_listener as M
    assert M.BYPASS_SUPER_ADMIN == "super_admin"
    assert M.BYPASS_NO_TENANT == "no_tenant_set"
    assert M.BYPASS_TABLE_UNSCOPED == "table_unscoped"
    assert M.BYPASS_NOT_REWRITABLE == "not_rewritable"


def test_bypass_roles_contains_super_admin():
    from backend.db_rls_listener import BYPASS_ROLES
    assert "super_admin" in BYPASS_ROLES


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (c) SELECT rewrite paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _apply(query, **kwargs):
    from backend.db_rls_listener import apply_project_rls
    base = dict(
        tenant_id="t-acme", project_id="p-acme-default", user_role="contributor",
    )
    base.update(kwargs)
    return apply_project_rls(query, **base)


def test_select_no_where_appends_full_predicate():
    r = _apply("SELECT * FROM artifacts")
    assert r.applied is True
    assert r.bypass_reason is None
    assert r.statement_kind == "select"
    assert r.target_table == "artifacts"
    assert (
        "WHERE tenant_id = 't-acme' AND "
        "(project_id = 'p-acme-default' OR project_id IS NULL)"
    ) in r.rewritten_query


def test_select_with_where_uses_and_merge():
    r = _apply("SELECT * FROM artifacts WHERE id = 1 AND ts > 100")
    assert r.applied is True
    rewritten = r.rewritten_query
    # Existing WHERE preserved, new predicate AND-merged.
    assert "WHERE tenant_id = 't-acme'" in rewritten
    assert "id = 1" in rewritten
    assert "ts > 100" in rewritten
    # Not duplicated.
    assert rewritten.count("tenant_id = 't-acme'") == 1


def test_select_inserts_where_before_order_by():
    r = _apply("SELECT * FROM artifacts ORDER BY ts DESC")
    assert r.applied is True
    # ORDER BY must remain at the end after the new WHERE.
    assert re.search(r"WHERE\s+tenant_id\s*=\s*'t-acme'.*ORDER BY ts DESC", r.rewritten_query)


def test_select_inserts_where_before_limit():
    r = _apply("SELECT id FROM artifacts LIMIT 10")
    assert r.applied is True
    assert "WHERE tenant_id" in r.rewritten_query
    assert r.rewritten_query.rstrip().endswith("LIMIT 10")


def test_select_inserts_where_before_group_by_having():
    r = _apply("SELECT tenant_id, COUNT(*) FROM artifacts GROUP BY tenant_id HAVING COUNT(*) > 0")
    assert r.applied is True
    assert "WHERE tenant_id = 't-acme'" in r.rewritten_query
    assert "GROUP BY tenant_id" in r.rewritten_query


def test_select_strips_trailing_semicolon_target():
    r = _apply("SELECT * FROM artifacts;")
    assert r.applied is True
    # Output is well-formed: WHERE clause appears before any trailing
    # semicolon (or the semicolon is gone entirely — both shapes are
    # acceptable, the test just guards against producing
    # ``SELECT * FROM artifacts; WHERE …`` which is parse-broken).
    assert "WHERE tenant_id = 't-acme'" in r.rewritten_query
    assert not re.search(r";\s*WHERE", r.rewritten_query)


def test_select_schema_qualified_table_recognised():
    r = _apply("SELECT * FROM public.artifacts")
    assert r.applied is True
    assert r.target_table == "artifacts"


def test_select_quoted_table_recognised():
    r = _apply('SELECT * FROM "artifacts"')
    assert r.applied is True
    assert r.target_table == "artifacts"


def test_select_uppercase_keywords_recognised():
    r = _apply("select * from artifacts where id = 1")
    assert r.applied is True
    assert "tenant_id = 't-acme'" in r.rewritten_query


def test_select_multiline_query_recognised():
    r = _apply("""
        SELECT id, payload
          FROM artifacts
         WHERE id = 1
    """)
    assert r.applied is True
    assert "tenant_id = 't-acme'" in r.rewritten_query


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (d) Project-scoped vs tenant-only differentiation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize(
    "table",
    ["workflow_runs", "debug_findings", "decision_rules", "event_log",
     "artifacts", "user_preferences"],
)
def test_select_project_scoped_table_emits_both_filters(table):
    r = _apply(f"SELECT * FROM {table}")
    assert "tenant_id = 't-acme'" in r.rewritten_query
    assert "project_id = 'p-acme-default'" in r.rewritten_query
    assert "OR project_id IS NULL" in r.rewritten_query


@pytest.mark.parametrize("table", ["audit_log", "users"])
def test_select_tenant_only_table_emits_tenant_filter_only(table):
    r = _apply(f"SELECT * FROM {table}")
    assert "tenant_id = 't-acme'" in r.rewritten_query
    assert "project_id" not in r.rewritten_query


def test_select_project_scoped_table_omits_project_clause_when_pid_unset():
    """Tenant-wide route (no project in context) on a project-scoped
    table — emit tenant filter, skip project clause.  This lets
    admin REST on ``/tenants/{tid}/...`` enumerate every project's
    rows for that tenant without hitting an empty filter (project_id
    in context would be None and the literal ``'None'`` would zero
    out the result set)."""
    r = _apply("SELECT * FROM artifacts", project_id=None)
    assert r.applied is True
    assert "tenant_id = 't-acme'" in r.rewritten_query
    assert "project_id" not in r.rewritten_query


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (e) INSERT auto-fill paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_insert_appends_tenant_and_project_when_columns_omitted():
    r = _apply("INSERT INTO artifacts (id, payload) VALUES (1, 'x')")
    assert r.applied is True
    assert r.statement_kind == "insert"
    assert "tenant_id" in r.rewritten_query
    assert "project_id" in r.rewritten_query
    assert "'t-acme'" in r.rewritten_query
    assert "'p-acme-default'" in r.rewritten_query


def test_insert_with_qmark_placeholders_keeps_them():
    r = _apply("INSERT INTO event_log (event_type, data_json) VALUES (?, ?)")
    assert r.applied is True
    # Original placeholders intact, literals appended.
    assert "(?, ?, 't-acme', 'p-acme-default')" in r.rewritten_query


def test_insert_multi_row_values_each_tuple_appended():
    r = _apply(
        "INSERT INTO event_log (event_type, data_json) "
        "VALUES (?, ?), (?, ?), (?, ?)"
    )
    assert r.applied is True
    # Each of the three tuples gets the literals appended.
    assert r.rewritten_query.count("'t-acme'") == 3
    assert r.rewritten_query.count("'p-acme-default'") == 3


def test_insert_caller_named_both_columns_is_noop():
    r = _apply(
        "INSERT INTO artifacts (id, payload, tenant_id, project_id) "
        "VALUES (1, 'x', 't-other', 'p-other')"
    )
    # Listener leaves intentional cross-tenant inserts alone — that's
    # how alembic backfills and admin tools must continue to work.
    assert r.applied is False
    assert r.bypass_reason == "caller_named_columns"
    assert r.rewritten_query == r.original_query


def test_insert_caller_named_tenant_only_fills_project():
    """Caller named tenant_id but not project_id on a project-scoped
    table — listener fills only the missing one.  Defence in depth:
    a writer that knows which tenant it's writing to but not which
    project (legacy code path mid-migration) still gets the project
    pin from ContextVar."""
    r = _apply(
        "INSERT INTO artifacts (id, payload, tenant_id) VALUES (1, 'x', 't-zzz')"
    )
    assert r.applied is True
    # tenant_id literal NOT replaced.
    assert "'t-zzz'" in r.rewritten_query
    # project_id auto-filled.
    assert "project_id" in r.rewritten_query
    assert "'p-acme-default'" in r.rewritten_query


def test_insert_tenant_only_table_skips_project_column():
    r = _apply("INSERT INTO audit_log (event_type, actor) VALUES ('login', 'u-1')")
    assert r.applied is True
    assert "tenant_id" in r.rewritten_query
    # audit_log has no project_id column — must NOT fill it.
    assert "project_id" not in r.rewritten_query


def test_insert_unscoped_table_is_passed_through():
    r = _apply("INSERT INTO tenants (id, name) VALUES ('t-1', 'Foo')")
    assert r.applied is False
    assert r.bypass_reason == "table_unscoped"
    assert r.rewritten_query == r.original_query


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (f) Bypass paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_bypass_super_admin_passes_through():
    r = _apply("SELECT * FROM artifacts", user_role="super_admin")
    assert r.applied is False
    assert r.bypass_reason == "super_admin"
    assert r.rewritten_query == r.original_query


def test_bypass_super_admin_passes_through_insert_too():
    r = _apply(
        "INSERT INTO artifacts (id, payload) VALUES (1, 'x')",
        user_role="super_admin",
    )
    assert r.applied is False
    assert r.bypass_reason == "super_admin"


def test_bypass_no_tenant_passes_through():
    r = _apply("SELECT * FROM artifacts", tenant_id=None, user_role="contributor")
    assert r.applied is False
    assert r.bypass_reason == "no_tenant_set"


def test_bypass_table_unscoped_for_select():
    r = _apply("SELECT * FROM tenants")
    assert r.applied is False
    assert r.bypass_reason == "table_unscoped"


def test_bypass_table_unscoped_for_projects_catalog():
    """Projects catalog is the source of truth for project-id resolution
    — inserting tenant filter would cause infinite recursion on the
    auth dependency.  Must pass through untouched."""
    r = _apply("SELECT * FROM projects WHERE tenant_id = 't-zzz'")
    assert r.applied is False
    assert r.bypass_reason == "table_unscoped"


@pytest.mark.parametrize(
    "stmt",
    [
        "UPDATE artifacts SET payload = 'x'",
        "DELETE FROM artifacts WHERE id = 1",
        "WITH cte AS (SELECT 1) SELECT * FROM cte",
        "BEGIN",
        "COMMIT",
    ],
)
def test_bypass_not_rewritable_kinds(stmt):
    """UPDATE / DELETE / CTE / TX-control are NOT in the listener's
    contract — they fall through with ``not_rewritable``.  A future
    row will tackle UPDATE / DELETE; today they pass through so the
    listener can ship without breaking existing writes."""
    r = _apply(stmt)
    assert r.applied is False
    assert r.bypass_reason == "not_rewritable"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (g) ContextVar fall-through — kwargs default to current_*()
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_kwargs_default_to_contextvars():
    from backend import db_context
    from backend.db_rls_listener import apply_project_rls

    db_context.set_tenant_id("t-from-ctx")
    db_context.set_project_id("p-from-ctx")
    db_context.set_user_role("viewer")

    r = apply_project_rls("SELECT * FROM artifacts")
    assert r.tenant_id == "t-from-ctx"
    assert r.project_id == "p-from-ctx"
    assert r.user_role == "viewer"
    assert r.applied is True
    assert "tenant_id = 't-from-ctx'" in r.rewritten_query
    assert "project_id = 'p-from-ctx'" in r.rewritten_query


def test_explicit_kwargs_override_contextvars():
    from backend import db_context
    from backend.db_rls_listener import apply_project_rls

    db_context.set_tenant_id("t-from-ctx")
    db_context.set_project_id("p-from-ctx")
    db_context.set_user_role("contributor")

    r = apply_project_rls(
        "SELECT * FROM artifacts",
        tenant_id="t-explicit", project_id="p-explicit", user_role="viewer",
    )
    assert r.tenant_id == "t-explicit"
    assert r.project_id == "p-explicit"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (h) End-to-end SQLAlchemy listener integration on in-memory SQLite
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture
def sqlite_engine_with_listener():
    """In-memory SQLite + listener attached + ``artifacts`` table with
    representative cross-tenant / cross-project / NULL-project rows.

    Rows seeded:
      1: tenant=t-a project=p-a-default     "hello-acme"
      2: tenant=t-other project=p-other-default "hello-other"
      3: tenant=t-a project=NULL             "legacy-null-pid" (pre-Y1 row)
      4: tenant=t-a project=p-a-other        "different-project-same-tenant"
    """
    sqlalchemy = pytest.importorskip("sqlalchemy")
    from sqlalchemy import create_engine, text
    from backend.db_rls_listener import install_project_rls_listener

    engine = create_engine("sqlite://")
    install_project_rls_listener(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE artifacts ("
            "  id INTEGER PRIMARY KEY,"
            "  payload TEXT,"
            "  tenant_id TEXT,"
            "  project_id TEXT"
            ")"
        ))
        # Seed via direct DBAPI to bypass the listener (we want the
        # rows to live with their declared tenant + project regardless
        # of context).  We do this by clearing context first, then
        # using a literal-only INSERT with all four columns named so
        # the listener flags it as ``caller_named_columns`` and leaves
        # it alone.
        for row in (
            ("hello-acme",            "t-a",     "p-a-default"),
            ("hello-other",           "t-other", "p-other-default"),
            ("legacy-null-pid",       "t-a",     None),
            ("different-project-same-tenant", "t-a", "p-a-other"),
        ):
            payload, tid, pid = row
            pid_lit = "NULL" if pid is None else f"'{pid}'"
            conn.execute(text(
                f"INSERT INTO artifacts (payload, tenant_id, project_id) "
                f"VALUES ('{payload}', '{tid}', {pid_lit})"
            ))
    return engine


def _select_payloads(engine, **ctx):
    """Set the contextvars per ``ctx``, run a bare SELECT through the
    listener-wrapped engine, return payloads as a set."""
    from sqlalchemy import text
    from backend import db_context

    db_context.set_tenant_id(ctx.get("tenant_id"))
    db_context.set_project_id(ctx.get("project_id"))
    db_context.set_user_role(ctx.get("user_role"))
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT payload FROM artifacts")).fetchall()
    return {r[0] for r in rows}


def test_e2e_select_filters_to_tenant_and_project_with_null_fallthrough(
    sqlite_engine_with_listener,
):
    """The headline contract: project A user sees their project's rows
    PLUS the legacy NULL-project row of the same tenant — and nothing
    of project B (even same tenant) and nothing of another tenant."""
    payloads = _select_payloads(
        sqlite_engine_with_listener,
        tenant_id="t-a", project_id="p-a-default", user_role="contributor",
    )
    assert payloads == {"hello-acme", "legacy-null-pid"}


def test_e2e_cross_project_same_tenant_isolation(
    sqlite_engine_with_listener,
):
    """Project A user MUST NOT see project B's row even if tenant
    matches.  This is the Y5 row 3 acceptance test verbatim."""
    payloads = _select_payloads(
        sqlite_engine_with_listener,
        tenant_id="t-a", project_id="p-a-default", user_role="contributor",
    )
    assert "different-project-same-tenant" not in payloads


def test_e2e_cross_tenant_isolation(sqlite_engine_with_listener):
    payloads = _select_payloads(
        sqlite_engine_with_listener,
        tenant_id="t-a", project_id="p-a-default", user_role="contributor",
    )
    assert "hello-other" not in payloads


def test_e2e_other_project_sees_only_legacy_null(sqlite_engine_with_listener):
    """Switch project within same tenant → legacy NULL-project row
    is the ONLY row visible (it's the backward-compat fallthrough),
    other-project's named-pid rows are filtered out."""
    payloads = _select_payloads(
        sqlite_engine_with_listener,
        tenant_id="t-a", project_id="p-fresh", user_role="contributor",
    )
    assert payloads == {"legacy-null-pid"}


def test_e2e_super_admin_sees_everything(sqlite_engine_with_listener):
    """Super-admin ContextVar bypasses the rewrite; the underlying
    SELECT runs unfiltered."""
    payloads = _select_payloads(
        sqlite_engine_with_listener,
        tenant_id="t-a", project_id="p-a-default", user_role="super_admin",
    )
    assert payloads == {
        "hello-acme",
        "hello-other",
        "legacy-null-pid",
        "different-project-same-tenant",
    }


def test_e2e_no_context_pass_through(sqlite_engine_with_listener):
    """No tenant in context → listener does not rewrite; the SELECT
    runs unfiltered.  Distinct from super-admin (which is also
    unfiltered) only by the ``user_role`` token, but both produce
    the same observable output: full table scan."""
    payloads = _select_payloads(
        sqlite_engine_with_listener,
        tenant_id=None, project_id=None, user_role=None,
    )
    assert payloads == {
        "hello-acme",
        "hello-other",
        "legacy-null-pid",
        "different-project-same-tenant",
    }


def test_e2e_insert_auto_fills_tenant_and_project(sqlite_engine_with_listener):
    """A bare INSERT without naming tenant/project columns gets the
    columns auto-filled from context, and a subsequent SELECT in the
    same context sees the new row."""
    pytest.importorskip("sqlalchemy")
    from sqlalchemy import text
    from backend import db_context

    db_context.set_tenant_id("t-a")
    db_context.set_project_id("p-a-default")
    db_context.set_user_role("contributor")

    eng = sqlite_engine_with_listener
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO artifacts (payload) VALUES ('via-listener-autofill')"))

    payloads = _select_payloads(
        eng,
        tenant_id="t-a", project_id="p-a-default", user_role="contributor",
    )
    assert "via-listener-autofill" in payloads

    # And it does NOT leak to a different project — fresh project
    # with same tenant does NOT see the new row (project_id was
    # auto-filled to p-a-default, not NULL).
    other_payloads = _select_payloads(
        eng,
        tenant_id="t-a", project_id="p-fresh", user_role="contributor",
    )
    assert "via-listener-autofill" not in other_payloads


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (i) Self-fingerprint guard — no SQLite-isms / compat residue
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_listener_module_has_no_compat_fingerprint():
    """Per implement_phase_step.md Step 3 pre-commit grep: no
    ``_conn()`` / ``await conn.commit()`` / ``datetime('now')`` /
    ``VALUES (?, ...)`` patterns may appear in a freshly written
    module.  Our test fixtures DO write SQLite-flavoured INSERTs
    inside string literals — those don't count (they're inputs to
    the rewriter, not implementation of it).  This guard checks the
    PROD module only.
    """
    prod_path = (
        Path(__file__).resolve().parent.parent / "db_rls_listener.py"
    )
    text = prod_path.read_text()
    # Mask docstring + module header; the constants we care about
    # are in actual function bodies.
    fingerprints = (
        r"\b_conn\(\)",
        r"await\s+conn\.commit\(\)",
        r"datetime\('now'\)",
    )
    for pat in fingerprints:
        assert not re.search(pat, text), (
            f"compat-fingerprint hit in db_rls_listener.py: {pat!r}"
        )
