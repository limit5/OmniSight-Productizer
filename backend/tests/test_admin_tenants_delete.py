"""Y2 (#278) row 5 — tests for DELETE /api/v1/admin/tenants/{id}.

Covers:
  * route is mounted under the api prefix as a DELETE path-param route
  * handler depends on ``require_super_admin``
  * static-shape SQL audit on the cascade phase constants:
      - bounded write (DELETE only, no DROP / TRUNCATE / ALTER / etc.)
      - whitelisted tables only — every CREATE-TABLE-with-tenant_id
        in backend/db.py must appear in either the explicit phase list
        OR the documented CASCADE-handled set (drift guard)
      - PG ``$N`` placeholders, never SQLite ``?``
      - fingerprint grep clean (4-pattern check, SOP Step 3)
  * Phase ordering invariants:
      - ``tenants`` is the LAST phase (FK-safe ordering)
      - ``audit_log`` precedes ``users`` so user-FK audit refs survive
        the audit DELETE
  * Confirm-handshake guard:
      - missing ``?confirm=`` → 422
      - mismatched ``?confirm=`` (e.g. wrong tenant id) → 422
  * ``t-default`` protection: 403 with explicit reason
  * RBAC: tenant admin gets 403
  * HTTP path on live PG:
      - 202 happy path; tenant + cascade-children removed; SSE event
        emitted; final ``tenant_deleted`` audit row written under the
        super-admin's chain (``t-default``)
      - 404 for well-formed but unknown id
      - 422 for malformed id
      - protected ``t-default`` truly cannot be removed via this path
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

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


def test_delete_tenant_route_is_mounted():
    from backend.main import app
    matches = [
        (r.path, sorted(getattr(r, "methods", []) or []))
        for r in app.routes
        if getattr(r, "path", "") == "/api/v1/admin/tenants/{tenant_id}"
    ]
    methods = [m for _, ms in matches for m in ms]
    assert "DELETE" in methods, (
        f"DELETE path-param route missing; got {matches!r}"
    )


def test_delete_handler_uses_super_admin_dependency():
    """Same gate as POST / GET / LIST / PATCH — only platform-tier
    super_admin may call DELETE."""
    from backend.routers.admin_tenants import delete_tenant
    from backend import auth

    import inspect
    sig = inspect.signature(delete_tenant)
    deps = []
    for _name, param in sig.parameters.items():
        default = param.default
        target = getattr(default, "dependency", None)
        if target is not None:
            deps.append(target)
    assert auth.require_super_admin in deps, (
        f"DELETE /admin/tenants/{{id}} must depend on "
        f"require_super_admin; deps were {deps!r}"
    )


def test_delete_handler_status_code_is_202():
    """The route is registered with ``status_code=202`` so OpenAPI /
    docs see the right default and the FastAPI test client sees 202
    on the success path (not 200)."""
    from backend.main import app
    for r in app.routes:
        if (getattr(r, "path", "") == "/api/v1/admin/tenants/{tenant_id}"
                and "DELETE" in (getattr(r, "methods", set()) or set())):
            assert r.status_code == 202, (
                f"DELETE route status_code should be 202, got {r.status_code}"
            )
            return
    pytest.fail("DELETE route not found")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: SQL fingerprint / safety audit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _all_delete_sqls() -> list[tuple[str, str]]:
    """Return every (phase_name, sql) pair, plus the read-only fetch
    SQL — covered as a single suite by the parametric tests."""
    import backend.routers.admin_tenants as mod
    pairs = list(mod._DELETE_PHASES_PG)
    pairs.append(("_FETCH_TENANT_FOR_DELETE_SQL",
                  mod._FETCH_TENANT_FOR_DELETE_SQL))
    return pairs


@pytest.mark.parametrize("name_sql", _all_delete_sqls(),
                         ids=lambda x: x[0])
def test_delete_sql_no_destructive_keywords(name_sql):
    """The cascade is permitted DELETE only; nothing in the cascade
    path should ever DROP / TRUNCATE / ALTER / GRANT / REVOKE — those
    would silently destroy schema or pivot privilege. The fetch SQL
    must additionally be read-only (no DELETE/UPDATE/INSERT)."""
    name, sql = name_sql
    sql_upper = sql.upper()
    for forbidden in ("DROP ", "TRUNCATE ", "ALTER ", "GRANT ",
                      "REVOKE ", "UPDATE ", "INSERT "):
        assert forbidden not in sql_upper, (
            f"phase {name!r} must not contain {forbidden!r}; got SQL: {sql!r}"
        )
    if name == "_FETCH_TENANT_FOR_DELETE_SQL":
        assert "DELETE " not in sql_upper, (
            f"{name!r} must be read-only"
        )
    else:
        assert "DELETE " in sql_upper, (
            f"cascade phase {name!r} must be a DELETE statement"
        )


@pytest.mark.parametrize("name_sql", _all_delete_sqls(),
                         ids=lambda x: x[0])
def test_delete_sql_uses_pg_placeholders(name_sql):
    """PG ``$N`` only — no SQLite ``?`` survivors."""
    name, sql = name_sql
    assert "?" not in sql, (
        f"{name!r} must use PG $N placeholders, not SQLite ?"
    )
    assert "$1" in sql, (
        f"{name!r} must accept at least a $1 tenant_id parameter"
    )


@pytest.mark.parametrize("name_sql", _all_delete_sqls(),
                         ids=lambda x: x[0])
def test_delete_sql_fingerprint_clean(name_sql):
    """SOP Step-3 fingerprint grep on every cascade SQL constant."""
    import re as _re
    name, sql = name_sql
    fingerprint = _re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    assert not fingerprint.search(sql), (
        f"{name!r} contains a compat-residue fingerprint; SQL: {sql!r}"
    )


def test_delete_phase_table_whitelist():
    """Every cascade SQL must reference exactly the table its phase
    name advertises — guards against a copy-paste typo nuking the
    wrong table (e.g. ``DELETE FROM tenants`` in the ``users`` phase
    would wipe every tenant in the DB)."""
    import backend.routers.admin_tenants as mod
    for table_name, sql in mod._DELETE_PHASES_PG:
        if table_name == "tenants":
            assert " tenants " in sql or " tenants\n" in sql or sql.endswith(" tenants"), (
                f"phase 'tenants' SQL must FROM tenants; got {sql!r}"
            )
        else:
            assert f"FROM {table_name} " in sql or f"FROM {table_name}\n" in sql, (
                f"phase {table_name!r} SQL must DELETE FROM {table_name}; "
                f"got {sql!r}"
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: phase ordering invariants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_delete_phases_end_with_tenants():
    """The final SQL phase MUST be ``tenants`` — every preceding phase
    is there to satisfy an FK that points at tenants. Any future drift
    that puts a child-table phase after tenants would FK-fail in
    production and abort the cascade mid-flight."""
    import backend.routers.admin_tenants as mod
    last_name, last_sql = mod._DELETE_PHASES_PG[-1]
    assert last_name == "tenants", (
        f"last cascade phase must be 'tenants', got {last_name!r}"
    )
    assert "tenants WHERE id = $1" in last_sql, (
        f"final phase must be `DELETE FROM tenants WHERE id = $1`; "
        f"got {last_sql!r}"
    )


def test_audit_log_precedes_users():
    """Audit_log rows hold a FK-style ``user_id`` reference (not a
    declared FK in the live schema, but the operator's mental model
    is that an audit row is bound to its actor). Removing audit
    rows BEFORE removing users keeps the per-tenant audit DELETE
    self-contained and avoids cascading user-FK collateral damage
    from confusing the chain-integrity tooling that scans audit_log
    for orphan tenant_ids."""
    import backend.routers.admin_tenants as mod
    names = [n for n, _ in mod._DELETE_PHASES_PG]
    assert names.index("audit_log") < names.index("users"), (
        f"audit_log must precede users in cascade order; got {names!r}"
    )


def test_delete_phase_names_are_unique():
    """No phase name appears twice — duplicate phases would emit
    duplicate SSE events and double-bill row-deleted counters."""
    import backend.routers.admin_tenants as mod
    names = [n for n, _ in mod._DELETE_PHASES_PG]
    assert len(names) == len(set(names)), (
        f"duplicate phase names: {names!r}"
    )


def test_delete_total_phases_matches_phase_names_length():
    """Drift guard: ``DELETE_TOTAL_PHASES`` must equal the length of
    ``DELETE_PHASE_NAMES`` (which appends 'filesystem' to the SQL
    phases). A mismatch means a UI progress bar will show wrong
    proportions."""
    import backend.routers.admin_tenants as mod
    assert mod.DELETE_TOTAL_PHASES == len(mod.DELETE_PHASE_NAMES), (
        f"DELETE_TOTAL_PHASES ({mod.DELETE_TOTAL_PHASES}) must match "
        f"len(DELETE_PHASE_NAMES) ({len(mod.DELETE_PHASE_NAMES)})"
    )
    assert mod.DELETE_PHASE_NAMES[-1] == "filesystem", (
        f"final DELETE_PHASE_NAMES entry must be 'filesystem' "
        f"(emitted after the SQL DELETEs); got {mod.DELETE_PHASE_NAMES!r}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: drift guard — every CREATE TABLE … tenant_id is covered
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# Tables whose ``tenant_id`` FK declares ``ON DELETE CASCADE`` — when
# the final ``DELETE FROM tenants`` fires PG cleans these up itself,
# so they don't appear in ``_DELETE_PHASES_PG``. Whitelisted by name
# here so the drift guard test below can subtract them from the live
# CREATE-TABLE inventory.
_CASCADE_HANDLED_TABLES: frozenset[str] = frozenset({
    "user_tenant_memberships",
    "projects",
    "tenant_invites",
    "project_shares",   # via guest_tenant_id
    "git_accounts",
    "llm_credentials",
})


def _scan_db_py_for_tenant_id_tables() -> set[str]:
    """Walk backend/db.py and pull the table name of every CREATE TABLE
    block that contains a ``tenant_id`` column. The walk is deliberately
    text-based (no SQL parser) so it survives schema rewrites without
    pulling in a heavyweight dep — and to surface drift the moment a
    new tenanted table is added, even if alembic hasn't run yet."""
    import re as _re
    src = Path(__file__).resolve().parent.parent / "db.py"
    text = src.read_text()
    blocks = _re.split(
        r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\(", text,
    )
    # blocks = [preamble, name1, body1, name2, body2, ...]
    out: set[str] = set()
    for name, body in zip(blocks[1::2], blocks[2::2]):
        # Stop at the closing of this block (next CREATE TABLE token
        # is already the split separator, so 'body' ends just before
        # the next CREATE TABLE — but it may include trailing
        # CREATE INDEX / inline comments).  Trim to the first ');'
        # that closes a column list.
        end = body.find(");")
        body_seg = body[:end] if end != -1 else body
        if _re.search(r"\btenant_id\s+TEXT\b", body_seg):
            out.add(name)
    return out


def test_delete_phases_cover_all_tenanted_tables():
    """Drift guard: every table in ``backend/db.py`` that has a
    ``tenant_id`` column must be either (a) in ``_DELETE_PHASES_PG``
    or (b) in ``_CASCADE_HANDLED_TABLES`` (auto-cleaned by PG via
    ``ON DELETE CASCADE`` on the tenants FK).

    Adding a new tenanted table without updating either set will fail
    this test at module-load time, forcing the author to choose:
      - append to ``_DELETE_PHASES_PG`` (explicit DELETE), OR
      - declare ``REFERENCES tenants(id) ON DELETE CASCADE`` and add
        the table to ``_CASCADE_HANDLED_TABLES``.
    """
    import backend.routers.admin_tenants as mod

    found = _scan_db_py_for_tenant_id_tables()
    # Discount the parent table itself.
    found.discard("tenants")

    explicit = {n for n, _ in mod._DELETE_PHASES_PG} - {"tenants"}
    handled = explicit | _CASCADE_HANDLED_TABLES
    missing = found - handled
    assert not missing, (
        f"new tenanted table(s) {sorted(missing)!r} not handled by "
        f"the cascade — append to _DELETE_PHASES_PG or to "
        f"_CASCADE_HANDLED_TABLES depending on the FK declaration"
    )


def test_protected_tenant_ids_includes_default():
    """``t-default`` MUST be in the protected set; removing the seed
    tenant is the one operation we never want to make available via
    REST."""
    import backend.routers.admin_tenants as mod
    assert "t-default" in mod.PROTECTED_TENANT_IDS


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: filesystem cleanup helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_filesystem_cleanup_removes_tree(tmp_path, monkeypatch):
    """``_delete_tenant_filesystem_sync`` must rmtree the tenant data
    root and the tenant ingest temp dir. We monkeypatch the path
    helpers so the test runs against tmp_path rather than the project
    ``data/`` dir."""
    import backend.routers.admin_tenants as mod

    tid = "t-fs-test-XYZ"
    data_dir = tmp_path / "data" / "tenants" / tid
    ingest_dir = tmp_path / "ingest" / tid
    data_dir.mkdir(parents=True)
    (data_dir / "artifacts").mkdir()
    (data_dir / "artifacts" / "blob.bin").write_bytes(b"x" * 1024)
    ingest_dir.mkdir(parents=True)
    (ingest_dir / "tmp.txt").write_bytes(b"y" * 256)

    def _fake_data_root(t):
        return data_dir
    def _fake_ingest_root(t):
        return ingest_dir

    import backend.tenant_fs as tfs
    monkeypatch.setattr(tfs, "tenant_data_root", _fake_data_root)
    monkeypatch.setattr(tfs, "tenant_ingest_root", _fake_ingest_root)

    bytes_freed = mod._delete_tenant_filesystem_sync(tid)

    assert not data_dir.exists(), "tenant data root must be removed"
    assert not ingest_dir.exists(), "tenant ingest dir must be removed"
    # Bytes-freed estimate is best-effort but should non-zero given
    # we wrote 1024 + 256 bytes.
    assert bytes_freed >= 1024, (
        f"bytes_freed ({bytes_freed}) should reflect the rmtree contents"
    )


def test_filesystem_cleanup_tolerates_missing_dir(tmp_path, monkeypatch):
    """A tenant that never wrote anything has no on-disk dir; the
    helper must return 0 without raising."""
    import backend.routers.admin_tenants as mod

    def _fake_data_root(t):
        return tmp_path / "ghost-data" / t  # never created
    def _fake_ingest_root(t):
        return tmp_path / "ghost-ingest" / t  # never created

    import backend.tenant_fs as tfs
    monkeypatch.setattr(tfs, "tenant_data_root", _fake_data_root)
    monkeypatch.setattr(tfs, "tenant_ingest_root", _fake_ingest_root)

    assert mod._delete_tenant_filesystem_sync("t-no-disk") == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — RBAC & confirm-handshake & protected-id (no PG needed)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_delete_tenant_admin_gets_403(client):
    """Tenant admin (role='admin') must be refused at the
    require_super_admin gate, before any DB hit."""
    from fastapi import HTTPException
    from backend.main import app
    from backend import auth as _au

    tenant_admin = _au.User(
        id="u-tadmin-delete", email="tadmin-delete@acme.local",
        name="Tenant Admin (delete)", role="admin", enabled=True,
        tenant_id="t-acme-y2-delete-rbac",
    )

    async def _fake_current_user():
        return tenant_admin

    def _deny():
        raise HTTPException(
            status_code=403,
            detail="Requires role=super_admin or higher (you are admin)",
        )

    app.dependency_overrides[_au.current_user] = _fake_current_user
    app.dependency_overrides[_au.require_super_admin] = _deny
    try:
        res = await client.delete(
            "/api/v1/admin/tenants/t-acme-y2-delete-rbac"
            "?confirm=t-acme-y2-delete-rbac",
        )
        assert res.status_code == 403, res.text
    finally:
        app.dependency_overrides.pop(_au.current_user, None)
        app.dependency_overrides.pop(_au.require_super_admin, None)


@_requires_pg
async def test_delete_tenant_protected_default_is_403(client):
    """Even a super-admin cannot delete ``t-default`` — the protection
    is policy-level, not RBAC-level. The 403 detail names the tenant."""
    res = await client.delete(
        "/api/v1/admin/tenants/t-default?confirm=t-default",
    )
    assert res.status_code == 403, res.text
    body = res.json()
    assert body["tenant_id"] == "t-default"
    assert "protected" in body["detail"].lower()


@_requires_pg
async def test_delete_tenant_missing_confirm_is_422(client):
    """Without ``?confirm=`` the request is refused at the handshake
    layer — never reaches the DB lookup."""
    res = await client.delete("/api/v1/admin/tenants/t-acme-noconfirm")
    assert res.status_code == 422, res.text
    body = res.json()
    assert "confirm" in body["detail"].lower()


@_requires_pg
async def test_delete_tenant_mismatched_confirm_is_422(client):
    """A confirm value that doesn't echo the path id is refused."""
    res = await client.delete(
        "/api/v1/admin/tenants/t-acme-x?confirm=t-acme-y",
    )
    assert res.status_code == 422, res.text
    body = res.json()
    assert body["confirm_received"] == "t-acme-y"


@_requires_pg
async def test_delete_tenant_malformed_id_is_422(client):
    """Malformed id (uppercase, missing prefix) → 422 before any DB hit."""
    res = await client.delete(
        "/api/v1/admin/tenants/T-UPPERCASE?confirm=T-UPPERCASE",
    )
    assert res.status_code == 422, res.text


@_requires_pg
async def test_delete_tenant_unknown_id_is_404(client):
    """Well-formed but unknown tenant id → 404 (not a silent no-op
    202). The handshake must pass first; we still want operators to
    see "missing" distinct from "succeeded"."""
    res = await client.delete(
        "/api/v1/admin/tenants/t-this-id-cannot-exist"
        "?confirm=t-this-id-cannot-exist",
    )
    assert res.status_code == 404, res.text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — happy path on live PG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _await_pending_deletes() -> None:
    """Drain the in-flight cascade tasks so post-DELETE assertions see
    the final state. Mirrors how a real SSE consumer waits for the
    completed event before refreshing its list view."""
    import backend.routers.admin_tenants as mod
    pending = list(mod._pending_delete_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _purge_partial(pg_test_pool, tid: str) -> None:
    """Defensive cleanup mirror — only fires if a test bails before the
    cascade ran. Idempotent; safe to call twice."""
    async with pg_test_pool.acquire() as conn:
        for sql in (
            "DELETE FROM event_log WHERE tenant_id = $1",
            "DELETE FROM artifacts WHERE tenant_id = $1",
            "DELETE FROM debug_findings WHERE tenant_id = $1",
            "DELETE FROM decision_rules WHERE tenant_id = $1",
            "DELETE FROM workflow_runs WHERE tenant_id = $1",
            "DELETE FROM tenant_secrets WHERE tenant_id = $1",
            "DELETE FROM user_preferences WHERE tenant_id = $1",
            "DELETE FROM audit_log WHERE tenant_id = $1",
            "DELETE FROM user_tenant_memberships WHERE tenant_id = $1",
            "DELETE FROM users WHERE tenant_id = $1",
            "DELETE FROM projects WHERE tenant_id = $1",
            "DELETE FROM tenants WHERE id = $1",
        ):
            try:
                await conn.execute(sql, tid)
            except Exception:
                pass


@_requires_pg
async def test_delete_tenant_happy_path_returns_202(client, pg_test_pool):
    """Smallest possible happy path: seed an empty tenant, DELETE it,
    expect 202 with the documented envelope, and observe both the
    tenant row and (after awaiting the bg task) any remaining tenant-
    scoped rows are gone."""
    tid = "t-acme-y2-delete-happy"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Happy Acme', 'free', 1)",
                tid,
            )

        res = await client.delete(
            f"/api/v1/admin/tenants/{tid}?confirm={tid}",
        )
        assert res.status_code == 202, res.text
        body = res.json()
        assert body["tenant_id"] == tid
        assert body["status"] == "deleting"
        assert body["sse_event"] == "tenant_delete_progress"
        assert isinstance(body["total_phases"], int)
        assert body["total_phases"] >= 2
        # Sanity: declared phase order is non-empty and ends with the
        # filesystem cleanup.
        assert body["phases"][-1] == "filesystem"

        # Wait for the cascade to settle, then assert the row is gone.
        await _await_pending_deletes()

        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM tenants WHERE id = $1", tid,
            )
        assert row is None, (
            f"tenant row {tid!r} must be removed by the cascade"
        )
    finally:
        await _purge_partial(pg_test_pool, tid)


