"""Y4 (#280) row 1 — drift guard for POST /api/v1/tenants/{tid}/projects.

Pure-unit + ASGI mount tests run without PG. Live-PG HTTP path tests
exercise end-to-end behaviour (happy path, slug uniqueness, parent
linkage, RBAC, audit emission) and skip when ``OMNI_TEST_PG_URL`` is
unset.

Drift guard families:
  (a) Module-level constants — TENANT_ID_PATTERN / PROJECT_ID_PATTERN /
      SLUG_PATTERN regexes + PRODUCT_LINE_ENUM / PROJECT_PLAN_ENUM
      tuples + their boundary cases.
  (b) Pydantic schema (CreateProjectRequest) — happy + reject cases for
      product_line / slug / plan_override / disk_budget_bytes / parent_id.
  (c) SQL constants — PG ``$N`` placeholder, ON CONFLICT DO NOTHING +
      RETURNING shape, no-secret-leak, parent probe shape.
  (d) Router endpoint mounted with ``auth.current_user`` dependency.
  (e) Main app full-prefix mount confirms
      ``/api/v1/tenants/{tenant_id}/projects``.
  (f) HTTP path: happy / slug-dup 409 / unknown-tenant 404 / malformed
      body 422 / unknown product_line 422 / parent missing 422 / parent
      cross-tenant 422 / RBAC 403 / sub-project happy.
  (g) Audit: tenant_project_created row + actor + after-payload + no
      secret leak.
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


def test_tenant_id_pattern_matches_y2_y3_source_of_truth():
    """The Y4 router must agree with admin_tenants / tenant_invites /
    tenant_members on the tenant id regex — drift would let a malformed
    id sneak through one but not the others."""
    from backend.routers import tenant_projects
    from backend.routers import admin_tenants
    assert tenant_projects.TENANT_ID_PATTERN == admin_tenants.TENANT_ID_PATTERN


def test_product_line_enum_matches_todo_literal():
    """TODO row literal: ``embedded / web / mobile / software / custom``."""
    from backend.routers import tenant_projects
    assert tenant_projects.PRODUCT_LINE_ENUM == (
        "embedded", "web", "mobile", "software", "custom",
    )


def test_project_plan_enum_matches_migration_check():
    """Must match the migration 0033 CHECK on ``projects.plan_override``."""
    from backend.routers import tenant_projects
    assert tenant_projects.PROJECT_PLAN_ENUM == (
        "free", "starter", "pro", "enterprise",
    )


def test_project_id_pattern_constant_shape():
    from backend.routers.tenant_projects import PROJECT_ID_PATTERN
    assert PROJECT_ID_PATTERN.startswith("^p-")
    assert PROJECT_ID_PATTERN.endswith("$")


def test_slug_pattern_constant_shape():
    from backend.routers.tenant_projects import SLUG_PATTERN
    # Must enforce lowercase alnum + hyphen; leading char alnum.
    assert SLUG_PATTERN == r"^[a-z0-9][a-z0-9-]{0,63}$"


@pytest.mark.parametrize("good", [
    "t-default", "t-acme", "t-acme-corp", "t-a1b", "t-0abc",
])
def test_is_valid_tenant_id_accepts(good):
    from backend.routers.tenant_projects import _is_valid_tenant_id
    assert _is_valid_tenant_id(good), good


@pytest.mark.parametrize("bad", [
    "", "T-default", "tdefault", "t--double", "t-",
    "t-acme_corp", "t-acme.corp",
])
def test_is_valid_tenant_id_rejects(bad):
    from backend.routers.tenant_projects import _is_valid_tenant_id
    assert not _is_valid_tenant_id(bad), bad


@pytest.mark.parametrize("good", [
    "p-abcd",
    "p-0123456789abcdef",       # 16 hex chars (this is what _mint mints)
    "p-a-b-c",
    "p-" + "a" * 63,
])
def test_is_valid_project_id_accepts(good):
    from backend.routers.tenant_projects import _is_valid_project_id
    assert _is_valid_project_id(good), good


@pytest.mark.parametrize("bad", [
    "",
    "p-",
    "p-x",                       # too short (<3 trailing chars)
    "P-abc1",                    # uppercase prefix
    "p-ABC1",                    # uppercase trailing
    "p-_underscore",             # underscore not allowed
    "p--double",                 # leading hyphen in trailing class
    "p-" + "a" * 65,             # too long (max 64 trailing)
    " p-abcd",                   # leading whitespace
])
def test_is_valid_project_id_rejects(bad):
    from backend.routers.tenant_projects import _is_valid_project_id
    assert not _is_valid_project_id(bad), bad


def test_mint_project_id_shape():
    from backend.routers.tenant_projects import (
        _mint_project_id,
        _is_valid_project_id,
    )
    seen = {_mint_project_id() for _ in range(50)}
    # All 50 unique with overwhelming probability.
    assert len(seen) == 50
    for pid in seen:
        assert pid.startswith("p-")
        # 16 hex chars after the prefix.
        body = pid[2:]
        assert re.fullmatch(r"[0-9a-f]{16}", body), pid
        # Self-format check passes.
        assert _is_valid_project_id(pid), pid


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (b) Pydantic schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_create_project_request_minimum_body():
    """Required fields: product_line + name + slug."""
    from backend.routers.tenant_projects import CreateProjectRequest
    body = CreateProjectRequest(
        product_line="embedded", name="ISP Tuning", slug="isp-tuning",
    )
    assert body.product_line == "embedded"
    assert body.name == "ISP Tuning"
    assert body.slug == "isp-tuning"
    # Optional fields default to None.
    assert body.plan_override is None
    assert body.disk_budget_bytes is None
    assert body.parent_id is None


@pytest.mark.parametrize("pl", ["embedded", "web", "mobile", "software", "custom"])
def test_create_project_request_accepts_each_product_line(pl):
    from backend.routers.tenant_projects import CreateProjectRequest
    body = CreateProjectRequest(product_line=pl, name="P", slug="p")
    assert body.product_line == pl


@pytest.mark.parametrize("bad_pl", [
    "EMBEDDED", "Embedded", "firmware", "default", "", "iot", "  embedded ",
])
def test_create_project_request_rejects_unknown_product_line(bad_pl):
    from pydantic import ValidationError
    from backend.routers.tenant_projects import CreateProjectRequest
    with pytest.raises(ValidationError):
        CreateProjectRequest(product_line=bad_pl, name="P", slug="p")


@pytest.mark.parametrize("bad_slug", [
    "",                  # empty
    "Has-Caps",          # uppercase
    "_under",            # leading underscore
    "-leading-hyphen",   # leading hyphen disallowed
    "with space",        # space
    "with.dot",          # dot
    "x" * 65,            # too long
])
def test_create_project_request_rejects_bad_slug(bad_slug):
    from pydantic import ValidationError
    from backend.routers.tenant_projects import CreateProjectRequest
    with pytest.raises(ValidationError):
        CreateProjectRequest(product_line="embedded", name="P", slug=bad_slug)


@pytest.mark.parametrize("good_slug", [
    "a", "x" * 64, "isp-tuning", "v2", "1-version",
])
def test_create_project_request_accepts_good_slug(good_slug):
    from backend.routers.tenant_projects import CreateProjectRequest
    body = CreateProjectRequest(
        product_line="embedded", name="P", slug=good_slug,
    )
    assert body.slug == good_slug


def test_create_project_request_rejects_empty_name():
    from pydantic import ValidationError
    from backend.routers.tenant_projects import CreateProjectRequest
    with pytest.raises(ValidationError):
        CreateProjectRequest(product_line="embedded", name="", slug="p")


def test_create_project_request_rejects_oversize_name():
    from pydantic import ValidationError
    from backend.routers.tenant_projects import CreateProjectRequest
    with pytest.raises(ValidationError):
        CreateProjectRequest(
            product_line="embedded", name="x" * 201, slug="p",
        )


@pytest.mark.parametrize("plan", ["free", "starter", "pro", "enterprise"])
def test_create_project_request_accepts_known_plan(plan):
    from backend.routers.tenant_projects import CreateProjectRequest
    body = CreateProjectRequest(
        product_line="embedded", name="P", slug="p", plan_override=plan,
    )
    assert body.plan_override == plan


@pytest.mark.parametrize("bad_plan", ["FREE", "platinum", "wizard", "", "  free"])
def test_create_project_request_rejects_unknown_plan(bad_plan):
    from pydantic import ValidationError
    from backend.routers.tenant_projects import CreateProjectRequest
    with pytest.raises(ValidationError):
        CreateProjectRequest(
            product_line="embedded", name="P", slug="p", plan_override=bad_plan,
        )


def test_create_project_request_rejects_negative_budget():
    from pydantic import ValidationError
    from backend.routers.tenant_projects import CreateProjectRequest
    with pytest.raises(ValidationError):
        CreateProjectRequest(
            product_line="embedded", name="P", slug="p",
            disk_budget_bytes=-1,
        )


def test_create_project_request_accepts_zero_and_positive_budget():
    from backend.routers.tenant_projects import CreateProjectRequest
    body0 = CreateProjectRequest(
        product_line="embedded", name="P", slug="p", disk_budget_bytes=0,
    )
    assert body0.disk_budget_bytes == 0
    body1 = CreateProjectRequest(
        product_line="embedded", name="P", slug="p",
        disk_budget_bytes=1024 * 1024 * 1024,
    )
    assert body1.disk_budget_bytes == 1024 * 1024 * 1024


@pytest.mark.parametrize("bad_pid", [
    "P-abcd", "p-", "abcd", "p-_under", " p-abcd",
])
def test_create_project_request_rejects_bad_parent_id(bad_pid):
    from pydantic import ValidationError
    from backend.routers.tenant_projects import CreateProjectRequest
    with pytest.raises(ValidationError):
        CreateProjectRequest(
            product_line="embedded", name="P", slug="p", parent_id=bad_pid,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (c) SQL constants — shape sentinels
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_SQL_NAMES = (
    "_FETCH_TENANT_SQL",
    "_FETCH_PARENT_PROJECT_SQL",
    "_INSERT_PROJECT_SQL",
    "_FETCH_EXISTING_PROJECT_SQL",
)


@pytest.mark.parametrize("sql_name", _SQL_NAMES)
def test_sql_uses_pg_placeholders_only(sql_name):
    """Drift guard: every SQL constant must use ``$N`` placeholders.
    A regressed SQLite-style ``?`` would silently break asyncpg."""
    from backend.routers import tenant_projects as m
    sql = getattr(m, sql_name)
    assert "?" not in sql, f"{sql_name} contains SQLite-style ?"


@pytest.mark.parametrize("sql_name", _SQL_NAMES)
def test_sql_does_not_leak_secret_columns(sql_name):
    """Project / tenant SQL must not project password_hash / oidc_*."""
    from backend.routers import tenant_projects as m
    sql = getattr(m, sql_name)
    assert "password_hash" not in sql, f"{sql_name} projects password_hash"
    assert "oidc_subject" not in sql, f"{sql_name} projects oidc_subject"
    assert "oidc_provider" not in sql, f"{sql_name} projects oidc_provider"
    assert "token_hash" not in sql, f"{sql_name} projects token_hash"


def test_insert_sql_atomic_on_conflict_returning():
    """The INSERT must be ``ON CONFLICT (tenant_id, product_line, slug)
    DO NOTHING RETURNING`` so duplicate slug is detected atomically."""
    from backend.routers.tenant_projects import _INSERT_PROJECT_SQL
    upper = _INSERT_PROJECT_SQL.upper()
    assert "INSERT INTO PROJECTS" in upper
    assert "ON CONFLICT (TENANT_ID, PRODUCT_LINE, SLUG)" in upper
    assert "DO NOTHING" in upper
    assert "RETURNING" in upper


def test_fetch_tenant_sql_is_read_only():
    from backend.routers.tenant_projects import _FETCH_TENANT_SQL
    upper = _FETCH_TENANT_SQL.upper()
    for verb in ("UPDATE ", "INSERT ", "DELETE ", "DROP ", "ALTER "):
        assert verb not in upper, f"_FETCH_TENANT_SQL contains {verb!r}"
    assert "SELECT" in upper


def test_fetch_parent_project_sql_projects_tenant_id():
    """Cross-tenant guard depends on knowing the parent's tenant_id."""
    from backend.routers.tenant_projects import _FETCH_PARENT_PROJECT_SQL
    upper = _FETCH_PARENT_PROJECT_SQL.upper()
    assert "SELECT" in upper
    assert "TENANT_ID" in upper
    assert "FROM PROJECTS" in upper


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (d) Router wiring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_router_exposes_post_endpoint():
    from backend.routers.tenant_projects import router
    paths = {(tuple(sorted(r.methods)), r.path) for r in router.routes}
    assert (("POST",), "/tenants/{tenant_id}/projects") in paths


