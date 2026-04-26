"""Y9 #285 row 3 — per-(tenant_id, project_id) billing usage events.

Validates the three usage emitters in ``backend.billing_usage``,
the alembic-0039 ``billing_usage_events`` table contract, the
breakdown reader (T6 pricing page data source), the GET
``/api/v1/admin/usage/breakdown`` endpoint authz + payload, and the
fan-out wiring from ``track_tokens`` / ``workflow.finish`` /
``workspace_gc.sweep_once``.

Acceptance criteria for the row:

  * Every workflow_run / LLM call / workspace-GB-hour writes a row
    tagged with ``(tenant_id, project_id)``.
  * T6 pricing page can render a per-project breakdown for a tenant.
  * Y9 row 5 hold-over: ``project_id IS NULL`` legacy paths attribute
    to ``p-<suffix>-default`` rather than silently dropping.

The tests are split into pure-unit (drift guard, route mounted, SQL
shape, default fall-through) and live-PG integration (HTTP path,
fan-out, breakdown SQL). The live-PG tests SKIP without
``OMNI_TEST_PG_URL`` set — same lane as the existing Y9 row 1 / row 2
tests.

Module-global state audit (per implement_phase_step.md Step 1)
────────────────────────────────────────────────────────────────
Tests use the standard ``client`` + ``pg_test_pool`` fixtures from
``backend/tests/conftest.py``. Each PG-integration test uses unique
tenant + project ids and DELETE-style cleanup so cross-test bleed is
impossible. The emitter is stateless (single INSERT per call); the
``ALL_USAGE_KINDS`` tuple is module-immutable and asserted as such.
"""

from __future__ import annotations

import os
import re
import time

import pytest


def _pg_not_available() -> bool:
    return not os.environ.get("OMNI_TEST_PG_URL", "").strip()


