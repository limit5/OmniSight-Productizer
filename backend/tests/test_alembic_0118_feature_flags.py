"""WP.7.1 -- alembic 0118 ``feature_flags`` contract."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0118 = BACKEND_ROOT / "alembic" / "versions" / "0118_feature_flags.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0118():
    return _load_module(MIGRATION_0118, "_alembic_test_0118")


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0118.read_text()

    def test_revision_id_is_0118(self, source: str) -> None:
        assert 'revision = "0118"' in source

    def test_down_revision_is_current_head(self, source: str) -> None:
        assert 'down_revision = "0186"' in source

    def test_required_columns_present(self, m0118) -> None:
        required = (
            "flag_name",
            "tier",
            "state",
            "expires_at",
            "owner",
            "created_at",
        )
        for col in required:
            assert col in m0118._PG_CREATE_TABLE, f"PG branch missing {col}"
            assert col in m0118._SQLITE_CREATE_TABLE, f"SQLite branch missing {col}"

    def test_text_pk_and_state_check_declared(self, m0118) -> None:
        assert "flag_name  TEXT PRIMARY KEY" in m0118._PG_CREATE_TABLE
        assert m0118._STATES_SQL == "'disabled','enabled'"
        assert "DEFAULT 'disabled'" in m0118._PG_CREATE_TABLE
        assert "DEFAULT 'disabled'" in m0118._SQLITE_CREATE_TABLE

    def test_created_and_expiry_timestamp_dialect_shape(self, m0118) -> None:
        assert "expires_at TIMESTAMPTZ" in m0118._PG_CREATE_TABLE
        assert "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()" in (
            m0118._PG_CREATE_TABLE
        )
        assert "expires_at TEXT" in m0118._SQLITE_CREATE_TABLE
        assert "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP" in (
            m0118._SQLITE_CREATE_TABLE
        )

    def test_indexes_declared(self, m0118) -> None:
        assert "idx_feature_flags_tier_state" in m0118._INDEX_TIER_STATE
        assert "idx_feature_flags_expires_at" in m0118._INDEX_EXPIRES_AT

    def test_audit_log_namespace_is_documented(self, source: str) -> None:
        assert 'entity_kind="feature_flag"' in source
        assert "idx_audit_log_entity" in source
        assert "does not add a\nparallel audit table" in source


def test_sqlite_upgrade_creates_table_and_indexes(m0118) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(m0118._SQLITE_CREATE_TABLE)
    conn.execute(m0118._INDEX_TIER_STATE)
    conn.execute(m0118._INDEX_EXPIRES_AT)

    columns = {
        row[1]: row[2]
        for row in conn.execute("PRAGMA table_info(feature_flags)")
    }
    assert columns == {
        "flag_name": "TEXT",
        "tier": "TEXT",
        "state": "TEXT",
        "expires_at": "TEXT",
        "owner": "TEXT",
        "created_at": "TEXT",
    }
    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(feature_flags)")
    }
    assert "idx_feature_flags_tier_state" in indexes
    assert "idx_feature_flags_expires_at" in indexes


def test_sqlite_state_check_rejects_invalid_state(m0118) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(m0118._SQLITE_CREATE_TABLE)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO feature_flags (
                flag_name, tier, state, owner
            ) VALUES (:flag_name, :tier, :state, :owner)
            """,
            {
                "flag_name": "wp.registry.invalid-state",
                "tier": "preview",
                "state": "paused",
                "owner": "platform",
            },
        )


class TestMigratorListsTable:
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

    def test_feature_flags_in_tables_in_order(self) -> None:
        mig = self._load_migrator()
        assert "feature_flags" in mig.TABLES_IN_ORDER

    def test_feature_flags_not_in_identity_id_set(self) -> None:
        mig = self._load_migrator()
        assert "feature_flags" not in mig.TABLES_WITH_IDENTITY_ID

    def test_feature_flags_replays_after_tenants(self) -> None:
        mig = self._load_migrator()
        order = list(mig.TABLES_IN_ORDER)
        assert order.index("tenants") < order.index("feature_flags")
