"""Y4 (#280) row 3 — drift guard for
``PATCH /api/v1/tenants/{tid}/projects/{pid}``.

Pure-unit + ASGI mount tests run without PG. Live-PG HTTP path tests
exercise end-to-end behaviour (happy path per field, tri-state
explicit-null clearing, parent re-link, cycle detection, RBAC, audit
emission) and skip when ``OMNI_TEST_PG_URL`` is unset.

Drift guard families:
  (a) Module-level constants — ``_PROJECT_PATCH_LOCK_PREFIX`` /
      ``_PROJECT_PATCH_ALLOWED_MEMBERSHIP_ROLES`` /
      ``_PATCHABLE_PROJECT_FIELDS`` shape + alignment with the
      pydantic schema.
  (b) Pydantic schema (``PatchProjectRequest``) — happy + reject
      cases for each of the 4 fields, plus the at-least-one rule
      enforced in the handler (not the schema, so tested in the HTTP
      layer).
  (c) SQL constants — PG ``$N`` placeholder, secret-leak guard,
      ``FOR UPDATE`` lock on fetch, recursive CTE shape on cycle
      detection, ``CASE WHEN $flag`` shape on the UPDATE template.
  (d) Router endpoint mounted with ``auth.current_user`` dependency.
  (e) Main app full-prefix mount confirms
      ``PATCH /api/v1/tenants/{tenant_id}/projects/{project_id}``.
  (f) HTTP path: happy per field / tri-state clear / no-change /
      404 unknown tenant / 404 unknown project / 422 malformed ids /
      422 empty body / 422 name=null / 422 self-loop / 422 cross-
      tenant parent / 422 unknown parent / 422 cycle / RBAC 403.
  (g) Audit: ``tenant_project_updated`` row + before/after of
      changed fields only + secret-leak guard.
  (h) Self-fingerprint guard.
"""

from __future__ import annotations

import inspect
import os
import re
from pathlib import Path

import pytest


def _pg_not_available() -> bool:
    return not os.environ.get("OMNI_TEST_PG_URL", "").strip()


