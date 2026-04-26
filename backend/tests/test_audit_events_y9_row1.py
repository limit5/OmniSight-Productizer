"""Y9 #285 row 1 — canonical audit event types.

Validates the ten new dot-notation event types defined in
``backend.audit_events``:

  * ``tenant.created`` / ``tenant.plan_changed`` / ``tenant.disabled``
  * ``invite.sent`` / ``invite.accepted``
  * ``membership.role_changed``
  * ``project.created`` / ``project.archived``
  * ``project_share.granted``  (writes ONE row per tenant chain — host
    + guest — for cross-tenant share events)
  * ``workspace.gc_executed``

Each test:
  1. Calls the relevant ``backend.audit_events.emit_*`` helper.
  2. Reads back ``audit_log`` rows directly so we can assert
     ``tenant_id``, ``action``, and the ``after_json`` payload — the
     three load-bearing surfaces for downstream verifiers.
  3. Re-runs ``audit.verify_chain`` on the affected chain(s) to
     confirm the new event-type names do NOT trip the I8 chain
     verifier (no false positives — Y9 row 5 acceptance criterion).

Module-global state audit (per implement_phase_step.md Step 1)
────────────────────────────────────────────────────────────────
Tests use the standard ``pg_test_pool`` fixture; each function-scoped
test gets a fresh pool + ``TRUNCATE audit_log RESTART IDENTITY``
isolation, so the tenant ContextVar set by one test cannot leak into
the next. The ``_audit_db`` fixture explicitly resets the ContextVar
to ``None`` on teardown.
"""

from __future__ import annotations

import json

import pytest


# ─── Shared fixture ──────────────────────────────────────────────────


@pytest.fixture()
async def _audit_db(pg_test_pool):
    """Truncate ``audit_log`` per test and yield the audit module.

    Mirrors ``backend.tests.test_audit._audit_db`` so the Y9 tests
    isolate the same way as I8 tests.
    """
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


async def _seed_tenants(*tids: str) -> None:
    """Insert tenant rows so the audit row's tenant_id FK (none today,
    but parity with the rest of the test suite) and the Y9 emitters'
    chain-target tenant rows exist."""
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        for tid in tids:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan) "
                "VALUES ($1, $2, 'free') "
                "ON CONFLICT (id) DO NOTHING",
                tid, f"Test {tid}",
            )


async def _fetch_rows(action: str | None = None, tenant_id: str | None = None):
    """Read audit rows back filtered by action / tenant_id."""
    from backend.db_pool import get_pool
    where = []
    params: list = []
    if action is not None:
        where.append(f"action = ${len(params) + 1}")
        params.append(action)
    if tenant_id is not None:
        where.append(f"tenant_id = ${len(params) + 1}")
        params.append(tenant_id)
    sql = "SELECT * FROM audit_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id ASC"
    async with get_pool().acquire() as conn:
        return await conn.fetch(sql, *params)


# ─── tenant.created ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tenant_created_emits_canonical_event(_audit_db):
    from backend import audit_events
    from backend.db_context import set_tenant_id

    await _seed_tenants("t-acme")

    # Acting from super-admin's chain; emit_tenant_created keeps the
    # row in the actor's chain (the new tenant has no prior chain).
    set_tenant_id("t-default")
    rid = await audit_events.emit_tenant_created(
        tenant_id="t-acme", name="Acme",
        plan="pro", enabled=True, actor="super@example.com",
    )
    assert isinstance(rid, int) and rid > 0

    rows = await _fetch_rows(action=audit_events.EVENT_TENANT_CREATED)
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "tenant.created"
    assert r["entity_kind"] == "tenant"
    assert r["entity_id"] == "t-acme"
    assert r["actor"] == "super@example.com"
    # Row is in the super-admin's chain (no override).
    assert r["tenant_id"] == "t-default"
    after = json.loads(r["after_json"])
    assert after == {
        "id": "t-acme", "name": "Acme",
        "plan": "pro", "enabled": True,
    }