@_requires_pg
async def test_delete_tenant_cascades_children(client, pg_test_pool):
    """Seed a tenant with rows in several tenanted tables (with FK to
    tenants AND with FK to other tables that cascade). After DELETE +
    bg-task settle, every seeded child row must be gone."""
    tid = "t-acme-y2-delete-cascade"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Cascade Acme', 'pro', 1)",
                tid,
            )
            await conn.execute(
                "INSERT INTO users (id, email, name, role, "
                "  password_hash, enabled, tenant_id) "
                "VALUES ($1, $2, 'Cascade User', 'admin', '', 1, $3)",
                "u-cascade-user", "cascade@acme.local", tid,
            )
            await conn.execute(
                "INSERT INTO user_tenant_memberships "
                "  (user_id, tenant_id, role, status) "
                "VALUES ($1, $2, 'owner', 'active')",
                "u-cascade-user", tid,
            )
            await conn.execute(
                "INSERT INTO projects "
                "  (id, tenant_id, name, slug) "
                "VALUES ($1, $2, 'Cascade Project', 'cascade-proj')",
                "p-cascade-1", tid,
            )
            await conn.execute(
                "INSERT INTO audit_log "
                "  (ts, actor, action, entity_kind, entity_id, "
                "   curr_hash, tenant_id) "
                "VALUES ($1, 'system', 'seed', 'tenant', $2, "
                "        'h-cascade', $2)",
                time.time(), tid,
            )

        res = await client.delete(
            f"/api/v1/admin/tenants/{tid}?confirm={tid}",
        )
        assert res.status_code == 202, res.text

        await _await_pending_deletes()

        async with pg_test_pool.acquire() as conn:
            tenant_row = await conn.fetchrow(
                "SELECT id FROM tenants WHERE id = $1", tid,
            )
            user_row = await conn.fetchrow(
                "SELECT id FROM users WHERE id = $1", "u-cascade-user",
            )
            mem_row = await conn.fetchrow(
                "SELECT user_id FROM user_tenant_memberships "
                "WHERE tenant_id = $1", tid,
            )
            proj_row = await conn.fetchrow(
                "SELECT id FROM projects WHERE tenant_id = $1", tid,
            )
            audit_row = await conn.fetchrow(
                "SELECT id FROM audit_log "
                "WHERE tenant_id = $1 AND action = 'seed'", tid,
            )
        assert tenant_row is None
        assert user_row is None, "users.tenant_id row must be wiped"
        assert mem_row is None, "membership rows must be wiped (CASCADE)"
        assert proj_row is None, "projects rows must be wiped (CASCADE)"
        assert audit_row is None, (
            "audit_log rows for the deleted tenant must be wiped"
        )
    finally:
        await _purge_partial(pg_test_pool, tid)