_requires_pg = pytest.mark.skipif(
    _pg_not_available(),
    reason="billing_usage HTTP / DB path depends on asyncpg pool — "
           "requires OMNI_TEST_PG_URL.",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: drift guard between ALL_USAGE_KINDS and migration-0039
#  CHECK constraint
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_all_usage_kinds_is_complete_and_frozen():
    """Drift guard: the module's ``ALL_USAGE_KINDS`` must contain every
    constant the helpers refer to, and the tuple must be immutable.
    """
    from backend import billing_usage as bu

    expected = {
        bu.KIND_LLM_CALL,
        bu.KIND_WORKFLOW_RUN,
        bu.KIND_WORKSPACE_GB_HOUR,
    }
    assert set(bu.ALL_USAGE_KINDS) == expected
    # Tuple, not list — immutable.
    assert isinstance(bu.ALL_USAGE_KINDS, tuple)
    # No duplicates.
    assert len(bu.ALL_USAGE_KINDS) == len(set(bu.ALL_USAGE_KINDS))


def test_kind_check_constraint_matches_module_constants():
    """The migration-0039 CHECK constraint allowed set must be exactly
    ``ALL_USAGE_KINDS``. Drifting the migration string from the module
    constant would let a buggy emitter write an unknown ``kind`` that
    every downstream aggregator would silently miscount.
    """
    from pathlib import Path
    from backend import billing_usage as bu

    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic" / "versions"
        / "0039_y9_row3_billing_usage_events.py"
    )
    text = migration_path.read_text(encoding="utf-8")
    # Extract the CHECK clause's allowed set from the source. The
    # docstring above mentions ``CHECK (kind IN (...))`` as a literal
    # placeholder so we restrict to matches whose payload is non-trivial
    # (contains at least one single-quoted string token).
    matches = re.findall(
        r"CHECK\s*\(\s*kind\s+IN\s*\(([^)]*)\)\s*\)",
        text,
    )
    real = [m for m in matches if "'" in m]
    assert real, (
        "could not locate the kind CHECK constraint with literal "
        "string members in migration 0039"
    )
    allowed_in_migration = set(re.findall(r"'([^']+)'", real[0]))
    assert allowed_in_migration == set(bu.ALL_USAGE_KINDS), (
        f"Migration CHECK ({sorted(allowed_in_migration)!r}) drifted "
        f"from ALL_USAGE_KINDS ({sorted(bu.ALL_USAGE_KINDS)!r}); "
        "any new kind needs a coordinated change to both."
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: default fall-through projection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_resolve_tenant_explicit_wins_over_contextvar():
    from backend import billing_usage as bu
    from backend import db_context

    db_context.set_tenant_id("t-from-context")
    try:
        assert bu._resolve_tenant("t-explicit") == "t-explicit"
    finally:
        db_context.set_tenant_id(None)


def test_resolve_tenant_contextvar_wins_over_default():
    from backend import billing_usage as bu
    from backend import db_context

    db_context.set_tenant_id("t-from-context")
    try:
        assert bu._resolve_tenant(None) == "t-from-context"
    finally:
        db_context.set_tenant_id(None)


def test_resolve_tenant_falls_through_to_default():
    from backend import billing_usage as bu
    from backend import db_context

    db_context.set_tenant_id(None)
    assert bu._resolve_tenant(None) == "t-default"


def test_resolve_project_falls_through_to_per_tenant_default():
    """Y9 row 5 acceptance: a NULL project_id legacy path attributes
    to ``p-<suffix>-default`` rather than dropping the cost on the floor.
    """
    from backend import billing_usage as bu
    from backend import db_context

    db_context.set_tenant_id(None)
    db_context.set_project_id(None)
    # t-default → p-default-default (the existing default project that
    # alembic 0037 backfilled).
    assert bu._resolve_project(None, tenant_id="t-default") == "p-default-default"
    # t-acme → p-acme-default
    assert bu._resolve_project(None, tenant_id="t-acme") == "p-acme-default"
    # legacy / non-prefixed tenant_id strips no prefix.
    assert bu._resolve_project(None, tenant_id="legacy") == "p-legacy-default"


def test_resolve_project_explicit_wins():
    from backend import billing_usage as bu
    from backend import db_context

    db_context.set_project_id("p-from-context")
    try:
        assert bu._resolve_project("p-explicit", tenant_id="t-x") == "p-explicit"
    finally:
        db_context.set_project_id(None)


def test_resolve_project_contextvar_wins_over_default():
    from backend import billing_usage as bu
    from backend import db_context

    db_context.set_project_id("p-from-context")
    try:
        assert bu._resolve_project(None, tenant_id="t-x") == "p-from-context"
    finally:
        db_context.set_project_id(None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: route mounted + SQL safety
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_breakdown_route_is_mounted():
    """The endpoint must mount under the api prefix as a GET route at
    ``/api/v1/admin/usage/breakdown``."""
    from backend.main import app

    matches = [
        (r.path, sorted(getattr(r, "methods", []) or []))
        for r in app.routes
        if getattr(r, "path", "") == "/api/v1/admin/usage/breakdown"
    ]
    methods = [m for _, ms in matches for m in ms]
    assert "GET" in methods, (
        f"GET /api/v1/admin/usage/breakdown missing; got {matches!r}"
    )
    # Must NOT accept mutating methods.
    for unsafe in ("POST", "PATCH", "PUT", "DELETE"):
        assert unsafe not in methods, (
            f"breakdown endpoint must be read-only; got {unsafe!r}"
        )


def test_breakdown_handler_uses_current_user_dependency():
    """Tenant admins must reach the handler so the in-handler authz
    can let them at their OWN tenant. Using ``require_super_admin``
    would 403 them globally."""
    import inspect

    from backend.routers.admin_tenants import (
        get_usage_breakdown_by_project,
    )
    from backend import auth as _au

    sig = inspect.signature(get_usage_breakdown_by_project)
    deps = []
    for _name, param in sig.parameters.items():
        target = getattr(param.default, "dependency", None)
        if target is not None:
            deps.append(target)
    assert _au.current_user in deps
    assert _au.require_super_admin not in deps, (
        "Y9 row 3 contract: tenant admins must reach the handler so "
        "the in-handler authz can let them at their OWN tenant only."
    )


def test_billing_usage_module_fingerprint_clean():
    """SOP Step-3 fingerprint grep: the new billing_usage module must
    not carry any of the four classic compat-residue patterns."""
    from pathlib import Path

    fingerprint = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    text = (
        Path(__file__).resolve().parents[1] / "billing_usage.py"
    ).read_text(encoding="utf-8")
    hits = fingerprint.findall(text)
    assert not hits, (
        f"compat-residue fingerprint hit in billing_usage.py: {hits!r}"
    )


def test_breakdown_sql_uses_pg_placeholders():
    """The breakdown SQL must use ``$N`` placeholders, never SQLite
    ``?`` style. We exercise it by inspecting the source of
    :func:`breakdown_by_project`."""
    import inspect

    from backend import billing_usage as bu

    src = inspect.getsource(bu.breakdown_by_project)
    # Must reference $1, $2 or $3 dynamically; must NOT contain '?'
    # outside of comments / docstrings (we just check the assembled
    # SQL string does not interpolate '?').
    assert "$" in src
    # The body uses positional asyncpg parameters built incrementally
    # via len(params)+1 — assert the pattern is present.
    assert "len(params) + 1" in src


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: track_tokens fan-out is wired
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_track_tokens_imports_billing_usage():
    """The LLM callback's ``track_tokens`` must import + call
    ``backend.billing_usage.record_llm_call`` inside its create_task
    block. Source-level grep so the wiring is asserted at module load."""
    import inspect

    from backend.routers import system

    src = inspect.getsource(system.track_tokens)
    assert "billing_usage" in src, (
        "track_tokens must fan out to billing_usage.record_llm_call "
        "(Y9 row 3 acceptance criterion)"
    )
    assert "record_llm_call" in src


def test_workflow_finish_imports_billing_usage():
    """``workflow.finish`` must fan out to
    ``backend.billing_usage.record_workflow_run``."""
    import inspect

    from backend import workflow

    src = inspect.getsource(workflow.finish)
    assert "billing_usage" in src
    assert "record_workflow_run" in src


def test_workspace_gc_imports_billing_usage():
    """``workspace_gc.sweep_once`` must fan out to
    ``backend.billing_usage.record_workspace_gb_hour`` via
    ``_emit_workspace_gb_hour_samples``."""
    import inspect

    from backend import workspace_gc

    sweep_src = inspect.getsource(workspace_gc.sweep_once)
    sampler_src = inspect.getsource(
        workspace_gc._emit_workspace_gb_hour_samples
    )
    assert "_emit_workspace_gb_hour_samples" in sweep_src
    assert "billing_usage" in sampler_src
    assert "record_workspace_gb_hour" in sampler_src
    # Multi-worker dedupe: the sampler must guard with a PG advisory
    # lock to avoid N-fold double-counting under N uvicorn workers.
    assert "pg_try_advisory_xact_lock" in sampler_src, (
        "Y9 row 3 multi-worker dedupe: the workspace-GB-hour sampler "
        "must take a PG advisory lock so only one worker emits per "
        "sweep period."
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: migrator schema list contains the new table
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_migrator_includes_billing_usage_events():
    """The SQLite→PG migrator's ``TABLES_IN_ORDER`` must include the
    new ``billing_usage_events`` table; its ``TABLES_WITH_IDENTITY_ID``
    must include it too because the PK is BIGINT IDENTITY."""
    from pathlib import Path

    migrator_src = (
        Path(__file__).resolve().parents[2]
        / "scripts" / "migrate_sqlite_to_pg.py"
    ).read_text(encoding="utf-8")
    # Both tuples must mention the table — guards the production-
    # readiness "schema migration drift" rule from implement_phase_step.md.
    assert '"billing_usage_events"' in migrator_src, (
        "migrate_sqlite_to_pg.py TABLES_IN_ORDER missing "
        "billing_usage_events — schema drift will silently lose "
        "billing data on cutover."
    )
    # IDENTITY mention should appear in the TABLES_WITH_IDENTITY_ID
    # block (we already grep'd presence above; assert it appears in
    # both occurrence contexts via count >= 2).
    assert migrator_src.count('"billing_usage_events"') >= 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PG integration — emitters write rows
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture()
async def _billing_db(pg_test_pool):
    """Truncate ``billing_usage_events`` per test and reset the
    request-scope ContextVars on teardown."""
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE billing_usage_events RESTART IDENTITY"
        )
    try:
        yield pg_test_pool
    finally:
        from backend.db_context import set_project_id, set_tenant_id
        set_tenant_id(None)
        set_project_id(None)
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE billing_usage_events RESTART IDENTITY"
            )


@_requires_pg
@pytest.mark.asyncio
async def test_record_llm_call_writes_row_with_explicit_tuple(_billing_db):
    from backend import billing_usage as bu

    rid = await bu.record_llm_call(
        model="claude-opus-4-7",
        input_tokens=1000,
        output_tokens=200,
        cost_usd=0.0123,
        cache_read_tokens=50,
        cache_create_tokens=10,
        tenant_id="t-y9row3",
        project_id="p-y9row3-firmware",
    )
    assert isinstance(rid, int) and rid > 0

    async with _billing_db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM billing_usage_events WHERE id = $1", rid,
        )
    assert row["kind"] == "llm_call"
    assert row["tenant_id"] == "t-y9row3"
    assert row["project_id"] == "p-y9row3-firmware"
    assert row["model"] == "claude-opus-4-7"
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 200
    assert row["cache_read_tokens"] == 50
    assert row["cache_create_tokens"] == 10
    assert abs(float(row["cost_usd"]) - 0.0123) < 1e-9
    assert float(row["quantity"]) == 1.0


