"""Y9 #285 row 5 — audit chain verifier + billing usage alignment.

Two acceptance criteria for this row, both validated here:

1. **The I8 chain verifier (``backend.audit.verify_chain`` /
   ``verify_all_chains``) does NOT false-positive on the ten new
   dot-notation event types from Y9 row 1.** The verifier walks the
   chain by recomputing ``sha256(prev_hash || canonical(payload) ||
   ts)``; any drift between the writer's and the verifier's payload
   shape would surface as a phantom break. We exercise every Y event
   type — including the cross-tenant ``project_share.granted`` dual-
   chain — interleaved with legacy ``snake_case`` actions and unicode
   payloads, and confirm the verifier still returns ``(True, None)``.
   We also confirm that REAL tampering on a Y row is still detected
   (the verifier must not have weakened in the process of accepting
   the new event names).

2. **Billing usage aggregation (``billing_usage_events``) aligned
   with ``workflow_runs`` does NOT lose pre-Y1 ``project_id IS NULL``
   legacy rows; they attribute to the ``p-<suffix>-default`` bucket
   per alembic 0037's projection.** The schema enforces ``NOT NULL``
   on ``billing_usage_events.project_id`` (alembic 0039), and the
   ``backend.billing_usage._resolve_project`` helper falls through to
   the deterministic projection when the explicit arg + ContextVar
   are both ``None``. End-to-end: insert a legacy ``workflow_runs``
   row with ``project_id = NULL`` (and ``tenant_id = NULL`` for the
   doubly-legacy case), call ``workflow.finish``, then assert the
   resulting billing row carries the correct default attribution.

Test layout
───────────
* Pure-unit tests (always run; document the static contracts and act
  as drift guards against future schema / module changes).
* PG-required tests (skip without ``OMNI_TEST_PG_URL``; exercise the
  real chain verifier + the real workflow.finish → billing fan-out
  end-to-end).

Same skip pattern as ``test_audit_events_y9_row1.py`` and
``test_billing_usage_y9_row3.py`` so the test lane gating stays
consistent across the Y9 rows.

Module-global state audit (per implement_phase_step.md Step 1)
────────────────────────────────────────────────────────────────
Tests use the standard ``pg_test_pool`` fixture (function-scoped pool +
TRUNCATE isolation). The ``_audit_db`` / ``_billing_db`` fixtures reset
ContextVars on teardown so a row-5 test cannot leak tenant / project
state into the next case.

Read-after-write timing audit (per implement_phase_step.md Step 1)
────────────────────────────────────────────────────────────────
Each test fully ``await``s the writer (audit.log / workflow.finish)
before asserting on the read-back. ``audit.log`` holds a per-tenant
``pg_advisory_xact_lock`` across its INSERT, so the chain row is
visible to ``verify_chain`` immediately after the await returns. The
billing fan-out in ``workflow.finish`` runs synchronously inside the
finish coroutine (not via ``loop.create_task``), so a SELECT after
``await workflow.finish(...)`` sees the newly-written billing row.
"""

from __future__ import annotations

import inspect
import os
import re
import time
from pathlib import Path

import pytest


def _pg_not_available() -> bool:
    return not os.environ.get("OMNI_TEST_PG_URL", "").strip()