def test_handler_depends_on_current_user():
    from backend.routers import tenant_projects
    from backend import auth
    fn = tenant_projects.create_project
    sig = inspect.signature(fn)
    deps = []
    for _name, p in sig.parameters.items():
        target = getattr(p.default, "dependency", None)
        if target is not None:
            deps.append(target)
    assert auth.current_user in deps, (
        f"create_project must depend on auth.current_user; "
        f"deps were {deps!r}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (e) Main app full-prefix mount
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_main_app_mounts_create_project_endpoint():
    """End-to-end: backend.main exposes the endpoint at
    ``/api/v1/tenants/{tenant_id}/projects`` so a deployment that
    forgets to ``include_router`` the new module fails this test."""
    os.environ.setdefault("OMNISIGHT_AUTH_MODE", "open")
    from backend.main import app
    paths = {
        (tuple(sorted(r.methods or [])), r.path)
        for r in app.routes if hasattr(r, "path")
    }
    assert (("POST",), "/api/v1/tenants/{tenant_id}/projects") in paths


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
        # Audit rows referencing any project of this tenant.
        await conn.execute(
            "DELETE FROM audit_log WHERE entity_kind = 'project' "
            "AND entity_id IN (SELECT id FROM projects WHERE tenant_id = $1)",
            tid,
        )
        await conn.execute("DELETE FROM projects WHERE tenant_id = $1", tid)
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)


