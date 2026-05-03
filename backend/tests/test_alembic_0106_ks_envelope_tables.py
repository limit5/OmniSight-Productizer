"""KS.1.10 -- alembic 0106 envelope-encryption table contract."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0106 = BACKEND_ROOT / "alembic" / "versions" / "0106_ks_envelope_tables.py"

KS_TABLES = (
    "kms_keys",
    "tenant_deks",
    "decryption_audits",
    "spend_thresholds",
    "kek_rotations",
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0106():
    return _load_module(MIGRATION_0106, "_alembic_test_0106")


# -- Group 1: structural guards --------------------------------------------


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0106.read_text()

    def test_revision_id_is_0106(self, source: str) -> None:
        assert 'revision = "0106"' in source

    def test_down_revision_is_0065(self, source: str) -> None:
        assert 'down_revision = "0065"' in source

    def test_all_ks_tables_declared(self, m0106) -> None:
        joined_pg = "\n".join(m0106._TABLES_PG)
        joined_sqlite = "\n".join(m0106._TABLES_SQLITE)
        for table in KS_TABLES:
            assert f"CREATE TABLE IF NOT EXISTS {table}" in joined_pg
            assert f"CREATE TABLE IF NOT EXISTS {table}" in joined_sqlite

    def test_provider_check_clause_matches_kms_adapters(self, m0106) -> None:
        from backend.security import kms_adapters as kms

        expected = ",".join(
            f"'{p}'"
            for p in sorted(
                {
                    kms.AWSKMSAdapter.provider,
                    kms.GCPKMSAdapter.provider,
                    kms.LocalFernetKMSAdapter.provider,
                    kms.VaultTransitKMSAdapter.provider,
                }
            )
        )
        assert expected == m0106._KMS_PROVIDERS_SQL

    def test_tenant_deks_columns_match_tenant_dek_ref_shape(self, m0106) -> None:
        required = (
            "dek_id",
            "tenant_id",
            "provider",
            "key_id",
            "wrapped_dek_b64",
            "key_version",
            "wrap_algorithm",
            "encryption_context_json",
            "schema_version",
        )
        for col in required:
            assert col in m0106._PG_CREATE_TENANT_DEKS
            assert col in m0106._SQLITE_CREATE_TENANT_DEKS

    def test_decryption_audits_cover_ks15_canonical_fields(self, m0106) -> None:
        for col in ("tenant_id", "user_id", "key_id", "request_id", "dek_id"):
            assert col in m0106._PG_CREATE_DECRYPTION_AUDITS
            assert col in m0106._SQLITE_CREATE_DECRYPTION_AUDITS
        assert "audit_log_id" in m0106._PG_CREATE_DECRYPTION_AUDITS

    def test_spend_thresholds_cover_ks16_threshold_fields(self, m0106) -> None:
        for col in (
            "tenant_id",
            "token_rate_limit",
            "window_seconds",
            "throttle_seconds",
            "enabled",
        ):
            assert col in m0106._PG_CREATE_SPEND_THRESHOLDS
            assert col in m0106._SQLITE_CREATE_SPEND_THRESHOLDS

    def test_pg_branch_uses_jsonb_boolean_and_double_precision(self, m0106) -> None:
        joined = "\n".join(m0106._TABLES_PG)
        assert "JSONB NOT NULL DEFAULT '{}'::jsonb" in joined
        assert "JSONB NOT NULL DEFAULT '[]'::jsonb" in joined
        assert "BOOLEAN NOT NULL DEFAULT TRUE" in joined
        assert "DOUBLE PRECISION" in joined
        assert " REAL" not in joined

    def test_sqlite_branch_uses_text_json_integer_bool_and_real(self, m0106) -> None:
        joined = "\n".join(m0106._TABLES_SQLITE)
        assert "TEXT NOT NULL DEFAULT '{}'" in joined
        assert "TEXT NOT NULL DEFAULT '[]'" in joined
        assert "INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))" in joined
        assert " REAL" in joined
        assert "JSONB" not in joined
        assert "BOOLEAN" not in joined
        assert "DOUBLE PRECISION" not in joined

    def test_indexes_declared(self, m0106) -> None:
        joined = "\n".join(m0106._INDEXES)
        for index in (
            "idx_kms_keys_provider_status",
            "idx_tenant_deks_tenant_purpose",
            "idx_tenant_deks_key_version",
            "idx_decryption_audits_tenant_time",
            "idx_decryption_audits_request",
            "idx_decryption_audits_key_time",
            "idx_kek_rotations_status_schedule",
            "idx_kek_rotations_key",
        ):
            assert index in joined


# -- Group 2: functional SQLite upgrade ------------------------------------


def _bootstrap_pre_0106_parent_schema(conn: sqlite3.Connection) -> None:
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
def upgraded_db(monkeypatch, m0106) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _bootstrap_pre_0106_parent_schema(conn)
    conn.execute("INSERT INTO tenants (id, name) VALUES ('t-a', 'Tenant A')")
    conn.execute("INSERT INTO tenants (id, name) VALUES ('t-b', 'Tenant B')")
    _bind(monkeypatch, conn)
    m0106.upgrade()
    return conn


def _insert_kms_key(conn: sqlite3.Connection, key_id: str = "local-fernet") -> None:
    conn.execute(
        "INSERT INTO kms_keys (key_id, provider, created_at) "
        "VALUES (:key_id, :provider, :created_at)",
        {
            "key_id": key_id,
            "provider": "local-fernet",
            "created_at": 1_777_000_000.0,
        },
    )


def _insert_tenant_dek(
    conn: sqlite3.Connection,
    dek_id: str = "dek-ks110",
    tenant_id: str = "t-a",
    key_id: str = "local-fernet",
) -> None:
    conn.execute(
        "INSERT INTO tenant_deks "
        "(dek_id, tenant_id, key_id, provider, wrapped_dek_b64, created_at) "
        "VALUES (:dek_id, :tenant_id, :key_id, :provider, :wrapped_dek_b64, "
        ":created_at)",
        {
            "dek_id": dek_id,
            "tenant_id": tenant_id,
            "key_id": key_id,
            "provider": "local-fernet",
            "wrapped_dek_b64": "d3JhcHBlZA==",
            "created_at": 1_777_000_001.0,
        },
    )


class TestSqliteUpgradeCreatesTables:
    def test_all_ks_tables_exist(self, upgraded_db) -> None:
        rows = upgraded_db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name IN "
            "('kms_keys','tenant_deks','decryption_audits',"
            "'spend_thresholds','kek_rotations') "
            "ORDER BY name"
        ).fetchall()
        assert rows == [
            ("decryption_audits",),
            ("kek_rotations",),
            ("kms_keys",),
            ("spend_thresholds",),
            ("tenant_deks",),
        ]

    def test_tenant_deks_defaults_match_ref_contract(self, upgraded_db) -> None:
        _insert_kms_key(upgraded_db)
        _insert_tenant_dek(upgraded_db)

        row = upgraded_db.execute(
            "SELECT wrap_algorithm, encryption_context_json, purpose, "
            "schema_version, key_version FROM tenant_deks WHERE dek_id='dek-ks110'"
        ).fetchone()
        assert row == ("", "{}", "tenant-secret", 1, None)

    def test_tenant_deks_reject_unknown_provider(self, upgraded_db) -> None:
        _insert_kms_key(upgraded_db)
        with pytest.raises(sqlite3.IntegrityError):
            upgraded_db.execute(
                "INSERT INTO tenant_deks "
                "(dek_id, tenant_id, key_id, provider, wrapped_dek_b64, created_at) "
                "VALUES (:dek_id, :tenant_id, :key_id, :provider, "
                ":wrapped_dek_b64, :created_at)",
                {
                    "dek_id": "dek-bad",
                    "tenant_id": "t-a",
                    "key_id": "local-fernet",
                    "provider": "bad-kms",
                    "wrapped_dek_b64": "x",
                    "created_at": 1.0,
                },
            )

    def test_tenant_delete_cascades_dek_audit_and_threshold(self, upgraded_db) -> None:
        _insert_kms_key(upgraded_db)
        _insert_tenant_dek(upgraded_db, "dek-a", "t-a")
        _insert_tenant_dek(upgraded_db, "dek-b", "t-b")
        upgraded_db.execute(
            "INSERT INTO decryption_audits "
            "(audit_id, tenant_id, user_id, key_id, request_id, provider, decrypted_at) "
            "VALUES (:audit_id, :tenant_id, :user_id, :key_id, :request_id, "
            ":provider, :decrypted_at)",
            {
                "audit_id": "aud-a",
                "tenant_id": "t-a",
                "user_id": "u-a",
                "key_id": "local-fernet",
                "request_id": "req-a",
                "provider": "local-fernet",
                "decrypted_at": 1.0,
            },
        )
        upgraded_db.execute(
            "INSERT INTO spend_thresholds "
            "(tenant_id, token_rate_limit, window_seconds, throttle_seconds, "
            "created_at, updated_at) VALUES (:tenant_id, :token_rate_limit, "
            ":window_seconds, :throttle_seconds, :created_at, :updated_at)",
            {
                "tenant_id": "t-a",
                "token_rate_limit": 500,
                "window_seconds": 60.0,
                "throttle_seconds": 120.0,
                "created_at": 1.0,
                "updated_at": 1.0,
            },
        )

        upgraded_db.execute("DELETE FROM tenants WHERE id='t-a'")

        assert upgraded_db.execute(
            "SELECT dek_id FROM tenant_deks ORDER BY dek_id"
        ).fetchall() == [("dek-b",)]
        assert upgraded_db.execute(
            "SELECT audit_id FROM decryption_audits"
        ).fetchall() == []
        assert upgraded_db.execute(
            "SELECT tenant_id FROM spend_thresholds"
        ).fetchall() == []

    def test_spend_threshold_positive_checks_and_defaults(self, upgraded_db) -> None:
        upgraded_db.execute(
            "INSERT INTO spend_thresholds "
            "(tenant_id, token_rate_limit, window_seconds, throttle_seconds, "
            "created_at, updated_at) VALUES (:tenant_id, :token_rate_limit, "
            ":window_seconds, :throttle_seconds, :created_at, :updated_at)",
            {
                "tenant_id": "t-a",
                "token_rate_limit": 500,
                "window_seconds": 60.0,
                "throttle_seconds": 120.0,
                "created_at": 1.0,
                "updated_at": 2.0,
            },
        )
        row = upgraded_db.execute(
            "SELECT enabled, alert_channels_json FROM spend_thresholds "
            "WHERE tenant_id='t-a'"
        ).fetchone()
        assert row == (1, "[]")

        with pytest.raises(sqlite3.IntegrityError):
            upgraded_db.execute(
                "INSERT INTO spend_thresholds "
                "(tenant_id, token_rate_limit, window_seconds, throttle_seconds, "
                "created_at, updated_at) VALUES (:tenant_id, :token_rate_limit, "
                ":window_seconds, :throttle_seconds, :created_at, :updated_at)",
                {
                    "tenant_id": "t-b",
                    "token_rate_limit": 0,
                    "window_seconds": 60.0,
                    "throttle_seconds": 120.0,
                    "created_at": 1.0,
                    "updated_at": 2.0,
                },
            )

    def test_kek_rotations_status_check_and_key_fk(self, upgraded_db) -> None:
        _insert_kms_key(upgraded_db)
        upgraded_db.execute(
            "INSERT INTO kek_rotations "
            "(rotation_id, key_id, provider, from_key_version, to_key_version) "
            "VALUES (:rotation_id, :key_id, :provider, :from_key_version, "
            ":to_key_version)",
            {
                "rotation_id": "rot-a",
                "key_id": "local-fernet",
                "provider": "local-fernet",
                "from_key_version": "1",
                "to_key_version": "2",
            },
        )
        row = upgraded_db.execute(
            "SELECT status, rotated_rows, error, metadata_json "
            "FROM kek_rotations WHERE rotation_id='rot-a'"
        ).fetchone()
        assert row == ("scheduled", 0, "", "{}")

        with pytest.raises(sqlite3.IntegrityError):
            upgraded_db.execute(
                "INSERT INTO kek_rotations "
                "(rotation_id, key_id, provider, from_key_version, "
                "to_key_version, status) VALUES (:rotation_id, :key_id, "
                ":provider, :from_key_version, :to_key_version, :status)",
                {
                    "rotation_id": "rot-bad",
                    "key_id": "local-fernet",
                    "provider": "local-fernet",
                    "from_key_version": "1",
                    "to_key_version": "2",
                    "status": "done",
                },
            )

    def test_indexes_created(self, upgraded_db) -> None:
        rows = upgraded_db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name LIKE 'idx_kms_%' "
            "OR type='index' AND name LIKE 'idx_tenant_deks_%' "
            "OR type='index' AND name LIKE 'idx_decryption_audits_%' "
            "OR type='index' AND name LIKE 'idx_kek_rotations_%' "
            "ORDER BY name"
        ).fetchall()
        names = {row[0] for row in rows}
        assert {
            "idx_kms_keys_provider_status",
            "idx_tenant_deks_tenant_purpose",
            "idx_tenant_deks_key_version",
            "idx_decryption_audits_tenant_time",
            "idx_decryption_audits_request",
            "idx_decryption_audits_key_time",
            "idx_kek_rotations_status_schedule",
            "idx_kek_rotations_key",
        } <= names


# -- Group 3: idempotency ---------------------------------------------------


class TestIdempotentReupgrade:
    def test_running_upgrade_twice_no_dup_no_change(self, monkeypatch, m0106) -> None:
        conn = sqlite3.connect(":memory:")
        _bootstrap_pre_0106_parent_schema(conn)
        _bind(monkeypatch, conn)
        m0106.upgrade()
        m0106.upgrade()
        assert conn.execute("SELECT count(*) FROM kms_keys").fetchone() == (0,)


# -- Group 4: PG dialect branch executes -----------------------------------


class TestPgBranchExecutes:
    def test_pg_branch_emits_five_tables_and_indexes(self, monkeypatch, m0106) -> None:
        from alembic import op as alembic_op

        captured: list[str] = []

        class _PgBind:
            class _Dialect:
                name = "postgresql"

            dialect = _Dialect()

            def exec_driver_sql(self, sql, *a, **k):
                captured.append(sql)

        monkeypatch.setattr(alembic_op, "get_bind", lambda: _PgBind())
        m0106.upgrade()

        assert len(captured) == len(m0106._TABLES_PG) + len(m0106._INDEXES)
        joined = "\n".join(captured)
        for table in KS_TABLES:
            assert f"CREATE TABLE IF NOT EXISTS {table}" in joined
        assert "JSONB NOT NULL DEFAULT '{}'::jsonb" in joined
        assert "BOOLEAN NOT NULL DEFAULT TRUE" in joined
        assert "DOUBLE PRECISION" in joined
        assert "idx_tenant_deks_key_version" in joined

    def test_pg_downgrade_drops_indexes_and_tables(self, monkeypatch, m0106) -> None:
        from alembic import op as alembic_op

        captured: list[str] = []

        class _PgBind:
            class _Dialect:
                name = "postgresql"

            dialect = _Dialect()

            def exec_driver_sql(self, sql, *a, **k):
                captured.append(sql)

        def _exec(sql):
            captured.append(str(sql))

        monkeypatch.setattr(alembic_op, "get_bind", lambda: _PgBind())
        monkeypatch.setattr(alembic_op, "execute", _exec)
        m0106.downgrade()
        joined = "\n".join(captured)
        assert "DROP INDEX IF EXISTS idx_tenant_deks_key_version" in joined
        assert "DROP TABLE IF EXISTS tenant_deks" in joined
        assert "DROP TABLE IF EXISTS kms_keys" in joined


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

    def test_ks_tables_in_tables_in_order(self) -> None:
        mig = self._load_migrator()
        for table in KS_TABLES:
            assert table in mig.TABLES_IN_ORDER

    def test_ks_tables_not_in_identity_id_set(self) -> None:
        mig = self._load_migrator()
        for table in KS_TABLES:
            assert table not in mig.TABLES_WITH_IDENTITY_ID

    def test_ks_tables_replay_after_tenants_and_kms_before_children(self) -> None:
        mig = self._load_migrator()
        order = list(mig.TABLES_IN_ORDER)
        for table in ("tenant_deks", "decryption_audits", "spend_thresholds"):
            assert order.index("tenants") < order.index(table)
        assert order.index("kms_keys") < order.index("tenant_deks")
        assert order.index("kms_keys") < order.index("kek_rotations")
