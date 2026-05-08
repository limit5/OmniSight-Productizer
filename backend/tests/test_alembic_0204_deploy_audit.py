"""OP-779 D18 -- alembic 0204 ``deploy_audit`` contract."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0204 = BACKEND_ROOT / "alembic" / "versions" / "0204_deploy_audit.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0204():
    return _load_module(MIGRATION_0204, "_alembic_test_0204")


@pytest.fixture()
def sqlite_conn(m0204):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        # CHECK constraints need foreign_keys-style enforcement; SQLite
        # enforces CHECK by default, so no PRAGMA tweak is required.
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            m0204.upgrade()
        yield conn


def test_revision_id_and_parent_are_declared() -> None:
    source = MIGRATION_0204.read_text()
    assert 'revision = "0204"' in source
    assert 'down_revision = "0203"' in source
    assert "branch_labels = None" in source
    assert "depends_on = None" in source


def test_required_columns_present_for_sqlite(sqlite_conn) -> None:
    columns = {
        row[1]: row
        for row in sqlite_conn.exec_driver_sql("PRAGMA table_info(deploy_audit)")
    }
    expected = {
        "id",
        "ts",
        "kind",
        "tag",
        "actor",
        "reason",
        "status",
        "elapsed_seconds",
        "context",
        "prev_hash",
        "curr_hash",
    }
    assert expected.issubset(columns)
    # NOT NULL contracts: id, ts, kind, status, prev_hash, curr_hash
    assert columns["kind"][3] == 1
    assert columns["status"][3] == 1
    assert columns["prev_hash"][3] == 1
    assert columns["curr_hash"][3] == 1
    # nullable: tag, actor, reason, elapsed_seconds, context
    assert columns["tag"][3] == 0
    assert columns["actor"][3] == 0
    assert columns["reason"][3] == 0
    assert columns["elapsed_seconds"][3] == 0
    assert columns["context"][3] == 0


def test_indexes_exist(sqlite_conn) -> None:
    rows = sqlite_conn.exec_driver_sql("PRAGMA index_list(deploy_audit)").fetchall()
    names = {row[1] for row in rows}
    assert "idx_deploy_audit_ts" in names
    assert "idx_deploy_audit_kind_ts" in names


def test_kind_check_rejects_unknown_value(sqlite_conn) -> None:
    with pytest.raises(sa.exc.IntegrityError):
        sqlite_conn.exec_driver_sql(
            "INSERT INTO deploy_audit "
            "(kind, status, prev_hash, curr_hash) "
            "VALUES ('typo_kind', 'started', 'p', 'c')"
        )


def test_status_check_rejects_unknown_value(sqlite_conn) -> None:
    with pytest.raises(sa.exc.IntegrityError):
        sqlite_conn.exec_driver_sql(
            "INSERT INTO deploy_audit "
            "(kind, status, prev_hash, curr_hash) "
            "VALUES ('deploy', 'fizzled', 'p', 'c')"
        )


def test_minimal_insert_works(sqlite_conn) -> None:
    sqlite_conn.exec_driver_sql(
        "INSERT INTO deploy_audit "
        "(kind, status, prev_hash, curr_hash) "
        "VALUES ('deploy', 'started', 'prev', 'curr')"
    )
    row = sqlite_conn.exec_driver_sql(
        "SELECT id, kind, status, ts FROM deploy_audit ORDER BY id DESC LIMIT 1"
    ).first()
    assert row.kind == "deploy"
    assert row.status == "started"
    # ts default fired
    assert row.ts is not None


def test_downgrade_drops_table_and_indexes(m0204) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            m0204.upgrade()
            m0204.downgrade()
        names = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
            )
        }
        assert "deploy_audit" not in names
        assert "idx_deploy_audit_ts" not in names
        assert "idx_deploy_audit_kind_ts" not in names