_requires_pg = pytest.mark.skipif(
    _pg_not_available(),
    reason="HTTP path depends on asyncpg pool — requires OMNI_TEST_PG_URL.",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (a) Module-level constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_project_patch_lock_prefix_shape():
    """The advisory lock key must be per-tenant (suffix is the tenant
    id) so re-parent races on different tenants do not contend."""
    from backend.routers.tenant_projects import _PROJECT_PATCH_LOCK_PREFIX
    assert _PROJECT_PATCH_LOCK_PREFIX == "omnisight_project_patch:"
    assert _PROJECT_PATCH_LOCK_PREFIX.endswith(":")


def test_project_patch_allowed_roles_is_admin_tier_only():
    """Same role gate as POST/GET on project — owner/admin only.
    Regression here would silently grant member/viewer the ability to
    rename / re-budget projects, including setting parent_id to point
    at projects they shouldn't be able to see."""
    from backend.routers.tenant_projects import (
        _PROJECT_PATCH_ALLOWED_MEMBERSHIP_ROLES,
    )
    assert _PROJECT_PATCH_ALLOWED_MEMBERSHIP_ROLES == frozenset(
        {"owner", "admin"}
    )
    assert "member" not in _PROJECT_PATCH_ALLOWED_MEMBERSHIP_ROLES
    assert "viewer" not in _PROJECT_PATCH_ALLOWED_MEMBERSHIP_ROLES


def test_patchable_fields_match_pydantic_schema():
    """The whitelist used to filter ``model_fields_set`` must agree
    with the pydantic schema's declared fields. Drift would either
    silently drop a field the schema accepts (data loss) or accept
    a field the schema declares unset (UB)."""
    from backend.routers.tenant_projects import (
        _PATCHABLE_PROJECT_FIELDS,
        PatchProjectRequest,
    )
    schema_fields = set(PatchProjectRequest.model_fields.keys())
    assert _PATCHABLE_PROJECT_FIELDS == schema_fields, (
        f"_PATCHABLE_PROJECT_FIELDS={_PATCHABLE_PROJECT_FIELDS!r} "
        f"!= schema fields {schema_fields!r}"
    )


def test_patchable_fields_match_todo_row_literal():
    """The TODO row literal calls out exactly four fields:
    ``name / plan_override / budget / parent_id``. The schema
    materialises ``budget`` as ``disk_budget_bytes``. Y4 row 7
    extends the patchable surface to also include
    ``llm_budget_tokens`` (the per-project LLM token quota override
    promised by row 7's "disk_budget_bytes / llm_budget_tokens"
    pair). Drift here would either expand the patch surface beyond
    the contract or drop a field the operator is supposed to be
    able to mutate."""
    from backend.routers.tenant_projects import _PATCHABLE_PROJECT_FIELDS
    assert _PATCHABLE_PROJECT_FIELDS == frozenset({
        "name", "plan_override", "disk_budget_bytes",
        "llm_budget_tokens", "parent_id",
    })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (b) Pydantic schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_patch_request_minimum_body():
    """Empty body parses (the at-least-one rule is enforced in the
    handler, not the schema, so external callers can compose the
    object incrementally without raising)."""
    from backend.routers.tenant_projects import PatchProjectRequest
    body = PatchProjectRequest()
    assert body.model_fields_set == set()
    assert body.name is None
    assert body.plan_override is None
    assert body.disk_budget_bytes is None
    assert body.parent_id is None


def test_patch_request_name_only():
    from backend.routers.tenant_projects import PatchProjectRequest
    body = PatchProjectRequest(name="Renamed")
    assert body.model_fields_set == {"name"}
    assert body.name == "Renamed"


def test_patch_request_explicit_null_is_in_fields_set():
    """Pydantic v2 contract: a key explicitly present in the input
    appears in ``model_fields_set`` even when its value is JSON
    null. The handler relies on this to distinguish "leave alone"
    from "clear"."""
    from backend.routers.tenant_projects import PatchProjectRequest
    body = PatchProjectRequest.model_validate({"plan_override": None})
    assert "plan_override" in body.model_fields_set
    assert body.plan_override is None


def test_patch_request_rejects_empty_name_at_schema_level():
    from pydantic import ValidationError
    from backend.routers.tenant_projects import PatchProjectRequest
    with pytest.raises(ValidationError):
        PatchProjectRequest(name="")


def test_patch_request_rejects_oversize_name():
    from pydantic import ValidationError
    from backend.routers.tenant_projects import PatchProjectRequest
    with pytest.raises(ValidationError):
        PatchProjectRequest(name="x" * 201)


@pytest.mark.parametrize("plan", ["free", "starter", "pro", "enterprise"])
def test_patch_request_accepts_known_plan(plan):
    from backend.routers.tenant_projects import PatchProjectRequest
    body = PatchProjectRequest(plan_override=plan)
    assert body.plan_override == plan


@pytest.mark.parametrize("bad_plan", ["FREE", "platinum", "wizard", ""])
def test_patch_request_rejects_unknown_plan(bad_plan):
    from pydantic import ValidationError
    from backend.routers.tenant_projects import PatchProjectRequest
    with pytest.raises(ValidationError):
        PatchProjectRequest(plan_override=bad_plan)


def test_patch_request_rejects_negative_budget():
    from pydantic import ValidationError
    from backend.routers.tenant_projects import PatchProjectRequest
    with pytest.raises(ValidationError):
        PatchProjectRequest(disk_budget_bytes=-1)


def test_patch_request_accepts_zero_and_positive_budget():
    from backend.routers.tenant_projects import PatchProjectRequest
    body0 = PatchProjectRequest(disk_budget_bytes=0)
    assert body0.disk_budget_bytes == 0
    # Pydantic accepts any non-negative int; the DB column is INTEGER
    # (~2.1GB cap from migration 0033) so the schema layer is not the
    # right place to enforce a tighter bound.
    body_big = PatchProjectRequest(disk_budget_bytes=10 * 1024 * 1024 * 1024)
    assert body_big.disk_budget_bytes == 10 * 1024 * 1024 * 1024


@pytest.mark.parametrize("bad_pid", [
    "P-abcd", "p-", "abcd", "p-_under", " p-abcd",
])
def test_patch_request_rejects_bad_parent_id(bad_pid):
    from pydantic import ValidationError
    from backend.routers.tenant_projects import PatchProjectRequest
    with pytest.raises(ValidationError):
        PatchProjectRequest(parent_id=bad_pid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (c) SQL constants — shape sentinels
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_PATCH_SQL_NAMES = (
    "_FETCH_PROJECT_FOR_UPDATE_SQL",
    "_CYCLE_DETECT_SQL",
    "_PATCH_PROJECT_SQL",
)


@pytest.mark.parametrize("sql_name", _PATCH_SQL_NAMES)
def test_patch_sql_uses_pg_placeholders_only(sql_name):
    from backend.routers import tenant_projects as m
    sql = getattr(m, sql_name)
    assert "?" not in sql, f"{sql_name} contains SQLite-style ?"


@pytest.mark.parametrize("sql_name", _PATCH_SQL_NAMES)
def test_patch_sql_does_not_leak_secret_columns(sql_name):
    from backend.routers import tenant_projects as m
    sql = getattr(m, sql_name)
    assert "password_hash" not in sql, f"{sql_name} projects password_hash"
    assert "oidc_subject" not in sql, f"{sql_name} projects oidc_subject"
    assert "oidc_provider" not in sql, f"{sql_name} projects oidc_provider"
    assert "token_hash" not in sql, f"{sql_name} projects token_hash"


def test_fetch_project_for_update_sql_takes_row_lock():
    """The pre-PATCH SELECT must hold ``FOR UPDATE`` on the row so a
    concurrent PATCH on the same project blocks rather than racing."""
    from backend.routers.tenant_projects import _FETCH_PROJECT_FOR_UPDATE_SQL
    upper = _FETCH_PROJECT_FOR_UPDATE_SQL.upper()
    assert "FOR UPDATE" in upper, _FETCH_PROJECT_FOR_UPDATE_SQL
    assert "FROM PROJECTS" in upper
    assert "WHERE ID = $1 AND TENANT_ID = $2" in upper


def test_fetch_project_for_update_sql_scopes_by_tenant_id():
    """Drift here would let a tenant-A admin PATCH a project owned by
    tenant-B if they could guess the project_id — the worst-class
    multi-tenant bug."""
    from backend.routers.tenant_projects import _FETCH_PROJECT_FOR_UPDATE_SQL
    assert "tenant_id = $2" in _FETCH_PROJECT_FOR_UPDATE_SQL


def test_cycle_detect_sql_is_recursive_cte():
    """The cycle check walks the ancestor chain of the proposed new
    parent. If the project being patched appears, the assignment
    would create a cycle."""
    from backend.routers.tenant_projects import _CYCLE_DETECT_SQL
    upper = _CYCLE_DETECT_SQL.upper()
    assert "WITH RECURSIVE" in upper
    assert "ANCESTOR_CHAIN" in upper
    assert "JOIN ANCESTOR_CHAIN" in upper
    assert "WHERE ID = $2" in upper, _CYCLE_DETECT_SQL


def test_cycle_detect_sql_terminates_at_root():
    """The recursive arm must filter ``c.parent_id IS NOT NULL`` so
    the walk terminates cleanly at the tree root."""
    from backend.routers.tenant_projects import _CYCLE_DETECT_SQL
    assert "c.parent_id IS NOT NULL" in _CYCLE_DETECT_SQL


def test_patch_project_sql_uses_case_when_flag_pattern():
    """The UPDATE template must use ``CASE WHEN $flag THEN $val ELSE
    col END`` per column. This is the trick that lets one static SQL
    handle every subset of fields, AND the only way to express
    "set this column to NULL" without dynamic SQL."""
    from backend.routers.tenant_projects import _PATCH_PROJECT_SQL
    sql = _PATCH_PROJECT_SQL
    # Each of the 4 patchable columns must have its own CASE WHEN.
    for col, flag, val in (
        ("name",              "$3", "$4"),
        ("plan_override",     "$5", "$6"),
        ("disk_budget_bytes", "$7", "$8"),
        ("parent_id",         "$9", "$10"),
    ):
        # Match across whitespace; the static SQL pretty-prints it
        # but the placeholders + column should appear together.
        assert f"WHEN {flag}" in sql, (col, flag, sql)
        assert f"ELSE {col}" in sql, (col, sql)


def test_patch_project_sql_returning_full_row():
    from backend.routers.tenant_projects import _PATCH_PROJECT_SQL
    upper = _PATCH_PROJECT_SQL.upper()
    assert "UPDATE PROJECTS" in upper
    assert "RETURNING" in upper
    # Drift guard: every column the response body emits must be in
    # the RETURNING clause so the post-update body is correct.
    for col in (
        "id", "tenant_id", "product_line", "name", "slug",
        "parent_id", "plan_override", "disk_budget_bytes",
        "llm_budget_tokens", "created_by", "created_at", "archived_at",
    ):
        assert col in _PATCH_PROJECT_SQL, col


def test_patch_project_sql_scopes_by_tenant_id():
    """Same defence-in-depth as the FETCH: WHERE id=$1 AND
    tenant_id=$2 makes a stolen project_id useless if the caller
    is pointed at the wrong tenant."""
    from backend.routers.tenant_projects import _PATCH_PROJECT_SQL
    assert "WHERE id = $1 AND tenant_id = $2" in _PATCH_PROJECT_SQL


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (d) Router wiring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_router_exposes_patch_endpoint():
    from backend.routers.tenant_projects import router
    paths = {(tuple(sorted(r.methods)), r.path) for r in router.routes}
    assert (
        ("PATCH",), "/tenants/{tenant_id}/projects/{project_id}",
    ) in paths


def test_patch_handler_depends_on_current_user():
    from backend.routers import tenant_projects
    from backend import auth
    fn = tenant_projects.patch_project
    sig = inspect.signature(fn)
    deps = []
    for _name, p in sig.parameters.items():
        target = getattr(p.default, "dependency", None)
        if target is not None:
            deps.append(target)
    assert auth.current_user in deps, (
        f"patch_project must depend on auth.current_user; "
        f"deps were {deps!r}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (e) Main app full-prefix mount
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_main_app_mounts_patch_project_endpoint():
    """End-to-end: ``backend.main`` exposes the endpoint at
    ``PATCH /api/v1/tenants/{tenant_id}/projects/{project_id}``."""
    os.environ.setdefault("OMNISIGHT_AUTH_MODE", "open")
    from backend.main import app
    paths = {
        (tuple(sorted(r.methods or [])), r.path)
        for r in app.routes if hasattr(r, "path")
    }
    assert (
        ("PATCH",),
        "/api/v1/tenants/{tenant_id}/projects/{project_id}",
    ) in paths


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (f) HTTP path — happy + error branches (require live PG)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _seed_tenant(pool, tid: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan, enabled) "
            "VALUES ($1, $2, 'free', 1) "
            "ON CONFLICT (id) DO NOTHING",
            tid, f"Test {tid}",
        )


async def _purge_tenant(pool, tid: str) -> None:
    async with pool.acquire() as conn:
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
        await conn.execute("DELETE FROM projects WHERE tenant_id = $1", tid)
        await conn.execute(
            "DELETE FROM user_tenant_memberships WHERE tenant_id = $1", tid,
        )
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)


async def _create_project(
    client, tid: str, *, product_line: str = "embedded",
    slug: str, name: str | None = None,
    parent_id: str | None = None,
) -> str:
    payload = {
        "product_line": product_line,
        "name": name or f"P-{slug}",
        "slug": slug,
    }
    if parent_id is not None:
        payload["parent_id"] = parent_id
    res = await client.post(
        f"/api/v1/tenants/{tid}/projects", json=payload,
    )
    assert res.status_code == 201, res.text
    return res.json()["project_id"]


@_requires_pg
async def test_patch_project_rename_happy(client, pg_test_pool):
    tid = "t-y4-patch-name"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="orig", name="Old")

        res = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}",
            json={"name": "New Name"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["project_id"] == pid
        assert body["tenant_id"] == tid
        assert body["name"] == "New Name"
        assert body["no_change"] is False
        # Other fields must remain untouched.
        assert body["slug"] == "orig"
        assert body["product_line"] == "embedded"
        assert body["plan_override"] is None
        assert body["disk_budget_bytes"] is None
        assert body["parent_id"] is None
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_project_set_plan_override(client, pg_test_pool):
    tid = "t-y4-patch-plan"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="planned")

        res = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}",
            json={"plan_override": "pro"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["plan_override"] == "pro"
        assert body["no_change"] is False
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_project_set_budget(client, pg_test_pool):
    tid = "t-y4-patch-budg"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="b")

        # 1 GiB — sits comfortably inside the migration 0033
        # ``disk_budget_bytes INTEGER`` column (max ~2.1GB).
        budget = 1 * 1024 * 1024 * 1024
        res = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}",
            json={"disk_budget_bytes": budget},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["disk_budget_bytes"] == budget
        assert body["no_change"] is False
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_project_clear_plan_override_via_explicit_null(
    client, pg_test_pool,
):
    """Tri-state semantics: ``plan_override: null`` clears the
    override (the project then inherits the tenant's plan)."""
    tid = "t-y4-patch-clear-plan"
    try:
        await _seed_tenant(pg_test_pool, tid)
        # Create with a non-null plan_override.
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded",
                "name": "P", "slug": "p",
                "plan_override": "pro",
            },
        )
        assert res.status_code == 201, res.text
        pid = res.json()["project_id"]
        assert res.json()["plan_override"] == "pro"

        # Clear it via explicit JSON null.
        clear = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}",
            json={"plan_override": None},
        )
        assert clear.status_code == 200, clear.text
        assert clear.json()["plan_override"] is None
        assert clear.json()["no_change"] is False

        # Confirm persisted.
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT plan_override FROM projects WHERE id = $1", pid,
            )
        assert row["plan_override"] is None
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_project_clear_budget_via_explicit_null(
    client, pg_test_pool,
):
    """Tri-state semantics on disk_budget_bytes — null clears it back
    to "inherit from tenant PLAN quota"."""
    tid = "t-y4-patch-clear-budg"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded",
                "name": "P", "slug": "p",
                "disk_budget_bytes": 1024,
            },
        )
        assert res.status_code == 201, res.text
        pid = res.json()["project_id"]

        clear = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}",
            json={"disk_budget_bytes": None},
        )
        assert clear.status_code == 200, clear.text
        assert clear.json()["disk_budget_bytes"] is None
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_project_clear_parent_via_explicit_null(
    client, pg_test_pool,
):
    """``parent_id: null`` promotes the project back to top-level."""
    tid = "t-y4-patch-clear-parent"
    try:
        await _seed_tenant(pg_test_pool, tid)
        parent = await _create_project(client, tid, slug="parent")
        child = await _create_project(
            client, tid, slug="child", parent_id=parent,
        )

        # Clear parent_id.
        res = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{child}",
            json={"parent_id": None},
        )
        assert res.status_code == 200, res.text
        assert res.json()["parent_id"] is None
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_project_re_parent_to_sibling_in_same_tenant(
    client, pg_test_pool,
):
    """Re-parent an existing leaf to a different parent (not its own
    descendant). Same tenant. Should land at 200."""
    tid = "t-y4-patch-reparent"
    try:
        await _seed_tenant(pg_test_pool, tid)
        parent_a = await _create_project(client, tid, slug="parent-a")
        parent_b = await _create_project(client, tid, slug="parent-b")
        child = await _create_project(
            client, tid, slug="child", parent_id=parent_a,
        )

        res = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{child}",
            json={"parent_id": parent_b},
        )
        assert res.status_code == 200, res.text
        assert res.json()["parent_id"] == parent_b
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_project_multiple_fields_at_once(client, pg_test_pool):
    tid = "t-y4-patch-multi"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="multi", name="A")

        res = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}",
            json={
                "name": "B",
                "plan_override": "starter",
                "disk_budget_bytes": 4096,
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["name"] == "B"
        assert body["plan_override"] == "starter"
        assert body["disk_budget_bytes"] == 4096
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_project_no_change_when_values_match(
    client, pg_test_pool,
):
    """PATCHing with the same values returns 200 with
    ``no_change=True`` and emits no audit row."""
    tid = "t-y4-patch-noop"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="same", name="Same")

        # PATCH name to its current value.
        res = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}",
            json={"name": "Same"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["no_change"] is True
        assert body["name"] == "Same"

        # No audit row should have been emitted for tenant_project_updated.
        async with pg_test_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE action = 'tenant_project_updated' "
                "  AND entity_id = $1",
                pid,
            )
        assert int(count) == 0
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_project_unknown_tenant_returns_404(client):
    res = await client.patch(
        "/api/v1/tenants/t-y4-patch-missing/projects/p-deadbeefdeadbeef",
        json={"name": "X"},
    )
    assert res.status_code == 404, res.text


