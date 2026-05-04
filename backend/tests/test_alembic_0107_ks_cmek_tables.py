"""KS.2.11 -- alembic 0107 CMEK table contract."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0107 = BACKEND_ROOT / "alembic" / "versions" / "0107_ks_cmek_tables.py"

KS_CMEK_TABLES = (
    "cmek_configs",
    "tier_assignments",
    "cmek_revoke_events",
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0107():
    return _load_module(MIGRATION_0107, "_alembic_test_0107")


# -- Group 1: structural guards --------------------------------------------


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0107.read_text()

    def test_revision_id_is_0107(self, source: str) -> None:
        assert 'revision = "0107"' in source

    def test_down_revision_is_0106(self, source: str) -> None:
        assert 'down_revision = "0106"' in source

    def test_all_cmek_tables_declared(self, m0107) -> None:
        joined_pg = "\n".join(m0107._TABLES_PG)
        joined_sqlite = "\n".join(m0107._TABLES_SQLITE)
        for table in KS_CMEK_TABLES:
            assert f"CREATE TABLE IF NOT EXISTS {table}" in joined_pg
            assert f"CREATE TABLE IF NOT EXISTS {table}" in joined_sqlite

    def test_provider_check_clause_matches_tier2_wizard_catalog(self, m0107) -> None:
        from backend.security import cmek_wizard as cmek

        expected = ",".join(f"'{p['provider']}'" for p in cmek.list_provider_specs())
        assert expected == m0107._CMEK_PROVIDERS_SQL
        assert "local-fernet" not in m0107._CMEK_PROVIDERS_SQL

    def test_cmek_configs_cover_wizard_completion_fields(self, m0107) -> None:
        required = (
            "config_id",
            "tenant_id",
            "provider",
            "key_id",
            "policy_principal",
            "verification_id",
            "status",
            "verified_at",
        )
        for col in required:
            assert col in m0107._PG_CREATE_CMEK_CONFIGS
            assert col in m0107._SQLITE_CREATE_CMEK_CONFIGS

    def test_tier_assignments_cover_upgrade_downgrade_state(self, m0107) -> None:
        for col in (
            "tenant_id",
            "security_tier",
            "cmek_config_id",
            "assigned_by",
            "assigned_at",
            "updated_at",
        ):
            assert col in m0107._PG_CREATE_TIER_ASSIGNMENTS
            assert col in m0107._SQLITE_CREATE_TIER_ASSIGNMENTS
        assert "'tier-1','tier-2'" == m0107._SECURITY_TIERS_SQL
        assert "fallback_to_tier1" in m0107._TIER_ASSIGNMENT_STATUSES_SQL

    def test_revoke_events_cover_detector_result_fields(self, m0107) -> None:
        for col in (
            "event_id",
            "tenant_id",
            "cmek_config_id",
            "provider",
            "key_id",
            "reason",
            "raw_state",
            "detected_at",
            "restored_at",
            "detail_json",
        ):
            assert col in m0107._PG_CREATE_CMEK_REVOKE_EVENTS
            assert col in m0107._SQLITE_CREATE_CMEK_REVOKE_EVENTS
        assert "describe_failed" in m0107._REVOKE_EVENT_REASONS_SQL
        assert "key_disabled" in m0107._REVOKE_EVENT_REASONS_SQL

    def test_pg_branch_uses_jsonb_and_double_precision(self, m0107) -> None:
        joined = "\n".join(m0107._TABLES_PG)
        assert "JSONB NOT NULL DEFAULT '{}'::jsonb" in joined
        assert "DOUBLE PRECISION" in joined
        assert " REAL" not in joined

    def test_sqlite_branch_uses_text_json_and_real(self, m0107) -> None:
        joined = "\n".join(m0107._TABLES_SQLITE)
        assert "TEXT NOT NULL DEFAULT '{}'" in joined
        assert " REAL" in joined
        assert "JSONB" not in joined
        assert "DOUBLE PRECISION" not in joined

    def test_indexes_declared(self, m0107) -> None:
        joined = "\n".join(m0107._INDEXES)
        for index in (
            "idx_cmek_configs_tenant_status",
            "idx_cmek_configs_provider_key",
            "idx_tier_assignments_security_tier",
            "idx_tier_assignments_cmek_config",
            "idx_cmek_revoke_events_tenant_time",
            "idx_cmek_revoke_events_config_time",
        ):
            assert index in joined


# -- Group 2: functional SQLite upgrade ------------------------------------


def _bootstrap_pre_0107_parent_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE tenants (
            id   TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT ''
        );
        """
    )


