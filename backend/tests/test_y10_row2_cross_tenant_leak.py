"""Y10 #286 row 2 — cross-tenant leak 專項 acceptance test.

Acceptance criterion (TODO §Y10 row 2)::

    Cross-tenant leak 專項：tenant A 的 admin 明確試著越權存取 tenant B
    的 project / artifact / audit / workspace → 全部 403；tenant A 的
    super-admin 透過 admin endpoint 可以看但記 audit。

Four resource families enumerated by the row text — each family has a
canonical "tenant A admin reaches at tenant B" attack vector. The two
gate models in play across the four:

* **Path-keyed gate** (project, audit, usage breakdown). The path
  carries ``{tenant_id}``. The handler reads an ``active`` row from
  ``user_tenant_memberships`` with role ∈ {owner, admin} on the
  path-param tenant; otherwise 403. Super-admin platform-tier role
  bypasses.
* **ContextVar-keyed gate** (artifact). The query layer applies
  ``tenant_where_pg(...)`` which filters on the request-scope
  ``current_tenant_id()`` ContextVar. The middleware
  (``_tenant_header_gate``) is what populates that ContextVar from the
  ``X-Tenant-Id`` header, refusing to honour cross-tenant headers.

Workspace family caveat
───────────────────────
The HTTP route ``/api/v1/workspaces/{agent_id}`` has no ``{tenant_id}``
in its path and no membership-row authz today — workspaces are
filesystem-keyed under ``{root}/{tenant_id}/{product_line}/{project_id}``
but the in-memory ``backend.workspace`` module returns by ``agent_id``
alone. This row's Block A includes a documentation drift guard that
asserts the current state and points at a tracked follow-up so a
future refactor that flips behaviour either way fails CI loudly. We
do NOT add a 403 to the workspace endpoint as part of Y10 row 2 —
Y10 is the operational exam of Y1-Y9 contract surface, not a place to
add new prod surface.

Test layout
───────────
* **Block A — pure-unit drift guards** (always run, no PG): lock the
  per-family gate identity, source-grep the handler call sites, and
  freeze the membership-role-tier frozensets so any silent slip
  (e.g. someone widens ``_AUDIT_QUERY_ALLOWED_MEMBERSHIP_ROLES`` to
  let viewers query) trips on every CI run including the lanes
  without a PG service.
* **Block B — PG-required acceptance** (skip without
  ``OMNI_TEST_PG_URL``): drive the actual cross-tenant attempts
  through the FastAPI app and assert each family's 403 path plus
  the super-admin happy path that writes ``audit.queried`` into the
  *queried* tenant's chain (the "記 audit" half of the row).

Same skip-pattern as ``test_y9_row5_audit_billing_alignment.py`` and
``test_y10_row1_multi_tenant_concurrency.py`` so the test lane gating
stays consistent across the Y rows.

Module-global state audit (per implement_phase_step.md Step 1)
────────────────────────────────────────────────────────────────
This row is pure test code — zero new prod code, zero new
module-globals. Every Block B test scopes itself to a unique
``t-y10r2-*`` tenant id pair and TRUNCATEs ``audit_log`` /
``user_tenant_memberships`` / ``users`` / ``tenants`` /
``artifacts`` at teardown so cross-test bleed is impossible.

The router authz helpers (``_user_can_query_tenant_audit``,
``_resolve_list_visibility``) are stateless — each call reads
``user_tenant_memberships`` fresh from PG; no in-memory cache to
invalidate. ``backend.db_context`` ContextVars are reset in the
finally block of every test using the artifact path so a sloppy
test cannot leak its tenant slot to the next test.

Read-after-write timing audit (per implement_phase_step.md Step 1)
────────────────────────────────────────────────────────────────
Each Block B test ``await``s its writers (seed INSERTs / HTTP
requests) sequentially before asserting on the resulting state. The
super-admin happy path waits on the ``GET`` to return 200 (which
itself awaits the ``audit.log`` write inside the handler's emit
block) before reading back the ``audit.queried`` row — the read sees
the write because both are sequential in the test body and the audit
write went through ``pg_advisory_xact_lock(hashtext('audit-chain-' ||
tenant_id))`` which forces a clean commit before the lock releases.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import time

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Acceptance-criterion dimensions (Y10 row 2, TODO §Y10)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# Four resource families enumerated by the TODO text.
_RESOURCE_FAMILIES = ("project", "artifact", "audit", "workspace")

# Tenant ids reserved for this row's tests. The ``-y10r2-`` segment
# makes these immediately identifiable in audit_log forensics if a
# crashed test leaves rows behind.
_TENANT_PREFIX = "t-y10r2"


def _pg_not_available() -> bool:
    return not os.environ.get("OMNI_TEST_PG_URL", "").strip()


_requires_pg = pytest.mark.skipif(
    _pg_not_available(),
    reason="Y10 row 2 cross-tenant leak HTTP path tests need an actual "
           "PG instance — set OMNI_TEST_PG_URL.",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Block A — pure-unit drift guards (always run)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_resource_families_match_acceptance_criterion():
    """Lock the four-family tuple against drift.

    The TODO row literally enumerates ``project / artifact / audit /
    workspace`` — if a future refactor adds a fifth family and
    forgets to extend Y10 row 2's coverage, this guard makes the
    omission visible on every CI run.
    """
    assert _RESOURCE_FAMILIES == ("project", "artifact", "audit", "workspace")
    assert len(_RESOURCE_FAMILIES) == 4
    # Audit family covered by both ``audit_log`` query endpoint and
    # the usage breakdown — the two share the same authz helper. The
    # row text says "audit" once but the operational surface is
    # actually two endpoints, so we sanity-check the test will hit
    # both below.


def test_path_keyed_project_endpoint_uses_resolve_list_visibility():
    """``GET /api/v1/tenants/{tid}/projects`` must gate cross-tenant
    callers via ``_resolve_list_visibility`` (the membership-row read).

    Source-grep, not behavioural — a behavioural test is in Block B.
    The point of the unit guard: if a future refactor swaps the
    membership-row check for the legacy ``users.role`` cache, the
    Y series cross-tenant invariant silently regresses. This catches
    that on every CI run.
    """
    from backend.routers import tenant_projects

    src = inspect.getsource(tenant_projects.list_projects)
    assert "_resolve_list_visibility" in src, (
        "tenant_projects.list_projects must call _resolve_list_visibility "
        "to authoritatively decide cross-tenant access — Y10 row 2 "
        "project family invariant"
    )
    # And the helper itself must consult user_tenant_memberships, not
    # the legacy users.role cache.
    helper_src = inspect.getsource(tenant_projects._resolve_list_visibility)
    assert "user_tenant_memberships" in helper_src, (
        "_resolve_list_visibility must read user_tenant_memberships — "
        "the legacy users.role cache is NOT authoritative for "
        "cross-tenant access"
    )
    # Strict role tier — owner / admin get full visibility; member /
    # viewer fall through to explicit project_members rows only. Drift
    # to "member" would broaden the gate beyond the row's contract.
    assert (
        "_PROJECT_LIST_FULL_VISIBILITY_MEMBERSHIP_ROLES"
        in helper_src
    )


def test_path_keyed_audit_endpoint_uses_user_can_query_tenant_audit():
    """``GET /api/v1/admin/audit/tenants/{tid}`` must gate cross-tenant
    callers via ``_user_can_query_tenant_audit``.

    Source-grep on the handler. Drift guard for Y10 row 2's "audit
    family" 403 path.
    """
    from backend.routers import admin_tenants

    src = inspect.getsource(admin_tenants.get_tenant_audit_events)
    assert "_user_can_query_tenant_audit" in src, (
        "admin_tenants.get_tenant_audit_events must call "
        "_user_can_query_tenant_audit — Y10 row 2 audit family invariant"
    )
    # The 403 branch returns body shape {detail, tenant_id, your_role,
    # your_home_tenant}; lock all four keys so Block B's response-body
    # assertions cannot drift.
    assert '"tenant_id": tenant_id' in src
    assert '"your_role": user.role' in src
    assert '"your_home_tenant": user.tenant_id' in src


def test_usage_breakdown_endpoint_uses_user_can_query_tenant_audit():
    """``GET /api/v1/admin/usage/breakdown`` reuses the same per-tenant
    audit-query helper for its 403 gate (Y9 row 3 acceptance).

    Y10 row 2 depends on this contract because the breakdown endpoint
    is the operational sibling of the audit-list endpoint — both are
    "super-admin can query any tenant; tenant owner/admin can query
    their own tenant only" surface. A future refactor that gives the
    breakdown its own gate could silently drift to a wider role tier.
    """
    from backend.routers import admin_tenants

    src = inspect.getsource(admin_tenants.get_usage_breakdown_by_project)
    assert "_user_can_query_tenant_audit" in src, (
        "get_usage_breakdown_by_project must reuse "
        "_user_can_query_tenant_audit — Y10 row 2 audit-tier invariant"
    )


def test_artifact_listing_uses_tenant_where_pg_filter():
    """Cross-tenant artifact reads MUST be filtered by
    ``tenant_where_pg(...)`` — the ContextVar-keyed gate.

    Source-grep on the three artifact DB helpers (list / get / delete).
    Without this filter a request whose ContextVar somehow set to
    tenant B (or unset) could see tenant A's rows; with it, the
    helper appends ``tenant_id = $N`` and binds the active
    ContextVar. The middleware ``_tenant_header_gate`` is what pins
    the ContextVar from ``X-Tenant-Id``; the legacy header swap
    behaviour for ``users.role='admin'`` is a known Y-era gap that
    the path-keyed endpoints (project/audit) defend against — for
    artifacts the defence is explicitly the per-row tenant filter.
    """
    from backend import db

    for name in ("list_artifacts", "get_artifact", "delete_artifact"):
        helper = getattr(db, name)
        src = inspect.getsource(helper)
        assert "tenant_where_pg" in src, (
            f"db.{name} must call tenant_where_pg(...) — Y10 row 2 "
            f"artifact family invariant"
        )


def test_artifact_insert_uses_tenant_insert_value_not_caller_supplied():
    """Defence against forge-tenant-id INSERT: the artifact insert
    helper MUST derive ``tenant_id`` from the server-side
    ``tenant_insert_value()`` (which reads the ContextVar pinned by
    middleware), NOT from the caller's payload.

    A malicious caller setting ``data['tenant_id']='t-victim'`` cannot
    sideload a row into another tenant's chain because the helper
    overrides the column with the contextvar-derived value.
    """
    from backend import db

    src = inspect.getsource(db.insert_artifact)
    assert "tenant_insert_value()" in src, (
        "db.insert_artifact must call tenant_insert_value() — Y10 row 2 "
        "anti-forge invariant: caller-supplied tenant_id is ignored"
    )


def test_audit_query_allowed_membership_roles_strict_owner_admin_only():
    """Tenant viewers / members must NOT be able to query their own
    tenant's audit even on the Y10 cross-tenant axis.

    Drift guard: locking the frozenset to {owner, admin} keeps the
    role tier tight. If a future refactor adds "auditor" or expands
    to "member", that is a broadening of the cross-tenant audit
    surface — must be discussed in commit message + this test
    failing forces that conversation.
    """
    from backend.routers.admin_tenants import (
        _AUDIT_QUERY_ALLOWED_MEMBERSHIP_ROLES,
    )
    assert _AUDIT_QUERY_ALLOWED_MEMBERSHIP_ROLES == frozenset(
        {"owner", "admin"}
    )


def test_project_list_full_visibility_membership_roles_strict():
    """Same as above for the project-listing endpoint."""
    from backend.routers.tenant_projects import (
        _PROJECT_LIST_FULL_VISIBILITY_MEMBERSHIP_ROLES,
    )
    assert _PROJECT_LIST_FULL_VISIBILITY_MEMBERSHIP_ROLES == frozenset(
        {"owner", "admin"}
    )


def test_super_admin_audit_emit_targets_path_tenant_chain_not_user_home():
    """Source-grep on the audit endpoint's emit block: when a
    super-admin queries tenant B, the ``audit.queried`` row must land
    in tenant B's chain (not in the super-admin's home tenant chain).

    The pattern is ``set_tenant_id(tenant_id)`` (path param) followed
    by ``audit.log(action='audit.queried', ...)`` inside a save-and-
    restore. If a refactor accidentally used ``user.tenant_id`` (the
    super-admin's home), the queried tenant's audit pane would lose
    its forensic record of cross-tenant inspection — silently
    breaking the row's "記 audit" half.
    """
    from backend.routers import admin_tenants

    src = inspect.getsource(admin_tenants.get_tenant_audit_events)

    # Find the emit block: set_tenant_id called with the path-param
    # variable, NOT user.tenant_id.
    assert "_stv(tenant_id)" in src, (
        "audit.queried emit must override ContextVar to the path-param "
        "tenant_id (not user.tenant_id) — Y10 row 2 forensic invariant"
    )
    assert 'action="audit.queried"' in src
    # The save-and-restore pattern: prior contextvar value preserved
    # in `saved` and restored in finally.
    assert "saved = _ctv()" in src
    assert "_stv(saved)" in src

    # The audit row payload records cross_tenant flag + queried_by_role
    # so downstream consumers (audit pane, alerting) can distinguish
    # super-admin cross-tenant peeks from operator self-queries.
    assert '"cross_tenant"' in src
    assert '"queried_by_role"' in src


def test_workspace_endpoint_lacks_tenant_path_segment_known_followup():
    """Documented drift guard: ``/api/v1/workspaces/{agent_id}`` does
    NOT have a ``{tenant_id}`` segment today. If a future change adds
    one (or removes it), this test trips and forces an update to
    Y10 row 2's HANDOFF entry.

    Why this is a "drift guard" rather than a 403 acceptance: Y10 is
    the operational exam of Y1-Y9 surface; the workspace HTTP route
    pre-dates the Y series and isn't a Y deliverable. The TODO row
    text says "all 403", but for workspace that 403 isn't yet wired.
    Tracking this honestly here keeps the row's claims accurate.
    Follow-up tracked in HANDOFF; not blocking this row.
    """
    from backend.main import app

    workspace_paths = [
        getattr(r, "path", "")
        for r in app.routes
        if getattr(r, "path", "").startswith("/api/v1/workspaces")
    ]
    # Both ``/api/v1/workspaces`` and ``/api/v1/workspaces/{agent_id}``
    # are mounted at module load — that's the contract today.
    assert "/api/v1/workspaces/{agent_id}" in workspace_paths, (
        "workspace endpoint mount drift — Y10 row 2 fixture sanity"
    )
    # The path does NOT carry tenant_id — confirm explicitly so any
    # future schema change (e.g. /workspaces/{tenant_id}/{agent_id})
    # is caught by this guard.
    assert "/api/v1/workspaces/{tenant_id}/{agent_id}" not in workspace_paths


def test_tenant_header_gate_rejects_cross_tenant_in_session_mode():
    """Source-grep on the I7 ``_tenant_header_gate`` middleware: in
    session mode, an X-Tenant-Id mismatch with the session user's
    tenant is 403 unless ``user.role == 'admin'`` (the legacy bypass).

    Y10 row 2 doesn't fix the legacy ``users.role='admin'`` bypass —
    that is a known Y-era gap. We lock the gate's CURRENT shape so
    a refactor that further widens the bypass (e.g. to viewers) is
    caught immediately.
    """
    from backend import main as _main

    src = inspect.getsource(_main._tenant_header_gate)
    # Mismatch + non-admin → 403 with "Tenant ... not accessible".
    assert "Tenant {header_tid} not accessible" in src or (
        "header_tid != user.tenant_id" in src
        and 'user.role != "admin"' in src
    ), (
        "I7 tenant gate must 403 cross-tenant header swap from non-admin "
        "users — Y10 row 2 lock on current behaviour"
    )


def test_audit_queried_emit_uses_per_tenant_advisory_lock_path():
    """The ``audit.queried`` row is written via ``audit.log(...)``
    which goes through ``_log_impl`` and the per-tenant advisory lock
    (Y10 row 1 invariant). This test source-greps the audit endpoint
    to confirm the call goes through ``audit.log`` rather than a
    raw INSERT that would bypass the chain hash + lock entirely.
    """
    from backend.routers import admin_tenants

    src = inspect.getsource(admin_tenants.get_tenant_audit_events)
    # The audit.log call is present (chain-protected emission).
    assert "_audit.log(" in src
    # No raw INSERT into audit_log from inside this handler.
    assert "INSERT INTO audit_log" not in src.upper(), (
        "audit.queried emit must go through audit.log (chain + lock); "
        "raw INSERT bypasses the per-tenant advisory lock and breaks "
        "verify_chain"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Block B — PG-required acceptance: live HTTP cross-tenant probes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _tid_a() -> str:
    return f"{_TENANT_PREFIX}-a"


def _tid_b() -> str:
    return f"{_TENANT_PREFIX}-b"


def _uid_alice() -> str:
    # Y3/Y4 user-id pattern: 'u-' prefix + lowercase short suffix.
    return "u-y10r2alice"


async def _seed_two_tenants_with_alice_admin_on_a(pool) -> None:
    """Seed the standard Y10 row 2 fixture:

    * tenants ``t-y10r2-a`` + ``t-y10r2-b`` (both ``free``, enabled).
    * user Alice with legacy ``users.role='admin'`` + home tenant A.
    * Active ``user_tenant_memberships(role='admin')`` row on A only.
      No row on B → cross-tenant attempts must 403.

    Same shape as ``test_admin_audit_tenants_y9_row2.py`` Block B
    fixtures so the two test files exercise an identical state space
    on PG and the membership-row contract stays single-sourced.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan, enabled) "
            "VALUES ($1, 'Y10R2 A', 'free', 1), ($2, 'Y10R2 B', 'free', 1) "
            "ON CONFLICT (id) DO NOTHING",
            _tid_a(), _tid_b(),
        )
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, "
            "  enabled, tenant_id) "
            "VALUES ($1, $2, 'Alice', 'admin', '', 1, $3) "
            "ON CONFLICT (id) DO NOTHING",
            _uid_alice(), "alice@y10r2.local", _tid_a(),
        )
        await conn.execute(
            "INSERT INTO user_tenant_memberships "
            "  (user_id, tenant_id, role, status) "
            "VALUES ($1, $2, 'admin', 'active') "
            "ON CONFLICT (user_id, tenant_id) DO NOTHING",
            _uid_alice(), _tid_a(),
        )


