"""I2 — Row-Level Security (RLS) tests.

Covers: tenant context isolation, cross-tenant query returns empty,
INSERT auto-fills tenant_id, INSERT cannot override to another tenant,
require_tenant dependency wiring.
"""

import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("OMNISIGHT_AUTH_MODE", "open")


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


@pytest.fixture(autouse=True)
def _reset_tenant_context():
    from backend.db_context import set_tenant_id
    set_tenant_id(None)
    yield
    set_tenant_id(None)


TENANT_A = "t-alpha"
TENANT_B = "t-beta"
DEFAULT_TENANT = "t-default"


def _seed_tenants(db):
    async def seed():
        conn = db._conn()
        await conn.execute(
            "INSERT OR IGNORE INTO tenants (id, name) VALUES (?, ?)",
            (TENANT_A, "Alpha Corp"),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO tenants (id, name) VALUES (?, ?)",
            (TENANT_B, "Beta Inc"),
        )
        await conn.commit()
    _run(seed())


class TestCurrentTenantContext:
    def test_default_is_none(self):
        from backend.db_context import current_tenant_id
        assert current_tenant_id() is None

    def test_set_and_get(self):
        from backend.db_context import set_tenant_id, current_tenant_id
        set_tenant_id("t-acme")
        assert current_tenant_id() == "t-acme"

    def test_require_raises_when_unset(self):
        from backend.db_context import require_current_tenant, set_tenant_id
        set_tenant_id(None)
        with pytest.raises(RuntimeError, match="No tenant_id"):
            require_current_tenant()

    def test_require_returns_when_set(self):
        from backend.db_context import set_tenant_id, require_current_tenant
        set_tenant_id("t-test")
        assert require_current_tenant() == "t-test"

    def test_tenant_insert_value_default(self):
        from backend.db_context import set_tenant_id, tenant_insert_value
        set_tenant_id(None)
        assert tenant_insert_value() == DEFAULT_TENANT

    def test_tenant_insert_value_set(self):
        from backend.db_context import set_tenant_id, tenant_insert_value
        set_tenant_id(TENANT_A)
        assert tenant_insert_value() == TENANT_A


class TestTenantWhereHelper:
    def test_no_tenant_no_filter(self):
        from backend.db_context import set_tenant_id, tenant_where
        set_tenant_id(None)
        conds, params = [], []
        tenant_where(conds, params)
        assert conds == []
        assert params == []

    def test_with_tenant_adds_filter(self):
        from backend.db_context import set_tenant_id, tenant_where
        set_tenant_id(TENANT_A)
        conds, params = [], []
        tenant_where(conds, params)
        assert conds == ["tenant_id = ?"]
        assert params == [TENANT_A]

    def test_with_table_alias(self):
        from backend.db_context import set_tenant_id, tenant_where
        set_tenant_id(TENANT_B)
        conds, params = [], []
        tenant_where(conds, params, table_alias="a")
        assert conds == ["a.tenant_id = ?"]
        assert params == [TENANT_B]