_requires_pg = pytest.mark.skipif(
    _pg_not_available(),
    reason="Y9 row 5 chain verifier + billing alignment integration "
           "tests need an actual PG instance — set OMNI_TEST_PG_URL.",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: chain hash function accepts every Y event name
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_chain_hash_function_accepts_every_y_event_action_string():
    """The audit chain hash composer must accept all ten Y event names
    without raising / mangling output. Pure-unit so the contract is
    validated even when no PG is around — if ``audit._canonical`` ever
    grows special-case branches that mishandle ``"."`` in actions,
    this test trips immediately.
    """
    from backend import audit, audit_events

    prev = ""
    for action in audit_events.ALL_EVENT_TYPES:
        payload = {
            "action": action,
            "entity_kind": "tenant",
            "entity_id": "t-y9row5",
            "before": {},
            "after": {"id": "t-y9row5"},
            "actor": "test",
        }
        canon = audit._canonical(payload)
        h = audit._hash(prev, canon + str(round(1700000000.0, 6)))
        assert isinstance(h, str)
        assert len(h) == 64  # sha256 hex
        assert all(c in "0123456789abcdef" for c in h)
        prev = h


def test_chain_canonical_serialisation_is_stable_across_event_types():
    """Two payloads identical except for ``action`` produce different
    canonical strings — the dot-notation names must not collide via
    accidental key-order or whitespace shenanigans."""
    from backend import audit

    a = audit._canonical({
        "action": "tenant.created",
        "entity_kind": "tenant", "entity_id": "t-x",
        "before": {}, "after": {}, "actor": "test",
    })
    b = audit._canonical({
        "action": "tenant.disabled",
        "entity_kind": "tenant", "entity_id": "t-x",
        "before": {}, "after": {}, "actor": "test",
    })
    assert a != b
    # Sorted keys → first divergence must be on the action value.
    assert a.replace("tenant.created", "X") == b.replace("tenant.disabled", "X")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: billing default attribution invariants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_billing_default_constants_match_alembic_0037_projection():
    """The fall-through bucket (``t-default`` / ``p-default-default``)
    that ``_resolve_tenant`` / ``_resolve_project`` projects to MUST
    match alembic 0037's bootstrapped row — otherwise legacy NULL
    rows would attribute to a project_id that no ``projects`` row
    actually backs."""
    from backend import billing_usage as bu

    assert bu._DEFAULT_TENANT_ID == "t-default"
    assert bu._DEFAULT_PROJECT_ID == "p-default-default"
    # The projection helper agrees on t-default → p-default-default
    # without requiring the caller to special-case the tenant id.
    assert bu._project_id_from_tenant("t-default") == bu._DEFAULT_PROJECT_ID


def test_project_id_from_tenant_aligns_with_alembic_0038_sql_projection():
    """Alembic 0038 backfilled ``project_id`` from ``tenant_id`` using
    the SQL CASE in ``_PROJECT_ID_FROM_TENANT_ID``. The Python helper
    in ``billing_usage._project_id_from_tenant`` must produce the
    same string for the same inputs — otherwise the runtime fall-
    through and the migration backfill would map the same legacy
    row to two different default projects.
    """
    from backend import billing_usage as bu

    cases = {
        "t-default": "p-default-default",
        "t-acme": "p-acme-default",
        "t-host": "p-host-default",
        # No t- prefix → strip nothing (matches the SQL ELSE branch).
        "legacy": "p-legacy-default",
    }
    for tenant_id, expected in cases.items():
        assert bu._project_id_from_tenant(tenant_id) == expected, (
            f"projection mismatch for {tenant_id!r}: "
            f"got {bu._project_id_from_tenant(tenant_id)!r}, "
            f"want {expected!r}"
        )


def test_resolve_project_falls_through_when_both_explicit_and_contextvar_are_none():
    """The Y9 row 5 acceptance condition expressed at the resolver
    layer: a legacy code path with neither explicit project_id nor
    request-scope ContextVar still resolves to a real bucket, never
    ``None`` (which would violate the schema NOT NULL)."""
    from backend import billing_usage as bu
    from backend import db_context

    db_context.set_tenant_id(None)
    db_context.set_project_id(None)
    try:
        # Workflow-finish path: tenant from row, project NULL on row.
        assert bu._resolve_project(None, tenant_id="t-acme") == "p-acme-default"
        # System cron path: tenant resolves to default, project too.
        assert bu._resolve_tenant(None) == "t-default"
        assert bu._resolve_project(None, tenant_id="t-default") == "p-default-default"
    finally:
        db_context.set_tenant_id(None)
        db_context.set_project_id(None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: schema invariants — NOT NULL + DEFAULT on billing table
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _read_migration_0039() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "alembic" / "versions"
        / "0039_y9_row3_billing_usage_events.py"
    ).read_text(encoding="utf-8")


def test_billing_table_project_id_is_not_null_with_default_bucket():
    """The schema must guarantee that no row in ``billing_usage_events``
    has ``project_id IS NULL``. Two layers of defence:

    1. ``project_id NOT NULL`` so a buggy emitter that forgets to
       resolve cannot insert a NULL.
    2. ``DEFAULT 'p-default-default'`` so a hypothetical raw INSERT
       that omits the column doesn't violate the NOT NULL — it
       attributes to the platform default project."""
    text = _read_migration_0039()
    pattern = re.compile(
        r"project_id\s+TEXT\s+NOT\s+NULL\s+DEFAULT\s+'p-default-default'",
        re.IGNORECASE,
    )
    assert pattern.search(text), (
        "alembic 0039 must enforce project_id NOT NULL + the "
        "p-default-default fall-through bucket — Y9 row 5 acceptance."
    )


def test_billing_table_tenant_id_is_not_null_with_default_bucket():
    """Same NOT NULL + default contract for ``tenant_id`` — a row
    written without a tenant scope still attributes to ``t-default``
    rather than a NULL bucket."""
    text = _read_migration_0039()
    pattern = re.compile(
        r"tenant_id\s+TEXT\s+NOT\s+NULL\s+DEFAULT\s+'t-default'",
        re.IGNORECASE,
    )
    assert pattern.search(text), (
        "alembic 0039 must enforce tenant_id NOT NULL + the "
        "t-default fall-through bucket — Y9 row 5 acceptance."
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: workflow.finish wires legacy NULL through to resolver
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_workflow_finish_passes_row_project_id_directly_to_resolver():
    """``workflow.finish`` must pass ``row["project_id"]`` straight
    through to ``record_workflow_run``. If a future refactor wraps
    that with ``or "..."`` or coerces the NULL to an empty string,
    legacy attribution would break (the resolver only fires the
    fall-through projection when the explicit arg is exactly ``None``).
    """
    from backend import workflow

    src = inspect.getsource(workflow.finish)
    # The finish() body must contain ``project_id=row["project_id"]``
    # so a NULL on the row hits the helper as None and the fall-
    # through projection runs. Defensive coercion would mask legacy
    # attribution.
    assert 'project_id=row["project_id"]' in src, (
        "workflow.finish must pass workflow_runs.project_id straight "
        "to record_workflow_run; otherwise NULL legacy rows can't be "
        "resolved to the p-<suffix>-default bucket."
    )
    assert 'tenant_id=row["tenant_id"]' in src, (
        "workflow.finish must pass workflow_runs.tenant_id straight "
        "to record_workflow_run; otherwise NULL legacy rows can't be "
        "resolved to the t-default bucket."
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PG-required fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture()
async def _audit_db(pg_test_pool):
    """TRUNCATE audit_log + reset tenant ContextVar around each test."""
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


@pytest.fixture()
async def _billing_and_workflow_db(pg_test_pool):
    """TRUNCATE billing_usage_events + workflow_runs + workflow_steps
    and reset both request-scope ContextVars on teardown.

    workflow_steps has a dangling FK-shape in some legacy schemas
    (run_id is just TEXT, not a hard FK) so the truncate order is
    cosmetic — but cleaning both keeps cross-test isolation tight.
    """
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE billing_usage_events RESTART IDENTITY"
        )
        await conn.execute("DELETE FROM workflow_steps")
        await conn.execute("DELETE FROM workflow_runs")
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
            await conn.execute("DELETE FROM workflow_steps")
            await conn.execute("DELETE FROM workflow_runs")


async def _seed_tenants(*tids: str) -> None:
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        for tid in tids:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan) "
                "VALUES ($1, $2, 'free') "
                "ON CONFLICT (id) DO NOTHING",
                tid, f"Test {tid}",
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PG: chain verifier — no false-positive on Y events
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
@pytest.mark.asyncio
async def test_verifier_passes_with_all_ten_event_types_in_one_chain(
    _audit_db,
):
    """Drive every Y9 row 1 emitter at the same tenant chain and
    confirm verify_chain returns OK. The dual-chain
    ``project_share.granted`` is exercised separately so its second
    write doesn't pollute the host chain order — that case is
    covered by ``test_share_dual_chain_each_passes_verifier`` below.
    """
    from backend import audit_events
    from backend.db_context import set_tenant_id

    await _seed_tenants("t-y9row5-A")
    set_tenant_id("t-y9row5-A")

    # Cover all single-tenant emitters (9 of the 10).
    await audit_events.emit_tenant_created(
        tenant_id="t-y9row5-A", name="Y9-row5-A", plan="free",
        enabled=True, actor="op@example.com",
    )
    await audit_events.emit_tenant_plan_changed(
        tenant_id="t-y9row5-A", old_plan="free", new_plan="pro",
        actor="op@example.com",
    )
    await audit_events.emit_tenant_disabled(
        tenant_id="t-y9row5-A", actor="op@example.com",
    )
    await audit_events.emit_invite_sent(
        tenant_id="t-y9row5-A", invite_id="inv-y9r5-1",
        email="alice@example.com", role="member",
        expires_at="2026-05-01 00:00:00", invited_by="u-admin",
        actor="op@example.com",
    )
    await audit_events.emit_invite_accepted(
        tenant_id="t-y9row5-A", invite_id="inv-y9r5-1",
        user_id="u-y9r5-alice", role="member",
        user_was_created=True, already_member=False,
        actor="anonymous",
    )
    await audit_events.emit_membership_role_changed(
        tenant_id="t-y9row5-A", user_id="u-y9r5-alice",
        old_role="member", new_role="admin",
        actor="owner@example.com",
    )
    await audit_events.emit_project_created(
        tenant_id="t-y9row5-A", project_id="p-y9r5-cam01",
        name="Front Door", slug="front-door",
        product_line="embedded", actor="op@example.com",
    )
    await audit_events.emit_project_archived(
        tenant_id="t-y9row5-A", project_id="p-y9r5-cam01",
        archived_at="2026-04-26 12:00:00", retention_days=90,
        actor="op@example.com",
    )
    await audit_events.emit_workspace_gc_executed(summary={
        "trashed": [{"leaf": "x"}], "purged": [],
        "quota_evicted": [], "skipped_busy": [], "skipped_fresh": [],
    })

    from backend import audit
    ok, bad = await audit.verify_chain(tenant_id="t-y9row5-A")
    assert ok, (
        f"verifier reported false-positive on Y dot-notation events; "
        f"first bad row id = {bad}"
    )
    assert bad is None


@_requires_pg
@pytest.mark.asyncio
async def test_verifier_passes_with_y_events_interleaved_with_legacy_actions(
    _audit_db,
):
    """A real chain in production carries pre-Y9 ``snake_case``
    actions (``tenant_created``, ``tenant_member_updated``,
    ``mode_change`` …) BEFORE / AFTER the new dot-notation rows.
    Mix the two and verify the chain is still intact — proves the
    verifier doesn't bucket-sort by action style.
    """
    from backend import audit, audit_events
    from backend.db_context import set_tenant_id

    await _seed_tenants("t-y9row5-mix")
    set_tenant_id("t-y9row5-mix")

    # Legacy → dot-notation → legacy → dot-notation pattern.
    await audit.log("tenant_created", "tenant", "t-y9row5-mix",
                    after={"id": "t-y9row5-mix"})
    await audit_events.emit_tenant_plan_changed(
        tenant_id="t-y9row5-mix", old_plan="free", new_plan="pro",
        actor="op@example.com",
    )
    await audit.log("mode_change", "operation_mode", "global",
                    before={"mode": "supervised"},
                    after={"mode": "full_auto"})
    await audit_events.emit_project_created(
        tenant_id="t-y9row5-mix", project_id="p-y9r5-mix-1",
        name="Mix", slug="mix", product_line="web",
        actor="op@example.com",
    )
    await audit.log("tenant_member_updated", "tenant_membership",
                    "u-y9r5-bob", before={"role": "member"},
                    after={"role": "admin"})
    await audit_events.emit_membership_role_changed(
        tenant_id="t-y9row5-mix", user_id="u-y9r5-bob",
        old_role="member", new_role="admin",
        actor="owner@example.com",
    )

    ok, bad = await audit.verify_chain(tenant_id="t-y9row5-mix")
    assert ok, (
        f"verifier reported false-positive on mixed legacy + Y "
        f"actions; first bad row id = {bad}"
    )
    assert bad is None


@_requires_pg
@pytest.mark.asyncio
async def test_share_dual_chain_each_passes_verifier(_audit_db):
    """Cross-tenant ``project_share.granted`` writes one row to the
    host chain and one to the guest chain. Both chains must verify
    independently — proves the contextvar swap-and-restore in
    ``emit_project_share_granted`` doesn't bleed a stale prev_hash
    between tenants.
    """
    from backend import audit, audit_events
    from backend.db_context import set_tenant_id

    await _seed_tenants("t-y9r5-host", "t-y9r5-guest")

    # Pre-seed each chain so the share rows are NOT genesis rows
    # (exercises the prev_hash linkage).
    set_tenant_id("t-y9r5-host")
    await audit.log("seed_host", "thing", "h1")
    set_tenant_id("t-y9r5-guest")
    await audit.log("seed_guest", "thing", "g1")

    set_tenant_id("t-default")
    host_id, guest_id = await audit_events.emit_project_share_granted(
        host_tenant_id="t-y9r5-host",
        guest_tenant_id="t-y9r5-guest",
        project_id="p-y9r5-shared",
        share_id="ps-y9r5-1",
        role="contributor",
        expires_at=None,
        granted_by="u-host-admin",
        actor="host-admin@example.com",
    )
    assert isinstance(host_id, int)
    assert isinstance(guest_id, int)

    ok_host, bad_host = await audit.verify_chain(tenant_id="t-y9r5-host")
    ok_guest, bad_guest = await audit.verify_chain(tenant_id="t-y9r5-guest")
    assert ok_host, f"host chain false-positive at row {bad_host}"
    assert ok_guest, f"guest chain false-positive at row {bad_guest}"
    assert bad_host is None and bad_guest is None


@_requires_pg
@pytest.mark.asyncio
async def test_verify_all_chains_passes_with_y_events_across_three_tenants(
    _audit_db,
):
    """``verify_all_chains`` runs the verifier independently per
    tenant — each tenant gets its own genesis (empty prev_hash) and
    its own walk. With Y events in three different tenants, every
    chain must come back ``(True, None)``.
    """
    from backend import audit, audit_events
    from backend.db_context import set_tenant_id

    await _seed_tenants("t-y9r5-x", "t-y9r5-y", "t-y9r5-z")

    set_tenant_id("t-y9r5-x")
    await audit_events.emit_tenant_created(
        tenant_id="t-y9r5-x", name="X", plan="free",
        enabled=True, actor="op@example.com",
    )
    await audit_events.emit_project_created(
        tenant_id="t-y9r5-x", project_id="p-y9r5-x1",
        name="x1", slug="x1", product_line="embedded",
        actor="op@example.com",
    )

    set_tenant_id("t-y9r5-y")
    await audit_events.emit_invite_sent(
        tenant_id="t-y9r5-y", invite_id="inv-y9r5-y1",
        email="y@example.com", role="member",
        expires_at=None, invited_by=None,
        actor="op@example.com",
    )

    set_tenant_id("t-y9r5-z")
    await audit_events.emit_workspace_gc_executed(summary={})

    results = await audit.verify_all_chains()
    for tid in ("t-y9r5-x", "t-y9r5-y", "t-y9r5-z"):
        assert tid in results, f"verify_all_chains missing {tid}"
        ok, bad = results[tid]
        assert ok, f"chain {tid!r} false-positive at row {bad}"
        assert bad is None


@_requires_pg
@pytest.mark.asyncio
async def test_verifier_still_detects_tampering_on_y_event_row(_audit_db):
    """The verifier MUST still flag a real tamper after we taught it
    to accept dot-notation actions. Emit one Y event, mutate its
    ``after_json`` post-write, and confirm verify_chain returns
    ``(False, that_row_id)`` — same contract as the legacy tamper
    test in ``test_audit.py``.
    """
    from backend import audit, audit_events
    from backend.db_context import set_tenant_id
    from backend.db_pool import get_pool

    await _seed_tenants("t-y9r5-tamper")
    set_tenant_id("t-y9r5-tamper")

    await audit_events.emit_tenant_created(
        tenant_id="t-y9r5-tamper", name="T", plan="free",
        enabled=True, actor="op@example.com",
    )
    rid_to_tamper = await audit_events.emit_project_created(
        tenant_id="t-y9r5-tamper", project_id="p-y9r5-vic",
        name="V", slug="v", product_line="embedded",
        actor="op@example.com",
    )
    await audit_events.emit_workspace_gc_executed(summary={})

    assert isinstance(rid_to_tamper, int)
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE audit_log "
            "SET after_json = '{\"forged\":true}' "
            "WHERE id = $1",
            rid_to_tamper,
        )

    ok, bad = await audit.verify_chain(tenant_id="t-y9r5-tamper")
    assert not ok, (
        "verifier failed to detect tamper on a project.created row — "
        "Y event acceptance must not weaken integrity guarantees."
    )
    assert bad == rid_to_tamper


