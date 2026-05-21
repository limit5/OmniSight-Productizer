"""OP-1585 [RT-10a] -- alembic 0247 ``release_train`` contract."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0247 = BACKEND_ROOT / "alembic" / "versions" / "0247_release_train.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0247():
    return _load_module(MIGRATION_0247, "_alembic_test_0247")


# -- Group 1: structural guards --------------------------------------------


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0247.read_text()

    def test_revision_id_is_0247(self, source: str) -> None:
        assert 'revision = "0247"' in source

    def test_down_revision_is_0246_current_head(self, source: str) -> None:
        # 0246 (green_evidence) is the single alembic head this chains onto.
        assert 'down_revision = "0246"' in source

    def test_required_columns_present_in_both_branches(self, m0247) -> None:
        required = (
            "candidate_sha",
            "source_digest_backend",
            "source_digest_frontend",
            "reserved_version",
            "promotion_state",
            "actor",
            "row_version",
            "final_digest_equality",
            "promotion_audit_id",
            "created_at",
            "updated_at",
        )
        for col in required:
            assert col in m0247._PG_DDL, f"PG branch missing {col}"
            assert col in m0247._SQLITE_DDL, f"SQLite branch missing {col}"

    def test_candidate_sha_is_unique_and_length_checked(self, m0247) -> None:
        for ddl in (m0247._PG_DDL, m0247._SQLITE_DDL):
            assert "UNIQUE (candidate_sha)" in ddl
            assert "CHECK (length(candidate_sha) = 40)" in ddl

    def test_promotion_state_check_is_closed_enum(self, m0247) -> None:
        assert m0247._STATE_LITERAL == "'pending','promoting','promoted','failed'"
        for ddl in (m0247._PG_DDL, m0247._SQLITE_DDL):
            assert (
                "CHECK (promotion_state IN "
                "('pending','promoting','promoted','failed'))" in ddl
            )

    def test_row_version_default_zero(self, m0247) -> None:
        assert "row_version            INTEGER NOT NULL DEFAULT 0" in m0247._PG_DDL
        assert "row_version            INTEGER NOT NULL DEFAULT 0" in m0247._SQLITE_DDL

    def test_pg_branch_uses_timestamptz_and_boolean(self, m0247) -> None:
        assert "TIMESTAMPTZ NOT NULL DEFAULT now()" in m0247._PG_DDL
        assert "final_digest_equality  BOOLEAN" in m0247._PG_DDL
        assert "TIMESTAMPTZ" not in m0247._SQLITE_DDL
        # sqlite has no BOOLEAN affinity -- the portable spelling is INTEGER.
        assert "final_digest_equality  INTEGER" in m0247._SQLITE_DDL


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
def upgraded_db(monkeypatch, m0247) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _bind(monkeypatch, conn)
    m0247.upgrade()
    return conn


SHA_A = "a" * 40
SHA_B = "0123456789abcdef0123456789abcdef01234567"


def _insert(
    conn: sqlite3.Connection,
    candidate_sha: str,
    *,
    state: str = "pending",
) -> None:
    conn.execute(
        "INSERT INTO release_train "
        "(candidate_sha, source_digest_backend, source_digest_frontend, "
        " promotion_state) VALUES (?, 'sha256:be', 'sha256:fe', ?)",
        (candidate_sha, state),
    )


class TestSqliteUpgrade:
    def test_table_exists(self, upgraded_db) -> None:
        row = upgraded_db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='release_train'"
        ).fetchone()
        assert row is not None

    def test_accepts_closed_state_enum_only(self, upgraded_db) -> None:
        _insert(upgraded_db, SHA_A, state="pending")
        _insert(upgraded_db, SHA_B, state="promoting")
        with pytest.raises(sqlite3.IntegrityError):
            _insert(upgraded_db, "b" * 40, state="shipped")

    def test_candidate_sha_must_be_40_chars(self, upgraded_db) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            _insert(upgraded_db, "abc123")

    def test_candidate_sha_is_unique(self, upgraded_db) -> None:
        _insert(upgraded_db, SHA_A)
        with pytest.raises(sqlite3.IntegrityError):
            _insert(upgraded_db, SHA_A)

    def test_row_version_and_state_defaults(self, upgraded_db) -> None:
        upgraded_db.execute(
            "INSERT INTO release_train "
            "(candidate_sha, source_digest_backend, source_digest_frontend) "
            "VALUES (?, 'sha256:be', 'sha256:fe')",
            (SHA_A,),
        )
        row = upgraded_db.execute(
            "SELECT promotion_state, row_version, final_digest_equality, "
            "promotion_audit_id FROM release_train WHERE candidate_sha = ?",
            (SHA_A,),
        ).fetchone()
        assert row == ("pending", 0, None, None)

    def test_index_created(self, upgraded_db) -> None:
        rows = upgraded_db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_release_train_state_updated'"
        ).fetchall()
        assert rows == [("idx_release_train_state_updated",)]


class TestIdempotentReupgrade:
    def test_running_upgrade_twice_is_safe(self, monkeypatch, m0247) -> None:
        conn = sqlite3.connect(":memory:")
        _bind(monkeypatch, conn)
        m0247.upgrade()
        m0247.upgrade()
        row = conn.execute("SELECT count(*) FROM release_train").fetchone()
        assert row == (0,)


# -- Group 3: PG dialect branch executes -----------------------------------


class _PgBind:
    class _Dialect:
        name = "postgresql"

    dialect = _Dialect()

    def __init__(self) -> None:
        self.captured: list[str] = []

    def exec_driver_sql(self, sql, *a, **k):
        self.captured.append(sql)


class TestPgBranchExecutes:
    def test_pg_upgrade_emits_create_and_index(self, monkeypatch, m0247) -> None:
        from alembic import op as alembic_op

        bind = _PgBind()
        monkeypatch.setattr(alembic_op, "get_bind", lambda: bind)
        m0247.upgrade()

        assert len(bind.captured) == 2
        joined = "\n".join(bind.captured)
        assert "CREATE TABLE IF NOT EXISTS release_train" in joined
        assert "TIMESTAMPTZ NOT NULL DEFAULT now()" in joined
        assert "idx_release_train_state_updated" in joined

    def test_pg_downgrade_drops_index_then_table(self, monkeypatch, m0247) -> None:
        from alembic import op as alembic_op

        bind = _PgBind()
        monkeypatch.setattr(alembic_op, "get_bind", lambda: bind)
        m0247.downgrade()
        joined = "\n".join(bind.captured)
        assert "DROP INDEX IF EXISTS idx_release_train_state_updated" in joined
        assert "DROP TABLE IF EXISTS release_train" in joined
        assert joined.index("DROP INDEX") < joined.index("DROP TABLE")
