"""Y9 #285 row 2 — GET /api/v1/admin/audit/tenants/{tid} per-tenant audit query.

Acceptance criteria for the row:

  * super_admin may query ANY tenant.
  * tenant admin / owner may query their OWN tenant only (membership-row
    based, not the legacy ``users.role`` cache).
  * caller below the tenant-admin tier gets 403.
  * every successful query writes one ``audit.queried`` row INTO THE
    QUERIED TENANT'S CHAIN with actor / role / cross-tenant flag /
    filter shape (the "who-queried-which" forensic record).

The tests are split into pure-unit (route mounted, SQL safety, role
helper) and live-PG integration (HTTP path, audit fan-out). The live-PG
tests SKIP without ``OMNI_TEST_PG_URL`` set — the same lane as the
existing admin_tenants tests.

Module-global state audit (per implement_phase_step.md Step 1)
────────────────────────────────────────────────────────────────
Tests use the standard ``client`` + ``pg_test_pool`` fixtures from
``backend/tests/conftest.py``. Each PG-integration test uses a unique
tenant id and TRUNCATE-style cleanup so cross-test bleed is impossible.
The router authz helper ``_user_can_query_tenant_audit`` is stateless;
each call reads ``user_tenant_memberships`` fresh from PG — no
in-memory cache to invalidate.
"""

from __future__ import annotations

import json
import os
import re
import time

import pytest


def _pg_not_available() -> bool:
    return not os.environ.get("OMNI_TEST_PG_URL", "").strip()