@_requires_pg
@pytest.mark.asyncio
async def test_verifier_passes_with_unicode_payload_in_y_event(_audit_db):
    """``project.created`` carries the project's display name verbatim;
    Chinese / emoji content must hash + verify cleanly. ``_canonical``
    sets ``ensure_ascii=False`` so the bytes that go into sha256 are
    UTF-8 (not the escape-sequence form). If a future refactor flips
    that flag, this test trips because the writer's hash and the
    verifier's recomputed hash would diverge on encoding-only.
    """
    from backend import audit, audit_events
    from backend.db_context import set_tenant_id

    await _seed_tenants("t-y9r5-unicode")
    set_tenant_id("t-y9r5-unicode")

    await audit_events.emit_project_created(
        tenant_id="t-y9r5-unicode",
        project_id="p-y9r5-cjk",
        name="專案 ⚙️ 攝影機",
        slug="cjk-cam",
        product_line="嵌入式",
        actor="操作員@example.com",
    )

    ok, bad = await audit.verify_chain(tenant_id="t-y9r5-unicode")
    assert ok, f"verifier broke on unicode payload; bad={bad}"
    assert bad is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PG: billing alignment — legacy NULL project_id → default bucket
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _insert_legacy_workflow_run(
    pool, *, run_id: str, kind: str = "invoke",
    tenant_id: str | None = "t-default",
    project_id: str | None = None,
    started_at: float | None = None,
) -> None:
    """Hand-rolled legacy workflow_runs INSERT that bypasses
    ``workflow.start`` (which would write the current ContextVar
    tenant_id and never NULL-out project_id).

    Models the pre-Y1 reality where ``project_id`` was added by
    alembic 0038 as NULL-able and many existing rows simply have
    ``project_id IS NULL`` until / unless 0038's backfill runs.
    """
    started_at = started_at if started_at is not None else time.time()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO workflow_runs "
            "(id, kind, started_at, status, metadata, "
            " tenant_id, project_id) "
            "VALUES ($1, $2, $3, 'running', '{}', $4, $5)",
            run_id, kind, started_at, tenant_id, project_id,
        )