@_requires_pg
async def test_post_project_201_happy_path(client, pg_test_pool):
    tid = "t-y4-proj-happy"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded",
                "name": "ISP Tuning",
                "slug": "isp-tuning",
            },
        )
        assert res.status_code == 201, res.text
        body = res.json()
        # Response shape — every documented field present.
        for key in (
            "project_id", "tenant_id", "product_line", "name", "slug",
            "parent_id", "plan_override", "disk_budget_bytes",
            "llm_budget_tokens", "created_by", "created_at", "archived_at",
        ):
            assert key in body, f"missing response key: {key}"
        assert body["project_id"].startswith("p-")
        assert body["tenant_id"] == tid
        assert body["product_line"] == "embedded"
        assert body["name"] == "ISP Tuning"
        assert body["slug"] == "isp-tuning"
        assert body["parent_id"] is None
        assert body["plan_override"] is None
        assert body["disk_budget_bytes"] is None
        assert body["llm_budget_tokens"] is None
        assert body["archived_at"] is None
        # Persisted row matches the response.
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT tenant_id, product_line, name, slug "
                "FROM projects WHERE id = $1",
                body["project_id"],
            )
        assert row is not None
        assert row["tenant_id"] == tid
        assert row["product_line"] == "embedded"
        assert row["slug"] == "isp-tuning"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_project_with_optional_fields(client, pg_test_pool):
    tid = "t-y4-proj-opt"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "web",
                "name": "Marketing Site",
                "slug": "site",
                "plan_override": "pro",
                "disk_budget_bytes": 1024 * 1024 * 1024,
            },
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["plan_override"] == "pro"
        assert body["disk_budget_bytes"] == 1024 * 1024 * 1024
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_project_slug_dup_409_within_same_product_line(
    client, pg_test_pool,
):
    tid = "t-y4-proj-dup"
    try:
        await _seed_tenant(pg_test_pool, tid)
        first = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded", "name": "A", "slug": "isp",
            },
        )
        assert first.status_code == 201, first.text
        # Same product_line + slug → 409.
        dup = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded", "name": "B", "slug": "isp",
            },
        )
        assert dup.status_code == 409, dup.text
        body = dup.json()
        assert body["existing_project_id"] == first.json()["project_id"]
        assert body["slug"] == "isp"
        assert body["product_line"] == "embedded"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_project_same_slug_different_product_line_allowed(
    client, pg_test_pool,
):
    tid = "t-y4-proj-pl"
    try:
        await _seed_tenant(pg_test_pool, tid)
        a = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded", "name": "A", "slug": "shared",
            },
        )
        assert a.status_code == 201, a.text
        # Different product_line + same slug → permitted.
        b = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "web", "name": "B", "slug": "shared",
            },
        )
        assert b.status_code == 201, b.text
        assert a.json()["project_id"] != b.json()["project_id"]
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_project_unknown_tenant_returns_404(client):
    res = await client.post(
        "/api/v1/tenants/t-y4-does-not-exist/projects",
        json={
            "product_line": "embedded", "name": "P", "slug": "p",
        },
    )
    assert res.status_code == 404, res.text