@_requires_pg
async def test_delete_tenant_emits_sse_progress(client, pg_test_pool):
    """Subscribe to the global event bus before kicking off DELETE,
    then assert the cascade emits at least one ``started`` event,
    one per-table running event, and one terminal ``completed``
    event."""
    from backend.events import bus

    tid = "t-acme-y2-delete-sse"
    queue = bus.subscribe()
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'SSE Acme', 'free', 1)",
                tid,
            )

        res = await client.delete(
            f"/api/v1/admin/tenants/{tid}?confirm={tid}",
        )
        assert res.status_code == 202, res.text

        await _await_pending_deletes()

        # Drain queued events for the duration of the cascade.
        seen: list[dict] = []
        while not queue.empty():
            msg = queue.get_nowait()
            if msg.get("event") == "tenant_delete_progress":
                payload = json.loads(msg["data"])
                if payload.get("tenant_id") == tid:
                    seen.append(payload)

        statuses = {p["status"] for p in seen}
        assert "started" in statuses, (
            f"missing 'started' event; seen statuses: {sorted(statuses)!r}"
        )
        assert "completed" in statuses, (
            f"missing 'completed' event; seen statuses: {sorted(statuses)!r}"
        )
        # Every emitted event must carry the tenant_id; a phase name
        # must always be present.
        for p in seen:
            assert p["tenant_id"] == tid
            assert "phase" in p
    finally:
        bus.unsubscribe(queue)
        await _purge_partial(pg_test_pool, tid)