@_requires_pg
@pytest.mark.asyncio
async def test_workflow_finish_legacy_null_project_id_hits_default_bucket(
    _billing_and_workflow_db,
):
    """End-to-end: insert a legacy ``workflow_runs`` row with
    ``project_id = NULL`` and ``tenant_id = 't-acme'``, call
    ``workflow.finish``, and assert the resulting
    ``billing_usage_events`` row attributes to ``p-acme-default``
    (alembic 0037's projection of t-acme's default project) rather
    than dropping the cost on the floor or violating NOT NULL.
    """
    from backend import workflow
    from backend.db_context import set_project_id, set_tenant_id

    pool = _billing_and_workflow_db
    set_tenant_id(None)  # ensure no contextvar leak from a prior test
    set_project_id(None)

    await _seed_tenants("t-acme")
    run_id = "wf-y9r5-legacy-null-proj"
    await _insert_legacy_workflow_run(
        pool, run_id=run_id, tenant_id="t-acme", project_id=None,
    )

    await workflow.finish(run_id, status="completed")

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tenant_id, project_id, kind, workflow_run_id, "
            "       workflow_status "
            "FROM billing_usage_events "
            "WHERE workflow_run_id = $1 ORDER BY id ASC",
            run_id,
        )
    assert len(rows) == 1, (
        "exactly one workflow_run billing event must fire per "
        "workflow.finish call"
    )
    r = rows[0]
    assert r["kind"] == "workflow_run"
    assert r["tenant_id"] == "t-acme", (
        "tenant attribution must be preserved from the legacy row"
    )
    # Y9 row 5 acceptance: NULL project_id → p-acme-default.
    assert r["project_id"] == "p-acme-default", (
        f"NULL project_id legacy row must attribute to "
        f"p-<suffix>-default; got {r['project_id']!r}"
    )
    assert r["workflow_status"] == "completed"


