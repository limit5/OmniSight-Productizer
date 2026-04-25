"""Y4 (#280) row 4 — drift guard for the project archive / restore /
GC surface.

Pure-unit + ASGI mount tests run without PG. Live-PG HTTP path tests
exercise end-to-end behaviour (archive happy path, restore happy
path, idempotent no-change branches, RBAC, audit emission, and the
``gc_archived_projects`` helper deleting time-aged rows + emitting
billing audit events) and skip when ``OMNI_TEST_PG_URL`` is unset.

Drift guard families:
  (a) Module-level constants — env var name, default retention,
      allowed-roles frozenset.
  (b) Retention-resolver helper — env var parsing + invalid-fallback.
  (c) SQL constants — PG ``$N`` placeholder, secret-leak guard, the
      precondition WHERE clauses (``archived_at IS NULL`` /
      ``archived_at IS NOT NULL``), and the GC enumeration shape.
  (d) Router endpoints mounted with ``auth.current_user`` dependency.
  (e) Main app full-prefix mount confirms both archive + restore
      paths under ``/api/v1``.
  (f) HTTP path: archive happy / archive idempotent / restore happy /
      restore idempotent / 404 unknown tenant / 404 unknown project /
      cross-tenant 404 / 422 malformed ids / RBAC 403.
  (g) GC helper: deletes only past-cutoff rows / emits billing audit
      row / leaves recent archives + live rows alone / restore after
      GC returns 404 / retention env override is honoured.
  (h) Self-fingerprint guard.
"""

from __future__ import annotations

import inspect
import os
import re
from datetime import datetime, timedelta
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


def test_project_gc_retention_env_var_name():
    """The env-var name is operator-facing — drift would silently
    revert prod to the 90-day default after an operator set the
    knob under the old name."""
    from backend.routers.tenant_projects import (
        _PROJECT_GC_RETENTION_DAYS_ENV,
    )
    assert _PROJECT_GC_RETENTION_DAYS_ENV == (
        "OMNISIGHT_PROJECT_GC_RETENTION_DAYS"
    )


def test_project_gc_retention_default_is_90_days():
    """The TODO row literal is explicit: 90 days."""
    from backend.routers.tenant_projects import (
        _PROJECT_GC_RETENTION_DAYS_DEFAULT,
    )
    assert _PROJECT_GC_RETENTION_DAYS_DEFAULT == 90


def test_archive_allowed_roles_is_admin_tier_only():
    from backend.routers.tenant_projects import (
        _PROJECT_ARCHIVE_ALLOWED_MEMBERSHIP_ROLES,
    )
    assert _PROJECT_ARCHIVE_ALLOWED_MEMBERSHIP_ROLES == frozenset(
        {"owner", "admin"}
    )
    assert "member" not in _PROJECT_ARCHIVE_ALLOWED_MEMBERSHIP_ROLES
    assert "viewer" not in _PROJECT_ARCHIVE_ALLOWED_MEMBERSHIP_ROLES


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (b) Retention-resolver helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_resolve_retention_returns_default_when_env_unset(monkeypatch):
    monkeypatch.delenv(
        "OMNISIGHT_PROJECT_GC_RETENTION_DAYS", raising=False,
    )
    from backend.routers.tenant_projects import (
        _resolve_archive_retention_days,
    )
    assert _resolve_archive_retention_days() == 90


def test_resolve_retention_honours_env_override(monkeypatch):
    monkeypatch.setenv(
        "OMNISIGHT_PROJECT_GC_RETENTION_DAYS", "7",
    )
    from backend.routers.tenant_projects import (
        _resolve_archive_retention_days,
    )
    assert _resolve_archive_retention_days() == 7