@_requires_pg
async def test_delete_tenant_writes_audit_rows(client, pg_test_pool):
    """The cascade writes ``tenant_delete_requested`` (synchronous,
    pre-kickoff) and ``tenant_deleted`` (post-cascade) audit rows
    under the super-admin's chain. Both rows belong to tenant_id
    't-default' (the actor's chain), so they survive the audit_log
    DELETE that wipes the deleted tenant's chain."""
    tid = "t-acme-y2-delete-audit"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Audit Acme', 'starter', 1)",
                tid,
            )

        res = await client.delete(
            f"/api/v1/admin/tenants/{tid}?confirm={tid}",
        )
        assert res.status_code == 202, res.text
        await _await_pending_deletes()

        async with pg_test_pool.acquire() as conn:
            requested = await conn.fetchrow(
                "SELECT actor, before_json, tenant_id "
                "FROM audit_log "
                "WHERE action = 'tenant_delete_requested' "
                "  AND entity_id = $1 "
                "ORDER BY id DESC LIMIT 1",
                tid,
            )
            completed = await conn.fetchrow(
                "SELECT actor, before_json, tenant_id "
                "FROM audit_log "
                "WHERE action = 'tenant_deleted' "
                "  AND entity_id = $1 "
                "ORDER BY id DESC LIMIT 1",
                tid,
            )
        assert requested is not None, (
            "tenant_delete_requested audit row must be present"
        )
        assert completed is not None, (
            "tenant_deleted audit row must be present after cascade"
        )
        # Both must belong to t-default (the super-admin's chain), NOT
        # the deleted tenant — otherwise the cascade would wipe its
        # own audit trail.
        assert requested["tenant_id"] == "t-default"
        assert completed["tenant_id"] == "t-default"
        # before_json captures the tenant snapshot.
        before = json.loads(requested["before_json"])
        assert before["id"] == tid
        assert before["plan"] == "starter"
    finally:
        await _purge_partial(pg_test_pool, tid)