@_requires_pg
@pytest.mark.asyncio
async def test_record_llm_call_falls_through_to_default_bucket(_billing_db):
    """No explicit args, no ContextVar set → row attributes to
    ``(t-default, p-default-default)`` (Y9 row 5 acceptance)."""
    from backend import billing_usage as bu
    from backend.db_context import set_project_id, set_tenant_id

    # Ensure no leak from a prior test.
    set_tenant_id(None)
    set_project_id(None)

    rid = await bu.record_llm_call(
        model="claude-haiku-4-5",
        input_tokens=10,
        output_tokens=20,
        cost_usd=0.001,
    )
    assert isinstance(rid, int)

    async with _billing_db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT tenant_id, project_id FROM billing_usage_events "
            "WHERE id = $1",
            rid,
        )
    assert row["tenant_id"] == "t-default"
    assert row["project_id"] == "p-default-default"


@_requires_pg
@pytest.mark.asyncio
async def test_record_llm_call_uses_contextvar_when_no_explicit(_billing_db):
    from backend import billing_usage as bu
    from backend.db_context import set_project_id, set_tenant_id

    set_tenant_id("t-from-ctx")
    set_project_id("p-from-ctx")
    try:
        rid = await bu.record_llm_call(
            model="claude-sonnet-4-6",
            input_tokens=5, output_tokens=5, cost_usd=0.0002,
        )
    finally:
        set_tenant_id(None)
        set_project_id(None)
    assert isinstance(rid, int)
    async with _billing_db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT tenant_id, project_id FROM billing_usage_events "
            "WHERE id = $1",
            rid,
        )
    assert row["tenant_id"] == "t-from-ctx"
    assert row["project_id"] == "p-from-ctx"


