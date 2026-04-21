"""I1 — Multi-tenancy schema + context tests (PG-native).

Consolidation target for the Phase-3 Step C.1 migration. Absorbs
the real coverage that used to live in three legacy ``tests/``
files — ``tests/test_tenants.py``, ``tests/test_rls.py``,
``tests/test_tenant_secrets.py`` — which were all pinned to the
SQLite-file ``_setup_db`` fixture via ``db._conn()`` and broke
at fixture setup once Phase-3 made the pool the authoritative
connection source.

Groups covered here:
  * ``tenants`` table existence + default-tenant seed row
  * ``tenant_id`` column + index presence on every business table
    (``users``, ``workflow_runs``, ``debug_findings``,
    ``decision_rules``, ``event_log``, ``audit_log``, ``artifacts``,
    ``user_preferences``)
  * Backfill / default-on-insert semantics for the above
  * ``tenant_secrets`` schema + uniqueness
  * ``api_keys`` has ``tenant_id`` + default
  * Pure-unit coverage of ``backend.db_context`` (no DB required)
  * Migration-file sanity (0012 revision chain)

Domain-specific RLS behaviours that used to be tested here have
already been re-homed to the per-domain files:
  * artifacts → ``backend/tests/test_db_artifacts.py``
  * event_log → ``backend/tests/test_db_events.py``
  * debug_findings → ``backend/tests/test_db_debug_findings.py``
  * decision_rules → ``backend/tests/test_db_decision_rules.py``
  * tenant_secrets CRUD + isolation →
    ``backend/tests/test_tenant_secrets.py``
so this file deliberately stops at the schema + backfill + unit
boundary and does not re-port those domain RLS suites.
"""

from __future__ import annotations

import os

import pytest


DEFAULT_TENANT = "t-default"

_BUSINESS_TABLES = [
    "users",
    "workflow_runs",
    "debug_findings",
    "decision_rules",
    "event_log",
    "audit_log",
    "artifacts",
    "user_preferences",
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  tenants table: existence + default seed
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_tenants_table_exists(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name = 'tenants'"
        )
    assert row is not None


@pytest.mark.asyncio
async def test_default_tenant_seeded(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, plan, enabled FROM tenants WHERE id = $1",
            DEFAULT_TENANT,
        )
    assert row is not None
    assert row["name"] == "Default Tenant"
    assert row["plan"] == "free"
    # ``enabled`` is stored as INTEGER NOT NULL DEFAULT 1 across
    # dialects; the compat wrapper mapped SQLite 1/0 straight
    # through, and PG keeps the same ``INTEGER`` column type.
    assert row["enabled"] == 1


@pytest.mark.asyncio
async def test_create_custom_tenant(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES ($1, $2, $3) "
            "ON CONFLICT (id) DO NOTHING",
            "t-acme-schema", "Acme Corp", "enterprise",
        )
        row = await conn.fetchrow(
            "SELECT id, name, plan FROM tenants WHERE id = $1",
            "t-acme-schema",
        )
    assert row is not None
    assert row["name"] == "Acme Corp"
    assert row["plan"] == "enterprise"


@pytest.mark.asyncio
async def test_tenant_enabled_default(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            "t-new-schema", "New Tenant",
        )
        row = await conn.fetchrow(
            "SELECT enabled FROM tenants WHERE id = $1",
            "t-new-schema",
        )
    assert row["enabled"] == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  tenant_id column + index on business tables
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
@pytest.mark.parametrize("table", _BUSINESS_TABLES)
async def test_business_table_has_tenant_id(pg_test_pool, table):
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = $1 AND column_name = 'tenant_id'",
            table,
        )
    assert row is not None, f"{table} missing tenant_id column"


@pytest.mark.asyncio
@pytest.mark.parametrize("table", _BUSINESS_TABLES)
async def test_business_table_tenant_id_has_index(pg_test_pool, table):
    idx_name = f"idx_{table}_tenant"
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = $1 AND indexname = $2",
            table, idx_name,
        )
    assert row is not None, f"Missing index {idx_name}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Backfill / default-on-insert
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_new_user_gets_default_tenant(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        uid = f"u-tenant-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash) "
            "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (id) DO NOTHING",
            uid, f"{uid}@test.com", "Test User", "viewer", "hash",
        )
        row = await conn.fetchrow(
            "SELECT tenant_id FROM users WHERE id = $1", uid,
        )
    assert row["tenant_id"] == DEFAULT_TENANT