@_requires_pg
async def test_post_project_malformed_tenant_id_returns_422(client):
    res = await client.post(
        "/api/v1/tenants/T-Bad-Id/projects",
        json={
            "product_line": "embedded", "name": "P", "slug": "p",
        },
    )
    assert res.status_code == 422, res.text


@_requires_pg
async def test_post_project_unknown_product_line_returns_422(client, pg_test_pool):
    tid = "t-y4-proj-bad-pl"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "iot", "name": "P", "slug": "p",
            },
        )
        assert res.status_code == 422, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_project_parent_not_found_returns_422(client, pg_test_pool):
    tid = "t-y4-proj-pn"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded", "name": "P", "slug": "p",
                "parent_id": "p-deadbeef00000000",
            },
        )
        assert res.status_code == 422, res.text
        assert "does not exist" in res.json()["detail"]
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_project_parent_cross_tenant_returns_422(client, pg_test_pool):
    """A parent project in tenant A cannot be linked from a child in
    tenant B — the handler must 422 even though both tenants exist
    and the caller has admin on B (super-admin via open mode)."""
    t_a = "t-y4-proj-cta"
    t_b = "t-y4-proj-ctb"
    try:
        await _seed_tenant(pg_test_pool, t_a)
        await _seed_tenant(pg_test_pool, t_b)
        # Create a parent in tenant A.
        res_a = await client.post(
            f"/api/v1/tenants/{t_a}/projects",
            json={
                "product_line": "embedded", "name": "Parent", "slug": "p",
            },
        )
        assert res_a.status_code == 201, res_a.text
        parent_id = res_a.json()["project_id"]

        # Try to attach a child in tenant B to the parent in A.
        res_b = await client.post(
            f"/api/v1/tenants/{t_b}/projects",
            json={
                "product_line": "embedded", "name": "Child", "slug": "c",
                "parent_id": parent_id,
            },
        )
        assert res_b.status_code == 422, res_b.text
        body = res_b.json()
        assert body["parent_tenant_id"] == t_a
        assert body["tenant_id"] == t_b
    finally:
        await _purge_tenant(pg_test_pool, t_a)
        await _purge_tenant(pg_test_pool, t_b)