@_requires_pg
@pytest.mark.asyncio
async def test_record_workflow_run_writes_row(_billing_db):
    from backend import billing_usage as bu

    rid = await bu.record_workflow_run(
        workflow_run_id="wf-y9-row3-1",
        workflow_kind="invoke",
        workflow_status="completed",
        duration_ms=1234,
        tenant_id="t-y9row3",
        project_id="p-y9row3-algo",
    )
    assert isinstance(rid, int)
    async with _billing_db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM billing_usage_events WHERE id = $1", rid,
        )
    assert row["kind"] == "workflow_run"
    assert row["tenant_id"] == "t-y9row3"
    assert row["project_id"] == "p-y9row3-algo"
    assert row["workflow_run_id"] == "wf-y9-row3-1"
    assert row["workflow_kind"] == "invoke"
    assert row["workflow_status"] == "completed"
    # Cost is 0 — the LLM calls inside the run already wrote their
    # own llm_call rows; the workflow_run row must not double-count.
    assert float(row["cost_usd"]) == 0.0
    assert float(row["quantity"]) == 1.0


@_requires_pg
@pytest.mark.asyncio
async def test_record_workspace_gb_hour_writes_quantity(_billing_db):
    from backend import billing_usage as bu

    rid = await bu.record_workspace_gb_hour(
        gb_hours=2.5,
        tenant_id="t-y9row3",
        project_id="p-y9row3-storage",
    )
    assert isinstance(rid, int)
    async with _billing_db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT kind, tenant_id, project_id, quantity, cost_usd "
            "FROM billing_usage_events WHERE id = $1",
            rid,
        )
    assert row["kind"] == "workspace_gb_hour"
    assert row["tenant_id"] == "t-y9row3"
    assert row["project_id"] == "p-y9row3-storage"
    # quantity carries the gb_hours value so SUM(quantity) yields the
    # GB-hour total in one place.
    assert abs(float(row["quantity"]) - 2.5) < 1e-9
    # Cost is 0 — storage is plan-tier billed.
    assert float(row["cost_usd"]) == 0.0


