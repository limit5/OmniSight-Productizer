"""MP.W1.3 -- alembic 0199 ``provider_quota_state`` contract."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0199 = (
    BACKEND_ROOT / "alembic" / "versions" / "0199_provider_quota_state.py"
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0199():
    return _load_module(MIGRATION_0199, "_alembic_test_0199")


@pytest.fixture()
def sqlite_conn(m0199):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            m0199.upgrade()
        yield conn


def test_revision_id_and_parent_are_declared() -> None:
    source = MIGRATION_0199.read_text()
    assert 'revision = "0199"' in source
    assert 'down_revision = "0198"' in source
    assert "branch_labels = None" in source
    assert "depends_on = None" in source


def test_required_columns_present_and_typed_for_sqlite(sqlite_conn) -> None:
    columns = {
        row[1]: row
        for row in sqlite_conn.exec_driver_sql(
            "PRAGMA table_info(provider_quota_state)"
        )
    }
    required = {
        "provider": "TEXT",
        "rolling_5h_tokens": "BIGINT",
        "weekly_tokens": "BIGINT",
        "last_reset_at": "DATETIME",
        "last_cap_hit_at": "DATETIME",
        "circuit_state": "TEXT",
        "updated_at": "DATETIME",
    }

    assert set(required).issubset(columns)
    for name, expected_type in required.items():
        assert columns[name][2] == expected_type

    assert columns["provider"][5] == 1
    assert columns["rolling_5h_tokens"][3] == 1
    assert columns["weekly_tokens"][3] == 1
    assert columns["circuit_state"][3] == 1
    assert columns["updated_at"][3] == 1


def test_required_columns_declared_with_pg_timestamptz_shape() -> None:
    source = MIGRATION_0199.read_text()
    assert 'sa.Column("provider", sa.Text(), primary_key=True)' in source
    assert '"rolling_5h_tokens"' in source
    assert '"weekly_tokens"' in source
    assert source.count("sa.BigInteger()") == 2
    assert source.count("sa.DateTime(timezone=True)") == 3
    assert 'server_default=sa.func.now()' in source


def test_defaults_work_for_minimal_insert(sqlite_conn) -> None:
    sqlite_conn.exec_driver_sql(
        "INSERT INTO provider_quota_state (provider) VALUES (?)",
        ("anthropic-subscription",),
    )

    row = sqlite_conn.exec_driver_sql(
        """
        SELECT rolling_5h_tokens, weekly_tokens, circuit_state, updated_at
        FROM provider_quota_state
        WHERE provider = ?
        """,
        ("anthropic-subscription",),
    ).one()

    assert row.rolling_5h_tokens == 0
    assert row.weekly_tokens == 0
    assert row.circuit_state == "closed"
    assert row.updated_at is not None


def test_circuit_state_check_rejects_invalid_value(sqlite_conn) -> None:
    with pytest.raises(sa.exc.IntegrityError):
        sqlite_conn.exec_driver_sql(
            """
            INSERT INTO provider_quota_state (provider, circuit_state)
            VALUES (?, ?)
            """,
            ("openai-subscription", "tripped"),
        )


def test_downgrade_removes_table(m0199) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            m0199.upgrade()
            m0199.downgrade()

        names = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "provider_quota_state" not in names


def test_future_column_additions_do_not_break_required_shape(sqlite_conn) -> None:
    sqlite_conn.exec_driver_sql(
        "ALTER TABLE provider_quota_state ADD COLUMN future_window_tokens BIGINT"
    )

    columns = {
        row[1]
        for row in sqlite_conn.exec_driver_sql(
            "PRAGMA table_info(provider_quota_state)"
        )
    }
    assert {
        "provider",
        "rolling_5h_tokens",
        "weekly_tokens",
        "last_reset_at",
        "last_cap_hit_at",
        "circuit_state",
        "updated_at",
    }.issubset(columns)
    assert "future_window_tokens" in columns


def test_circuit_state_constraint_name_and_no_rls_note() -> None:
    source = MIGRATION_0199.read_text()
    assert "provider_quota_state_circuit_state_check" in source
    assert "No RLS policy is created here" in source