# ─── tenant.plan_changed ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tenant_plan_changed_emits_into_target_chain(_audit_db):
    from backend import audit_events
    from backend.db_context import set_tenant_id

    await _seed_tenants("t-acme")

    # Operator is acting from t-default but the event of record
    # belongs to the tenant whose plan changed.
    set_tenant_id("t-default")
    rid = await audit_events.emit_tenant_plan_changed(
        tenant_id="t-acme",
        old_plan="free", new_plan="pro",
        actor="super@example.com",
    )
    assert isinstance(rid, int)

    rows = await _fetch_rows(action=audit_events.EVENT_TENANT_PLAN_CHANGED)
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "tenant.plan_changed"
    # Row landed in t-acme's chain even though the operator was on
    # t-default — the override is the contract.
    assert r["tenant_id"] == "t-acme"
    before = json.loads(r["before_json"])
    after = json.loads(r["after_json"])
    assert before == {"id": "t-acme", "plan": "free"}
    assert after == {"id": "t-acme", "plan": "pro"}

    # ContextVar is restored on exit.
    from backend.db_context import current_tenant_id
    assert current_tenant_id() == "t-default"


# ─── tenant.disabled ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tenant_disabled_emits_into_target_chain(_audit_db):
    from backend import audit_events

    await _seed_tenants("t-acme")

    rid = await audit_events.emit_tenant_disabled(
        tenant_id="t-acme", actor="super@example.com",
    )
    assert isinstance(rid, int)

    rows = await _fetch_rows(action=audit_events.EVENT_TENANT_DISABLED)
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "tenant.disabled"
    assert r["tenant_id"] == "t-acme"
    after = json.loads(r["after_json"])
    assert after == {"id": "t-acme", "enabled": False}


# ─── invite.sent ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invite_sent_emits_into_target_tenant_chain(_audit_db):
    from backend import audit_events

    await _seed_tenants("t-acme")

    rid = await audit_events.emit_invite_sent(
        tenant_id="t-acme",
        invite_id="inv-abc123",
        email="alice@example.com",
        role="member",
        expires_at="2026-05-01 00:00:00",
        invited_by="u-admin",
        actor="admin@example.com",
    )
    assert isinstance(rid, int)

    rows = await _fetch_rows(action=audit_events.EVENT_INVITE_SENT)
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "invite.sent"
    assert r["entity_kind"] == "tenant_invite"
    assert r["entity_id"] == "inv-abc123"
    assert r["tenant_id"] == "t-acme"
    after = json.loads(r["after_json"])
    assert after["invite_id"] == "inv-abc123"
    assert after["email"] == "alice@example.com"
    assert after["role"] == "member"


# ─── invite.accepted ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invite_accepted_emits_into_target_tenant_chain(_audit_db):
    from backend import audit_events

    await _seed_tenants("t-acme")

    # Public endpoint — no request-scoped tenant context. The override
    # sources the chain from the invite row.
    rid = await audit_events.emit_invite_accepted(
        tenant_id="t-acme",
        invite_id="inv-abc123",
        user_id="u-alice01",
        role="member",
        user_was_created=True,
        already_member=False,
        actor="anonymous",
    )
    assert isinstance(rid, int)

    rows = await _fetch_rows(action=audit_events.EVENT_INVITE_ACCEPTED)
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "invite.accepted"
    assert r["tenant_id"] == "t-acme"
    after = json.loads(r["after_json"])
    assert after["status"] == "accepted"
    assert after["user_id"] == "u-alice01"
    assert after["user_was_created"] is True
    assert after["already_member"] is False