_requires_pg = pytest.mark.skipif(
    _pg_not_available(),
    reason="HTTP path depends on asyncpg pool — requires OMNI_TEST_PG_URL.",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: route surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_audit_route_is_mounted():
    """The endpoint must mount under the api prefix as a GET path-param
    route at ``/api/v1/admin/audit/tenants/{tenant_id}``."""
    from backend.main import app
    matches = [
        (r.path, sorted(getattr(r, "methods", []) or []))
        for r in app.routes
        if getattr(r, "path", "")
            == "/api/v1/admin/audit/tenants/{tenant_id}"
    ]
    methods = [m for _, ms in matches for m in ms]
    assert "GET" in methods, (
        f"GET path-param route missing; got {matches!r}"
    )


def test_audit_handler_uses_current_user_dependency():
    """The handler must depend on ``auth.current_user`` (not
    ``require_super_admin``) — the per-tenant authz path needs to
    let tenant admins through and gate them inside the handler
    against the path-param tenant.
    """
    from backend.routers.admin_tenants import get_tenant_audit_events
    from backend import auth as _au

    import inspect
    sig = inspect.signature(get_tenant_audit_events)
    deps = []
    for _name, param in sig.parameters.items():
        target = getattr(param.default, "dependency", None)
        if target is not None:
            deps.append(target)
    assert _au.current_user in deps, (
        f"GET /admin/audit/tenants/{{id}} must depend on auth.current_user; "
        f"deps were {deps!r}"
    )
    # Must NOT use require_super_admin — that would lock tenant admins
    # out of their own tenant's audit, which is the whole point of
    # this endpoint over /admin/tenants/{id}.
    assert _au.require_super_admin not in deps, (
        "Y9 row 2 contract: tenant admins must reach the handler so the "
        "in-handler authz can let them at their OWN tenant only. Using "
        "require_super_admin would 403 them globally."
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: SQL safety + drift guards
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_query_sql_base_is_read_only():
    """The base SELECT must contain no destructive verbs."""
    import backend.routers.admin_tenants as mod
    sql_upper = mod._QUERY_TENANT_AUDIT_SQL_BASE.upper()
    for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "DROP ",
                      "TRUNCATE ", "ALTER ", "GRANT ", "REVOKE "):
        assert forbidden not in sql_upper, (
            f"_QUERY_TENANT_AUDIT_SQL_BASE must be read-only; "
            f"found {forbidden!r}"
        )


def test_query_sql_uses_pg_placeholders():
    """The base + every assembled variant must use ``$N`` (asyncpg),
    never SQLite-style ``?``."""
    from backend.routers.admin_tenants import (
        _build_audit_query_sql,
        _QUERY_TENANT_AUDIT_SQL_BASE,
    )
    assert "?" not in _QUERY_TENANT_AUDIT_SQL_BASE
    assert "$1" in _QUERY_TENANT_AUDIT_SQL_BASE

    # Assemble every combination of optional filters and confirm none
    # introduces a SQLite placeholder.
    for has_since in (False, True):
        for has_until in (False, True):
            for has_actor in (False, True):
                for has_action in (False, True):
                    for has_entity_kind in (False, True):
                        for has_cursor in (False, True):
                            sql, slots = _build_audit_query_sql(
                                has_since=has_since,
                                has_until=has_until,
                                has_actor=has_actor,
                                has_action=has_action,
                                has_entity_kind=has_entity_kind,
                                has_cursor=has_cursor,
                            )
                            assert "?" not in sql, (
                                f"assembled SQL must not contain '?'; "
                                f"got: {sql!r}"
                            )
                            # Slot count exactly matches highest $N.
                            highest_n = max(
                                int(m) for m in re.findall(r"\$(\d+)", sql)
                            )
                            assert highest_n == len(slots), (
                                f"placeholder/slot mismatch: "
                                f"highest=${highest_n} slots={slots!r}"
                            )


def test_query_sql_orders_by_id_descending_with_limit():
    """Newest-first / id-descending pagination contract: callers depend
    on the response being monotone in id so cursor=id<X works."""
    from backend.routers.admin_tenants import _build_audit_query_sql
    sql, _ = _build_audit_query_sql(
        has_since=False, has_until=False, has_actor=False,
        has_action=False, has_entity_kind=False, has_cursor=False,
    )
    assert "ORDER BY id DESC" in sql
    # The LIMIT placeholder is always the last param.
    assert sql.rstrip().endswith("LIMIT $2"), (
        f"expected 'LIMIT $2' as last clause; got {sql!r}"
    )


def test_query_sql_fingerprint_clean():
    """SOP Step-3 fingerprint grep on the SQL constant: catch the four
    classic compat-residue patterns at module-load time."""
    from backend.routers.admin_tenants import _QUERY_TENANT_AUDIT_SQL_BASE
    fingerprint = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    assert not fingerprint.search(_QUERY_TENANT_AUDIT_SQL_BASE)


def test_audit_query_hard_max_is_bounded():
    """The hard-cap must be set to a sane integer so a hostile caller
    cannot request an unbounded scan."""
    from backend.routers.admin_tenants import (
        _AUDIT_QUERY_HARD_MAX,
        _AUDIT_QUERY_DEFAULT_LIMIT,
    )
    assert isinstance(_AUDIT_QUERY_HARD_MAX, int)
    assert isinstance(_AUDIT_QUERY_DEFAULT_LIMIT, int)
    assert 1 <= _AUDIT_QUERY_DEFAULT_LIMIT <= _AUDIT_QUERY_HARD_MAX
    # Mirrors the existing /api/v1/audit endpoint cap of 500 (the I8
    # contract; lower would surprise the operator UI, higher would let
    # a single request pull megabytes from PG).
    assert _AUDIT_QUERY_HARD_MAX == 500


def test_allowed_membership_roles_are_owner_or_admin_only():
    """Tenant viewers / members must NOT be able to query their own
    tenant's audit — only owner / admin tier matches the Y3 / Y4
    admin-tier helper contract."""
    from backend.routers.admin_tenants import (
        _AUDIT_QUERY_ALLOWED_MEMBERSHIP_ROLES,
    )
    assert _AUDIT_QUERY_ALLOWED_MEMBERSHIP_ROLES == frozenset(
        {"owner", "admin"}
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — RBAC: tenant-admin denied on cross-tenant query
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_tenant_admin_blocked_from_cross_tenant_audit(
    client, pg_test_pool,
):
    """A user with role='admin' on tenant A cannot read tenant B's
    audit, even if their account-tier role is admin. Membership row
    is the authoritative source — a missing / wrong-tenant row → 403.
    """
    from backend.main import app
    from backend import auth as _au

    tid_a = "t-y9-row2-a"
    tid_b = "t-y9-row2-b"
    uid_alice = "u-y9-row2-alice"
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
                uid_alice, "alice@y9-row2.local", tid_a,
            )
            # Alice is admin on tenant A only.
            await conn.execute(
                "INSERT INTO user_tenant_memberships "
                "  (user_id, tenant_id, role, status) "
                "VALUES ($1, $2, 'admin', 'active')",
                uid_alice, tid_a,
            )

        alice = _au.User(
            id=uid_alice, email="alice@y9-row2.local", name="Alice",
            role="admin", enabled=True, tenant_id=tid_a,
        )

        async def _fake_current_user():
            return alice

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            # Cross-tenant: 403
            res = await client.get(
                f"/api/v1/admin/audit/tenants/{tid_b}"
            )
            assert res.status_code == 403, res.text
            body = res.json()
            assert body["tenant_id"] == tid_b
            assert body["your_role"] == "admin"

            # Same-tenant: 200 (sanity check the gate isn't blanket-denying
            # Alice — she should get her own tenant's slice).
            res2 = await client.get(
                f"/api/v1/admin/audit/tenants/{tid_a}"
            )
            assert res2.status_code == 200, res2.text
            body2 = res2.json()
            assert body2["tenant_id"] == tid_a
            assert body2["filtered_to_self"] is True
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM audit_log WHERE tenant_id = ANY($1)",
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
async def test_tenant_member_role_blocked_even_on_own_tenant(
    client, pg_test_pool,
):
    """A user with membership role 'member' on their own tenant gets
    403 — the gate requires ``role ∈ {owner, admin}``."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y9-row2-member"
    uid = "u-y9-row2-bob"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Bob Tenant', 'free', 1) "
                "ON CONFLICT DO NOTHING",
                tid,
            )
            await conn.execute(
                "INSERT INTO users (id, email, name, role, "
                "  password_hash, enabled, tenant_id) "
                "VALUES ($1, $2, 'Bob', 'admin', '', 1, $3)",
                uid, "bob@y9-row2.local", tid,
            )
            await conn.execute(
                "INSERT INTO user_tenant_memberships "
                "  (user_id, tenant_id, role, status) "
                "VALUES ($1, $2, 'member', 'active')",
                uid, tid,
            )

        bob = _au.User(
            id=uid, email="bob@y9-row2.local", name="Bob",
            role="admin", enabled=True, tenant_id=tid,
        )

        async def _fake_current_user():
            return bob

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.get(f"/api/v1/admin/audit/tenants/{tid}")
            assert res.status_code == 403, res.text
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM audit_log WHERE tenant_id = $1", tid,
            )
            await conn.execute(
                "DELETE FROM user_tenant_memberships WHERE tenant_id = $1",
                tid,
            )
            await conn.execute(
                "DELETE FROM users WHERE tenant_id = $1", tid,
            )
            await conn.execute(
                "DELETE FROM tenants WHERE id = $1", tid,
            )


@_requires_pg
async def test_suspended_membership_blocked_even_admin_role(
    client, pg_test_pool,
):
    """A *suspended* membership row with role='admin' must NOT pass —
    only ``status='active'`` confers the right to query."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y9-row2-suspended"
    uid = "u-y9-row2-carol"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Carol Tenant', 'free', 1) "
                "ON CONFLICT DO NOTHING",
                tid,
            )
            await conn.execute(
                "INSERT INTO users (id, email, name, role, "
                "  password_hash, enabled, tenant_id) "
                "VALUES ($1, $2, 'Carol', 'admin', '', 1, $3)",
                uid, "carol@y9-row2.local", tid,
            )
            await conn.execute(
                "INSERT INTO user_tenant_memberships "
                "  (user_id, tenant_id, role, status) "
                "VALUES ($1, $2, 'admin', 'suspended')",
                uid, tid,
            )

        carol = _au.User(
            id=uid, email="carol@y9-row2.local", name="Carol",
            role="admin", enabled=True, tenant_id=tid,
        )

        async def _fake_current_user():
            return carol

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.get(f"/api/v1/admin/audit/tenants/{tid}")
            assert res.status_code == 403, res.text
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM audit_log WHERE tenant_id = $1", tid,
            )
            await conn.execute(
                "DELETE FROM user_tenant_memberships WHERE tenant_id = $1",
                tid,
            )
            await conn.execute(
                "DELETE FROM users WHERE tenant_id = $1", tid,
            )
            await conn.execute(
                "DELETE FROM tenants WHERE id = $1", tid,
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — super-admin happy path: cross-tenant query
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_super_admin_can_query_any_tenant_and_writes_who_queried_row(
    client, pg_test_pool,
):
    """End-to-end: a super-admin queries a foreign tenant's audit. The
    response must (a) return rows for THAT tenant only, and (b) trigger
    a single ``audit.queried`` row IN THE QUERIED TENANT'S CHAIN with
    ``cross_tenant=true`` and the actor / role / filter shape recorded.
    """
    tid = "t-y9-row2-super-cross"
    seed_ts = time.time() - 100.0
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Cross Acme', 'pro', 1) "
                "ON CONFLICT DO NOTHING",
                tid,
            )
            # Seed two events directly into THIS tenant's chain so the
            # query has something to return. Each row needs a non-null
            # curr_hash so the chain reader sees them.
            await conn.execute(
                "INSERT INTO audit_log "
                "  (ts, actor, action, entity_kind, entity_id, "
                "   curr_hash, tenant_id) "
                "VALUES ($1, 'system', 'tenant.created', 'tenant', "
                "        $2, 'h-seed-1', $2)",
                seed_ts, tid,
            )
            await conn.execute(
                "INSERT INTO audit_log "
                "  (ts, actor, action, entity_kind, entity_id, "
                "   curr_hash, tenant_id) "
                "VALUES ($1, 'system', 'project.created', 'project', "
                "        'p-seed', 'h-seed-2', $2)",
                seed_ts + 1.0, tid,
            )

        # In the conftest ``client`` fixture the env defaults to open
        # mode → current_user resolves to _ANON_ADMIN with role
        # super_admin. That's the right shape for the cross-tenant
        # super-admin path.
        res = await client.get(f"/api/v1/admin/audit/tenants/{tid}")
        assert res.status_code == 200, res.text
        body = res.json()

        # Response is scoped to this tenant only — the seeded rows
        # appear, no foreign rows leak.
        assert body["tenant_id"] == tid
        assert body["count"] >= 2
        actions = [it["action"] for it in body["items"]]
        assert "tenant.created" in actions
        assert "project.created" in actions
        # Newest-first: tenant.created (older) appears AFTER
        # project.created (newer) in the items list.
        assert actions.index("project.created") < actions.index(
            "tenant.created"
        )
        # filtered_to_self reflects super_admin querying a foreign
        # tenant (super-admin's home tenant is t-default).
        assert body["filtered_to_self"] is False

        # The query itself must have written a single audit.queried
        # row INTO THE QUERIED TENANT'S CHAIN.
        async with pg_test_pool.acquire() as conn:
            audit_rows = await conn.fetch(
                "SELECT actor, action, entity_kind, entity_id, "
                "       after_json, tenant_id "
                "FROM audit_log "
                "WHERE tenant_id = $1 AND action = 'audit.queried' "
                "ORDER BY id DESC",
                tid,
            )
        assert len(audit_rows) == 1, (
            f"expected exactly one audit.queried row in tenant chain; "
            f"got {len(audit_rows)}"
        )
        r = audit_rows[0]
        assert r["entity_kind"] == "tenant"
        assert r["entity_id"] == tid
        assert r["tenant_id"] == tid
        after = json.loads(r["after_json"])
        assert after["queried_tenant"] == tid
        assert after["cross_tenant"] is True
        assert after["queried_by_role"] == "super_admin"
        assert after["filters"]["limit"] == 200
        assert after["result_count"] == body["count"]
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM audit_log WHERE tenant_id = $1", tid,
            )
            await conn.execute(
                "DELETE FROM tenants WHERE id = $1", tid,
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — tenant admin happy path on own tenant (no cross-tenant flag)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_tenant_admin_self_tenant_logs_with_cross_tenant_false(
    client, pg_test_pool,
):
    """When a tenant admin queries their OWN tenant, the audit row
    must record ``cross_tenant=false`` and ``filtered_to_self=true``
    in the response — that's what differentiates "operator looked at
    their own audit" from "super-admin peeked at someone else's".
    """
    from backend.main import app
    from backend import auth as _au

    tid = "t-y9-row2-self"
    uid = "u-y9-row2-dave"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Self Acme', 'free', 1) "
                "ON CONFLICT DO NOTHING",
                tid,
            )
            await conn.execute(
                "INSERT INTO users (id, email, name, role, "
                "  password_hash, enabled, tenant_id) "
                "VALUES ($1, $2, 'Dave', 'admin', '', 1, $3)",
                uid, "dave@y9-row2.local", tid,
            )
            await conn.execute(
                "INSERT INTO user_tenant_memberships "
                "  (user_id, tenant_id, role, status) "
                "VALUES ($1, $2, 'owner', 'active')",
                uid, tid,
            )

        dave = _au.User(
            id=uid, email="dave@y9-row2.local", name="Dave",
            role="admin", enabled=True, tenant_id=tid,
        )

        async def _fake_current_user():
            return dave

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.get(f"/api/v1/admin/audit/tenants/{tid}")
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["tenant_id"] == tid
            assert body["filtered_to_self"] is True

            # The forensic audit row must record cross_tenant=False.
            async with pg_test_pool.acquire() as conn:
                audit_rows = await conn.fetch(
                    "SELECT after_json FROM audit_log "
                    "WHERE tenant_id = $1 AND action = 'audit.queried'",
                    tid,
                )
            assert len(audit_rows) == 1
            after = json.loads(audit_rows[0]["after_json"])
            assert after["cross_tenant"] is False
            assert after["queried_by_user_id"] == uid
            assert after["queried_tenant"] == tid
            assert after["querier_home_tenant"] == tid
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM audit_log WHERE tenant_id = $1", tid,
            )
            await conn.execute(
                "DELETE FROM user_tenant_memberships WHERE tenant_id = $1",
                tid,
            )
            await conn.execute(
                "DELETE FROM users WHERE tenant_id = $1", tid,
            )
            await conn.execute(
                "DELETE FROM tenants WHERE id = $1", tid,
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — filter / pagination behaviour
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_action_filter_narrows_to_canonical_event_type(
    client, pg_test_pool,
):
    """The ``?action=`` filter must narrow to that exact action — the
    canonical Y9 row 1 dot-notation event types must be filterable
    surgically (this is the load-bearing read-side of Y9 row 1)."""
    tid = "t-y9-row2-filter"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Filter Acme', 'free', 1) "
                "ON CONFLICT DO NOTHING",
                tid,
            )
            base = time.time() - 1000.0
            for idx, action in enumerate([
                "tenant.created", "project.created", "tenant.created",
                "invite.sent", "project.archived",
            ]):
                await conn.execute(
                    "INSERT INTO audit_log "
                    "  (ts, actor, action, entity_kind, entity_id, "
                    "   curr_hash, tenant_id) "
                    "VALUES ($1, 'system', $2, 'tenant', $3, $4, $3)",
                    base + idx, action, tid, f"h-{idx}",
                )

        res = await client.get(
            f"/api/v1/admin/audit/tenants/{tid}",
            params={"action": "tenant.created"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        actions = [it["action"] for it in body["items"]]
        assert all(a == "tenant.created" for a in actions)
        assert len(actions) == 2
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM audit_log WHERE tenant_id = $1", tid,
            )
            await conn.execute(
                "DELETE FROM tenants WHERE id = $1", tid,
            )


@_requires_pg
async def test_cursor_pagination_walks_chain_in_id_descending_order(
    client, pg_test_pool,
):
    """Pagination contract: ``?limit=N`` returns N rows; ``?cursor=X``
    returns rows with id < X. ``next_cursor`` is the smallest id in
    the current page (or null at end of stream)."""
    tid = "t-y9-row2-pagination"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Page Acme', 'free', 1) "
                "ON CONFLICT DO NOTHING",
                tid,
            )
            base = time.time() - 1000.0
            for idx in range(5):
                await conn.execute(
                    "INSERT INTO audit_log "
                    "  (ts, actor, action, entity_kind, entity_id, "
                    "   curr_hash, tenant_id) "
                    "VALUES ($1, 'system', 'tenant.created', "
                    "        'tenant', $2, $3, $2)",
                    base + idx, tid, f"h-page-{idx}",
                )

        # First page — limit=2, no cursor → 2 newest rows.
        res = await client.get(
            f"/api/v1/admin/audit/tenants/{tid}",
            params={"limit": 2},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["count"] == 2
        # Rows are newest-first; id strictly decreasing.
        ids = [it["id"] for it in body["items"]]
        assert ids[0] > ids[1]
        nc = body["next_cursor"]
        assert nc == ids[-1]

        # Second page — cursor=next_cursor → next 2 older rows.
        res2 = await client.get(
            f"/api/v1/admin/audit/tenants/{tid}",
            params={"limit": 2, "cursor": nc},
        )
        assert res2.status_code == 200, res2.text
        body2 = res2.json()
        ids2 = [it["id"] for it in body2["items"]]
        assert all(i < nc for i in ids2)
        assert ids2[0] > ids2[1]
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM audit_log WHERE tenant_id = $1", tid,
            )
            await conn.execute(
                "DELETE FROM tenants WHERE id = $1", tid,
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — error branches
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_malformed_tenant_id_returns_422(client):
    """Malformed id (uppercase, no t- prefix) must 422 before any DB hit."""
    res = await client.get("/api/v1/admin/audit/tenants/T-UPPERCASE")
    assert res.status_code == 422, res.text
    res2 = await client.get("/api/v1/admin/audit/tenants/no-prefix-xyz")
    assert res2.status_code == 422, res2.text


@_requires_pg
async def test_unknown_tenant_returns_404_after_authz_passes(client):
    """Well-formed but unknown id must return 404, not 200 with an
    empty list — operator can't tell "tenant deleted" from "tenant
    has zero events" otherwise. The 404 is returned *after* the authz
    check passes (super-admin reaches this branch), so a non-super-
    admin probing arbitrary IDs gets 403 first, no 404 enumeration.
    """
    res = await client.get(
        "/api/v1/admin/audit/tenants/t-this-id-cannot-exist"
    )
    assert res.status_code == 404, res.text


@_requires_pg
async def test_limit_above_hard_cap_returns_422(client):
    """``?limit=10000`` violates the Pydantic constraint (le=500) →
    422 before any DB hit."""
    res = await client.get(
        "/api/v1/admin/audit/tenants/t-default",
        params={"limit": 10_000},
    )
    assert res.status_code == 422, res.text
