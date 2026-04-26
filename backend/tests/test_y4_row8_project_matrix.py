"""Y4 (#280) row 8 — composition matrix for the project surface.

Rows 1-7 each ship their own per-row drift-guard test file
(``test_tenant_projects_create.py`` ... ``test_tenant_projects_quota_override.py``).
Those are *unit-shaped* tests: each pin one endpoint's contract.

Row 8's value-add is the *composition*: realistic operator scenarios
that span multiple endpoints in sequence and assert the cross-row
invariants (visibility / archive / share / oversell / RBAC) hold under
real tenant + member + project_members + project_shares state.

Test families
─────────────
A. Project CRUD lifecycle — POST → GET (live + archived + all) →
   PATCH name / parent / plan / budget → archive → restore → idempotency
   on repeat archive.

B. Member permission matrix — the headline TODO row literal "viewer
   不能 modify artifact、contributor 可以 push 但不能改 secrets" is
   exercised via ``require_project_member(min_role=...)`` (the only
   project-scoped RBAC dependency in the codebase). Five caller
   archetypes × three gates = 15 assertions:
     - tenant viewer (no project_members row) → fails every gate
     - tenant member + project viewer → passes viewer, fails contributor / owner
     - tenant member + project contributor → passes viewer + contributor, fails owner
     - tenant admin (no project_members row, falls back per alembic 0034) →
       passes viewer + contributor, fails owner
     - tenant member + project owner → passes every gate

   Plus: tenant-level admin gate on ``/secrets`` rejects a project
   contributor (proves "contributor can push but cannot change secrets"
   semantically — the secrets surface is tenant-scoped, not project-
   scoped).

C. Archive / restore × oversell — soft-archived rows free their
   reservation; restoring re-claims it. Round-trip:
     1. tenant 'free' (10 GiB cap), project A reserves the full cap
     2. POST B with disk=half-cap → 409 oversell
     3. archive A → POST B with disk=half-cap succeeds
     4. restore A → POST C with disk=quarter-cap → 409 oversell

D. Cross-tenant share RBAC — a guest tenant's admin must NOT be able
   to enumerate the host tenant's other projects via the regular
   ``GET /api/v1/tenants/{host}/projects`` endpoint, even though a
   ``project_shares`` row connects the guest to ONE of the host's
   projects. The share surface is purposefully one-way: it grants
   per-project access, not tenant-list visibility.

E. Budget oversell defence — the per-row 7 file already covers the
   green / red / archived / null branches in isolation; row 8 adds the
   post-PATCH state-untouched assertion (a 409 on PATCH leaves the row
   unchanged, exactly like a 422) plus the "set to NULL clears
   reservation" cross-check (subsequent POST on a different project
   must succeed once the prior one's override is cleared).

Pure-unit fall-back
───────────────────
Tests are skipped when ``OMNI_TEST_PG_URL`` is unset (same posture as
all the per-row files). The two pure-unit assertions at module load
verify the route table includes every endpoint the matrix exercises —
a router-include drift would otherwise silently turn the matrix into
404-only tests.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest


def _pg_not_available() -> bool:
    return not os.environ.get("OMNI_TEST_PG_URL", "").strip()


_requires_pg = pytest.mark.skipif(
    _pg_not_available(),
    reason="HTTP composition matrix depends on asyncpg pool — "
           "requires OMNI_TEST_PG_URL.",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit route presence guards
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_main_app_mounts_full_project_surface():
    """A router-include drift that drops one of the row 1-7 endpoints
    would silently turn this matrix's HTTP calls into 404s and the
    integration tests would degenerate into "negative space" coverage.
    Pin the full mount table here."""
    os.environ.setdefault("OMNISIGHT_AUTH_MODE", "open")
    from backend.main import app

    expected = {
        ("POST", "/api/v1/tenants/{tenant_id}/projects"),
        ("GET", "/api/v1/tenants/{tenant_id}/projects"),
        ("PATCH", "/api/v1/tenants/{tenant_id}/projects/{project_id}"),
        ("POST", "/api/v1/tenants/{tenant_id}/projects/{project_id}/archive"),
        ("POST", "/api/v1/tenants/{tenant_id}/projects/{project_id}/restore"),
        ("POST", "/api/v1/tenants/{tenant_id}/projects/{project_id}/members"),
        ("PATCH", "/api/v1/tenants/{tenant_id}/projects/{project_id}/members/{user_id}"),
        ("DELETE", "/api/v1/tenants/{tenant_id}/projects/{project_id}/members/{user_id}"),
        ("POST", "/api/v1/tenants/{tenant_id}/projects/{project_id}/shares"),
    }
    actual = {
        (next(iter(r.methods or [])), r.path)
        for r in app.routes
        if hasattr(r, "path") and r.methods and len(r.methods) == 1
    }
    missing = expected - actual
    assert not missing, f"main app is missing project routes: {missing!r}"


def test_secrets_router_is_tenant_admin_scoped_not_project_scoped():
    """Row 8 family B's "contributor cannot change secrets" claim hangs
    on this fact: ``backend.routers.secrets`` gates every endpoint
    behind ``auth.require_admin`` (tenant-level admin), NOT
    ``require_project_member``. A regression that loosened the gate to
    ``require_project_member(min_role='contributor')`` would invalidate
    the row 8 narrative."""
    import inspect

    from backend.routers import secrets as _secrets
    from backend import auth as _au

    handlers = [
        _secrets.list_tenant_secrets,
        _secrets.create_secret,
        _secrets.update_secret,
        _secrets.delete_secret_endpoint,
    ]
    for fn in handlers:
        sig = inspect.signature(fn)
        deps = []
        for _name, p in sig.parameters.items():
            target = getattr(p.default, "dependency", None)
            if target is not None:
                deps.append(target)
        assert _au.require_admin in deps, (
            f"{fn.__name__} must depend on auth.require_admin "
            f"(tenant-admin gate); deps were {deps!r}"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers — DB seed + purge + actor swap
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _seed_tenant(pool, tid: str, *, plan: str = "free") -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan, enabled) "
            "VALUES ($1, $2, $3, 1) "
            "ON CONFLICT (id) DO NOTHING",
            tid, f"Test {tid}", plan,
        )


async def _seed_user(
    pool, *, uid: str, tid: str, email: str, role: str = "viewer",
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, "
            "enabled, tenant_id) "
            "VALUES ($1, $2, $3, $4, '', 1, $5) "
            "ON CONFLICT (id) DO NOTHING",
            uid, email, email.split("@")[0], role, tid,
        )


async def _seed_membership(
    pool, *, uid: str, tid: str, role: str, status: str = "active",
) -> None:
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_tenant_memberships "
            "(user_id, tenant_id, role, status, created_at) "
            "VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (user_id, tenant_id) DO NOTHING",
            uid, tid, role, status, created_at,
        )


async def _seed_project_member(
    pool, *, uid: str, pid: str, role: str,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO project_members (user_id, project_id, role) "
            "VALUES ($1, $2, $3) "
            "ON CONFLICT (user_id, project_id) DO NOTHING",
            uid, pid, role,
        )


async def _purge_tenant(pool, tid: str) -> None:
    """Defensive cleanup of every row this matrix may seed. Always
    runs in the test's ``finally`` block so a failed assertion still
    leaves the DB clean for the next test."""
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM project_shares WHERE project_id IN "
            "(SELECT id FROM projects WHERE tenant_id = $1)",
            tid,
        )
        await conn.execute(
            "DELETE FROM project_shares WHERE guest_tenant_id = $1", tid,
        )
        await conn.execute(
            "DELETE FROM project_members WHERE project_id IN "
            "(SELECT id FROM projects WHERE tenant_id = $1)",
            tid,
        )
        await conn.execute(
            "DELETE FROM audit_log WHERE entity_kind = 'project' "
            "AND entity_id IN (SELECT id FROM projects WHERE tenant_id = $1)",
            tid,
        )
        await conn.execute(
            "DELETE FROM audit_log WHERE entity_kind = 'project' "
            "AND tenant_id = $1",
            tid,
        )
        await conn.execute(
            "DELETE FROM audit_log WHERE entity_kind = 'project_member' "
            "AND tenant_id = $1",
            tid,
        )
        await conn.execute(
            "DELETE FROM audit_log WHERE entity_kind = 'project_share' "
            "AND tenant_id = $1",
            tid,
        )
        await conn.execute(
            "DELETE FROM projects WHERE tenant_id = $1", tid,
        )
        await conn.execute(
            "DELETE FROM user_tenant_memberships WHERE tenant_id = $1", tid,
        )
        await conn.execute(
            "DELETE FROM users WHERE tenant_id = $1", tid,
        )
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)


class _ActorSwap:
    """Context manager that swaps ``auth.current_user`` for the
    duration of a test step. Cleans up reliably even if assertions
    fire mid-step.

    The matrix needs to ping the same endpoints under several actor
    archetypes per test (super-admin / tenant admin / tenant viewer /
    project owner / cross-tenant guest) without paying the
    cookie-session round-trip; ``dependency_overrides`` is the standard
    FastAPI escape hatch for exactly this.
    """

    def __init__(self, user) -> None:
        self.user = user
        self._app = None

    def __enter__(self):
        from backend.main import app
        from backend import auth as _au

        async def _fake():
            return self.user

        self._app = app
        app.dependency_overrides[_au.current_user] = _fake
        return self

    def __exit__(self, exc_type, exc, tb):
        from backend import auth as _au

        if self._app is not None:
            self._app.dependency_overrides.pop(_au.current_user, None)
        return False


def _make_user(uid: str, tid: str, *, role: str = "viewer", email: str | None = None):
    """Build a populated ``auth.User`` for ``_ActorSwap``."""
    from backend import auth as _au

    return _au.User(
        id=uid,
        email=email or f"{uid}@example.com",
        name=uid,
        role=role,
        enabled=True,
        tenant_id=tid,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family A — Project CRUD lifecycle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_a1_project_crud_full_lifecycle(client, pg_test_pool):
    """POST → GET → PATCH name → archive → list-live excludes /
    list-archived includes / list-all shows both → restore → list-live
    shows again. End-to-end happy path that proves the rows compose."""
    tid = "t-y4r8-a1-life"
    try:
        await _seed_tenant(pg_test_pool, tid, plan="pro")

        # 1. Create a fresh project.
        r1 = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded",
                "name": "ISP Tuning v1",
                "slug": "isp-v1",
            },
        )
        assert r1.status_code == 201, r1.text
        pid = r1.json()["project_id"]
        assert r1.json()["archived_at"] is None

        # 2. List (default archived=false) sees the row.
        r2 = await client.get(f"/api/v1/tenants/{tid}/projects")
        assert r2.status_code == 200, r2.text
        assert r2.json()["count"] == 1
        assert r2.json()["projects"][0]["project_id"] == pid

        # 3. Rename via PATCH.
        r3 = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}",
            json={"name": "ISP Tuning v2"},
        )
        assert r3.status_code == 200, r3.text
        assert r3.json()["name"] == "ISP Tuning v2"
        assert r3.json().get("no_change") in (False, None)

        # 4. Archive → list-live excludes; list-archived includes.
        r4 = await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/archive",
        )
        assert r4.status_code == 200, r4.text
        live = await client.get(f"/api/v1/tenants/{tid}/projects")
        archived = await client.get(
            f"/api/v1/tenants/{tid}/projects?archived=true",
        )
        all_proj = await client.get(
            f"/api/v1/tenants/{tid}/projects?archived=all",
        )
        assert live.json()["count"] == 0
        assert archived.json()["count"] == 1
        assert all_proj.json()["count"] == 1
        assert archived.json()["projects"][0]["project_id"] == pid
        assert archived.json()["projects"][0]["archived_at"] is not None

        # 5. Restore → list-live shows it again, list-archived empty.
        r5 = await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/restore",
        )
        assert r5.status_code == 200, r5.text
        live2 = await client.get(f"/api/v1/tenants/{tid}/projects")
        archived2 = await client.get(
            f"/api/v1/tenants/{tid}/projects?archived=true",
        )
        assert live2.json()["count"] == 1
        assert archived2.json()["count"] == 0

        # 6. Re-archive is idempotent (no_change=True on second hit).
        await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/archive",
        )
        again = await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/archive",
        )
        assert again.status_code == 200, again.text
        assert again.json().get("no_change") is True
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_a2_patch_clears_optional_fields_via_explicit_null(
    client, pg_test_pool,
):
    """The PATCH tri-state semantics — ``{"plan_override": null}``
    distinct from absence — must propagate end-to-end. POST with
    plan_override + budget, then PATCH with explicit nulls clears both
    back to inheritance."""
    tid = "t-y4r8-a2-tri"
    try:
        await _seed_tenant(pg_test_pool, tid, plan="enterprise")
        r1 = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "web",
                "name": "Marketing",
                "slug": "mkt",
                "plan_override": "pro",
                "disk_budget_bytes": 1024 * 1024 * 1024,
                "llm_budget_tokens": 10_000_000,
            },
        )
        assert r1.status_code == 201, r1.text
        pid = r1.json()["project_id"]
        assert r1.json()["plan_override"] == "pro"
        assert r1.json()["disk_budget_bytes"] == 1024 * 1024 * 1024
        assert r1.json()["llm_budget_tokens"] == 10_000_000

        # Clear all three via explicit null (tri-state PATCH).
        r2 = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}",
            json={
                "plan_override": None,
                "disk_budget_bytes": None,
                "llm_budget_tokens": None,
            },
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["plan_override"] is None
        assert r2.json()["disk_budget_bytes"] is None
        assert r2.json()["llm_budget_tokens"] is None
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_a3_sub_project_parent_link_visible_in_list(
    client, pg_test_pool,
):
    """Create a parent and child; the list endpoint must return
    parent_id linkage so the UI can render the tree. Archiving the
    parent is soft (does NOT cascade), so the child remains in the
    live list with its parent_id pointing at the archived row."""
    tid = "t-y4r8-a3-tree"
    try:
        await _seed_tenant(pg_test_pool, tid)
        rp = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={"product_line": "embedded", "name": "Parent",
                  "slug": "parent"},
        )
        assert rp.status_code == 201, rp.text
        parent_id = rp.json()["project_id"]
        rc = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={"product_line": "embedded", "name": "Child",
                  "slug": "child", "parent_id": parent_id},
        )
        assert rc.status_code == 201, rc.text
        child_id = rc.json()["project_id"]

        # List shows both with the linkage intact.
        rl = await client.get(f"/api/v1/tenants/{tid}/projects")
        assert rl.status_code == 200, rl.text
        by_id = {p["project_id"]: p for p in rl.json()["projects"]}
        assert by_id[child_id]["parent_id"] == parent_id
        assert by_id[parent_id]["parent_id"] is None

        # Archive parent — child must remain live (soft archive
        # doesn't cascade). The child's parent_id still points at
        # the archived row (the archive endpoint flips archived_at
        # but does not touch parent_id wiring).
        await client.post(
            f"/api/v1/tenants/{tid}/projects/{parent_id}/archive",
        )
        live = await client.get(f"/api/v1/tenants/{tid}/projects")
        live_ids = {p["project_id"] for p in live.json()["projects"]}
        assert child_id in live_ids
        assert parent_id not in live_ids
        # And child's parent_id is unchanged.
        rl2 = await client.get(
            f"/api/v1/tenants/{tid}/projects?archived=all",
        )
        by_id = {p["project_id"]: p for p in rl2.json()["projects"]}
        assert by_id[child_id]["parent_id"] == parent_id
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family B — Member permission matrix
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _build_project_gate_app(min_role: str):
    """Spin up a tiny FastAPI app with a single route guarded by the
    ``require_project_member(min_role=...)`` dependency. Returns a
    callable that drives one HTTP probe under a given ``current_user``
    override.

    Mirrors ``test_require_project_member._build_authz_app`` but lives
    in this matrix file so the row 8 narrative is self-contained."""
    from fastapi import Depends, FastAPI
    from backend import auth as _au

    app = FastAPI()

    @app.get("/api/v1/tenants/{tenant_id}/projects/{project_id}/probe")
    async def _probe(
        tenant_id: str,
        project_id: str,
        user: _au.User = Depends(
            _au.require_project_member(min_role=min_role),
        ),
    ) -> dict:
        return {"ok": True, "user_id": user.id}

    return app


async def _http_probe(app, *, override_user, tid: str, pid: str) -> int:
    """Run one probe request under ``override_user`` and return its
    status code. Restores ``dependency_overrides`` on exit."""
    from httpx import ASGITransport, AsyncClient
    from backend import auth as _au

    async def _fake():
        return override_user

    app.dependency_overrides[_au.current_user] = _fake
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test",
        ) as ac:
            res = await ac.get(
                f"/api/v1/tenants/{tid}/projects/{pid}/probe",
            )
            return res.status_code
    finally:
        app.dependency_overrides.pop(_au.current_user, None)


@_requires_pg
async def test_b_permission_matrix_five_actors_three_gates(
    client, pg_test_pool,
):
    """The headline matrix. For each of the three project gates
    (viewer / contributor / owner) and each of the five caller
    archetypes (no membership / project viewer / project contributor /
    tenant admin no explicit / project owner), assert the gate's
    decision matches the alembic 0034 fall-through table.

    This is the formal assertion behind "viewer 不能 modify artifact"
    and "contributor 可以 push 但不能改 secrets" — the project layer
    has three distinct capabilities, and only the rows that say "≥X"
    pass for that gate.
    """
    tid = "t-y4r8-b-matrix"
    try:
        await _seed_tenant(pg_test_pool, tid)

        # Use the row 1 endpoint to actually create the project row
        # (we want a real ``projects`` row + its tenant linkage so the
        # gate's PG SELECT path resolves properly).
        rp = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={"product_line": "embedded", "name": "Gate",
                  "slug": "gate"},
        )
        assert rp.status_code == 201, rp.text
        pid = rp.json()["project_id"]

        # Seed five users with distinct membership shapes.
        actors = {
            "no_membership": ("u-y4r8bnone001", "no_member"),
            "proj_viewer":   ("u-y4r8bvwr0001", "viewer"),
            "proj_contrib":  ("u-y4r8bcontrib", "contributor"),
            "tenant_admin":  ("u-y4r8badmin01", "tadmin"),
            "proj_owner":    ("u-y4r8bowner01", "owner"),
        }
        for archetype, (uid, slug) in actors.items():
            await _seed_user(
                pg_test_pool, uid=uid, tid=tid,
                email=f"{slug}@b.x", role="viewer",
            )

        # Tenant memberships (NOT the no_membership archetype).
        await _seed_membership(
            pg_test_pool, uid=actors["proj_viewer"][0], tid=tid,
            role="member",
        )
        await _seed_membership(
            pg_test_pool, uid=actors["proj_contrib"][0], tid=tid,
            role="member",
        )
        await _seed_membership(
            pg_test_pool, uid=actors["tenant_admin"][0], tid=tid,
            role="admin",
        )
        await _seed_membership(
            pg_test_pool, uid=actors["proj_owner"][0], tid=tid,
            role="member",
        )

        # Project_members (only the three project-explicit archetypes).
        await _seed_project_member(
            pg_test_pool, uid=actors["proj_viewer"][0], pid=pid,
            role="viewer",
        )
        await _seed_project_member(
            pg_test_pool, uid=actors["proj_contrib"][0], pid=pid,
            role="contributor",
        )
        await _seed_project_member(
            pg_test_pool, uid=actors["proj_owner"][0], pid=pid,
            role="owner",
        )

        # Build one app per gate and run the matrix.
        app_viewer = _build_project_gate_app("viewer")
        app_contrib = _build_project_gate_app("contributor")
        app_owner = _build_project_gate_app("owner")

        # Per-archetype expected outcomes per gate. (alembic 0034 says
        # tenant admin/owner default to "contributor" effective project
        # role; member/viewer fall through to no project access.)
        expected = {
            #                          viewer   contrib   owner
            "no_membership":          (403,     403,      403),
            "proj_viewer":            (200,     403,      403),
            "proj_contrib":           (200,     200,      403),
            "tenant_admin":           (200,     200,      403),
            "proj_owner":             (200,     200,      200),
        }

        for archetype, (uid, _slug) in actors.items():
            actor_user = _make_user(uid, tid, role="viewer")
            for gate_app, gate_name, exp_index in (
                (app_viewer, "viewer", 0),
                (app_contrib, "contributor", 1),
                (app_owner, "owner", 2),
            ):
                code = await _http_probe(
                    gate_app, override_user=actor_user, tid=tid, pid=pid,
                )
                want = expected[archetype][exp_index]
                assert code == want, (
                    f"{archetype} on {gate_name} gate: got {code}, "
                    f"expected {want}"
                )
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_b_secrets_router_rejects_project_contributor(
    client, pg_test_pool,
):
    """Semantic complement to family B: a *project contributor* (who
    passes the per-project contributor gate above) is still rejected
    by the tenant-scoped ``/api/v1/secrets`` surface. This is what
    "contributor 可以 push 但不能改 secrets" means in practice — the
    secrets surface predates the project layer and is bound to tenant
    admin."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y4r8-b-sec"
    uid = "u-y4r8bsecuser1"
    try:
        await _seed_tenant(pg_test_pool, tid)
        rp = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={"product_line": "embedded", "name": "S", "slug": "s"},
        )
        assert rp.status_code == 201, rp.text
        pid = rp.json()["project_id"]

        await _seed_user(
            pg_test_pool, uid=uid, tid=tid, email="ctrb@s.x",
            role="viewer",
        )
        await _seed_membership(
            pg_test_pool, uid=uid, tid=tid, role="member",
        )
        await _seed_project_member(
            pg_test_pool, uid=uid, pid=pid, role="contributor",
        )

        contrib_user = _make_user(uid, tid, role="viewer")

        async def _fake():
            return contrib_user

        # Override current_user (and require_admin reads from
        # current_user via auth.role_at_least). The cleanest path is to
        # also override require_admin directly to surface the 403 the
        # production gate produces — but we want to exercise the real
        # path, so we override current_user only and let require_admin
        # decide.
        app.dependency_overrides[_au.current_user] = _fake
        try:
            r = await client.get("/api/v1/secrets")
            assert r.status_code in (401, 403), (
                f"contributor must not be able to list secrets: "
                f"got {r.status_code}: {r.text}"
            )
            r2 = await client.post(
                "/api/v1/secrets",
                json={
                    "key_name": "a", "value": "x",
                    "secret_type": "custom",
                },
            )
            assert r2.status_code in (401, 403), (
                f"contributor must not be able to create secrets: "
                f"got {r2.status_code}: {r2.text}"
            )
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family C — Archive / restore × oversell
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_c_archive_frees_quota_restore_reclaims(
    client, pg_test_pool,
):
    """Round-trip:
      1. tenant 'free' (1M LLM tokens cap), project A reserves the full cap
      2. POST B with llm=half-cap → 409 oversell
      3. archive A → POST B with llm=half-cap → 201 (A's reservation
         no longer counts)
      4. restore A → POST C with llm=quarter-cap → 409 oversell
         (A=cap + B=cap/2 → adding cap/4 always overshoots)

    This composes row 4 (archive/restore) with row 7 (oversell). We
    use ``llm_budget_tokens`` rather than ``disk_budget_bytes`` because
    the alembic 0033 schema declares both budget columns as PG
    ``INTEGER`` (int4); the ``free`` plan disk cap is 10 GiB which
    overflows int4, while the LLM cap (1M tokens) fits comfortably.
    The oversell guard logic exercised here is the same for both
    dimensions — see the per-dimension drift guards in
    ``test_tenant_projects_quota_override.py``."""
    from backend.project_quota import tenant_llm_total_tokens

    tid = "t-y4r8-c-quota"
    try:
        await _seed_tenant(pg_test_pool, tid, plan="free")
        cap = tenant_llm_total_tokens("free")  # 1_000_000

        # 1. Project A holds the entire cap.
        ra = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={"product_line": "embedded", "name": "A", "slug": "a",
                  "llm_budget_tokens": cap},
        )
        assert ra.status_code == 201, ra.text
        a_id = ra.json()["project_id"]

        # 2. POST B with half the cap → oversell.
        rb1 = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={"product_line": "embedded", "name": "B", "slug": "b",
                  "llm_budget_tokens": cap // 2},
        )
        assert rb1.status_code == 409, rb1.text
        body = rb1.json()
        assert body["dimension"] == "llm_budget_tokens"

        # 3. Archive A; now POST B succeeds.
        arc = await client.post(
            f"/api/v1/tenants/{tid}/projects/{a_id}/archive",
        )
        assert arc.status_code == 200, arc.text
        rb2 = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={"product_line": "embedded", "name": "B", "slug": "b",
                  "llm_budget_tokens": cap // 2},
        )
        assert rb2.status_code == 201, rb2.text

        # 4. Restore A → POST C (quarter-cap) → oversell again
        #    (A=cap + B=cap/2 → adding cap/4 always overshoots).
        rs = await client.post(
            f"/api/v1/tenants/{tid}/projects/{a_id}/restore",
        )
        assert rs.status_code == 200, rs.text
        rc = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={"product_line": "embedded", "name": "C", "slug": "c",
                  "llm_budget_tokens": cap // 4},
        )
        assert rc.status_code == 409, rc.text
        # Persisted set still has exactly two live projects (A + B);
        # C did not land.
        async with pg_test_pool.acquire() as conn:
            cnt = await conn.fetchval(
                "SELECT COUNT(*) FROM projects WHERE tenant_id = $1 "
                "AND archived_at IS NULL",
                tid,
            )
        assert cnt == 2
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family D — Cross-tenant share RBAC
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_d_guest_admin_cannot_list_host_projects_via_share(
    client, pg_test_pool,
):
    """Host tenant has 2 projects (P1, P2). A ``project_shares`` row
    grants the guest tenant ``contributor`` access to P1 only. The
    guest tenant's admin must get **403** when calling
    ``GET /api/v1/tenants/{host}/projects`` — the share row buys
    per-project access, NOT tenant-list visibility.

    This is the headline RBAC invariant for cross-tenant shares: a
    rogue guest admin cannot enumerate the host's product portfolio
    just because one project was shared in."""
    from backend.main import app
    from backend import auth as _au

    host = "t-y4r8-d-host"
    guest = "t-y4r8-d-guest"
    try:
        await _seed_tenant(pg_test_pool, host)
        await _seed_tenant(pg_test_pool, guest)

        # Two projects on the host tenant (super-admin caller via the
        # default open-mode anon admin).
        r1 = await client.post(
            f"/api/v1/tenants/{host}/projects",
            json={"product_line": "embedded", "name": "Shared",
                  "slug": "shared"},
        )
        assert r1.status_code == 201, r1.text
        p_shared = r1.json()["project_id"]
        r2 = await client.post(
            f"/api/v1/tenants/{host}/projects",
            json={"product_line": "embedded", "name": "Hidden",
                  "slug": "hidden"},
        )
        assert r2.status_code == 201, r2.text
        p_hidden = r2.json()["project_id"]

        # Grant guest tenant contributor on the shared project.
        rs = await client.post(
            f"/api/v1/tenants/{host}/projects/{p_shared}/shares",
            json={"guest_tenant_id": guest, "role": "contributor"},
        )
        assert rs.status_code == 201, rs.text

        # Seed a guest-tenant admin user.
        guest_admin_uid = "u-y4r8dguestad1"
        await _seed_user(
            pg_test_pool, uid=guest_admin_uid, tid=guest,
            email="ga@guest.x", role="viewer",
        )
        await _seed_membership(
            pg_test_pool, uid=guest_admin_uid, tid=guest, role="admin",
        )
        guest_admin = _make_user(guest_admin_uid, guest, role="viewer")

        async def _fake():
            return guest_admin

        app.dependency_overrides[_au.current_user] = _fake
        try:
            # 1. The guest admin must NOT see ANY host project via
            #    GET /tenants/{host}/projects — even the one shared in.
            r_list_host = await client.get(
                f"/api/v1/tenants/{host}/projects",
            )
            assert r_list_host.status_code == 403, r_list_host.text

            # 2. Guest admin CAN list its own tenant's projects (200);
            #    that list does NOT contain host's projects.
            r_list_guest = await client.get(
                f"/api/v1/tenants/{guest}/projects",
            )
            assert r_list_guest.status_code == 200, r_list_guest.text
            ids = {
                p["project_id"]
                for p in r_list_guest.json()["projects"]
            }
            assert p_shared not in ids
            assert p_hidden not in ids
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        await _purge_tenant(pg_test_pool, host)
        await _purge_tenant(pg_test_pool, guest)