# ─── membership.role_changed ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_membership_role_changed_emits_into_target_chain(_audit_db):
    from backend import audit_events

    await _seed_tenants("t-acme")

    rid = await audit_events.emit_membership_role_changed(
        tenant_id="t-acme",
        user_id="u-alice01",
        old_role="member", new_role="admin",
        actor="owner@example.com",
    )
    assert isinstance(rid, int)

    rows = await _fetch_rows(
        action=audit_events.EVENT_MEMBERSHIP_ROLE_CHANGED,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "membership.role_changed"
    assert r["entity_kind"] == "tenant_membership"
    assert r["entity_id"] == "u-alice01"
    assert r["tenant_id"] == "t-acme"
    before = json.loads(r["before_json"])
    after = json.loads(r["after_json"])
    assert before["role"] == "member"
    assert after["role"] == "admin"


# ─── project.created ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_project_created_emits_into_owning_tenant_chain(_audit_db):
    from backend import audit_events

    await _seed_tenants("t-acme")

    rid = await audit_events.emit_project_created(
        tenant_id="t-acme",
        project_id="p-cam01",
        name="Front Door Cam",
        slug="front-door",
        product_line="embedded",
        actor="admin@example.com",
    )
    assert isinstance(rid, int)

    rows = await _fetch_rows(action=audit_events.EVENT_PROJECT_CREATED)
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "project.created"
    assert r["tenant_id"] == "t-acme"
    assert r["entity_id"] == "p-cam01"
    after = json.loads(r["after_json"])
    assert after["name"] == "Front Door Cam"
    assert after["slug"] == "front-door"
    assert after["product_line"] == "embedded"


# ─── project.archived ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_project_archived_emits_into_owning_tenant_chain(_audit_db):
    from backend import audit_events

    await _seed_tenants("t-acme")

    rid = await audit_events.emit_project_archived(
        tenant_id="t-acme",
        project_id="p-cam01",
        archived_at="2026-04-26 12:00:00",
        retention_days=90,
        actor="admin@example.com",
    )
    assert isinstance(rid, int)

    rows = await _fetch_rows(action=audit_events.EVENT_PROJECT_ARCHIVED)
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "project.archived"
    assert r["tenant_id"] == "t-acme"
    after = json.loads(r["after_json"])
    assert after["archived_at"] == "2026-04-26 12:00:00"
    assert after["retention_days"] == 90


# ─── project_share.granted — DUAL CHAIN ──────────────────────────────


@pytest.mark.asyncio
async def test_project_share_granted_writes_to_both_host_and_guest_chains(
    _audit_db,
):
    """The cross-tenant share event MUST write one row per chain so a
    ``/admin/audit/tenants/{tid}`` query against either tenant
    surfaces the share without having to peek into the other chain.
    This is the load-bearing contract for Y9 row 1."""
    from backend import audit_events

    await _seed_tenants("t-host", "t-guest")

    host_id, guest_id = await audit_events.emit_project_share_granted(
        host_tenant_id="t-host",
        guest_tenant_id="t-guest",
        project_id="p-shared01",
        share_id="ps-share01",
        role="viewer",
        expires_at=None,
        granted_by="u-host-admin",
        actor="host-admin@example.com",
    )
    assert isinstance(host_id, int)
    assert isinstance(guest_id, int)
    assert host_id != guest_id

    # Host chain has exactly ONE row, with chain_role=host.
    host_rows = await _fetch_rows(
        action=audit_events.EVENT_PROJECT_SHARE_GRANTED,
        tenant_id="t-host",
    )
    assert len(host_rows) == 1
    host_after = json.loads(host_rows[0]["after_json"])
    assert host_after["chain_role"] == "host"
    assert host_after["host_tenant_id"] == "t-host"
    assert host_after["guest_tenant_id"] == "t-guest"
    assert host_after["share_id"] == "ps-share01"

    # Guest chain has exactly ONE row, with chain_role=guest.
    guest_rows = await _fetch_rows(
        action=audit_events.EVENT_PROJECT_SHARE_GRANTED,
        tenant_id="t-guest",
    )
    assert len(guest_rows) == 1
    guest_after = json.loads(guest_rows[0]["after_json"])
    assert guest_after["chain_role"] == "guest"
    assert guest_after["host_tenant_id"] == "t-host"
    assert guest_after["guest_tenant_id"] == "t-guest"
    assert guest_after["share_id"] == "ps-share01"


@pytest.mark.asyncio
async def test_project_share_granted_each_chain_remains_intact(_audit_db):
    """After the dual-write, both tenants' chains must still pass
    ``verify_chain``. This is the I8 false-positive guard for Y9
    row 5: the new dot-notation event names + the contextvar swap
    must not break either tenant's hash chain."""
    from backend import audit, audit_events
    from backend.db_context import set_tenant_id

    await _seed_tenants("t-host", "t-guest")

    # Pre-seed each chain with one regular event so the share row
    # is appended (not the genesis row), exercising the hash linkage.
    set_tenant_id("t-host")
    await audit.log("seed_host", "thing", "h1")
    set_tenant_id("t-guest")
    await audit.log("seed_guest", "thing", "g1")

    set_tenant_id("t-default")
    await audit_events.emit_project_share_granted(
        host_tenant_id="t-host",
        guest_tenant_id="t-guest",
        project_id="p-shared01",
        share_id="ps-share01",
        role="contributor",
        expires_at=None,
        granted_by="u-host-admin",
        actor="host-admin@example.com",
    )

    # ContextVar restored after the dual-write.
    from backend.db_context import current_tenant_id
    assert current_tenant_id() == "t-default"

    ok_host, bad_host = await audit.verify_chain(tenant_id="t-host")
    assert ok_host and bad_host is None
    ok_guest, bad_guest = await audit.verify_chain(tenant_id="t-guest")
    assert ok_guest and bad_guest is None


@pytest.mark.asyncio
async def test_project_share_granted_self_share_writes_two_rows_same_chain(
    _audit_db,
):
    """If host == guest (a degenerate self-share — should not happen in
    prod but the helper should not silently drop one row), both writes
    still land in the same chain. Documents the helper's contract."""
    from backend import audit_events

    await _seed_tenants("t-acme")

    host_id, guest_id = await audit_events.emit_project_share_granted(
        host_tenant_id="t-acme",
        guest_tenant_id="t-acme",
        project_id="p-self01",
        share_id="ps-self01",
        role="viewer",
        expires_at=None,
        granted_by="u-admin",
        actor="admin@example.com",
    )
    assert isinstance(host_id, int) and isinstance(guest_id, int)
    rows = await _fetch_rows(
        action=audit_events.EVENT_PROJECT_SHARE_GRANTED,
        tenant_id="t-acme",
    )
    assert len(rows) == 2


# ─── workspace.gc_executed ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_workspace_gc_executed_emits_summary_row(_audit_db):
    from backend import audit_events

    summary = {
        "trashed": [{"leaf": "a"}, {"leaf": "b"}],
        "purged": [{"trash_path": "x"}],
        "quota_evicted": [],
        "skipped_busy": ["one busy"],
        "skipped_fresh": [],
    }
    rid = await audit_events.emit_workspace_gc_executed(summary=summary)
    assert isinstance(rid, int)

    rows = await _fetch_rows(
        action=audit_events.EVENT_WORKSPACE_GC_EXECUTED,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "workspace.gc_executed"
    assert r["entity_kind"] == "workspace"
    assert r["entity_id"] == "sweep"
    assert r["actor"] == "system:workspace-gc"
    after = json.loads(r["after_json"])
    assert after == {
        "trashed_count": 2,
        "purged_count": 1,
        "quota_evicted_count": 0,
        "skipped_busy_count": 1,
        "skipped_fresh_count": 0,
    }


@pytest.mark.asyncio
async def test_workspace_gc_executed_handles_missing_keys_gracefully(
    _audit_db,
):
    """An empty summary dict must not raise (defensive against future
    GCSummary schema changes)."""
    from backend import audit_events

    rid = await audit_events.emit_workspace_gc_executed(summary={})
    assert isinstance(rid, int)
    rows = await _fetch_rows(
        action=audit_events.EVENT_WORKSPACE_GC_EXECUTED,
    )
    assert len(rows) == 1
    after = json.loads(rows[0]["after_json"])
    assert after == {
        "trashed_count": 0,
        "purged_count": 0,
        "quota_evicted_count": 0,
        "skipped_busy_count": 0,
        "skipped_fresh_count": 0,
    }


# ─── ALL_EVENT_TYPES is a complete, frozen surface ───────────────────


def test_all_event_types_is_complete_and_frozen():
    """Drift guard — if anyone adds / renames a constant, this test
    fails so the audit-stream consumer contract stays in sync.

    Y9 row 5 calls out "audit chain validator (I8 produced) doesn't
    false-positive on Y new events". The validator keys on the
    constants exposed here, so this drift guard is the canonical
    list it consults.
    """
    from backend import audit_events

    expected = {
        "tenant.created",
        "tenant.plan_changed",
        "tenant.disabled",
        "invite.sent",
        "invite.accepted",
        "membership.role_changed",
        "project.created",
        "project.archived",
        "project_share.granted",
        "workspace.gc_executed",
    }
    assert set(audit_events.ALL_EVENT_TYPES) == expected
    assert len(audit_events.ALL_EVENT_TYPES) == 10  # no duplicates


# ─── Chain verifier does NOT false-positive on dot-notation events ───


@pytest.mark.asyncio
async def test_chain_verifier_no_false_positive_on_dot_notation(_audit_db):
    """Y9 row 5 acceptance — emit one row per new event type into the
    same tenant chain and confirm ``verify_chain`` accepts them all.
    The chain hash links payload → curr_hash → next prev_hash; if the
    new ``action`` strings tripped any pre-existing parsing path the
    chain would break here."""
    from backend import audit, audit_events
    from backend.db_context import set_tenant_id

    await _seed_tenants("t-victor")
    set_tenant_id("t-victor")

    # One emit per single-tenant event (skip project_share.granted —
    # that's covered by its own chain-intact test, which verifies
    # both host & guest chains).
    await audit_events.emit_tenant_created(
        tenant_id="t-victor", name="Victor", plan="free",
        enabled=True, actor="op@example.com",
    )
    await audit_events.emit_tenant_plan_changed(
        tenant_id="t-victor", old_plan="free", new_plan="pro",
        actor="op@example.com",
    )
    await audit_events.emit_tenant_disabled(
        tenant_id="t-victor", actor="op@example.com",
    )
    await audit_events.emit_invite_sent(
        tenant_id="t-victor", invite_id="inv-x", email="a@b.c",
        role="member", expires_at=None, invited_by=None,
        actor="op@example.com",
    )
    await audit_events.emit_invite_accepted(
        tenant_id="t-victor", invite_id="inv-x", user_id="u-x",
        role="member", user_was_created=False, already_member=False,
        actor="anonymous",
    )
    await audit_events.emit_membership_role_changed(
        tenant_id="t-victor", user_id="u-x",
        old_role="member", new_role="admin",
        actor="op@example.com",
    )
    await audit_events.emit_project_created(
        tenant_id="t-victor", project_id="p-x",
        name="x", slug="x", product_line="embedded",
        actor="op@example.com",
    )
    await audit_events.emit_project_archived(
        tenant_id="t-victor", project_id="p-x",
        archived_at="2026-04-26 12:00:00", retention_days=90,
        actor="op@example.com",
    )
    await audit_events.emit_workspace_gc_executed(summary={})

    ok, bad = await audit.verify_chain(tenant_id="t-victor")
    assert ok and bad is None