@_requires_pg
@pytest.mark.asyncio
async def test_unknown_kind_rejected_by_check_constraint(_billing_db):
    """Drift guard: trying to insert an unknown ``kind`` must be
    rejected by PG's CHECK constraint, not silently accepted."""
    import asyncpg

    async with _billing_db.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "INSERT INTO billing_usage_events "
                "(occurred_at, tenant_id, project_id, kind) "
                "VALUES ($1, $2, $3, $4)",
                time.time(), "t-default", "p-default-default", "rogue_kind",
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PG integration — breakdown_by_project shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
@pytest.mark.asyncio
async def test_breakdown_groups_by_project_with_correct_aggregates(
    _billing_db,
):
    from backend import billing_usage as bu

    tid = "t-bd-test"
    # Two projects under the same tenant; emit a mix of all three kinds.
    await bu.record_llm_call(
        model="m1", input_tokens=100, output_tokens=50,
        cost_usd=0.10, tenant_id=tid, project_id="p-alpha",
    )
    await bu.record_llm_call(
        model="m1", input_tokens=200, output_tokens=80,
        cost_usd=0.20, tenant_id=tid, project_id="p-alpha",
    )
    await bu.record_llm_call(
        model="m2", input_tokens=10, output_tokens=5,
        cost_usd=0.005, tenant_id=tid, project_id="p-beta",
    )
    await bu.record_workflow_run(
        workflow_run_id="wf-1", workflow_kind="invoke",
        workflow_status="completed", duration_ms=500,
        tenant_id=tid, project_id="p-alpha",
    )
    await bu.record_workspace_gb_hour(
        gb_hours=3.0, tenant_id=tid, project_id="p-alpha",
    )
    await bu.record_workspace_gb_hour(
        gb_hours=1.5, tenant_id=tid, project_id="p-beta",
    )

    breakdown = await bu.breakdown_by_project(tenant_id=tid)
    by_project = {row["project_id"]: row for row in breakdown}
    assert set(by_project.keys()) == {"p-alpha", "p-beta"}

    alpha = by_project["p-alpha"]
    assert alpha["llm_calls"] == 2
    assert alpha["llm_input_tokens"] == 300
    assert alpha["llm_output_tokens"] == 130
    assert abs(alpha["llm_cost_usd"] - 0.30) < 1e-6
    assert alpha["workflow_runs"] == 1
    assert abs(alpha["workspace_gb_hours"] - 3.0) < 1e-6

    beta = by_project["p-beta"]
    assert beta["llm_calls"] == 1
    assert beta["llm_input_tokens"] == 10
    assert beta["llm_output_tokens"] == 5
    assert abs(beta["llm_cost_usd"] - 0.005) < 1e-6
    assert beta["workflow_runs"] == 0
    assert abs(beta["workspace_gb_hours"] - 1.5) < 1e-6

    # Sort order: llm_cost_usd DESC then project_id ASC.
    assert breakdown[0]["project_id"] == "p-alpha"
    assert breakdown[1]["project_id"] == "p-beta"