@_requires_pg
async def test_patch_project_unknown_project_returns_404(
    client, pg_test_pool,
):
    tid = "t-y4-patch-noproj"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.patch(
            f"/api/v1/tenants/{tid}/projects/p-doesnotexisthere",
            json={"name": "X"},
        )
        assert res.status_code == 404, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_project_cross_tenant_project_returns_404(
    client, pg_test_pool,
):
    """A project that exists under tenant A is invisible from tenant
    B's namespace. PATCH on the wrong (tenant, project) pair returns
    404, not 403 — the caller has no business knowing it lives
    elsewhere."""
    t_a = "t-y4-patch-iso-a"
    t_b = "t-y4-patch-iso-b"
    try:
        await _seed_tenant(pg_test_pool, t_a)
        await _seed_tenant(pg_test_pool, t_b)
        pid_a = await _create_project(client, t_a, slug="x")

        res = await client.patch(
            f"/api/v1/tenants/{t_b}/projects/{pid_a}",
            json={"name": "Hijack"},
        )
        assert res.status_code == 404, res.text

        # And tenant A's row is unchanged.
        async with pg_test_pool.acquire() as conn:
            name = await conn.fetchval(
                "SELECT name FROM projects WHERE id = $1", pid_a,
            )
        assert name != "Hijack"
    finally:
        await _purge_tenant(pg_test_pool, t_a)
        await _purge_tenant(pg_test_pool, t_b)