class TestEventLogRLS:
    def test_insert_auto_fills_tenant(self, _setup_db):
        db = _setup_db
        _seed_tenants(db)
        from backend.db_context import set_tenant_id
        set_tenant_id(TENANT_A)

        async def check():
            await db.insert_event("test_event", '{"key": "val"}')
            async with db._conn().execute(
                "SELECT tenant_id FROM event_log ORDER BY id DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
            assert row["tenant_id"] == TENANT_A
        _run(check())

    def test_list_events_filtered_by_tenant(self, _setup_db):
        db = _setup_db
        _seed_tenants(db)
        from backend.db_context import set_tenant_id

        async def check():
            set_tenant_id(TENANT_A)
            await db.insert_event("alpha_event", '{}')
            set_tenant_id(TENANT_B)
            await db.insert_event("beta_event", '{}')

            set_tenant_id(TENANT_A)
            events = await db.list_events()
            types = [e["event_type"] for e in events]
            assert "alpha_event" in types
            assert "beta_event" not in types

            set_tenant_id(TENANT_B)
            events = await db.list_events()
            types = [e["event_type"] for e in events]
            assert "beta_event" in types
            assert "alpha_event" not in types
        _run(check())

    def test_no_tenant_returns_all(self, _setup_db):
        db = _setup_db
        _seed_tenants(db)
        from backend.db_context import set_tenant_id

        async def check():
            set_tenant_id(TENANT_A)
            await db.insert_event("alpha_event2", '{}')
            set_tenant_id(TENANT_B)
            await db.insert_event("beta_event2", '{}')

            set_tenant_id(None)
            events = await db.list_events()
            types = [e["event_type"] for e in events]
            assert "alpha_event2" in types
            assert "beta_event2" in types
        _run(check())


@pytest.mark.skip(
    reason="SP-3.6b: tenant-isolation coverage for artifacts moved to "
           "backend/tests/test_db_artifacts.py::TestArtifactsTenantIsolation "
           "(pg_test_conn savepoint fixture). Migrating these four tests "
           "to the tests/ tree requires wiring pg_test_pool across "
           "conftest.py trees — deferred as non-blocking (coverage "
           "preserved; no semantic gap between savepoint and auto-commit "
           "for the tenant-filter contract being tested)."
)
class TestArtifactRLS:
    @pytest.mark.asyncio
    async def test_insert_auto_fills_tenant(self, pg_test_pool):
        from backend import db
        from backend.db_context import set_tenant_id
        set_tenant_id(TENANT_A)
        try:
            async with pg_test_pool.acquire() as conn:
                await db.insert_artifact(conn, {
                    "id": "art-rls-1", "task_id": "", "agent_id": "",
                    "name": "test.bin", "type": "binary",
                    "file_path": "/tmp/test.bin", "size": 100,
                    "created_at": "2026-01-01",
                })
                row = await conn.fetchrow(
                    "SELECT tenant_id FROM artifacts WHERE id = $1",
                    "art-rls-1",
                )
                assert row["tenant_id"] == TENANT_A
        finally:
            async with pg_test_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM artifacts WHERE id = $1", "art-rls-1",
                )

    @pytest.mark.asyncio
    async def test_list_artifacts_filtered(self, pg_test_pool):
        from backend import db
        from backend.db_context import set_tenant_id
        try:
            set_tenant_id(TENANT_A)
            async with pg_test_pool.acquire() as conn:
                await db.insert_artifact(conn, {
                    "id": "art-rls-a", "task_id": "", "agent_id": "",
                    "name": "alpha.bin", "type": "binary",
                    "file_path": "/tmp/a.bin", "size": 10,
                    "created_at": "2026-01-01",
                })
            set_tenant_id(TENANT_B)
            async with pg_test_pool.acquire() as conn:
                await db.insert_artifact(conn, {
                    "id": "art-rls-b", "task_id": "", "agent_id": "",
                    "name": "beta.bin", "type": "binary",
                    "file_path": "/tmp/b.bin", "size": 20,
                    "created_at": "2026-01-01",
                })

            set_tenant_id(TENANT_A)
            async with pg_test_pool.acquire() as conn:
                arts = await db.list_artifacts(conn)
            ids = [a["id"] for a in arts]
            assert "art-rls-a" in ids
            assert "art-rls-b" not in ids
        finally:
            async with pg_test_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM artifacts WHERE id IN ($1, $2)",
                    "art-rls-a", "art-rls-b",
                )

    @pytest.mark.asyncio
    async def test_get_artifact_cross_tenant_returns_none(self, pg_test_pool):
        from backend import db
        from backend.db_context import set_tenant_id
        try:
            set_tenant_id(TENANT_A)
            async with pg_test_pool.acquire() as conn:
                await db.insert_artifact(conn, {
                    "id": "art-rls-priv", "task_id": "", "agent_id": "",
                    "name": "private.bin", "type": "binary",
                    "file_path": "/tmp/priv.bin", "size": 10,
                    "created_at": "2026-01-01",
                })

            set_tenant_id(TENANT_B)
            async with pg_test_pool.acquire() as conn:
                art = await db.get_artifact(conn, "art-rls-priv")
            assert art is None
        finally:
            async with pg_test_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM artifacts WHERE id = $1", "art-rls-priv",
                )

    @pytest.mark.asyncio
    async def test_delete_artifact_cross_tenant_fails(self, pg_test_pool):
        from backend import db
        from backend.db_context import set_tenant_id
        try:
            set_tenant_id(TENANT_A)
            async with pg_test_pool.acquire() as conn:
                await db.insert_artifact(conn, {
                    "id": "art-rls-nodelete", "task_id": "", "agent_id": "",
                    "name": "nodelete.bin", "type": "binary",
                    "file_path": "/tmp/nd.bin", "size": 10,
                    "created_at": "2026-01-01",
                })

            set_tenant_id(TENANT_B)
            async with pg_test_pool.acquire() as conn:
                deleted = await db.delete_artifact(conn, "art-rls-nodelete")
            assert deleted is False

            set_tenant_id(TENANT_A)
            async with pg_test_pool.acquire() as conn:
                art = await db.get_artifact(conn, "art-rls-nodelete")
            assert art is not None
        finally:
            async with pg_test_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM artifacts WHERE id = $1",
                    "art-rls-nodelete",
                )