@_requires_pg
@pytest.mark.asyncio
async def test_workflow_finish_legacy_null_tenant_and_project_hits_t_default(
    _billing_and_workflow_db,
):
    """Doubly-legacy row: pre-I1 ``tenant_id IS NULL`` AND pre-Y1
    ``project_id IS NULL``. The billing emitter must still write a
    row attributed to the platform defaults (``t-default`` /
    ``p-default-default``) — Y9 row 5 acceptance for the worst case.
    """
    from backend import workflow
    from backend.db_context import set_project_id, set_tenant_id

    pool = _billing_and_workflow_db
    set_tenant_id(None)
    set_project_id(None)

    run_id = "wf-y9r5-legacy-null-both"
    await _insert_legacy_workflow_run(
        pool, run_id=run_id, tenant_id=None, project_id=None,
    )

    await workflow.finish(run_id, status="completed")

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tenant_id, project_id "
            "FROM billing_usage_events "
            "WHERE workflow_run_id = $1",
            run_id,
        )
    assert len(rows) == 1
    r = rows[0]
    assert r["tenant_id"] == "t-default"
    assert r["project_id"] == "p-default-default"


@_requires_pg
@pytest.mark.asyncio
async def test_workflow_finish_with_explicit_project_does_not_hit_default(
    _billing_and_workflow_db,
):
    """Negative control: a row that DOES carry an explicit
    ``project_id`` must NOT be coerced into the default bucket. If
    the resolver were too eager (e.g. ``project_id or default``), this
    assertion would catch it and protect actually-attributed rows
    from being silently rebucketed.
    """
    from backend import workflow
    from backend.db_context import set_project_id, set_tenant_id

    pool = _billing_and_workflow_db
    set_tenant_id(None)
    set_project_id(None)

    await _seed_tenants("t-acme")
    # Seed a real project row so the LEFT JOIN in workflow.finish
    # exercises the joined product_line column path.
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO projects "
            "(id, tenant_id, name, slug, product_line, status) "
            "VALUES ($1, $2, 'Front', 'front', 'embedded', 'active') "
            "ON CONFLICT (id) DO NOTHING",
            "p-acme-front", "t-acme",
        )

    run_id = "wf-y9r5-explicit-proj"
    await _insert_legacy_workflow_run(
        pool, run_id=run_id, tenant_id="t-acme",
        project_id="p-acme-front",
    )

    await workflow.finish(run_id, status="completed")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT tenant_id, project_id "
            "FROM billing_usage_events WHERE workflow_run_id = $1",
            run_id,
        )
    assert row is not None
    assert row["tenant_id"] == "t-acme"
    # Explicit project survives — not coerced to p-acme-default.
    assert row["project_id"] == "p-acme-front"