@pytest.mark.asyncio
async def test_new_workflow_run_gets_default_tenant(pg_test_pool):
    import time
    async with pg_test_pool.acquire() as conn:
        wr_id = f"wr-tenant-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO workflow_runs (id, kind, started_at) "
            "VALUES ($1, $2, $3) ON CONFLICT (id) DO NOTHING",
            wr_id, "test", time.time(),
        )
        row = await conn.fetchrow(
            "SELECT tenant_id FROM workflow_runs WHERE id = $1", wr_id,
        )
    assert row["tenant_id"] == DEFAULT_TENANT


@pytest.mark.asyncio
async def test_new_artifact_gets_default_tenant(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        art_id = f"art-tenant-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO artifacts (id, name, file_path) "
            "VALUES ($1, $2, $3) ON CONFLICT (id) DO NOTHING",
            art_id, "test.pdf", "/tmp/test.pdf",
        )
        row = await conn.fetchrow(
            "SELECT tenant_id FROM artifacts WHERE id = $1", art_id,
        )
    assert row["tenant_id"] == DEFAULT_TENANT


@pytest.mark.asyncio
async def test_explicit_tenant_id_on_insert(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $2) "
            "ON CONFLICT (id) DO NOTHING",
            "t-acme-expl", "Acme",
        )
        uid = f"u-expl-{os.urandom(3).hex()}"
        await conn.execute(
            "INSERT INTO users (id, email, name, role, "
            "password_hash, tenant_id) VALUES "
            "($1, $2, $3, $4, $5, $6) ON CONFLICT (id) DO NOTHING",
            uid, f"{uid}@test.com", "Acme User", "admin", "hash", "t-acme-expl",
        )
        row = await conn.fetchrow(
            "SELECT tenant_id FROM users WHERE id = $1", uid,
        )
    assert row["tenant_id"] == "t-acme-expl"


@pytest.mark.asyncio
async def test_audit_log_gets_default_tenant(pg_test_pool):
    import time
    async with pg_test_pool.acquire() as conn:
        # audit_log uses SERIAL id — no ON CONFLICT needed, but we
        # capture the assigned id explicitly via RETURNING so the
        # follow-up SELECT doesn't race other tests inserting audit
        # rows concurrently.
        row = await conn.fetchrow(
            "INSERT INTO audit_log (ts, actor, action, entity_kind, "
            "curr_hash) VALUES ($1, $2, $3, $4, $5) RETURNING id",
            time.time(), "system", "test", "test", "abc123",
        )
        audit_id = row["id"]
        row = await conn.fetchrow(
            "SELECT tenant_id FROM audit_log WHERE id = $1", audit_id,
        )
    assert row["tenant_id"] == DEFAULT_TENANT


@pytest.mark.asyncio
async def test_event_log_gets_default_tenant(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO event_log (event_type, data_json) "
            "VALUES ($1, $2) RETURNING id",
            "test_tenant_event", "{}",
        )
        event_id = row["id"]
        row = await conn.fetchrow(
            "SELECT tenant_id FROM event_log WHERE id = $1", event_id,
        )
    assert row["tenant_id"] == DEFAULT_TENANT


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  tenant_secrets + api_keys schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_tenant_secrets_table_exists(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name = 'tenant_secrets'"
        )
    assert row is not None


@pytest.mark.asyncio
async def test_tenant_secrets_has_expected_columns(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'tenant_secrets'"
        )
    cols = {r["column_name"] for r in rows}
    expected = {"id", "tenant_id", "secret_type", "key_name",
                "encrypted_value", "metadata", "created_at", "updated_at"}
    assert expected.issubset(cols), f"missing: {expected - cols}"


@pytest.mark.asyncio
async def test_tenant_secrets_unique_per_tenant(pg_test_pool):
    from backend.secret_store import encrypt, _reset_for_tests
    _reset_for_tests()
    enc = encrypt("test-value-unique")
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM tenant_secrets WHERE tenant_id = $1 "
            "AND secret_type = 'provider_key' AND key_name = 'openai-unique'",
            DEFAULT_TENANT,
        )
        await conn.execute(
            "INSERT INTO tenant_secrets (id, tenant_id, secret_type, "
            "key_name, encrypted_value) VALUES ($1, $2, $3, $4, $5)",
            "s-unique-1", DEFAULT_TENANT, "provider_key",
            "openai-unique", enc,
        )
        with pytest.raises(Exception):
            await conn.execute(
                "INSERT INTO tenant_secrets (id, tenant_id, secret_type, "
                "key_name, encrypted_value) VALUES ($1, $2, $3, $4, $5)",
                "s-unique-2", DEFAULT_TENANT, "provider_key",
                "openai-unique", enc,
            )


@pytest.mark.asyncio
async def test_api_keys_has_tenant_id(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'api_keys' AND column_name = 'tenant_id'"
        )
    assert row is not None