@pytest.mark.parametrize("bad", ["", "ninety", "0", "-1", "  "])
def test_resolve_retention_falls_back_on_invalid(monkeypatch, bad):
    monkeypatch.setenv(
        "OMNISIGHT_PROJECT_GC_RETENTION_DAYS", bad,
    )
    from backend.routers.tenant_projects import (
        _resolve_archive_retention_days,
    )
    assert _resolve_archive_retention_days() == 90


def test_resolve_retention_strips_whitespace(monkeypatch):
    monkeypatch.setenv(
        "OMNISIGHT_PROJECT_GC_RETENTION_DAYS", "  30 ",
    )
    from backend.routers.tenant_projects import (
        _resolve_archive_retention_days,
    )
    assert _resolve_archive_retention_days() == 30


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (c) SQL constants — shape sentinels
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_ARCHIVE_SQL_NAMES = (
    "_ARCHIVE_PROJECT_SQL",
    "_RESTORE_PROJECT_SQL",
    "_LIST_GC_ELIGIBLE_PROJECTS_SQL",
    "_DELETE_PROJECT_SQL",
)


@pytest.mark.parametrize("sql_name", _ARCHIVE_SQL_NAMES)
def test_archive_sql_uses_pg_placeholders_only(sql_name):
    from backend.routers import tenant_projects as m
    sql = getattr(m, sql_name)
    assert "?" not in sql, f"{sql_name} contains SQLite-style ?"


@pytest.mark.parametrize("sql_name", _ARCHIVE_SQL_NAMES)
def test_archive_sql_does_not_leak_secret_columns(sql_name):
    from backend.routers import tenant_projects as m
    sql = getattr(m, sql_name)
    assert "password_hash" not in sql, f"{sql_name} projects password_hash"
    assert "oidc_subject" not in sql, f"{sql_name} projects oidc_subject"
    assert "oidc_provider" not in sql, f"{sql_name} projects oidc_provider"
    assert "token_hash" not in sql, f"{sql_name} projects token_hash"


def test_archive_sql_precondition_blocks_double_archive():
    """``AND archived_at IS NULL`` in the WHERE means a second archive
    is detected via RETURNING None — the no_change branch.  Drift
    here would either re-stamp the archive timestamp every call (data
    rot) or fall through to the audit emit on a no-op."""
    from backend.routers.tenant_projects import _ARCHIVE_PROJECT_SQL
    assert "archived_at IS NULL" in _ARCHIVE_PROJECT_SQL
    assert "RETURNING" in _ARCHIVE_PROJECT_SQL.upper()


def test_archive_sql_scopes_by_tenant_id():
    from backend.routers.tenant_projects import _ARCHIVE_PROJECT_SQL
    assert "WHERE id = $1 AND tenant_id = $2" in _ARCHIVE_PROJECT_SQL


def test_restore_sql_precondition_blocks_double_restore():
    """``AND archived_at IS NOT NULL`` means restore on a live row is
    detected via RETURNING None and falls through to no_change."""
    from backend.routers.tenant_projects import _RESTORE_PROJECT_SQL
    assert "archived_at IS NOT NULL" in _RESTORE_PROJECT_SQL
    assert "SET archived_at = NULL" in _RESTORE_PROJECT_SQL


def test_restore_sql_scopes_by_tenant_id():
    from backend.routers.tenant_projects import _RESTORE_PROJECT_SQL
    assert "WHERE id = $1 AND tenant_id = $2" in _RESTORE_PROJECT_SQL


def test_list_gc_eligible_sql_filters_by_cutoff():
    """The GC enumeration must filter on ``archived_at < $1`` against
    the cutoff string — drift would either GC live rows (catastrophic
    data loss) or never fire (silent retention bypass)."""
    from backend.routers.tenant_projects import (
        _LIST_GC_ELIGIBLE_PROJECTS_SQL,
    )
    assert "archived_at IS NOT NULL" in _LIST_GC_ELIGIBLE_PROJECTS_SQL
    assert "archived_at < $1" in _LIST_GC_ELIGIBLE_PROJECTS_SQL