@_requires_pg
@pytest.mark.asyncio
async def test_billing_usage_events_project_id_is_never_null_after_finish(
    _billing_and_workflow_db,
):
    """Mass invariant: after a mixed batch of legacy + new
    ``workflow.finish`` calls, ``billing_usage_events.project_id``
    is never NULL. The schema enforces this at the DDL layer; this
    test protects against an emitter regression that bypasses
    ``_resolve_project`` (e.g. raw asyncpg insert with a literal NULL
    that triggers the NOT NULL → 23502 error and silently swallows
    via the best-effort ``try/except``).
    """
    from backend import workflow
    from backend.db_context import set_project_id, set_tenant_id

    pool = _billing_and_workflow_db
    set_tenant_id(None)
    set_project_id(None)

    await _seed_tenants("t-acme", "t-host")
    # Real project (so the LEFT JOIN finds product_line).
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO projects "
            "(id, tenant_id, name, slug, product_line, status) "
            "VALUES ($1, $2, 'P', 'p', 'embedded', 'active') "
            "ON CONFLICT (id) DO NOTHING",
            "p-acme-real", "t-acme",
        )

    cases = [
        # legacy: NULL tenant + NULL project
        ("wf-y9r5-mass-1", None, None),
        # legacy: tenant set, NULL project
        ("wf-y9r5-mass-2", "t-acme", None),
        # legacy: tenant set, NULL project (different tenant)
        ("wf-y9r5-mass-3", "t-host", None),
        # new: tenant + project set
        ("wf-y9r5-mass-4", "t-acme", "p-acme-real"),
    ]
    for run_id, tenant_id, project_id in cases:
        await _insert_legacy_workflow_run(
            pool, run_id=run_id,
            tenant_id=tenant_id, project_id=project_id,
        )
        await workflow.finish(run_id, status="completed")

    async with pool.acquire() as conn:
        null_count = await conn.fetchval(
            "SELECT COUNT(*) FROM billing_usage_events "
            "WHERE project_id IS NULL OR tenant_id IS NULL"
        )
        total_count = await conn.fetchval(
            "SELECT COUNT(*) FROM billing_usage_events "
            "WHERE workflow_run_id = ANY($1::text[])",
            [c[0] for c in cases],
        )
    assert null_count == 0, (
        "billing_usage_events must never carry NULL tenant_id or "
        "project_id after any workflow.finish — Y9 row 5 acceptance"
    )
    # 4 finish() calls → 4 rows; no row dropped.
    assert total_count == len(cases)