@_requires_pg
async def test_post_project_parent_same_tenant_creates_subtree(
    client, pg_test_pool,
):
    tid = "t-y4-proj-sub"
    try:
        await _seed_tenant(pg_test_pool, tid)
        parent = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded", "name": "Parent", "slug": "p",
            },
        )
        assert parent.status_code == 201, parent.text
        parent_id = parent.json()["project_id"]

        child = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded", "name": "Child", "slug": "c",
                "parent_id": parent_id,
            },
        )
        assert child.status_code == 201, child.text
        assert child.json()["parent_id"] == parent_id
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  RBAC: non-admin member gets 403
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_post_project_non_admin_member_gets_403(client, pg_test_pool):
    """A user whose membership.role is 'member' (or 'viewer') on the
    target tenant must NOT be permitted to create projects."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y4-rbac-member"
    uid = "u-y4rbacmemberxx"
    try:
        await _seed_tenant(pg_test_pool, tid)
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
            res = await client.post(
                f"/api/v1/tenants/{tid}/projects",
                json={
                    "product_line": "embedded", "name": "P", "slug": "p",
                },
            )
            assert res.status_code == 403, res.text
            assert "tenant admin" in res.json()["detail"]
        finally:
            app.dependency_overrides.pop(_au.current_user, None)

        # No project row was inserted.
        async with pg_test_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM projects WHERE tenant_id = $1", tid,
            )
        assert int(count) == 0
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM user_tenant_memberships WHERE user_id = $1", uid,
            )
            await conn.execute("DELETE FROM users WHERE id = $1", uid)
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_project_tenant_admin_member_allowed(client, pg_test_pool):
    """Membership role='admin' on the target tenant is permitted to
    create — even when the *account* role is only 'viewer'."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y4-rbac-admin"
    uid = "u-y4rbacadminxx"
    try:
        await _seed_tenant(pg_test_pool, tid)
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, email, name, role, password_hash, "
                "enabled, tenant_id) "
                "VALUES ($1, $2, $3, 'viewer', '', 1, $4) "
                "ON CONFLICT (id) DO NOTHING",
                uid, "tadmin@example.com", "T Admin", tid,
            )
            await conn.execute(
                "INSERT INTO user_tenant_memberships "
                "(user_id, tenant_id, role, status) "
                "VALUES ($1, $2, 'admin', 'active') "
                "ON CONFLICT (user_id, tenant_id) DO NOTHING",
                uid, tid,
            )

        tadmin = _au.User(
            id=uid, email="tadmin@example.com", name="T Admin",
            role="viewer", enabled=True, tenant_id=tid,
        )

        async def _fake_current_user():
            return tadmin

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.post(
                f"/api/v1/tenants/{tid}/projects",
                json={
                    "product_line": "embedded", "name": "P", "slug": "p",
                },
            )
            assert res.status_code == 201, res.text
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
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
async def test_post_project_audit_row_written(client, pg_test_pool):
    tid = "t-y4-proj-audit"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "software", "name": "Audited", "slug": "a",
            },
        )
        assert res.status_code == 201, res.text
        project_id = res.json()["project_id"]

        async with pg_test_pool.acquire() as conn:
            audit_row = await conn.fetchrow(
                "SELECT actor, action, entity_kind, entity_id, "
                "       after_json "
                "FROM audit_log "
                "WHERE action = 'tenant_project_created' "
                "  AND entity_id = $1 "
                "ORDER BY id DESC LIMIT 1",
                project_id,
            )
        assert audit_row is not None
        assert audit_row["entity_kind"] == "project"
        # No secret leaks in audit blob.
        blob = audit_row["after_json"] or ""
        assert "password_hash" not in blob
        assert "oidc_subject" not in blob
        assert "token_hash" not in blob
        # No 64-hex-char sha256-shaped substring (overlaps token_hash leak).
        assert not re.search(r"[0-9a-f]{64}", blob), blob
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