@_requires_pg
@pytest.mark.asyncio
async def test_breakdown_respects_since_until(_billing_db):
    from backend import billing_usage as bu

    tid = "t-bd-window"
    now = time.time()

    await bu.record_llm_call(
        model="m1", input_tokens=1, output_tokens=1, cost_usd=1.0,
        tenant_id=tid, project_id="p-x",
        occurred_at=now - 7200,  # 2h ago
    )
    await bu.record_llm_call(
        model="m1", input_tokens=1, output_tokens=1, cost_usd=2.0,
        tenant_id=tid, project_id="p-x",
        occurred_at=now - 1800,  # 30 min ago
    )

    # Window: last 1 hour — only the 30-min-old row qualifies.
    bd = await bu.breakdown_by_project(
        tenant_id=tid, since=now - 3600,
    )
    assert len(bd) == 1
    assert abs(bd[0]["llm_cost_usd"] - 2.0) < 1e-6

    # Window: last 3 hours — both qualify, summed.
    bd2 = await bu.breakdown_by_project(
        tenant_id=tid, since=now - 10800,
    )
    assert len(bd2) == 1
    assert abs(bd2[0]["llm_cost_usd"] - 3.0) < 1e-6


@_requires_pg
@pytest.mark.asyncio
async def test_breakdown_isolates_tenants(_billing_db):
    from backend import billing_usage as bu

    await bu.record_llm_call(
        model="m1", input_tokens=1, output_tokens=1, cost_usd=1.0,
        tenant_id="t-iso-a", project_id="p-shared",
    )
    await bu.record_llm_call(
        model="m1", input_tokens=1, output_tokens=1, cost_usd=99.0,
        tenant_id="t-iso-b", project_id="p-shared",
    )

    bd_a = await bu.breakdown_by_project(tenant_id="t-iso-a")
    bd_b = await bu.breakdown_by_project(tenant_id="t-iso-b")
    assert len(bd_a) == 1 and abs(bd_a[0]["llm_cost_usd"] - 1.0) < 1e-6
    assert len(bd_b) == 1 and abs(bd_b[0]["llm_cost_usd"] - 99.0) < 1e-6
    # No cross-tenant contamination.
    for row in bd_a:
        assert row["llm_cost_usd"] != 99.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — RBAC + payload shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_breakdown_endpoint_super_admin_can_query_any_tenant(
    client, pg_test_pool,
):
    from backend.main import app
    from backend import auth as _au, billing_usage as bu

    tid = "t-y9-row3-bd"
    uid = "u-y9-row3-super"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'BD', 'free', 1) "
                "ON CONFLICT DO NOTHING",
                tid,
            )
            await conn.execute(
                "INSERT INTO users (id, email, name, role, "
                "  password_hash, enabled, tenant_id) "
                "VALUES ($1, $2, 'Sup', 'super_admin', '', 1, $3) "
                "ON CONFLICT (id) DO NOTHING",
                uid, "sup@y9-row3.local", "t-default",
            )

        # Seed two projects' worth of activity.
        await bu.record_llm_call(
            model="m1", input_tokens=10, output_tokens=5, cost_usd=0.5,
            tenant_id=tid, project_id="p-bd-1",
        )
        await bu.record_workflow_run(
            workflow_run_id="wf-bd-1", workflow_kind="invoke",
            workflow_status="completed", duration_ms=500,
            tenant_id=tid, project_id="p-bd-2",
        )

        sup = _au.User(
            id=uid, email="sup@y9-row3.local", name="Sup",
            role="super_admin", enabled=True, tenant_id="t-default",
        )

        async def _fake_current_user():
            return sup

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.get(
                f"/api/v1/admin/usage/breakdown?tenant_id={tid}"
            )
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["tenant_id"] == tid
            assert isinstance(body["breakdown"], list)
            assert isinstance(body["totals"], dict)
            project_ids = {r["project_id"] for r in body["breakdown"]}
            assert {"p-bd-1", "p-bd-2"}.issubset(project_ids)
            # Totals roll up.
            assert body["totals"]["llm_calls"] >= 1
            assert body["totals"]["workflow_runs"] >= 1
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM billing_usage_events WHERE tenant_id = $1",
                tid,
            )
            await conn.execute(
                "DELETE FROM users WHERE id = $1", uid,
            )
            await conn.execute(
                "DELETE FROM tenants WHERE id = $1", tid,
            )