class _StubBind:
    """Mimics enough of an alembic context bind for ``conn.exec_driver_sql``."""

    def __init__(self, raw: sqlite3.Connection) -> None:
        self._raw = raw

        class _Dialect:
            name = "sqlite"

        self.dialect = _Dialect()

    def exec_driver_sql(self, sql: str, *args, **kwargs):
        return self._raw.execute(sql)


def _bind(monkeypatch, conn: sqlite3.Connection) -> None:
    from alembic import op as alembic_op

    bind = _StubBind(conn)
    monkeypatch.setattr(alembic_op, "get_bind", lambda: bind)


@pytest.fixture()
def upgraded_db(monkeypatch, m0107) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _bootstrap_pre_0107_parent_schema(conn)
    conn.execute("INSERT INTO tenants (id, name) VALUES ('t-a', 'Tenant A')")
    conn.execute("INSERT INTO tenants (id, name) VALUES ('t-b', 'Tenant B')")
    _bind(monkeypatch, conn)
    m0107.upgrade()
    return conn


def _insert_config(
    conn: sqlite3.Connection,
    config_id: str = "cmekcfg-a",
    tenant_id: str = "t-a",
    provider: str = "aws-kms",
) -> None:
    conn.execute(
        "INSERT INTO cmek_configs "
        "(config_id, tenant_id, provider, key_id, created_at, updated_at) "
        "VALUES (:config_id, :tenant_id, :provider, :key_id, :created_at, "
        ":updated_at)",
        {
            "config_id": config_id,
            "tenant_id": tenant_id,
            "provider": provider,
            "key_id": (
                "arn:aws:kms:us-east-1:111122223333:key/"
                "00000000-0000-0000-0000-000000000000"
            ),
            "created_at": 1_777_000_000.0,
            "updated_at": 1_777_000_001.0,
        },
    )


