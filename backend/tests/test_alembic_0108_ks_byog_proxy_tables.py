"""KS.3.12 -- alembic 0108 BYOG proxy table contract."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0108 = BACKEND_ROOT / "alembic" / "versions" / "0108_ks_byog_proxy_tables.py"

KS_BYOG_PROXY_TABLES = (
    "proxy_registrations",
    "proxy_health_checks",
    "proxy_mtls_certs",
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0108():
    return _load_module(MIGRATION_0108, "_alembic_test_0108")


# -- Group 1: structural guards --------------------------------------------


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0108.read_text()

    def test_revision_id_is_0108(self, source: str) -> None:
        assert 'revision = "0108"' in source

    def test_down_revision_is_0107(self, source: str) -> None:
        assert 'down_revision = "0107"' in source

    def test_all_byog_proxy_tables_declared(self, m0108) -> None:
        joined_pg = "\n".join(m0108._TABLES_PG)
        joined_sqlite = "\n".join(m0108._TABLES_SQLITE)
        for table in KS_BYOG_PROXY_TABLES:
            assert f"CREATE TABLE IF NOT EXISTS {table}" in joined_pg
            assert f"CREATE TABLE IF NOT EXISTS {table}" in joined_sqlite

    def test_registration_table_covers_settings_panel_fields(self, m0108) -> None:
        for col in (
            "proxy_id",
            "tenant_id",
            "display_name",
            "proxy_url",
            "status",
            "nonce_key_ref",
            "client_cert_fingerprint_sha256",
            "heartbeat_interval_seconds",
            "stale_threshold_seconds",
        ):
            assert col in m0108._PG_CREATE_PROXY_REGISTRATIONS
            assert col in m0108._SQLITE_CREATE_PROXY_REGISTRATIONS
        assert "revoked" in m0108._PROXY_REGISTRATION_STATUSES_SQL

    def test_health_checks_cover_heartbeat_and_fail_fast_states(self, m0108) -> None:
        for col in (
            "check_id",
            "proxy_id",
            "tenant_id",
            "status",
            "service",
            "provider_count",
            "heartbeat_interval_seconds",
            "latency_ms",
            "checked_at",
            "detail_json",
        ):
            assert col in m0108._PG_CREATE_PROXY_HEALTH_CHECKS
            assert col in m0108._SQLITE_CREATE_PROXY_HEALTH_CHECKS
        assert "mtls_failed" in m0108._PROXY_HEALTH_STATUSES_SQL
        assert "unreachable" in m0108._PROXY_HEALTH_STATUSES_SQL

    def test_mtls_certs_store_metadata_not_private_material(self, m0108) -> None:
        for col in (
            "cert_id",
            "proxy_id",
            "tenant_id",
            "cert_role",
            "fingerprint_sha256",
            "not_before",
            "not_after",
            "status",
            "pinned",
            "material_ref",
        ):
            assert col in m0108._PG_CREATE_PROXY_MTLS_CERTS
            assert col in m0108._SQLITE_CREATE_PROXY_MTLS_CERTS
        joined = "\n".join(m0108._TABLES_PG + m0108._TABLES_SQLITE).lower()
        assert "private_key" not in joined
        assert "prompt" not in joined
        assert "response" not in joined

    def test_pg_branch_uses_jsonb_boolean_and_double_precision(self, m0108) -> None:
        joined = "\n".join(m0108._TABLES_PG)
        assert "JSONB NOT NULL DEFAULT '{}'::jsonb" in joined
        assert "BOOLEAN NOT NULL DEFAULT FALSE" in joined
        assert "DOUBLE PRECISION" in joined
        assert " REAL" not in joined

    def test_sqlite_branch_uses_text_json_integer_boolean_and_real(self, m0108) -> None:
        joined = "\n".join(m0108._TABLES_SQLITE)
        assert "TEXT NOT NULL DEFAULT '{}'" in joined
        assert "INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1))" in joined
        assert " REAL" in joined
        assert "JSONB" not in joined
        assert "DOUBLE PRECISION" not in joined
        assert "BOOLEAN" not in joined

    def test_indexes_declared(self, m0108) -> None:
        joined = "\n".join(m0108._INDEXES)
        for index in (
            "idx_proxy_registrations_tenant_status",
            "idx_proxy_registrations_url",
            "idx_proxy_health_checks_proxy_time",
            "idx_proxy_health_checks_tenant_status",
            "idx_proxy_mtls_certs_proxy_status",
            "idx_proxy_mtls_certs_fingerprint",
        ):
            assert index in joined


# -- Group 2: functional SQLite upgrade ------------------------------------


def _bootstrap_pre_0108_parent_schema(conn: sqlite3.Connection) -> None:
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
def upgraded_db(monkeypatch, m0108) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _bootstrap_pre_0108_parent_schema(conn)
    conn.execute("INSERT INTO tenants (id, name) VALUES ('t-a', 'Tenant A')")
    conn.execute("INSERT INTO tenants (id, name) VALUES ('t-b', 'Tenant B')")
    _bind(monkeypatch, conn)
    m0108.upgrade()
    return conn


def _insert_registration(
    conn: sqlite3.Connection,
    proxy_id: str = "proxy-a",
    tenant_id: str = "t-a",
    status: str = "active",
) -> None:
    conn.execute(
        "INSERT INTO proxy_registrations "
        "(proxy_id, tenant_id, proxy_url, status, created_at, updated_at) "
        "VALUES (:proxy_id, :tenant_id, :proxy_url, :status, :created_at, "
        ":updated_at)",
        {
            "proxy_id": proxy_id,
            "tenant_id": tenant_id,
            "proxy_url": "https://proxy.example.test",
            "status": status,
            "created_at": 1_777_000_000.0,
            "updated_at": 1_777_000_001.0,
        },
    )


class TestSqliteUpgradeCreatesTables:
    def test_all_byog_proxy_tables_exist(self, upgraded_db) -> None:
        rows = upgraded_db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name IN "
            "('proxy_registrations','proxy_health_checks','proxy_mtls_certs') "
            "ORDER BY name"
        ).fetchall()
        assert rows == [
            ("proxy_health_checks",),
            ("proxy_mtls_certs",),
            ("proxy_registrations",),
        ]

    def test_registration_defaults_and_status_check(self, upgraded_db) -> None:
        _insert_registration(upgraded_db, status="pending")
        row = upgraded_db.execute(
            "SELECT display_name, service, provider_count, "
            "heartbeat_interval_seconds, stale_threshold_seconds, "
            "nonce_key_ref, client_cert_fingerprint_sha256, metadata_json "
            "FROM proxy_registrations WHERE proxy_id='proxy-a'"
        ).fetchone()
        assert row == ("", "omnisight-proxy", 0, 30, 60, "", "", "{}")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_registration(upgraded_db, "proxy-bad", status="fallback")

    def test_health_check_defaults_and_status_check(self, upgraded_db) -> None:
        _insert_registration(upgraded_db)
        upgraded_db.execute(
            "INSERT INTO proxy_health_checks "
            "(check_id, proxy_id, tenant_id, status, checked_at) "
            "VALUES ('check-a', 'proxy-a', 't-a', 'ok', 1.0)"
        )
        row = upgraded_db.execute(
            "SELECT service, provider_count, heartbeat_interval_seconds, "
            "error, detail_json FROM proxy_health_checks WHERE check_id='check-a'"
        ).fetchone()
        assert row == ("omnisight-proxy", 0, 30, "", "{}")

        with pytest.raises(sqlite3.IntegrityError):
            upgraded_db.execute(
                "INSERT INTO proxy_health_checks "
                "(check_id, proxy_id, tenant_id, status, checked_at) "
                "VALUES ('check-bad', 'proxy-a', 't-a', 'fallback', 2.0)"
            )

    def test_mtls_cert_defaults_role_check_and_boolean_shape(self, upgraded_db) -> None:
        _insert_registration(upgraded_db)
        upgraded_db.execute(
            "INSERT INTO proxy_mtls_certs "
            "(cert_id, proxy_id, tenant_id, cert_role, fingerprint_sha256, "
            "created_at) VALUES ('cert-a', 'proxy-a', 't-a', 'client', "
            "'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
            "1.0)"
        )
        row = upgraded_db.execute(
            "SELECT subject, issuer, serial_number, status, pinned, material_ref, "
            "metadata_json FROM proxy_mtls_certs WHERE cert_id='cert-a'"
        ).fetchone()
        assert row == ("", "", "", "active", 0, "", "{}")

        with pytest.raises(sqlite3.IntegrityError):
            upgraded_db.execute(
                "INSERT INTO proxy_mtls_certs "
                "(cert_id, proxy_id, tenant_id, cert_role, fingerprint_sha256, "
                "pinned, created_at) VALUES ('cert-bad', 'proxy-a', 't-a', "
                "'leaf', 'sha256:bbbb', 2, 2.0)"
            )

    def test_registration_delete_cascades_children(self, upgraded_db) -> None:
        _insert_registration(upgraded_db)
        upgraded_db.execute(
            "INSERT INTO proxy_health_checks "
            "(check_id, proxy_id, tenant_id, status, checked_at) "
            "VALUES ('check-a', 'proxy-a', 't-a', 'ok', 1.0)"
        )
        upgraded_db.execute(
            "INSERT INTO proxy_mtls_certs "
            "(cert_id, proxy_id, tenant_id, cert_role, fingerprint_sha256, "
            "created_at) VALUES ('cert-a', 'proxy-a', 't-a', 'client', "
            "'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
            "1.0)"
        )

        upgraded_db.execute("DELETE FROM proxy_registrations WHERE proxy_id='proxy-a'")

        assert upgraded_db.execute(
            "SELECT count(*) FROM proxy_health_checks"
        ).fetchone() == (0,)
        assert upgraded_db.execute(
            "SELECT count(*) FROM proxy_mtls_certs"
        ).fetchone() == (0,)

    def test_tenant_delete_cascades_all_byog_proxy_rows(self, upgraded_db) -> None:
        _insert_registration(upgraded_db)
        upgraded_db.execute(
            "INSERT INTO proxy_health_checks "
            "(check_id, proxy_id, tenant_id, status, checked_at) "
            "VALUES ('check-a', 'proxy-a', 't-a', 'ok', 1.0)"
        )
        upgraded_db.execute(
            "INSERT INTO proxy_mtls_certs "
            "(cert_id, proxy_id, tenant_id, cert_role, fingerprint_sha256, "
            "created_at) VALUES ('cert-a', 'proxy-a', 't-a', 'client', "
            "'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
            "1.0)"
        )

        upgraded_db.execute("DELETE FROM tenants WHERE id='t-a'")

        for table in KS_BYOG_PROXY_TABLES:
            assert upgraded_db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)

    def test_indexes_created(self, upgraded_db) -> None:
        rows = upgraded_db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name LIKE 'idx_proxy_%' "
            "ORDER BY name"
        ).fetchall()
        names = {row[0] for row in rows}
        assert {
            "idx_proxy_registrations_tenant_status",
            "idx_proxy_registrations_url",
            "idx_proxy_health_checks_proxy_time",
            "idx_proxy_health_checks_tenant_status",
            "idx_proxy_mtls_certs_proxy_status",
            "idx_proxy_mtls_certs_fingerprint",
        } <= names


# -- Group 3: idempotency ---------------------------------------------------


class TestIdempotentReupgrade:
    def test_running_upgrade_twice_no_dup_no_change(self, monkeypatch, m0108) -> None:
        conn = sqlite3.connect(":memory:")
        _bootstrap_pre_0108_parent_schema(conn)
        _bind(monkeypatch, conn)
        m0108.upgrade()
        m0108.upgrade()
        assert conn.execute("SELECT count(*) FROM proxy_registrations").fetchone() == (0,)


# -- Group 4: PG dialect branch executes -----------------------------------


class TestPgBranchExecutes:
    def test_pg_branch_emits_three_tables_and_indexes(self, monkeypatch, m0108) -> None:
        from alembic import op as alembic_op

        captured: list[str] = []

        class _PgBind:
            class _Dialect:
                name = "postgresql"

            dialect = _Dialect()

            def exec_driver_sql(self, sql, *a, **k):
                captured.append(sql)

        monkeypatch.setattr(alembic_op, "get_bind", lambda: _PgBind())
        m0108.upgrade()

        assert len(captured) == len(m0108._TABLES_PG) + len(m0108._INDEXES)
        joined = "\n".join(captured)
        for table in KS_BYOG_PROXY_TABLES:
            assert f"CREATE TABLE IF NOT EXISTS {table}" in joined
        assert "JSONB NOT NULL DEFAULT '{}'::jsonb" in joined
        assert "BOOLEAN NOT NULL DEFAULT FALSE" in joined
        assert "idx_proxy_health_checks_proxy_time" in joined

    def test_pg_downgrade_drops_indexes_and_tables(self, monkeypatch, m0108) -> None:
        from alembic import op as alembic_op

        dropped_indexes: list[tuple[str, dict]] = []
        dropped_tables: list[tuple[str, dict]] = []

        def _drop_index(name, *args, **kwargs):
            dropped_indexes.append((name, kwargs))

        def _drop_table(name, *args, **kwargs):
            dropped_tables.append((name, kwargs))

        monkeypatch.setattr(alembic_op, "drop_index", _drop_index)
        monkeypatch.setattr(alembic_op, "drop_table", _drop_table)
        m0108.downgrade()

        assert [name for name, _ in dropped_indexes] == list(m0108._DROP_INDEXES)
        assert [name for name, _ in dropped_tables] == list(m0108._DROP_TABLES)
        assert dropped_tables[0][0] == "proxy_mtls_certs"
        assert dropped_tables[-1][0] == "proxy_registrations"
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

    def test_byog_proxy_tables_in_tables_in_order(self) -> None:
        mig = self._load_migrator()
        for table in KS_BYOG_PROXY_TABLES:
            assert table in mig.TABLES_IN_ORDER

    def test_byog_proxy_tables_not_in_identity_id_set(self) -> None:
        mig = self._load_migrator()
        for table in KS_BYOG_PROXY_TABLES:
            assert table not in mig.TABLES_WITH_IDENTITY_ID

    def test_byog_proxy_tables_replay_after_tenants_and_registration_first(self) -> None:
        mig = self._load_migrator()
        order = list(mig.TABLES_IN_ORDER)
        for table in KS_BYOG_PROXY_TABLES:
            assert order.index("tenants") < order.index(table)
        assert order.index("proxy_registrations") < order.index("proxy_health_checks")
        assert order.index("proxy_registrations") < order.index("proxy_mtls_certs")