@pytest.mark.asyncio
async def test_api_keys_default_tenant(pg_test_pool):
    import hashlib
    key_hash = hashlib.sha256(
        f"test-key-{os.urandom(4).hex()}".encode()
    ).hexdigest()
    ak_id = f"ak-tenant-{os.urandom(3).hex()}"
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO api_keys (id, name, key_hash, key_prefix, "
            "created_by) VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (id) DO NOTHING",
            ak_id, "test-default-tenant", key_hash, "test", "admin",
        )
        row = await conn.fetchrow(
            "SELECT tenant_id FROM api_keys WHERE id = $1", ak_id,
        )
    assert row["tenant_id"] == DEFAULT_TENANT


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: backend.db_context (no DB)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_current_tenant_default_is_none():
    from backend.db_context import current_tenant_id, set_tenant_id
    set_tenant_id(None)
    assert current_tenant_id() is None


def test_current_tenant_set_and_get():
    from backend.db_context import set_tenant_id, current_tenant_id
    set_tenant_id("t-acme-unit")
    try:
        assert current_tenant_id() == "t-acme-unit"
    finally:
        set_tenant_id(None)


def test_require_current_tenant_raises_when_unset():
    from backend.db_context import require_current_tenant, set_tenant_id
    set_tenant_id(None)
    with pytest.raises(RuntimeError, match="No tenant_id"):
        require_current_tenant()


def test_require_current_tenant_returns_when_set():
    from backend.db_context import set_tenant_id, require_current_tenant
    set_tenant_id("t-test-unit")
    try:
        assert require_current_tenant() == "t-test-unit"
    finally:
        set_tenant_id(None)


def test_tenant_insert_value_default():
    from backend.db_context import set_tenant_id, tenant_insert_value
    set_tenant_id(None)
    assert tenant_insert_value() == DEFAULT_TENANT


def test_tenant_insert_value_set():
    from backend.db_context import set_tenant_id, tenant_insert_value
    set_tenant_id("t-alpha-unit")
    try:
        assert tenant_insert_value() == "t-alpha-unit"
    finally:
        set_tenant_id(None)


def test_tenant_where_no_tenant_no_filter():
    from backend.db_context import set_tenant_id, tenant_where
    set_tenant_id(None)
    conds, params = [], []
    tenant_where(conds, params)
    assert conds == []
    assert params == []


def test_tenant_where_with_tenant_adds_filter():
    from backend.db_context import set_tenant_id, tenant_where
    set_tenant_id("t-alpha-unit")
    try:
        conds, params = [], []
        tenant_where(conds, params)
        assert conds == ["tenant_id = ?"]
        assert params == ["t-alpha-unit"]
    finally:
        set_tenant_id(None)


def test_tenant_where_with_table_alias():
    from backend.db_context import set_tenant_id, tenant_where
    set_tenant_id("t-beta-unit")
    try:
        conds, params = [], []
        tenant_where(conds, params, table_alias="a")
        assert conds == ["a.tenant_id = ?"]
        assert params == ["t-beta-unit"]
    finally:
        set_tenant_id(None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: User dataclass tenant_id
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_user_dataclass_carries_tenant_id():
    from backend.auth import User
    u = User(id="u-1", email="t@t.com", name="T", role="viewer",
             tenant_id="t-acme-unit")
    assert u.tenant_id == "t-acme-unit"
    assert u.to_dict()["tenant_id"] == "t-acme-unit"


def test_user_default_tenant():
    from backend.auth import User
    u = User(id="u-1", email="t@t.com", name="T", role="viewer")
    assert u.tenant_id == DEFAULT_TENANT


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: require_tenant dependency
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_require_tenant_sets_context():
    from backend.auth import User, require_tenant
    from backend.db_context import current_tenant_id, set_tenant_id
    set_tenant_id(None)
    assert current_tenant_id() is None
    user = User(id="u-dep", email="dep@t.com", name="D", role="viewer",
                tenant_id="t-alpha-dep")
    try:
        result = await require_tenant(user)
        assert result is user
        assert current_tenant_id() == "t-alpha-dep"
    finally:
        set_tenant_id(None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Alembic migration 0012 sanity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_migration_0012_file_exists():
    migration_path = os.path.join(
        os.path.dirname(__file__), "..",
        "alembic", "versions", "0012_tenants_multi_tenancy.py",
    )
    assert os.path.exists(migration_path)


def test_migration_0012_revision_chain():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "m0012",
        os.path.join(
            os.path.dirname(__file__), "..",
            "alembic", "versions", "0012_tenants_multi_tenancy.py",
        ),
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.revision == "0012"
    assert m.down_revision == "0011"