@_requires_pg
async def test_patch_project_malformed_tenant_id_returns_422(client):
    res = await client.patch(
        "/api/v1/tenants/T-Bad-Id/projects/p-aaaabbbbccccdddd",
        json={"name": "X"},
    )
    assert res.status_code == 422, res.text


@_requires_pg
async def test_patch_project_malformed_project_id_returns_422(
    client, pg_test_pool,
):
    tid = "t-y4-patch-badpid"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.patch(
            f"/api/v1/tenants/{tid}/projects/P-BAD",
            json={"name": "X"},
        )
        assert res.status_code == 422, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_project_empty_body_returns_422(
    client, pg_test_pool,
):
    tid = "t-y4-patch-empty"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="x")
        res = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}", json={},
        )
        assert res.status_code == 422, res.text
        assert "at least one" in res.json()["detail"]
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_project_explicit_null_name_returns_422(
    client, pg_test_pool,
):
    """Explicit ``name: null`` is rejected — the column is NOT NULL
    and the schema's union type would otherwise let it slip past
    pydantic."""
    tid = "t-y4-patch-nullname"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="x")
        res = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}",
            json={"name": None},
        )
        assert res.status_code == 422, res.text
        assert "name" in res.json()["detail"]
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_project_self_loop_parent_returns_422(
    client, pg_test_pool,
):
    """Setting ``parent_id == project_id`` is rejected ahead of the
    cycle CTE — a project cannot be its own parent."""
    tid = "t-y4-patch-selfloop"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="me")
        res = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}",
            json={"parent_id": pid},
        )
        assert res.status_code == 422, res.text
        assert "own parent" in res.json()["detail"]
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_project_unknown_parent_returns_422(
    client, pg_test_pool,
):
    tid = "t-y4-patch-noparent"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="x")
        res = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}",
            json={"parent_id": "p-deadbeef00000000"},
        )
        assert res.status_code == 422, res.text
        assert "does not exist" in res.json()["detail"]
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_project_cross_tenant_parent_returns_422(
    client, pg_test_pool,
):
    t_a = "t-y4-patch-ctp-a"
    t_b = "t-y4-patch-ctp-b"
    try:
        await _seed_tenant(pg_test_pool, t_a)
        await _seed_tenant(pg_test_pool, t_b)
        parent_a = await _create_project(client, t_a, slug="p-a")
        target_b = await _create_project(client, t_b, slug="t-b")

        res = await client.patch(
            f"/api/v1/tenants/{t_b}/projects/{target_b}",
            json={"parent_id": parent_a},
        )
        assert res.status_code == 422, res.text
        body = res.json()
        assert body["parent_tenant_id"] == t_a
        assert body["tenant_id"] == t_b
    finally:
        await _purge_tenant(pg_test_pool, t_a)
        await _purge_tenant(pg_test_pool, t_b)


