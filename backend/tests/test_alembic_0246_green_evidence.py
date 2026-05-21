"""OP-1569 [RT-04b] -- alembic 0246 ``green_evidence`` contract."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0246 = BACKEND_ROOT / "alembic" / "versions" / "0246_green_evidence.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0246():
    return _load_module(MIGRATION_0246, "_alembic_test_0246")


# -- Group 1: structural guards --------------------------------------------


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0246.read_text()

    def test_revision_id_is_0246(self, source: str) -> None:
        assert 'revision = "0246"' in source

    def test_down_revision_is_0245_current_head(self, source: str) -> None:
        # 0245 is the single alembic head this migration chains onto.
        assert 'down_revision = "0245"' in source

    def test_required_columns_present_in_both_branches(self, m0246) -> None:
        required = (
            "full_sha",
            "status",
            "pipeline_id",
            "evidence_url",
            "recorded_at",
            "updated_at",
        )
        for col in required:
            assert col in m0246._PG_DDL, f"PG branch missing {col}"
            assert col in m0246._SQLITE_DDL, f"SQLite branch missing {col}"

    def test_full_sha_is_primary_key(self, m0246) -> None:
        assert "full_sha      TEXT PRIMARY KEY" in m0246._PG_DDL
        assert "full_sha      TEXT PRIMARY KEY" in m0246._SQLITE_DDL

    def test_status_check_is_closed_pass_fail(self, m0246) -> None:
        assert m0246._STATUS_LITERAL == "'pass','fail'"
        assert "CHECK (status IN ('pass','fail'))" in m0246._PG_DDL
        assert "CHECK (status IN ('pass','fail'))" in m0246._SQLITE_DDL

    def test_full_sha_length_check_present(self, m0246) -> None:
        assert "CHECK (length(full_sha) = 40)" in m0246._PG_DDL
        assert "CHECK (length(full_sha) = 40)" in m0246._SQLITE_DDL

    def test_pg_branch_uses_timestamptz(self, m0246) -> None:
        assert "TIMESTAMPTZ NOT NULL DEFAULT NOW()" in m0246._PG_DDL
        assert "TIMESTAMPTZ" not in m0246._SQLITE_DDL


# -- Group 2: functional SQLite upgrade ------------------------------------


class _StubBind:
    def __init__(self, raw: sqlite3.Connection) -> None:
        self._raw = raw

        class _Dialect:
            name = "sqlite"

        self.dialect = _Dialect()

    def exec_driver_sql(self, sql: str, *args, **kwargs):
        return self._raw.execute(sql)


def _bind(monkeypatch, conn: sqlite3.Connection) -> None:
    from alembic import op as alembic_op

    monkeypatch.setattr(alembic_op, "get_bind", lambda: _StubBind(conn))


@pytest.fixture()
def upgraded_db(monkeypatch, m0246) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _bind(monkeypatch, conn)
    m0246.upgrade()
    return conn


SHA_A = "a" * 40
SHA_B = "0123456789abcdef0123456789abcdef01234567"


def _insert(conn: sqlite3.Connection, full_sha: str, status: str) -> None:
    conn.execute(
        "INSERT INTO green_evidence (full_sha, status, recorded_at) "
        "VALUES (?, ?, '2026-05-22T00:00:00Z')",
        (full_sha, status),
    )


class TestSqliteUpgrade:
    def test_table_exists(self, upgraded_db) -> None:
        row = upgraded_db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='green_evidence'"
        ).fetchone()
        assert row is not None

    def test_accepts_pass_and_fail_only(self, upgraded_db) -> None:
        _insert(upgraded_db, SHA_A, "pass")
        _insert(upgraded_db, SHA_B, "fail")
        with pytest.raises(sqlite3.IntegrityError):
            _insert(upgraded_db, "b" * 40, "green")

    def test_full_sha_must_be_40_chars(self, upgraded_db) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            _insert(upgraded_db, "abc123", "pass")

    def test_full_sha_is_unique_pk(self, upgraded_db) -> None:
        _insert(upgraded_db, SHA_A, "fail")
        with pytest.raises(sqlite3.IntegrityError):
            _insert(upgraded_db, SHA_A, "pass")

    def test_index_created(self, upgraded_db) -> None:
        rows = upgraded_db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_green_evidence_status_recorded'"
        ).fetchall()
        assert rows == [("idx_green_evidence_status_recorded",)]


class TestIdempotentReupgrade:
    def test_running_upgrade_twice_is_safe(self, monkeypatch, m0246) -> None:
        conn = sqlite3.connect(":memory:")
        _bind(monkeypatch, conn)
        m0246.upgrade()
        m0246.upgrade()
        row = conn.execute("SELECT count(*) FROM green_evidence").fetchone()
        assert row == (0,)


# -- Group 3: PG dialect branch executes -----------------------------------


class TestPgBranchExecutes:
    def test_pg_upgrade_emits_create_and_index(self, monkeypatch, m0246) -> None:
        from alembic import op as alembic_op

        captured: list[str] = []

        class _PgBind:
            class _Dialect:
                name = "postgresql"

            dialect = _Dialect()

            def exec_driver_sql(self, sql, *a, **k):
                captured.append(sql)

        monkeypatch.setattr(alembic_op, "get_bind", lambda: _PgBind())
        m0246.upgrade()

        assert len(captured) == 2
        joined = "\n".join(captured)
        assert "CREATE TABLE IF NOT EXISTS green_evidence" in joined
        assert "TIMESTAMPTZ NOT NULL DEFAULT NOW()" in joined
        assert "idx_green_evidence_status_recorded" in joined

    def test_pg_downgrade_drops_index_then_table(self, monkeypatch, m0246) -> None:
        from alembic import op as alembic_op

        captured: list[str] = []

        class _PgBind:
            class _Dialect:
                name = "postgresql"

            dialect = _Dialect()

            def exec_driver_sql(self, sql, *a, **k):
                captured.append(sql)

        monkeypatch.setattr(alembic_op, "get_bind", lambda: _PgBind())
        m0246.downgrade()
        joined = "\n".join(captured)
        assert "DROP INDEX IF EXISTS idx_green_evidence_status_recorded" in joined
        assert "DROP TABLE IF EXISTS green_evidence" in joined
        # index dropped before the table
        assert joined.index("DROP INDEX") < joined.index("DROP TABLE")
