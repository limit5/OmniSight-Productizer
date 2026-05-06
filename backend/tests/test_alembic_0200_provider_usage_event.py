"""MP.W1.2a -- alembic 0200 ``provider_usage_event`` contract."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0200 = (
    BACKEND_ROOT / "alembic" / "versions" / "0200_provider_usage_event.py"
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0200():
    return _load_module(MIGRATION_0200, "_alembic_test_0200")


@pytest.fixture()
def sqlite_conn(m0200):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            m0200.upgrade()
        yield conn


def _index_names(conn) -> set[str]:
    if conn.dialect.name == "postgresql":
        rows = conn.exec_driver_sql(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND tablename = 'provider_usage_event'
            """
        )
        return {row.indexname for row in rows}

    rows = conn.exec_driver_sql("PRAGMA index_list(provider_usage_event)")
    return {row.name for row in rows}


def test_revision_id_and_parent_are_declared() -> None:
    source = MIGRATION_0200.read_text()
    assert 'revision = "0200"' in source
    assert 'down_revision = "0199"' in source
    assert "branch_labels = None" in source
    assert "depends_on = None" in source


def test_required_columns_present_and_typed_for_sqlite(sqlite_conn) -> None:
    columns = {
        row[1]: row
        for row in sqlite_conn.exec_driver_sql(
            "PRAGMA table_info(provider_usage_event)"
        )
    }
    required = {
        "id": "INTEGER",
        "provider": "TEXT",
        "tokens": "BIGINT",
        "ts": "DATETIME",
        "agent_id": "TEXT",
        "correlation_id": "TEXT",
    }

    assert set(required).issubset(columns)
    for name, expected_type in required.items():
        assert columns[name][2] == expected_type

    assert columns["id"][5] == 1
    assert columns["provider"][3] == 1
    assert columns["tokens"][3] == 1
    assert columns["ts"][3] == 1
    assert columns["agent_id"][3] == 0
    assert columns["correlation_id"][3] == 0


def test_required_columns_declared_with_pg_shape() -> None:
    source = MIGRATION_0200.read_text()
    assert '"id"' in source
    assert 'sa.BigInteger().with_variant(sa.Integer(), "sqlite")' in source
    assert 'sa.Column("provider", sa.Text(), nullable=False)' in source
    assert 'sa.Column("tokens", sa.BigInteger(), nullable=False)' in source
    assert "sa.DateTime(timezone=True)" in source
    assert "server_default=sa.func.now()" in source


def test_indexes_exist(sqlite_conn) -> None:
    indexes = _index_names(sqlite_conn)
    assert "idx_provider_usage_event_provider_ts" in indexes
    assert "idx_provider_usage_event_ts" in indexes


def test_index_columns_include_desc_timestamp(sqlite_conn) -> None:
    provider_ts = sqlite_conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE name = ?",
        ("idx_provider_usage_event_provider_ts",),
    ).scalar_one()
    ts_only = sqlite_conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE name = ?",
        ("idx_provider_usage_event_ts",),
    ).scalar_one()

    assert "provider, ts DESC" in provider_ts
    assert "(ts DESC)" in ts_only


def test_defaults_work_for_minimal_insert(sqlite_conn) -> None:
    sqlite_conn.exec_driver_sql(
        """
        INSERT INTO provider_usage_event (provider, tokens)
        VALUES (?, ?)
        """,
        ("anthropic-subscription", 42),
    )

    row = sqlite_conn.exec_driver_sql(
        """
        SELECT id, provider, tokens, ts, agent_id, correlation_id
        FROM provider_usage_event
        WHERE provider = ?
        """,
        ("anthropic-subscription",),
    ).one()

    assert row.id == 1
    assert row.provider == "anthropic-subscription"
    assert row.tokens == 42
    assert row.ts is not None
    assert row.agent_id is None
    assert row.correlation_id is None


def test_tokens_check_rejects_negative_values(sqlite_conn) -> None:
    with pytest.raises(sa.exc.IntegrityError):
        sqlite_conn.exec_driver_sql(
            """
            INSERT INTO provider_usage_event (provider, tokens)
            VALUES (?, ?)
            """,
            ("openai-subscription", -1),
        )


def test_constraint_name_no_rls_note_and_pruning_hint() -> None:
    source = MIGRATION_0200.read_text()
    assert "provider_usage_event_tokens_nonneg" in source
    assert "No RLS policy is created here" in source
    assert "Events older than 30 days can be pruned" in source


def test_downgrade_removes_indexes_and_table(m0200) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            m0200.upgrade()
            m0200.downgrade()

        names = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master "
                "WHERE type IN ('table', 'index')"
            )
        }
        assert "provider_usage_event" not in names
        assert "idx_provider_usage_event_provider_ts" not in names
        assert "idx_provider_usage_event_ts" not in names