def test_delete_project_sql_scopes_by_tenant_id():
    """The hard-delete must always scope by tenant_id so a stolen
    project_id with wrong tenant is a no-op rather than cross-tenant
    deletion."""
    from backend.routers.tenant_projects import _DELETE_PROJECT_SQL
    assert _DELETE_PROJECT_SQL == (
        "DELETE FROM projects WHERE id = $1 AND tenant_id = $2"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (d) Router wiring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_router_exposes_archive_and_restore_endpoints():
    from backend.routers.tenant_projects import router
    paths = {(tuple(sorted(r.methods)), r.path) for r in router.routes}
    assert (
        ("POST",),
        "/tenants/{tenant_id}/projects/{project_id}/archive",
    ) in paths
    assert (
        ("POST",),
        "/tenants/{tenant_id}/projects/{project_id}/restore",
    ) in paths


@pytest.mark.parametrize("fn_name", ["archive_project", "restore_project"])
def test_archive_restore_handlers_depend_on_current_user(fn_name):
    from backend.routers import tenant_projects
    from backend import auth
    fn = getattr(tenant_projects, fn_name)
    sig = inspect.signature(fn)
    deps = []
    for _name, p in sig.parameters.items():
        target = getattr(p.default, "dependency", None)
        if target is not None:
            deps.append(target)
    assert auth.current_user in deps, (
        f"{fn_name} must depend on auth.current_user; deps were {deps!r}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (e) Main app full-prefix mount
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_main_app_mounts_archive_and_restore_endpoints():
    """``backend.main`` exposes both endpoints under ``/api/v1``."""
    os.environ.setdefault("OMNISIGHT_AUTH_MODE", "open")
    from backend.main import app
    paths = {
        (tuple(sorted(r.methods or [])), r.path)
        for r in app.routes if hasattr(r, "path")
    }
    assert (
        ("POST",),
        "/api/v1/tenants/{tenant_id}/projects/{project_id}/archive",
    ) in paths
    assert (
        ("POST",),
        "/api/v1/tenants/{tenant_id}/projects/{project_id}/restore",
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
        await conn.execute(
            "DELETE FROM audit_log WHERE entity_kind = 'project' "
            "AND tenant_id = $1",
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
) -> str:
    payload = {
        "product_line": product_line,
        "name": name or f"P-{slug}",
        "slug": slug,
    }
    res = await client.post(
        f"/api/v1/tenants/{tid}/projects", json=payload,
    )
    assert res.status_code == 201, res.text
    return res.json()["project_id"]


async def _force_archived_at(pool, project_id: str, when: str) -> None:
    """Test-only: backdate ``archived_at`` to a chosen UTC text stamp
    so the GC helper sees the row as past-cutoff without sleeping."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE projects SET archived_at = $1 WHERE id = $2",
            when, project_id,
        )


@_requires_pg
async def test_archive_project_happy_sets_archived_at(client, pg_test_pool):
    tid = "t-y4-arch-happy"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="x")
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/archive",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["project_id"] == pid
        assert body["tenant_id"] == tid
        assert body["archived_at"] is not None
        assert body["no_change"] is False
        # Archived_at format matches the rest of the table.
        assert re.match(
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", body["archived_at"],
        )
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_archive_project_idempotent_no_change(client, pg_test_pool):
    """A second archive on an already-archived project returns
    no_change=True, leaves archived_at intact, and emits no audit row."""
    tid = "t-y4-arch-idem"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="y")

        # First archive — happy.
        first = await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/archive",
        )
        assert first.status_code == 200
        first_stamp = first.json()["archived_at"]

        # Second archive — no_change.
        second = await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/archive",
        )
        assert second.status_code == 200
        body = second.json()
        assert body["no_change"] is True
        assert body["archived_at"] == first_stamp

        # Exactly one tenant_project_archived audit row.
        async with pg_test_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE action = 'tenant_project_archived' "
                "  AND entity_id = $1",
                pid,
            )
        assert int(count) == 1
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_restore_project_happy_clears_archived_at(
    client, pg_test_pool,
):
    tid = "t-y4-restore-happy"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="z")
        # Archive then restore.
        await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/archive",
        )
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/restore",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["archived_at"] is None
        assert body["no_change"] is False

        # And persisted.
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT archived_at FROM projects WHERE id = $1", pid,
            )
        assert row["archived_at"] is None
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_restore_project_idempotent_on_live_row(
    client, pg_test_pool,
):
    """Restore on a project that was never archived returns no_change
    and emits no audit row."""
    tid = "t-y4-restore-idem"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="z")

        res = await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/restore",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["no_change"] is True
        assert body["archived_at"] is None

        async with pg_test_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE action = 'tenant_project_restored' "
                "  AND entity_id = $1",
                pid,
            )
        assert int(count) == 0
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_archive_unknown_tenant_returns_404(client):
    res = await client.post(
        "/api/v1/tenants/t-y4-arch-missing/projects/"
        "p-deadbeefdeadbeef/archive",
    )
    assert res.status_code == 404, res.text


@_requires_pg
async def test_archive_unknown_project_returns_404(client, pg_test_pool):
    tid = "t-y4-arch-noproj"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects/p-deadbeefdeadbeef/archive",
        )
        assert res.status_code == 404, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_archive_cross_tenant_project_returns_404(
    client, pg_test_pool,
):
    """A project owned by tenant A is invisible from tenant B's
    namespace — archive on the wrong (tenant, project) pair returns
    404, not 403."""
    t_a = "t-y4-arch-iso-a"
    t_b = "t-y4-arch-iso-b"
    try:
        await _seed_tenant(pg_test_pool, t_a)
        await _seed_tenant(pg_test_pool, t_b)
        pid_a = await _create_project(client, t_a, slug="x")
        res = await client.post(
            f"/api/v1/tenants/{t_b}/projects/{pid_a}/archive",
        )
        assert res.status_code == 404, res.text
        # Tenant A's row untouched.
        async with pg_test_pool.acquire() as conn:
            archived_at = await conn.fetchval(
                "SELECT archived_at FROM projects WHERE id = $1", pid_a,
            )
        assert archived_at is None
    finally:
        await _purge_tenant(pg_test_pool, t_a)
        await _purge_tenant(pg_test_pool, t_b)


@_requires_pg
async def test_archive_malformed_tenant_id_returns_422(client):
    res = await client.post(
        "/api/v1/tenants/T-Bad/projects/p-aaaabbbbccccdddd/archive",
    )
    assert res.status_code == 422, res.text


@_requires_pg
async def test_archive_malformed_project_id_returns_422(
    client, pg_test_pool,
):
    tid = "t-y4-arch-badpid"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects/P-BAD/archive",
        )
        assert res.status_code == 422, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_restore_malformed_project_id_returns_422(
    client, pg_test_pool,
):
    tid = "t-y4-restore-badpid"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects/P-BAD/restore",
        )
        assert res.status_code == 422, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_archive_non_admin_member_gets_403(client, pg_test_pool):
    """A member-role user must not be permitted to archive projects."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y4-arch-rbac"
    uid = "u-y4archmemberx"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="x")

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
                f"/api/v1/tenants/{tid}/projects/{pid}/archive",
            )
            assert res.status_code == 403, res.text
            res2 = await client.post(
                f"/api/v1/tenants/{tid}/projects/{pid}/restore",
            )
            assert res2.status_code == 403, res2.text
        finally:
            app.dependency_overrides.pop(_au.current_user, None)

        # Project untouched.
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT archived_at FROM projects WHERE id = $1", pid,
            )
        assert row["archived_at"] is None
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
async def test_archive_audit_row_written(client, pg_test_pool):
    tid = "t-y4-arch-audit"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="aud")
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/archive",
        )
        assert res.status_code == 200, res.text

        async with pg_test_pool.acquire() as conn:
            audit_row = await conn.fetchrow(
                "SELECT action, entity_kind, entity_id, "
                "       before_json, after_json "
                "FROM audit_log "
                "WHERE action = 'tenant_project_archived' "
                "  AND entity_id = $1 "
                "ORDER BY id DESC LIMIT 1",
                pid,
            )
        assert audit_row is not None
        assert audit_row["entity_kind"] == "project"
        before_blob = audit_row["before_json"] or ""
        after_blob = audit_row["after_json"] or ""
        # No secret leaks.
        for blob in (before_blob, after_blob):
            assert "password_hash" not in blob
            assert "oidc_subject" not in blob
            assert "token_hash" not in blob
        # The retention_days knob shows up on the after blob — gives
        # accountants the contractual window at the time of archive.
        assert '"retention_days":' in after_blob, after_blob
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_restore_audit_row_written(client, pg_test_pool):
    tid = "t-y4-restore-audit"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="rea")
        await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/archive",
        )
        await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/restore",
        )

        async with pg_test_pool.acquire() as conn:
            audit_row = await conn.fetchrow(
                "SELECT action, entity_kind, entity_id "
                "FROM audit_log "
                "WHERE action = 'tenant_project_restored' "
                "  AND entity_id = $1 "
                "ORDER BY id DESC LIMIT 1",
                pid,
            )
        assert audit_row is not None
        assert audit_row["entity_kind"] == "project"
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (h) GC helper — past-cutoff deletion + billing audit emission
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_gc_helper_deletes_only_past_cutoff(client, pg_test_pool):
    """Two archived projects — one within the retention window, one
    past it. GC must delete only the past-cutoff one and leave the
    recent one intact."""
    from backend.routers.tenant_projects import gc_archived_projects

    tid = "t-y4-gc-cutoff"
    try:
        await _seed_tenant(pg_test_pool, tid)
        recent = await _create_project(client, tid, slug="recent")
        old = await _create_project(client, tid, slug="old")

        # Archive both.
        await client.post(
            f"/api/v1/tenants/{tid}/projects/{recent}/archive",
        )
        await client.post(
            f"/api/v1/tenants/{tid}/projects/{old}/archive",
        )

        # Backdate the "old" archive to 100 days ago — past the
        # 90-day retention default.
        long_ago = (
            datetime.utcnow() - timedelta(days=100)
        ).strftime("%Y-%m-%d %H:%M:%S")
        await _force_archived_at(pg_test_pool, old, long_ago)

        removed = await gc_archived_projects()
        removed_ids = {r["project_id"] for r in removed}
        assert old in removed_ids
        assert recent not in removed_ids

        async with pg_test_pool.acquire() as conn:
            still_there = await conn.fetch(
                "SELECT id FROM projects WHERE tenant_id = $1", tid,
            )
        ids = {r["id"] for r in still_there}
        assert recent in ids
        assert old not in ids
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_gc_helper_leaves_live_projects_alone(client, pg_test_pool):
    """A live (never-archived) project must never be deleted, even if
    the GC tick runs with a 0-second retention (shouldn't happen, but
    defence in depth)."""
    from backend.routers.tenant_projects import gc_archived_projects

    tid = "t-y4-gc-live"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="live")

        # Run with the smallest valid retention (1 day).
        removed = await gc_archived_projects(retention_days=1)
        ids = {r["project_id"] for r in removed}
        assert pid not in ids

        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM projects WHERE id = $1", pid,
            )
        assert row is not None
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_gc_helper_emits_billing_audit_event(client, pg_test_pool):
    """Each hard-deleted project must result in a single
    ``tenant_project_billing_gc`` audit row carrying tenant_id +
    project_id + retention_days for accounting."""
    from backend.routers.tenant_projects import gc_archived_projects

    tid = "t-y4-gc-bill"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="bill")
        await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/archive",
        )
        long_ago = (
            datetime.utcnow() - timedelta(days=100)
        ).strftime("%Y-%m-%d %H:%M:%S")
        await _force_archived_at(pg_test_pool, pid, long_ago)

        removed = await gc_archived_projects()
        assert any(r["project_id"] == pid for r in removed)

        async with pg_test_pool.acquire() as conn:
            audit_row = await conn.fetchrow(
                "SELECT action, entity_kind, entity_id, "
                "       before_json, after_json, tenant_id, actor "
                "FROM audit_log "
                "WHERE action = 'tenant_project_billing_gc' "
                "  AND entity_id = $1 "
                "ORDER BY id DESC LIMIT 1",
                pid,
            )
        assert audit_row is not None
        assert audit_row["entity_kind"] == "project"
        assert audit_row["actor"] == "system:gc"
        # Tenant id pinned to the deleted project's tenant — drift here
        # would route the billing event into ``t-default``'s chain.
        assert audit_row["tenant_id"] == tid
        after_blob = audit_row["after_json"] or ""
        assert tid in after_blob
        assert '"retention_days":' in after_blob
        assert '"gc_at":' in after_blob
    finally:
        # The GC has already deleted the project row + memberships
        # (CASCADE). Only the audit rows + tenant remain to clean.
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_restore_after_gc_returns_404(client, pg_test_pool):
    """Once the GC has fired, restoring the dead project_id returns
    404 — the row no longer exists."""
    from backend.routers.tenant_projects import gc_archived_projects

    tid = "t-y4-gc-restore-404"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="dead")
        await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/archive",
        )
        long_ago = (
            datetime.utcnow() - timedelta(days=200)
        ).strftime("%Y-%m-%d %H:%M:%S")
        await _force_archived_at(pg_test_pool, pid, long_ago)

        removed = await gc_archived_projects()
        assert any(r["project_id"] == pid for r in removed)

        # And restore is now a 404.
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/restore",
        )
        assert res.status_code == 404, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_gc_helper_honours_retention_days_override(
    client, pg_test_pool,
):
    """When called with ``retention_days=3``, a project archived 5
    days ago should be removed even though the default 90-day knob
    would not yet fire."""
    from backend.routers.tenant_projects import gc_archived_projects

    tid = "t-y4-gc-override"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="five")
        await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/archive",
        )
        five_days_ago = (
            datetime.utcnow() - timedelta(days=5)
        ).strftime("%Y-%m-%d %H:%M:%S")
        await _force_archived_at(pg_test_pool, pid, five_days_ago)

        # Default 90-day window — does NOT fire.
        baseline = await gc_archived_projects()
        assert all(r["project_id"] != pid for r in baseline)
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM projects WHERE id = $1", pid,
            )
        assert row is not None

        # 3-day override — DOES fire.
        removed = await gc_archived_projects(retention_days=3)
        assert any(r["project_id"] == pid for r in removed)
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM projects WHERE id = $1", pid,
            )
        assert row is None
    finally:
        await _purge_tenant(pg_test_pool, tid)


@pytest.mark.parametrize("bad", [0, -1, -90])
async def test_gc_helper_rejects_zero_or_negative_retention(bad):
    """``retention_days`` ≤ 0 would trip a ``cutoff = now - 0d`` and
    GC every archived project on the spot — guard with ValueError so
    a misconfigured cron tick fails loudly instead of nuking data."""
    from backend.routers.tenant_projects import gc_archived_projects
    with pytest.raises(ValueError):
        await gc_archived_projects(retention_days=bad)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (i) Self-fingerprint guard — SOP Step 3 pre-commit pattern
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_self_fingerprint_clean():
    """The router source must not contain compat-era SQLite fingerprints.
    asyncpg pool conns don't have ``.commit()`` and PG uses ``$1, $2``."""
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