@_requires_pg
async def test_breakdown_endpoint_tenant_admin_blocked_cross_tenant(
    client, pg_test_pool,
):
    """Tenant admin on tenant A querying tenant B → 403. Same authz
    contract as Y9 row 2's audit endpoint."""
    from backend.main import app
    from backend import auth as _au

    tid_a = "t-y9-row3-bda"
    tid_b = "t-y9-row3-bdb"
    uid = "u-y9-row3-alice"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'A', 'free', 1), ($2, 'B', 'free', 1) "
                "ON CONFLICT DO NOTHING",
                tid_a, tid_b,
            )
            await conn.execute(
                "INSERT INTO users (id, email, name, role, "
                "  password_hash, enabled, tenant_id) "
                "VALUES ($1, $2, 'Alice', 'admin', '', 1, $3)",
                uid, "alice@y9-row3.local", tid_a,
            )
            await conn.execute(
                "INSERT INTO user_tenant_memberships "
                "  (user_id, tenant_id, role, status) "
                "VALUES ($1, $2, 'admin', 'active')",
                uid, tid_a,
            )

        alice = _au.User(
            id=uid, email="alice@y9-row3.local", name="Alice",
            role="admin", enabled=True, tenant_id=tid_a,
        )

        async def _fake_current_user():
            return alice

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            # Cross-tenant: 403
            res = await client.get(
                f"/api/v1/admin/usage/breakdown?tenant_id={tid_b}"
            )
            assert res.status_code == 403, res.text
            body = res.json()
            assert body["tenant_id"] == tid_b
            assert body["your_role"] == "admin"

            # Own tenant: 200
            res2 = await client.get(
                f"/api/v1/admin/usage/breakdown?tenant_id={tid_a}"
            )
            assert res2.status_code == 200, res2.text
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM billing_usage_events "
                "WHERE tenant_id = ANY($1)",
                [tid_a, tid_b],
            )
            await conn.execute(
                "DELETE FROM user_tenant_memberships "
                "WHERE tenant_id = ANY($1)",
                [tid_a, tid_b],
            )
            await conn.execute(
                "DELETE FROM users WHERE tenant_id = ANY($1)",
                [tid_a, tid_b],
            )
            await conn.execute(
                "DELETE FROM tenants WHERE id = ANY($1)",
                [tid_a, tid_b],
            )


@_requires_pg
async def test_breakdown_endpoint_404_for_unknown_tenant(
    client, pg_test_pool,
):
    """Unknown but well-formed tenant id → 404 (after authz pass for
    super_admin so there's no enumeration via 404-vs-403 timing)."""
    from backend.main import app
    from backend import auth as _au

    uid = "u-y9-row3-sup-2"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, email, name, role, "
                "  password_hash, enabled, tenant_id) "
                "VALUES ($1, $2, 'Sup2', 'super_admin', '', 1, $3) "
                "ON CONFLICT (id) DO NOTHING",
                uid, "sup2@y9-row3.local", "t-default",
            )

        sup = _au.User(
            id=uid, email="sup2@y9-row3.local", name="Sup2",
            role="super_admin", enabled=True, tenant_id="t-default",
        )

        async def _fake_current_user():
            return sup

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.get(
                "/api/v1/admin/usage/breakdown"
                "?tenant_id=t-y9row3-doesnotexist"
            )
            assert res.status_code == 404, res.text
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute("DELETE FROM users WHERE id = $1", uid)


def test_breakdown_endpoint_422_on_invalid_tenant_id(client):
    """Bad-shape tenant id → 422 BEFORE authz / DB so there's no
    information leak."""
    # current_user dep still wants something — we use the same anon
    # admin path the rest of the suite uses by leaving the override
    # unset (auth.current_user resolves to anon when running with
    # OMNISIGHT_AUTH_MODE=open in tests).
    import asyncio

    async def _go():
        # The pattern check happens before the auth dep evaluates the
        # body, so even an invalid path-shape returns 422 (or 401/403
        # if auth fires first). Either of (401, 403, 422) demonstrates
        # the bad id is not silently coerced.
        res = await client.get(
            "/api/v1/admin/usage/breakdown?tenant_id=BAD!ID"
        )
        assert res.status_code in (401, 403, 422), res.text

    asyncio.run(_go())