@pytest.mark.skip(
    reason="SP-3.9: tenant-isolation coverage for debug_findings moved "
           "to backend/tests/test_db_debug_findings.py::"
           "TestDebugFindingsTenantIsolation (pg_test_conn-backed). "
           "The tests/ tree can't see backend/tests/conftest.py "
           "fixtures; migrating would require cross-tree fixture "
           "plumbing for no semantic gain (same SQL + same tenant "
           "filter contract)."
)
class TestDebugFindingRLS:
    def test_insert_auto_fills_tenant(self, _setup_db):
        db = _setup_db
        _seed_tenants(db)
        from backend.db_context import set_tenant_id
        set_tenant_id(TENANT_B)

        async def check():
            await db.insert_debug_finding({
                "id": "df-1", "task_id": "t-1", "agent_id": "a-1",
                "finding_type": "bug", "severity": "warn",
                "content": "test finding", "context": "{}",
                "status": "open", "created_at": "2026-01-01",
            })
            async with db._conn().execute(
                "SELECT tenant_id FROM debug_findings WHERE id = ?", ("df-1",)
            ) as cur:
                row = await cur.fetchone()
            assert row["tenant_id"] == TENANT_B
        _run(check())

    def test_list_debug_findings_filtered(self, _setup_db):
        db = _setup_db
        _seed_tenants(db)
        from backend.db_context import set_tenant_id

        async def check():
            set_tenant_id(TENANT_A)
            await db.insert_debug_finding({
                "id": "df-a", "task_id": "t-1", "agent_id": "a-1",
                "finding_type": "bug", "severity": "warn",
                "content": "alpha finding", "context": "{}",
                "status": "open", "created_at": "2026-01-01",
            })
            set_tenant_id(TENANT_B)
            await db.insert_debug_finding({
                "id": "df-b", "task_id": "t-1", "agent_id": "a-1",
                "finding_type": "bug", "severity": "warn",
                "content": "beta finding", "context": "{}",
                "status": "open", "created_at": "2026-01-02",
            })

            set_tenant_id(TENANT_A)
            findings = await db.list_debug_findings()
            ids = [f["id"] for f in findings]
            assert "df-a" in ids
            assert "df-b" not in ids
        _run(check())


class TestDecisionRulesRLS:
    def test_load_rules_filtered(self, _setup_db):
        db = _setup_db
        _seed_tenants(db)
        from backend.db_context import set_tenant_id

        async def check():
            conn = db._conn()
            await conn.execute(
                "INSERT INTO decision_rules (id, kind_pattern, severity, auto_in_modes, "
                "default_option_id, priority, enabled, note, tenant_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("rule-a", "*.alpha", "warn", "[]", "opt-1", 100, 1, "", TENANT_A),
            )
            await conn.execute(
                "INSERT INTO decision_rules (id, kind_pattern, severity, auto_in_modes, "
                "default_option_id, priority, enabled, note, tenant_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("rule-b", "*.beta", "warn", "[]", "opt-1", 100, 1, "", TENANT_B),
            )
            await conn.commit()

            set_tenant_id(TENANT_A)
            rules = await db.load_decision_rules()
            ids = [r["id"] for r in rules]
            assert "rule-a" in ids
            assert "rule-b" not in ids
        _run(check())

    def test_replace_rules_scoped_to_tenant(self, _setup_db):
        db = _setup_db
        _seed_tenants(db)
        from backend.db_context import set_tenant_id

        async def check():
            conn = db._conn()
            await conn.execute(
                "INSERT INTO decision_rules (id, kind_pattern, severity, auto_in_modes, "
                "default_option_id, priority, enabled, note, tenant_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("rule-keep", "*.keep", "warn", "[]", "opt-1", 100, 1, "", TENANT_B),
            )
            await conn.commit()

            set_tenant_id(TENANT_A)
            await db.replace_decision_rules([
                {"id": "rule-new", "kind_pattern": "*.new", "severity": "warn",
                 "auto_in_modes": [], "default_option_id": "opt-1", "priority": 100,
                 "enabled": True, "note": ""},
            ])

            async with conn.execute(
                "SELECT id, tenant_id FROM decision_rules ORDER BY id"
            ) as cur:
                rows = await cur.fetchall()
            ids = {r["id"]: r["tenant_id"] for r in rows}
            assert "rule-keep" in ids
            assert ids["rule-keep"] == TENANT_B
            assert "rule-new" in ids
            assert ids["rule-new"] == TENANT_A
        _run(check())