@_requires_pg
@pytest.mark.asyncio
async def test_breakdown_aggregates_legacy_and_explicit_buckets_for_same_tenant(
    _billing_and_workflow_db,
):
    """The T6-pricing-page breakdown must surface both the legacy
    ``p-acme-default`` bucket (NULL-project rows) AND the explicit
    ``p-acme-real`` bucket as separate projects under ``t-acme`` —
    proves Y9 row 5's "走 'default' project 歸因" doesn't merge legacy
    rows under an arbitrary other project.
    """
    from backend import billing_usage as bu
    from backend import workflow
    from backend.db_context import set_project_id, set_tenant_id

    pool = _billing_and_workflow_db
    set_tenant_id(None)
    set_project_id(None)

    await _seed_tenants("t-acme")
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO projects "
            "(id, tenant_id, name, slug, product_line, status) "
            "VALUES ($1, $2, 'R', 'r', 'embedded', 'active') "
            "ON CONFLICT (id) DO NOTHING",
            "p-acme-real", "t-acme",
        )

    # 2 legacy null-project runs + 1 explicit-project run.
    legacy_runs = ["wf-y9r5-bd-leg1", "wf-y9r5-bd-leg2"]
    for run_id in legacy_runs:
        await _insert_legacy_workflow_run(
            pool, run_id=run_id, tenant_id="t-acme", project_id=None,
        )
        await workflow.finish(run_id, status="completed")

    explicit_run = "wf-y9r5-bd-exp1"
    await _insert_legacy_workflow_run(
        pool, run_id=explicit_run,
        tenant_id="t-acme", project_id="p-acme-real",
    )
    await workflow.finish(explicit_run, status="completed")

    breakdown = await bu.breakdown_by_project(tenant_id="t-acme")
    by_pid = {row["project_id"]: row for row in breakdown}

    # Both buckets surface — neither swallows the other.
    assert "p-acme-default" in by_pid, (
        f"legacy bucket missing from breakdown; got {sorted(by_pid)!r}"
    )
    assert "p-acme-real" in by_pid

    # 2 legacy runs landed in the default bucket; 1 in the explicit.
    assert by_pid["p-acme-default"]["workflow_runs"] == 2
    assert by_pid["p-acme-real"]["workflow_runs"] == 1