@_requires_pg
async def test_d_guest_admin_cannot_pivot_to_host_admin_endpoints(
    client, pg_test_pool,
):
    """Tighter pin: even mutator endpoints on the host tenant must
    reject the guest admin. PATCH on the SHARED project (the one the
    guest can theoretically interact with) must be 403, not 200 —
    the share grants project-scope access to *use* the project, not
    to administer its row."""
    from backend.main import app
    from backend import auth as _au

    host = "t-y4r8-d-mut-h"
    guest = "t-y4r8-d-mut-g"
    try:
        await _seed_tenant(pg_test_pool, host)
        await _seed_tenant(pg_test_pool, guest)

        rp = await client.post(
            f"/api/v1/tenants/{host}/projects",
            json={"product_line": "embedded", "name": "Shared",
                  "slug": "sh"},
        )
        assert rp.status_code == 201, rp.text
        pid = rp.json()["project_id"]
        rs = await client.post(
            f"/api/v1/tenants/{host}/projects/{pid}/shares",
            json={"guest_tenant_id": guest, "role": "contributor"},
        )
        assert rs.status_code == 201, rs.text

        guest_uid = "u-y4r8dmutadmin"
        await _seed_user(
            pg_test_pool, uid=guest_uid, tid=guest,
            email="ga@m.x", role="viewer",
        )
        await _seed_membership(
            pg_test_pool, uid=guest_uid, tid=guest, role="admin",
        )
        guest_admin = _make_user(guest_uid, guest, role="viewer")

        async def _fake():
            return guest_admin

        app.dependency_overrides[_au.current_user] = _fake
        try:
            # PATCH on host project — must 403.
            rp1 = await client.patch(
                f"/api/v1/tenants/{host}/projects/{pid}",
                json={"name": "Hijacked"},
            )
            assert rp1.status_code == 403, rp1.text

            # Archive on host project — must 403.
            ra = await client.post(
                f"/api/v1/tenants/{host}/projects/{pid}/archive",
            )
            assert ra.status_code == 403, ra.text
        finally:
            app.dependency_overrides.pop(_au.current_user, None)

        # Project state untouched.
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT name, archived_at FROM projects WHERE id = $1",
                pid,
            )
        assert row["name"] == "Shared"
        assert row["archived_at"] is None
    finally:
        await _purge_tenant(pg_test_pool, host)
        await _purge_tenant(pg_test_pool, guest)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family E — Budget oversell defence
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_e_patch_oversell_leaves_row_state_unchanged(
    client, pg_test_pool,
):
    """A 409 oversell on PATCH must NOT mutate the row — same posture
    as a 422 validation failure. Pin both columns + name + plan to
    prove no partial write slipped through. (Row 7 covers the basic
    oversell; row 8 adds the 'no partial write' assertion.)

    Uses ``llm_budget_tokens`` for the same int4-schema reason as
    family C (see comment in test_c_archive_frees_quota_restore_reclaims)."""
    from backend.project_quota import tenant_llm_total_tokens

    tid = "t-y4r8-e-patch"
    try:
        await _seed_tenant(pg_test_pool, tid, plan="pro")
        cap = tenant_llm_total_tokens("pro")  # 100_000_000

        # Two projects each holding 50% of the cap → tenant fully reserved.
        r1 = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={"product_line": "embedded", "name": "A", "slug": "a",
                  "llm_budget_tokens": cap // 2},
        )
        assert r1.status_code == 201, r1.text
        a_id = r1.json()["project_id"]

        r2 = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={"product_line": "embedded", "name": "B", "slug": "b",
                  "llm_budget_tokens": cap // 2},
        )
        assert r2.status_code == 201, r2.text

        # PATCH A's name + bump A's budget → would push to ~150% cap.
        # Per row 7 the per-tenant advisory lock means the SUM(other)
        # check sees B's full reservation; this PATCH must 409.
        rp = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{a_id}",
            json={
                "name": "A-renamed",
                "llm_budget_tokens": cap,
            },
        )
        assert rp.status_code == 409, rp.text

        # Row state untouched: name + budget both unchanged.
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT name, llm_budget_tokens FROM projects "
                "WHERE id = $1",
                a_id,
            )
        assert row["name"] == "A"
        assert row["llm_budget_tokens"] == cap // 2
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_e_clearing_budget_via_null_frees_for_other_projects(
    client, pg_test_pool,
):
    """PATCH ``{"llm_budget_tokens": null}`` clears the project's
    reservation; a subsequent POST on a *different* project must then
    fit under the cap. Cross-row composition: row 3 PATCH × row 7
    oversell × row 1 POST.

    Uses ``llm_budget_tokens`` for the same int4-schema reason as
    family C."""
    from backend.project_quota import tenant_llm_total_tokens

    tid = "t-y4r8-e-clear"
    try:
        await _seed_tenant(pg_test_pool, tid, plan="free")
        cap = tenant_llm_total_tokens("free")  # 1_000_000

        # A holds the whole cap.
        ra = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={"product_line": "embedded", "name": "A", "slug": "a",
                  "llm_budget_tokens": cap},
        )
        assert ra.status_code == 201, ra.text
        a_id = ra.json()["project_id"]

        # POST B (half-cap) → oversell.
        rb1 = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={"product_line": "embedded", "name": "B", "slug": "b",
                  "llm_budget_tokens": cap // 2},
        )
        assert rb1.status_code == 409, rb1.text

        # Clear A's budget.
        rpa = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{a_id}",
            json={"llm_budget_tokens": None},
        )
        assert rpa.status_code == 200, rpa.text
        assert rpa.json()["llm_budget_tokens"] is None

        # POST B again — now succeeds (A is back to inheritance,
        # contributes 0 to the SUM).
        rb2 = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={"product_line": "embedded", "name": "B", "slug": "b",
                  "llm_budget_tokens": cap // 2},
        )
        assert rb2.status_code == 201, rb2.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_e_oversell_409_body_carries_full_diagnostic(
    client, pg_test_pool,
):
    """The 409 body shape is part of the public REST contract — the
    UI's error toast renders ``existing_sum_of_other_projects`` /
    ``proposed_value`` / ``would_be_total`` directly to the operator.
    Pin every field a UI consumer reads."""
    from backend.project_quota import tenant_llm_total_tokens

    tid = "t-y4r8-e-body"
    try:
        await _seed_tenant(pg_test_pool, tid, plan="free")
        cap = tenant_llm_total_tokens("free")  # 1M tokens
        existing = 800_000
        proposed = 400_000
        assert existing + proposed > cap

        ra = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={"product_line": "embedded", "name": "A", "slug": "a",
                  "llm_budget_tokens": existing},
        )
        assert ra.status_code == 201, ra.text

        rb = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={"product_line": "embedded", "name": "B", "slug": "b",
                  "llm_budget_tokens": proposed},
        )
        assert rb.status_code == 409, rb.text
        body = rb.json()
        for key in (
            "detail", "tenant_id", "tenant_plan", "dimension",
            "tenant_total", "existing_sum_of_other_projects",
            "proposed_value", "would_be_total",
        ):
            assert key in body, f"oversell 409 body missing {key!r}"
        assert body["dimension"] == "llm_budget_tokens"
        assert body["tenant_id"] == tid
        assert body["tenant_plan"] == "free"
        assert body["tenant_total"] == cap
        assert body["existing_sum_of_other_projects"] == existing
        assert body["proposed_value"] == proposed
        assert body["would_be_total"] == existing + proposed
    finally:
        await _purge_tenant(pg_test_pool, tid)