class TestUserTenantId:
    def test_user_dataclass_has_tenant_id(self):
        from backend.auth import User
        u = User(id="u-1", email="t@t.com", name="T", role="viewer",
                 tenant_id="t-acme")
        assert u.tenant_id == "t-acme"
        assert "tenant_id" in u.to_dict()
        assert u.to_dict()["tenant_id"] == "t-acme"

    def test_user_default_tenant(self):
        from backend.auth import User
        u = User(id="u-1", email="t@t.com", name="T", role="viewer")
        assert u.tenant_id == DEFAULT_TENANT

    def test_get_user_returns_tenant_id(self, _setup_db):
        db = _setup_db
        _seed_tenants(db)
        from backend import auth

        async def check():
            conn = db._conn()
            await conn.execute(
                "INSERT INTO users (id, email, name, role, password_hash, tenant_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("u-rls", "rls@test.com", "RLS User", "viewer", "", TENANT_A),
            )
            await conn.commit()
            user = await auth.get_user("u-rls")
            assert user is not None
            assert user.tenant_id == TENANT_A
        _run(check())

    def test_get_user_by_email_returns_tenant_id(self, _setup_db):
        db = _setup_db
        _seed_tenants(db)
        from backend import auth

        async def check():
            conn = db._conn()
            await conn.execute(
                "INSERT INTO users (id, email, name, role, password_hash, tenant_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("u-rls2", "rls2@test.com", "RLS User 2", "admin", "", TENANT_B),
            )
            await conn.commit()
            user = await auth.get_user_by_email("rls2@test.com")
            assert user is not None
            assert user.tenant_id == TENANT_B
        _run(check())


class TestRequireTenantDependency:
    def test_require_tenant_sets_context(self):
        from backend.auth import User, require_tenant
        from backend.db_context import current_tenant_id, set_tenant_id

        set_tenant_id(None)
        assert current_tenant_id() is None

        user = User(id="u-dep", email="dep@t.com", name="D", role="viewer",
                    tenant_id=TENANT_A)

        async def check():
            result = await require_tenant(user)
            assert result is user
            assert current_tenant_id() == TENANT_A
        _run(check())


class TestWorkflowRLS:
    def test_start_auto_fills_tenant(self, _setup_db):
        _seed_tenants(_setup_db)
        from backend.db_context import set_tenant_id
        from backend import workflow

        async def check():
            set_tenant_id(TENANT_A)
            run = await workflow.start("test_kind")
            conn = _setup_db._conn()
            async with conn.execute(
                "SELECT tenant_id FROM workflow_runs WHERE id = ?", (run.id,)
            ) as cur:
                row = await cur.fetchone()
            assert row["tenant_id"] == TENANT_A
        _run(check())

    def test_list_runs_filtered(self, _setup_db):
        _seed_tenants(_setup_db)
        from backend.db_context import set_tenant_id
        from backend import workflow

        async def check():
            set_tenant_id(TENANT_A)
            run_a = await workflow.start("alpha_run")
            set_tenant_id(TENANT_B)
            run_b = await workflow.start("beta_run")

            set_tenant_id(TENANT_A)
            runs = await workflow.list_runs()
            ids = [r.id for r in runs]
            assert run_a.id in ids
            assert run_b.id not in ids
        _run(check())

    def test_get_run_cross_tenant_returns_none(self, _setup_db):
        _seed_tenants(_setup_db)
        from backend.db_context import set_tenant_id
        from backend import workflow

        async def check():
            set_tenant_id(TENANT_A)
            run_a = await workflow.start("private_run")
            set_tenant_id(TENANT_B)
            result = await workflow.get_run(run_a.id)
            assert result is None
        _run(check())


class TestAuditRLS:
    def test_log_auto_fills_tenant(self, _setup_db):
        _seed_tenants(_setup_db)
        from backend.db_context import set_tenant_id
        from backend import audit

        async def check():
            set_tenant_id(TENANT_A)
            row_id = await audit.log(
                action="test_action", entity_kind="test",
                entity_id="e-1", actor="system",
            )
            assert row_id is not None
            conn = _setup_db._conn()
            async with conn.execute(
                "SELECT tenant_id FROM audit_log WHERE id = ?", (row_id,)
            ) as cur:
                row = await cur.fetchone()
            assert row["tenant_id"] == TENANT_A
        _run(check())

    def test_query_filtered_by_tenant(self, _setup_db):
        _seed_tenants(_setup_db)
        from backend.db_context import set_tenant_id
        from backend import audit

        async def check():
            set_tenant_id(TENANT_A)
            await audit.log(action="alpha_action", entity_kind="test",
                            entity_id="e-a", actor="alice")
            set_tenant_id(TENANT_B)
            await audit.log(action="beta_action", entity_kind="test",
                            entity_id="e-b", actor="bob")

            set_tenant_id(TENANT_A)
            rows = await audit.query()
            actions = [r["action"] for r in rows]
            assert "alpha_action" in actions
            assert "beta_action" not in actions
        _run(check())