@_requires_pg
@pytest.mark.asyncio
async def test_workflow_run_count_aligns_one_to_one_with_workflow_finish(
    _billing_and_workflow_db,
):
    """Per-row alignment guard: 5 legacy ``workflow.finish`` calls must
    produce exactly 5 ``kind = 'workflow_run'`` rows in
    ``billing_usage_events``. Catches the case where a future
    refactor accidentally batches / dedupes finish() emissions and
    legacy NULL rows get coalesced into one bucket entry."""
    from backend import workflow
    from backend.db_context import set_project_id, set_tenant_id

    pool = _billing_and_workflow_db
    set_tenant_id(None)
    set_project_id(None)
    await _seed_tenants("t-acme")

    run_ids = [f"wf-y9r5-align-{i}" for i in range(5)]
    for run_id in run_ids:
        await _insert_legacy_workflow_run(
            pool, run_id=run_id,
            tenant_id="t-acme", project_id=None,
        )
        await workflow.finish(run_id, status="completed")

    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM billing_usage_events "
            "WHERE kind = 'workflow_run' AND workflow_run_id = ANY($1::text[])",
            run_ids,
        )
        # Every run is attributed to the default bucket.
        bucket_n = await conn.fetchval(
            "SELECT COUNT(*) FROM billing_usage_events "
            "WHERE kind = 'workflow_run' AND project_id = $1",
            "p-acme-default",
        )
    assert n == 5
    assert bucket_n == 5


@_requires_pg
@pytest.mark.asyncio
async def test_legacy_null_project_and_audit_chain_remain_intact_together(
    _billing_and_workflow_db, _audit_db,
):
    """Cross-row 5 invariant: a tenant that has BOTH legacy NULL-
    project workflow_runs AND new dot-notation audit events must
    keep its audit chain intact while billing rows still attribute
    to the default bucket. Proves the two halves of row 5 don't
    interfere when exercised against the same tenant."""
    from backend import audit, audit_events, workflow
    from backend.db_context import set_project_id, set_tenant_id

    pool = _billing_and_workflow_db
    set_tenant_id("t-y9r5-combo")
    set_project_id(None)
    await _seed_tenants("t-y9r5-combo")

    # Y audit events into the chain.
    await audit_events.emit_tenant_created(
        tenant_id="t-y9r5-combo", name="Combo", plan="free",
        enabled=True, actor="op@example.com",
    )
    await audit_events.emit_project_created(
        tenant_id="t-y9r5-combo", project_id="p-y9r5-combo-x",
        name="x", slug="x", product_line="embedded",
        actor="op@example.com",
    )

    # Legacy NULL-project workflow run for the same tenant.
    run_id = "wf-y9r5-combo"
    await _insert_legacy_workflow_run(
        pool, run_id=run_id,
        tenant_id="t-y9r5-combo", project_id=None,
    )
    await workflow.finish(run_id, status="completed")

    # 1) Audit chain is intact (no false-positive on Y events).
    ok, bad = await audit.verify_chain(tenant_id="t-y9r5-combo")
    assert ok, f"audit chain false-positive at row {bad}"
    assert bad is None

    # 2) Billing row attributes to the default bucket for that tenant.
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT tenant_id, project_id FROM billing_usage_events "
            "WHERE workflow_run_id = $1",
            run_id,
        )
    assert row is not None
    assert row["tenant_id"] == "t-y9r5-combo"
    assert row["project_id"] == "p-y9r5-combo-default"