class TestSqliteUpgradeCreatesTables:
    def test_all_cmek_tables_exist(self, upgraded_db) -> None:
        rows = upgraded_db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name IN "
            "('cmek_configs','tier_assignments','cmek_revoke_events') "
            "ORDER BY name"
        ).fetchall()
        assert rows == [
            ("cmek_configs",),
            ("cmek_revoke_events",),
            ("tier_assignments",),
        ]

    def test_cmek_config_defaults_and_provider_check(self, upgraded_db) -> None:
        _insert_config(upgraded_db)
        row = upgraded_db.execute(
            "SELECT policy_principal, verification_id, status, metadata_json "
            "FROM cmek_configs WHERE config_id='cmekcfg-a'"
        ).fetchone()
        assert row == ("", "", "draft", "{}")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_config(upgraded_db, "cmekcfg-bad", provider="local-fernet")

    def test_tier_assignment_defaults_and_security_tier_check(
        self,
        upgraded_db,
    ) -> None:
        _insert_config(upgraded_db)
        upgraded_db.execute(
            "INSERT INTO tier_assignments "
            "(tenant_id, security_tier, cmek_config_id, assigned_at, updated_at) "
            "VALUES (:tenant_id, :security_tier, :cmek_config_id, "
            ":assigned_at, :updated_at)",
            {
                "tenant_id": "t-a",
                "security_tier": "tier-2",
                "cmek_config_id": "cmekcfg-a",
                "assigned_at": 1.0,
                "updated_at": 2.0,
            },
        )
        row = upgraded_db.execute(
            "SELECT status, assigned_by, metadata_json FROM tier_assignments "
            "WHERE tenant_id='t-a'"
        ).fetchone()
        assert row == ("active", "", "{}")

        with pytest.raises(sqlite3.IntegrityError):
            upgraded_db.execute(
                "INSERT INTO tier_assignments "
                "(tenant_id, security_tier, assigned_at, updated_at) "
                "VALUES ('t-b', 'tier-3', 1.0, 2.0)"
            )

    def test_revoke_event_defaults_and_reason_check(self, upgraded_db) -> None:
        _insert_config(upgraded_db)
        upgraded_db.execute(
            "INSERT INTO cmek_revoke_events "
            "(event_id, tenant_id, cmek_config_id, provider, key_id, reason, "
            "detected_at) VALUES (:event_id, :tenant_id, :cmek_config_id, "
            ":provider, :key_id, :reason, :detected_at)",
            {
                "event_id": "cmekrev-a",
                "tenant_id": "t-a",
                "cmek_config_id": "cmekcfg-a",
                "provider": "aws-kms",
                "key_id": "arn:aws:kms:us-east-1:111122223333:key/demo",
                "reason": "describe_failed",
                "detected_at": 3.0,
            },
        )
        row = upgraded_db.execute(
            "SELECT raw_state, source, detail_json FROM cmek_revoke_events "
            "WHERE event_id='cmekrev-a'"
        ).fetchone()
        assert row == ("", "cmek_revoke_detector", "{}")

        with pytest.raises(sqlite3.IntegrityError):
            upgraded_db.execute(
                "INSERT INTO cmek_revoke_events "
                "(event_id, tenant_id, provider, key_id, reason, detected_at) "
                "VALUES ('cmekrev-bad', 't-a', 'aws-kms', 'key', 'retry', 4.0)"
            )

    def test_config_delete_nulls_references(self, upgraded_db) -> None:
        _insert_config(upgraded_db)
        upgraded_db.execute(
            "INSERT INTO tier_assignments "
            "(tenant_id, security_tier, cmek_config_id, assigned_at, updated_at) "
            "VALUES ('t-a', 'tier-2', 'cmekcfg-a', 1.0, 2.0)"
        )
        upgraded_db.execute(
            "INSERT INTO cmek_revoke_events "
            "(event_id, tenant_id, cmek_config_id, provider, key_id, reason, "
            "detected_at) VALUES ('cmekrev-a', 't-a', 'cmekcfg-a', 'aws-kms', "
            "'key', 'key_disabled', 3.0)"
        )

        upgraded_db.execute("DELETE FROM cmek_configs WHERE config_id='cmekcfg-a'")

        assert upgraded_db.execute(
            "SELECT cmek_config_id FROM tier_assignments WHERE tenant_id='t-a'"
        ).fetchone() == (None,)
        assert upgraded_db.execute(
            "SELECT cmek_config_id FROM cmek_revoke_events WHERE event_id='cmekrev-a'"
        ).fetchone() == (None,)

    def test_tenant_delete_cascades_all_cmek_rows(self, upgraded_db) -> None:
        _insert_config(upgraded_db)
        upgraded_db.execute(
            "INSERT INTO tier_assignments "
            "(tenant_id, security_tier, cmek_config_id, assigned_at, updated_at) "
            "VALUES ('t-a', 'tier-2', 'cmekcfg-a', 1.0, 2.0)"
        )
        upgraded_db.execute(
            "INSERT INTO cmek_revoke_events "
            "(event_id, tenant_id, cmek_config_id, provider, key_id, reason, "
            "detected_at) VALUES ('cmekrev-a', 't-a', 'cmekcfg-a', 'aws-kms', "
            "'key', 'key_disabled', 3.0)"
        )

        upgraded_db.execute("DELETE FROM tenants WHERE id='t-a'")

        for table in KS_CMEK_TABLES:
            assert upgraded_db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)

    def test_indexes_created(self, upgraded_db) -> None:
        rows = upgraded_db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name LIKE 'idx_cmek_%' "
            "OR type='index' AND name LIKE 'idx_tier_assignments_%' "
            "ORDER BY name"
        ).fetchall()
        names = {row[0] for row in rows}
        assert {
            "idx_cmek_configs_tenant_status",
            "idx_cmek_configs_provider_key",
            "idx_tier_assignments_security_tier",
            "idx_tier_assignments_cmek_config",
            "idx_cmek_revoke_events_tenant_time",
            "idx_cmek_revoke_events_config_time",
        } <= names


