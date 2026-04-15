"""I1 — Multi-tenancy schema tests.

Covers: tenants table creation, tenant_id columns on business tables,
backfill correctness, migration idempotency, and default tenant seeding.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("OMNISIGHT_AUTH_MODE", "session")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _setup_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    os.environ["DATABASE_PATH"] = db_path
    from backend.config import settings
    settings.database_path = db_path
    from backend import db
    db._DB_PATH = tmp_path / "test.db"
    db._db = None
    _run(db.init())
    yield db
    _run(db.close())


DEFAULT_TENANT = "t-default"

TABLES_WITH_TENANT_ID = [
    "users",
    "workflow_runs",
    "debug_findings",
    "decision_rules",
    "event_log",
    "audit_log",
    "artifacts",
    "user_preferences",
]


class TestTenantsTable:
    def test_tenants_table_exists(self, _setup_db):
        db = _setup_db
        async def check():
            async with db._conn().execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tenants'"
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
        _run(check())

    def test_default_tenant_seeded(self, _setup_db):
        db = _setup_db
        async def check():
            async with db._conn().execute(
                "SELECT id, name, plan, enabled FROM tenants WHERE id = ?",
                (DEFAULT_TENANT,),
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row["name"] == "Default Tenant"
            assert row["plan"] == "free"
            assert row["enabled"] == 1
        _run(check())

    def test_create_custom_tenant(self, _setup_db):
        db = _setup_db
        async def check():
            await db._conn().execute(
                "INSERT INTO tenants (id, name, plan) VALUES (?, ?, ?)",
                ("t-acme", "Acme Corp", "enterprise"),
            )
            await db._conn().commit()
            async with db._conn().execute(
                "SELECT id, name, plan FROM tenants WHERE id = ?",
                ("t-acme",),
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row["name"] == "Acme Corp"
            assert row["plan"] == "enterprise"
        _run(check())

    def test_tenant_enabled_default(self, _setup_db):
        db = _setup_db
        async def check():
            await db._conn().execute(
                "INSERT INTO tenants (id, name) VALUES (?, ?)",
                ("t-new", "New Tenant"),
            )
            await db._conn().commit()
            async with db._conn().execute(
                "SELECT enabled FROM tenants WHERE id = ?", ("t-new",)
            ) as cur:
                row = await cur.fetchone()
            assert row["enabled"] == 1
        _run(check())


class TestTenantIdColumns:
    def test_all_business_tables_have_tenant_id(self, _setup_db):
        db = _setup_db
        async def check():
            for table in TABLES_WITH_TENANT_ID:
                async with db._conn().execute(
                    f"PRAGMA table_info({table})"
                ) as cur:
                    cols = {row[1] for row in await cur.fetchall()}
                assert "tenant_id" in cols, f"{table} missing tenant_id column"
        _run(check())

    def test_tenant_id_indexes_exist(self, _setup_db):
        db = _setup_db
        async def check():
            for table in TABLES_WITH_TENANT_ID:
                idx_name = f"idx_{table}_tenant"
                async with db._conn().execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                    (idx_name,),
                ) as cur:
                    row = await cur.fetchone()
                assert row is not None, f"Missing index {idx_name}"
        _run(check())


class TestBackfill:
    def test_new_user_gets_default_tenant(self, _setup_db):
        db = _setup_db
        async def check():
            await db._conn().execute(
                "INSERT INTO users (id, email, name, role, password_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                ("u-1", "user@test.com", "Test User", "viewer", "hash"),
            )
            await db._conn().commit()
            async with db._conn().execute(
                "SELECT tenant_id FROM users WHERE id = ?", ("u-1",)
            ) as cur:
                row = await cur.fetchone()
            assert row["tenant_id"] == DEFAULT_TENANT
        _run(check())

    def test_new_workflow_run_gets_default_tenant(self, _setup_db):
        db = _setup_db
        async def check():
            import time
            await db._conn().execute(
                "INSERT INTO workflow_runs (id, kind, started_at) "
                "VALUES (?, ?, ?)",
                ("wr-1", "test", time.time()),
            )
            await db._conn().commit()
            async with db._conn().execute(
                "SELECT tenant_id FROM workflow_runs WHERE id = ?", ("wr-1",)
            ) as cur:
                row = await cur.fetchone()
            assert row["tenant_id"] == DEFAULT_TENANT
        _run(check())

    def test_new_artifact_gets_default_tenant(self, _setup_db):
        db = _setup_db
        async def check():
            await db._conn().execute(
                "INSERT INTO artifacts (id, name, file_path) VALUES (?, ?, ?)",
                ("art-1", "test.pdf", "/tmp/test.pdf"),
            )
            await db._conn().commit()
            async with db._conn().execute(
                "SELECT tenant_id FROM artifacts WHERE id = ?", ("art-1",)
            ) as cur:
                row = await cur.fetchone()
            assert row["tenant_id"] == DEFAULT_TENANT
        _run(check())

    def test_explicit_tenant_id_on_insert(self, _setup_db):
        db = _setup_db
        async def check():
            await db._conn().execute(
                "INSERT INTO tenants (id, name) VALUES (?, ?)",
                ("t-acme", "Acme"),
            )
            await db._conn().execute(
                "INSERT INTO users (id, email, name, role, password_hash, tenant_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("u-2", "acme@test.com", "Acme User", "admin", "hash", "t-acme"),
            )
            await db._conn().commit()
            async with db._conn().execute(
                "SELECT tenant_id FROM users WHERE id = ?", ("u-2",)
            ) as cur:
                row = await cur.fetchone()
            assert row["tenant_id"] == "t-acme"
        _run(check())

    def test_audit_log_gets_default_tenant(self, _setup_db):
        db = _setup_db
        async def check():
            import time
            await db._conn().execute(
                "INSERT INTO audit_log (ts, actor, action, entity_kind, curr_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (time.time(), "system", "test", "test", "abc123"),
            )
            await db._conn().commit()
            async with db._conn().execute(
                "SELECT tenant_id FROM audit_log ORDER BY id DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
            assert row["tenant_id"] == DEFAULT_TENANT
        _run(check())

    def test_event_log_gets_default_tenant(self, _setup_db):
        db = _setup_db
        async def check():
            await db._conn().execute(
                "INSERT INTO event_log (event_type, data_json) VALUES (?, ?)",
                ("test_event", "{}"),
            )
            await db._conn().commit()
            async with db._conn().execute(
                "SELECT tenant_id FROM event_log ORDER BY id DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
            assert row["tenant_id"] == DEFAULT_TENANT
        _run(check())


class TestMigrationIdempotency:
    def test_double_init_is_safe(self, _setup_db):
        db = _setup_db
        async def check():
            await db.close()
            db._db = None
            await db.init()
            async with db._conn().execute(
                "SELECT COUNT(*) FROM tenants WHERE id = ?",
                (DEFAULT_TENANT,),
            ) as cur:
                row = await cur.fetchone()
            assert row[0] == 1
        _run(check())

    def test_double_init_preserves_data(self, _setup_db):
        db = _setup_db
        async def check():
            await db._conn().execute(
                "INSERT INTO users (id, email, name, role, password_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                ("u-persist", "persist@test.com", "Persist", "viewer", "hash"),
            )
            await db._conn().commit()
            await db.close()
            db._db = None
            await db.init()
            async with db._conn().execute(
                "SELECT tenant_id FROM users WHERE id = ?", ("u-persist",)
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row["tenant_id"] == DEFAULT_TENANT
        _run(check())


class TestAlembicMigration:
    def test_migration_file_exists(self):
        migration_path = os.path.join(
            os.path.dirname(__file__), "..",
            "backend", "alembic", "versions", "0012_tenants_multi_tenancy.py",
        )
        assert os.path.exists(migration_path)

    def test_migration_revision_chain(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "m0012",
            os.path.join(
                os.path.dirname(__file__), "..",
                "backend", "alembic", "versions",
                "0012_tenants_multi_tenancy.py",
            ),
        )
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        assert m.revision == "0012"
        assert m.down_revision == "0011"