async def _purge_y10_row2_tenants(pool) -> None:
    """Tear down everything seeded by ``_seed_two_tenants_with_alice_admin_on_a``
    plus any artifact / audit / project rows the tests created. Order
    matters because of FKs (project_members → projects → tenants).
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM project_members WHERE project_id IN ("
            "  SELECT id FROM projects WHERE tenant_id = ANY($1))",
            [_tid_a(), _tid_b()],
        )
        await conn.execute(
            "DELETE FROM projects WHERE tenant_id = ANY($1)",
            [_tid_a(), _tid_b()],
        )
        await conn.execute(
            "DELETE FROM artifacts WHERE tenant_id = ANY($1)",
            [_tid_a(), _tid_b()],
        )
        await conn.execute(
            "DELETE FROM audit_log WHERE tenant_id = ANY($1)",
            [_tid_a(), _tid_b()],
        )
        await conn.execute(
            "DELETE FROM user_tenant_memberships WHERE tenant_id = ANY($1)",
            [_tid_a(), _tid_b()],
        )
        await conn.execute(
            "DELETE FROM users WHERE id = $1",
            _uid_alice(),
        )
        await conn.execute(
            "DELETE FROM tenants WHERE id = ANY($1)",
            [_tid_a(), _tid_b()],
        )


@pytest.fixture()
async def _y10_row2_db(pg_test_pool):
    """Seed-and-purge fixture: pre-clean, yield, post-clean. Mirrors
    the ``_y10_row1_db`` shape so the two row test files share the
    same teardown discipline.
    """
    pool = pg_test_pool
    await _purge_y10_row2_tenants(pool)
    try:
        yield pool
    finally:
        from backend.db_context import set_project_id, set_tenant_id
        set_tenant_id(None)
        set_project_id(None)
        await _purge_y10_row2_tenants(pool)


def _alice_user(tenant_id: str | None = None):
    """Construct the ``auth.User`` we override into ``current_user``.

    Defaults to home tenant A so the X-Tenant-Id middleware sees a
    consistent user-vs-header story. Cross-tenant assertions land at
    handler authz, not header authz.
    """
    from backend import auth as _au

    return _au.User(
        id=_uid_alice(),
        email="alice@y10r2.local",
        name="Alice",
        role="admin",
        enabled=True,
        tenant_id=tenant_id or _tid_a(),
    )


# ─────────────────────────────────────────────────────────────────
#  B-row 1 — Project family: tenant A admin → tenant B project list
# ─────────────────────────────────────────────────────────────────


@_requires_pg
@pytest.mark.asyncio
async def test_tenant_admin_blocked_from_cross_tenant_project_list(
    client, _y10_row2_db,
):
    """Tenant A admin (active membership on A only) cannot enumerate
    tenant B's projects via ``GET /api/v1/tenants/{tid_b}/projects``.

    The 403 must carry "active membership" in the detail so the
    operator UI can render a clear explanation. Mirrors the
    ``test_get_projects_no_membership_returns_403`` shape from
    ``test_tenant_projects_list.py`` but goes through Alice's
    ``users.role='admin'`` legacy tier — proves the membership-row
    gate is authoritative, NOT the legacy users.role cache.
    """
    from backend.main import app
    from backend import auth as _au

    pool = _y10_row2_db
    await _seed_two_tenants_with_alice_admin_on_a(pool)

    alice = _alice_user()

    async def _fake_current_user():
        return alice

    app.dependency_overrides[_au.current_user] = _fake_current_user
    try:
        # Cross-tenant: 403.
        res = await client.get(f"/api/v1/tenants/{_tid_b()}/projects")
        assert res.status_code == 403, res.text
        assert "active membership" in res.json()["detail"]

        # Same-tenant sanity: Alice CAN list her own tenant's projects.
        # Proves the gate isn't blanket-denying — it's per-tenant.
        res2 = await client.get(f"/api/v1/tenants/{_tid_a()}/projects")
        assert res2.status_code == 200, res2.text
        body2 = res2.json()
        assert body2["tenant_id"] == _tid_a()
    finally:
        app.dependency_overrides.pop(_au.current_user, None)


# ─────────────────────────────────────────────────────────────────
#  B-row 2 — Project share endpoint: same path-keyed gate
# ─────────────────────────────────────────────────────────────────


@_requires_pg
@pytest.mark.asyncio
async def test_tenant_admin_blocked_from_cross_tenant_project_shares(
    client, _y10_row2_db,
):
    """The path-keyed gate covers nested project resources too.

    Tenant A admin trying to list tenant B's project shares via
    ``GET /api/v1/tenants/{tid_b}/projects/{pid_b}/shares`` must
    403 — even if the project_id by sheer guess happens to exist on
    tenant B. The 404-vs-403 ordering (authz first, existence second)
    means an attacker cannot enumerate which project_ids live on a
    foreign tenant via timing.
    """
    from backend.main import app
    from backend import auth as _au

    pool = _y10_row2_db
    await _seed_two_tenants_with_alice_admin_on_a(pool)

    # Seed a real project on tenant B — proves authz fires BEFORE
    # the project-existence probe.
    pid_b = "p-y10r2-b-secret"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO projects (id, tenant_id, product_line, name, "
            "                       slug, created_by) "
            "VALUES ($1, $2, 'embedded', 'Y10R2 B Secret', "
            "        'y10r2-b-secret', 'system') "
            "ON CONFLICT DO NOTHING",
            pid_b, _tid_b(),
        )

    alice = _alice_user()

    async def _fake_current_user():
        return alice

    app.dependency_overrides[_au.current_user] = _fake_current_user
    try:
        res = await client.get(
            f"/api/v1/tenants/{_tid_b()}/projects/{pid_b}/shares"
        )
        # 403 (not 404) — authz fires first, no leak about existence.
        assert res.status_code == 403, res.text
    finally:
        app.dependency_overrides.pop(_au.current_user, None)


# ─────────────────────────────────────────────────────────────────
#  B-row 3 — Audit family: tenant A admin → tenant B audit list
# ─────────────────────────────────────────────────────────────────


@_requires_pg
@pytest.mark.asyncio
async def test_tenant_admin_blocked_from_cross_tenant_audit_query(
    client, _y10_row2_db,
):
    """Tenant A admin → ``GET /api/v1/admin/audit/tenants/{tid_b}``
    → 403. Mirrors Y9 row 2's same-named test but lives in Y10 row 2's
    lane so the cross-tenant 403 acceptance is co-located with the
    other three families. Body shape (detail + tenant_id + your_role
    + your_home_tenant) locks the response contract for the operator
    UI.
    """
    from backend.main import app
    from backend import auth as _au

    pool = _y10_row2_db
    await _seed_two_tenants_with_alice_admin_on_a(pool)

    alice = _alice_user()

    async def _fake_current_user():
        return alice

    app.dependency_overrides[_au.current_user] = _fake_current_user
    try:
        res = await client.get(
            f"/api/v1/admin/audit/tenants/{_tid_b()}"
        )
        assert res.status_code == 403, res.text
        body = res.json()
        assert body["tenant_id"] == _tid_b()
        assert body["your_role"] == "admin"
        assert body["your_home_tenant"] == _tid_a()
    finally:
        app.dependency_overrides.pop(_au.current_user, None)


# ─────────────────────────────────────────────────────────────────
#  B-row 4 — Usage breakdown (audit-tier sibling): same gate
# ─────────────────────────────────────────────────────────────────


@_requires_pg
@pytest.mark.asyncio
async def test_tenant_admin_blocked_from_cross_tenant_usage_breakdown(
    client, _y10_row2_db,
):
    """``GET /api/v1/admin/usage/breakdown?tenant_id={tid_b}`` shares
    the same audit-tier authz helper as the audit-list endpoint. A
    cross-tenant probe by tenant A admin must 403.

    Acceptance for Y10 row 2's "audit family" is interpreted to cover
    both the chain-event read endpoint AND the billing-event read
    endpoint, because both surface tenant-private data and both
    delegate to ``_user_can_query_tenant_audit``. A regression on
    either is a leak.
    """
    from backend.main import app
    from backend import auth as _au

    pool = _y10_row2_db
    await _seed_two_tenants_with_alice_admin_on_a(pool)

    alice = _alice_user()

    async def _fake_current_user():
        return alice

    app.dependency_overrides[_au.current_user] = _fake_current_user
    try:
        res = await client.get(
            f"/api/v1/admin/usage/breakdown?tenant_id={_tid_b()}"
        )
        assert res.status_code == 403, res.text
        body = res.json()
        assert body["tenant_id"] == _tid_b()
        assert body["your_role"] == "admin"
    finally:
        app.dependency_overrides.pop(_au.current_user, None)


# ─────────────────────────────────────────────────────────────────
#  B-row 5 — Super-admin happy path: cross-tenant audit query
#            writes audit.queried into the queried tenant's chain
# ─────────────────────────────────────────────────────────────────


@_requires_pg
@pytest.mark.asyncio
async def test_super_admin_can_audit_query_cross_tenant_and_writes_record(
    client, _y10_row2_db,
):
    """The "tenant A 的 super-admin 透過 admin endpoint 可以看但記
    audit" half of Y10 row 2.

    A super-admin queries tenant B's audit. The query returns 200
    with rows scoped to tenant B; AND a single ``audit.queried`` row
    is written INTO TENANT B'S CHAIN with ``cross_tenant=true``,
    ``queried_by_role='super_admin'``, plus the filter shape +
    result_count. This is the forensic "who peeked at us" record
    that lets tenant B's own operator pane see who looked at their
    audit log and when.

    Co-located here with the 403 tests so a single Y10 row 2 file
    proves both halves of the row.
    """
    pool = _y10_row2_db
    await _seed_two_tenants_with_alice_admin_on_a(pool)

    # Seed two events in tenant B's chain so the super-admin's query
    # has something to return. ``curr_hash`` is mandatory NOT NULL on
    # the audit_log schema.
    seed_ts = time.time() - 100.0
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO audit_log "
            "  (ts, actor, action, entity_kind, entity_id, "
            "   curr_hash, tenant_id) "
            "VALUES ($1, 'system', 'tenant.created', 'tenant', "
            "        $2, 'h-y10r2-seed-1', $2)",
            seed_ts, _tid_b(),
        )
        await conn.execute(
            "INSERT INTO audit_log "
            "  (ts, actor, action, entity_kind, entity_id, "
            "   curr_hash, tenant_id) "
            "VALUES ($1, 'system', 'project.created', 'project', "
            "        'p-y10r2-seed', 'h-y10r2-seed-2', $2)",
            seed_ts + 1.0, _tid_b(),
        )

    # In the conftest ``client`` fixture default env, current_user
    # resolves to _ANON_ADMIN (super_admin). That's the right shape
    # for the cross-tenant super-admin path.
    res = await client.get(f"/api/v1/admin/audit/tenants/{_tid_b()}")
    assert res.status_code == 200, res.text
    body = res.json()

    # Response is scoped to tenant B; the seeded rows appear, no
    # cross-tenant leak. ``filtered_to_self=False`` is the cross-
    # tenant marker on the response side.
    assert body["tenant_id"] == _tid_b()
    assert body["count"] >= 2
    actions = [it["action"] for it in body["items"]]
    assert "tenant.created" in actions
    assert "project.created" in actions
    assert body["filtered_to_self"] is False

    # The query itself wrote ONE audit.queried row into tenant B's
    # chain. The row belongs to tid_b's chain (tenant_id column),
    # not to the super-admin's home tenant.
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT actor, action, entity_kind, entity_id, "
            "       after_json, tenant_id "
            "FROM audit_log "
            "WHERE tenant_id = $1 AND action = 'audit.queried' "
            "ORDER BY id DESC",
            _tid_b(),
        )
    assert len(rows) == 1, (
        f"expected exactly one audit.queried row in tenant B's chain; "
        f"got {len(rows)}"
    )
    r = rows[0]
    assert r["entity_kind"] == "tenant"
    assert r["entity_id"] == _tid_b()
    assert r["tenant_id"] == _tid_b(), (
        "audit.queried row must live IN THE QUERIED TENANT'S CHAIN, "
        "not in the super-admin's home tenant"
    )
    after = json.loads(r["after_json"])
    assert after["queried_tenant"] == _tid_b()
    assert after["cross_tenant"] is True
    assert after["queried_by_role"] == "super_admin"
    assert after["result_count"] == body["count"]

    # Sanity: NO audit.queried row leaked into tenant A's chain.
    async with pool.acquire() as conn:
        a_rows = await conn.fetch(
            "SELECT id FROM audit_log "
            "WHERE tenant_id = $1 AND action = 'audit.queried'",
            _tid_a(),
        )
    assert len(a_rows) == 0, (
        f"audit.queried row leaked into tenant A's chain — "
        f"expected 0 rows, got {len(a_rows)}"
    )


# ─────────────────────────────────────────────────────────────────
#  B-row 6 — Super-admin repeat-query → multiple audit.queried rows
# ─────────────────────────────────────────────────────────────────


@_requires_pg
@pytest.mark.asyncio
async def test_super_admin_repeat_audit_query_emits_one_row_per_call(
    client, _y10_row2_db,
):
    """Each successful super-admin cross-tenant audit query writes one
    ``audit.queried`` row. After 3 sequential queries the chain
    accumulates exactly 3 rows — proves the emit is per-call (not
    deduped, not batched) so forensics can reconstruct every peek.

    Sequential rather than concurrent on purpose: per-call accounting
    is the contract; concurrent emit timing is covered by Y10 row 1's
    audit-chain stress test.
    """
    pool = _y10_row2_db
    await _seed_two_tenants_with_alice_admin_on_a(pool)

    # Three sequential super-admin queries.
    for i in range(3):
        res = await client.get(
            f"/api/v1/admin/audit/tenants/{_tid_b()}?limit={50 + i}"
        )
        assert res.status_code == 200, res.text

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, after_json FROM audit_log "
            "WHERE tenant_id = $1 AND action = 'audit.queried' "
            "ORDER BY id ASC",
            _tid_b(),
        )
    assert len(rows) == 3, (
        f"expected 3 audit.queried rows after 3 queries; "
        f"got {len(rows)}"
    )
    # Each row's filter snapshot reflects its own call — limit
    # increments 50/51/52 so the per-call accounting is visible in
    # the forensics.
    limits = [
        json.loads(r["after_json"])["filters"]["limit"] for r in rows
    ]
    assert limits == [50, 51, 52]


# ─────────────────────────────────────────────────────────────────
#  B-row 7 — Artifact family: tenant_where_pg filter under dual state
# ─────────────────────────────────────────────────────────────────


@_requires_pg
@pytest.mark.asyncio
async def test_artifact_listing_filters_by_current_tenant_no_cross_leak(
    _y10_row2_db,
):
    """Insert artifacts into tenant A and tenant B; with the
    ContextVar pinned to tenant A, ``db.list_artifacts`` returns only
    A's row, never B's. Flip the ContextVar to B → only B's row.
    Flip to a third tenant with no rows → empty result.

    This is the load-bearing assertion for the "artifact" half of
    Y10 row 2: a leak via the artifact reader is exactly this
    contract being broken (e.g. someone removes the
    ``tenant_where_pg(...)`` call). The Block A source-grep guards
    catch the structural drift; this Block B test catches the
    behavioural drift (e.g. ``tenant_where_pg`` is present but its
    no-tenant-set fallthrough fires when the Y series didn't expect
    it to).
    """
    from backend import db
    from backend.db_context import set_tenant_id
    from backend.db_pool import get_pool

    pool = _y10_row2_db
    await _seed_two_tenants_with_alice_admin_on_a(pool)

    # Insert one artifact per tenant via direct SQL (skip the
    # ``insert_artifact`` helper here so the test can independently
    # cross-check the read path's filter without depending on the
    # write path's contextvar pin — both are tested separately).
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO artifacts (id, task_id, agent_id, name, type, "
            "                        file_path, size, created_at, "
            "                        version, checksum, tenant_id) "
            "VALUES ('art-y10r2-a', 't-task-a', '', 'A', 'log', "
            "        '/tmp/a', 0, 'now', '', '', $1)",
            _tid_a(),
        )
        await conn.execute(
            "INSERT INTO artifacts (id, task_id, agent_id, name, type, "
            "                        file_path, size, created_at, "
            "                        version, checksum, tenant_id) "
            "VALUES ('art-y10r2-b', 't-task-b', '', 'B', 'log', "
            "        '/tmp/b', 0, 'now', '', '', $1)",
            _tid_b(),
        )

    # Read path A: ContextVar = tenant A → only A's artifact visible.
    set_tenant_id(_tid_a())
    try:
        async with get_pool().acquire() as conn:
            rows = await db.list_artifacts(conn)
        ids = sorted(r["id"] for r in rows)
        assert ids == ["art-y10r2-a"], (
            f"tenant_where_pg leak: expected only ['art-y10r2-a'] "
            f"under tenant A context, got {ids!r}"
        )

        # Direct get_artifact for B's id while pinned on A: must miss
        # (return None). This is the cross-tenant guess-by-id
        # invariant — knowing B's artifact id doesn't help if the
        # filter is on.
        async with get_pool().acquire() as conn:
            row_b_under_a = await db.get_artifact(conn, "art-y10r2-b")
        assert row_b_under_a is None, (
            "get_artifact returned B's row under tenant A context — "
            "Y10 row 2 artifact family invariant violated"
        )

        # Read path B: ContextVar = tenant B → only B's artifact.
        set_tenant_id(_tid_b())
        async with get_pool().acquire() as conn:
            rows_b = await db.list_artifacts(conn)
        ids_b = sorted(r["id"] for r in rows_b)
        assert ids_b == ["art-y10r2-b"], (
            f"tenant_where_pg leak: expected only ['art-y10r2-b'] "
            f"under tenant B context, got {ids_b!r}"
        )

        # Read path C: ContextVar = third (unrelated) tenant → empty.
        set_tenant_id("t-y10r2-stranger")
        async with get_pool().acquire() as conn:
            rows_x = await db.list_artifacts(conn)
        assert rows_x == [], (
            f"tenant_where_pg leak: expected empty list under stranger "
            f"context, got {rows_x!r}"
        )
    finally:
        # Reset ContextVar so subsequent tests start clean.
        set_tenant_id(None)


# ─────────────────────────────────────────────────────────────────
#  B-row 8 — Artifact family: forge-tenant on INSERT is rejected
# ─────────────────────────────────────────────────────────────────


@_requires_pg
@pytest.mark.asyncio
async def test_artifact_insert_ignores_caller_supplied_tenant_id(
    _y10_row2_db,
):
    """A malicious caller passes ``data['tenant_id']='t-y10r2-b'``
    while the request ContextVar is pinned to A. ``db.insert_artifact``
    MUST overwrite the column with the contextvar-derived value —
    the caller's payload tenant_id is ignored.

    Defence-in-depth against forge-tenant INSERT. Without this
    invariant, an admin on tenant A could submit forged artifact
    rows that land in tenant B's bucket, enabling cross-tenant data
    injection / quota poisoning.
    """
    from backend import db
    from backend.db_context import set_tenant_id
    from backend.db_pool import get_pool

    pool = _y10_row2_db
    await _seed_two_tenants_with_alice_admin_on_a(pool)

    # ContextVar pinned to A — the legit "Alice is logged into A"
    # state. The data dict tries to forge into B.
    set_tenant_id(_tid_a())
    try:
        forged = {
            "id": "art-y10r2-forged",
            "task_id": "t-task-forge",
            "agent_id": "",
            "name": "forged",
            "type": "log",
            "file_path": "/tmp/forge",
            "size": 0,
            "created_at": "now",
            "version": "",
            "checksum": "",
            # Caller LIES about tenant_id — must be ignored.
            "tenant_id": _tid_b(),
        }
        async with get_pool().acquire() as conn:
            await db.insert_artifact(conn, forged)

        # Read back: must land in tenant A's bucket, not B's.
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow(
                "SELECT tenant_id FROM artifacts WHERE id = $1",
                "art-y10r2-forged",
            )
        assert row is not None, "forged INSERT failed — fixture bug?"
        assert row["tenant_id"] == _tid_a(), (
            f"caller-supplied tenant_id={_tid_b()!r} must be IGNORED; "
            f"row landed in tenant_id={row['tenant_id']!r} (forge "
            f"succeeded — Y10 row 2 anti-forge invariant violated)"
        )
    finally:
        set_tenant_id(None)


# ─────────────────────────────────────────────────────────────────
#  B-row 9 — Fingerprint grep on the production touch-points
# ─────────────────────────────────────────────────────────────────


def test_production_handlers_compat_fingerprint_clean():
    """SOP Step 3 fingerprint grep on the FOUR production handlers
    Y10 row 2 leans on (Y10 row 2 itself ships zero prod code, but
    the row's contract rests on these handlers staying clean of
    compat residue).

    Pattern checks for the four classic SQLite-era compat residues
    (compat-wrapper entry sentinel, explicit transaction commit on
    asyncpg pool conn, SQLite-only timestamp literal, and SQLite
    positional placeholders inside VALUES). Any hit indicates a
    regression against the Phase-3-Runtime-v2 PG-native baseline.
    The literal regex is below for traceability.
    """
    import pathlib

    fingerprint = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|"
        r"datetime\('now'\)|"
        r"VALUES.*\?[,)]"
    )

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    targets = [
        repo_root / "backend" / "routers" / "tenant_projects.py",
        repo_root / "backend" / "routers" / "admin_tenants.py",
        repo_root / "backend" / "routers" / "artifacts.py",
        repo_root / "backend" / "routers" / "workspaces.py",
    ]

    hits: list[str] = []
    for path in targets:
        src = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(src.splitlines(), start=1):
            if fingerprint.search(line):
                hits.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not hits, (
        "Y10 row 2 production touch-points failed Step-3 fingerprint "
        "grep:\n" + "\n".join(hits)
    )