# -- Group 3: idempotency ---------------------------------------------------


class TestIdempotentReupgrade:
    def test_running_upgrade_twice_no_dup_no_change(self, monkeypatch, m0107) -> None:
        conn = sqlite3.connect(":memory:")
        _bootstrap_pre_0107_parent_schema(conn)
        _bind(monkeypatch, conn)
        m0107.upgrade()
        m0107.upgrade()
        assert conn.execute("SELECT count(*) FROM cmek_configs").fetchone() == (0,)


# -- Group 4: PG dialect branch executes -----------------------------------


class TestPgBranchExecutes:
    def test_pg_branch_emits_three_tables_and_indexes(self, monkeypatch, m0107) -> None:
        from alembic import op as alembic_op

        captured: list[str] = []

        class _PgBind:
            class _Dialect:
                name = "postgresql"

            dialect = _Dialect()

            def exec_driver_sql(self, sql, *a, **k):
                captured.append(sql)

        monkeypatch.setattr(alembic_op, "get_bind", lambda: _PgBind())
        m0107.upgrade()

        assert len(captured) == len(m0107._TABLES_PG) + len(m0107._INDEXES)
        joined = "\n".join(captured)
        for table in KS_CMEK_TABLES:
            assert f"CREATE TABLE IF NOT EXISTS {table}" in joined
        assert "JSONB NOT NULL DEFAULT '{}'::jsonb" in joined
        assert "DOUBLE PRECISION" in joined
        assert "idx_cmek_revoke_events_tenant_time" in joined

    def test_pg_downgrade_drops_indexes_and_tables(self, monkeypatch, m0107) -> None:
        from alembic import op as alembic_op

        dropped_indexes: list[tuple[str, dict]] = []
        dropped_tables: list[tuple[str, dict]] = []

        def _drop_index(name, *args, **kwargs):
            dropped_indexes.append((name, kwargs))

        def _drop_table(name, *args, **kwargs):
            dropped_tables.append((name, kwargs))

        monkeypatch.setattr(alembic_op, "drop_index", _drop_index)
        monkeypatch.setattr(alembic_op, "drop_table", _drop_table)
        m0107.downgrade()

        assert [name for name, _ in dropped_indexes] == list(m0107._DROP_INDEXES)
        assert [name for name, _ in dropped_tables] == list(m0107._DROP_TABLES)
        assert dropped_tables[0][0] == "cmek_revoke_events"
        assert dropped_tables[-1][0] == "cmek_configs"
        for _, kwargs in dropped_indexes + dropped_tables:
            assert kwargs.get("if_exists") is True


# -- Group 5: migrator drift guard -----------------------------------------


class TestMigratorListsTables:
    def _load_migrator(self):
        import importlib.util as _u

        repo_root = Path(__file__).resolve().parents[2]
        spec = _u.spec_from_file_location(
            "migrate_sqlite_to_pg", repo_root / "scripts" / "migrate_sqlite_to_pg.py"
        )
        mig = _u.module_from_spec(spec)
        sys.modules["migrate_sqlite_to_pg"] = mig
        spec.loader.exec_module(mig)
        return mig

    def test_cmek_tables_in_tables_in_order(self) -> None:
        mig = self._load_migrator()
        for table in KS_CMEK_TABLES:
            assert table in mig.TABLES_IN_ORDER

    def test_cmek_tables_not_in_identity_id_set(self) -> None:
        mig = self._load_migrator()
        for table in KS_CMEK_TABLES:
            assert table not in mig.TABLES_WITH_IDENTITY_ID

    def test_cmek_tables_replay_after_tenants_and_configs_before_children(self) -> None:
        mig = self._load_migrator()
        order = list(mig.TABLES_IN_ORDER)
        for table in KS_CMEK_TABLES:
            assert order.index("tenants") < order.index(table)
        assert order.index("cmek_configs") < order.index("tier_assignments")
        assert order.index("cmek_configs") < order.index("cmek_revoke_events")