@_requires_pg
async def test_patch_project_cycle_via_descendant_returns_422(
    client, pg_test_pool,
):
    """Tree: A → B → C. Trying to set A.parent_id = C would create
    A → C → B → A. The recursive CTE detects this and 422s."""
    tid = "t-y4-patch-cycle"
    try:
        await _seed_tenant(pg_test_pool, tid)
        a = await _create_project(client, tid, slug="a")
        b = await _create_project(client, tid, slug="b", parent_id=a)
        c = await _create_project(client, tid, slug="c", parent_id=b)

        res = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{a}",
            json={"parent_id": c},
        )
        assert res.status_code == 422, res.text
        body = res.json()
        assert "cycle" in body["detail"].lower()
        assert body["project_id"] == a
        assert body["parent_id"] == c

        # State unchanged: A still top-level.
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT parent_id FROM projects WHERE id = $1", a,
            )
        assert row["parent_id"] is None
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_project_direct_descendant_parent_returns_422(
    client, pg_test_pool,
):
    """Setting parent_id to an immediate child also creates a cycle
    (A → B and then B.parent_id = A → A → B → A)."""
    tid = "t-y4-patch-cycle2"
    try:
        await _seed_tenant(pg_test_pool, tid)
        a = await _create_project(client, tid, slug="a")
        b = await _create_project(client, tid, slug="b", parent_id=a)

        res = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{a}",
            json={"parent_id": b},
        )
        assert res.status_code == 422, res.text
        assert "cycle" in res.json()["detail"].lower()
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  RBAC: non-admin member gets 403
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_patch_project_non_admin_member_gets_403(
    client, pg_test_pool,
):
    """A user whose membership.role is 'member' on the target tenant
    must NOT be permitted to PATCH projects."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y4-patch-rbac"
    uid = "u-y4patchmemberx"
    try:
        await _seed_tenant(pg_test_pool, tid)
        # Create a project as super_admin (default open mode caller).
        pid = await _create_project(client, tid, slug="x", name="orig")

        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, email, name, role, password_hash, "
                "enabled, tenant_id) "
                "VALUES ($1, $2, $3, 'viewer', '', 1, $4) "
                "ON CONFLICT (id) DO NOTHING",
                uid, "memberonly@example.com", "Member Only", tid,
            )
            await conn.execute(
                "INSERT INTO user_tenant_memberships "
                "(user_id, tenant_id, role, status) "
                "VALUES ($1, $2, 'member', 'active') "
                "ON CONFLICT (user_id, tenant_id) DO NOTHING",
                uid, tid,
            )

        member = _au.User(
            id=uid, email="memberonly@example.com", name="Member Only",
            role="viewer", enabled=True, tenant_id=tid,
        )

        async def _fake_current_user():
            return member

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.patch(
                f"/api/v1/tenants/{tid}/projects/{pid}",
                json={"name": "Hijack"},
            )
            assert res.status_code == 403, res.text
            assert "tenant admin" in res.json()["detail"]
        finally:
            app.dependency_overrides.pop(_au.current_user, None)

        # Project untouched.
        async with pg_test_pool.acquire() as conn:
            name = await conn.fetchval(
                "SELECT name FROM projects WHERE id = $1", pid,
            )
        assert name == "orig"
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM user_tenant_memberships WHERE user_id = $1", uid,
            )
            await conn.execute("DELETE FROM users WHERE id = $1", uid)
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (g) Audit emission
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_patch_project_audit_row_written(client, pg_test_pool):
    tid = "t-y4-patch-audit"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="ad", name="Old")

        res = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}",
            json={"name": "New", "plan_override": "pro"},
        )
        assert res.status_code == 200, res.text

        async with pg_test_pool.acquire() as conn:
            audit_row = await conn.fetchrow(
                "SELECT actor, action, entity_kind, entity_id, "
                "       before_json, after_json "
                "FROM audit_log "
                "WHERE action = 'tenant_project_updated' "
                "  AND entity_id = $1 "
                "ORDER BY id DESC LIMIT 1",
                pid,
            )
        assert audit_row is not None
        assert audit_row["entity_kind"] == "project"
        # before / after capture the changed fields.
        before_blob = audit_row["before_json"] or ""
        after_blob = audit_row["after_json"] or ""
        assert "Old" in before_blob, before_blob
        assert "New" in after_blob, after_blob
        assert "pro" in after_blob, after_blob
        # No secret leaks in audit blob.
        for blob_name, blob in (
            ("before_json", before_blob),
            ("after_json", after_blob),
        ):
            assert "password_hash" not in blob, blob_name
            assert "oidc_subject" not in blob, blob_name
            assert "token_hash" not in blob, blob_name
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (h) Self-fingerprint guard — SOP Step 3 pre-commit pattern
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_self_fingerprint_clean():
    """The router source must not contain compat-era SQLite fingerprints
    (``_conn()`` / ``await conn.commit()`` / ``datetime('now')`` /
    ``VALUES ... ?, ?`` placeholder). asyncpg pool conns don't have
    ``.commit()`` and PG uses ``$1, $2`` parameters."""
    src = Path(
        "backend/routers/tenant_projects.py"
    ).read_text(encoding="utf-8")
    fingerprint_re = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    hits = [
        (i, line) for i, line in enumerate(src.splitlines(), start=1)
        if fingerprint_re.search(line)
    ]
    assert hits == [], f"compat-era fingerprint(s) hit: {hits}"
