"""MP.W1.9 -- provider quota state alembic contract.

The TODO seed named this as alembic 0198, but revision 0198 is the
HubSpot OAuth provider migration in the current chain.  The provider
quota state table lives in revision 0199, which this contract pins.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
MIGRATION_0199 = (
    BACKEND_ROOT / "alembic" / "versions" / "0199_provider_quota_state.py"
)
DOWNGRADE_GUARD = REPO_ROOT / "scripts" / "check_alembic_downgrade.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0199():
    return _load_module(MIGRATION_0199, "_alembic_test_0198_seed_0199")


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


def _columns(conn) -> dict[str, object]:
    return {
        row[1]: row
        for row in conn.exec_driver_sql("PRAGMA table_info(provider_quota_state)")
    }


def test_ticket_seed_maps_to_current_provider_quota_revision() -> None:
    source = MIGRATION_0199.read_text()
    assert 'revision = "0199"' in source
    assert 'down_revision = "0198"' in source
    assert "provider_quota_state" in source


def test_required_schema_columns_present(sqlite_conn) -> None:
    columns = _columns(sqlite_conn)
    assert {
        "provider",
        "rolling_5h_tokens",
        "weekly_tokens",
        "last_reset_at",
        "last_cap_hit_at",
        "circuit_state",
        "updated_at",
    }.issubset(columns)


def test_required_schema_column_types(sqlite_conn) -> None:
    columns = _columns(sqlite_conn)
    expected = {
        "provider": "TEXT",
        "rolling_5h_tokens": "BIGINT",
        "weekly_tokens": "BIGINT",
        "last_reset_at": "DATETIME",
        "last_cap_hit_at": "DATETIME",
        "circuit_state": "TEXT",
        "updated_at": "DATETIME",
    }
    for name, expected_type in expected.items():
        assert columns[name][2] == expected_type


def test_provider_is_the_only_primary_key(sqlite_conn) -> None:
    columns = _columns(sqlite_conn)
    primary_keys = {name for name, row in columns.items() if row[5]}
    assert primary_keys == {"provider"}


def test_required_defaults_and_nullability(sqlite_conn) -> None:
    columns = _columns(sqlite_conn)
    assert columns["rolling_5h_tokens"][3] == 1
    assert columns["weekly_tokens"][3] == 1
    assert columns["circuit_state"][3] == 1
    assert columns["updated_at"][3] == 1
    assert columns["last_reset_at"][3] == 0
    assert columns["last_cap_hit_at"][3] == 0

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


def test_circuit_state_check_constraint_rejects_invalid_value(sqlite_conn) -> None:
    with pytest.raises(sa.exc.IntegrityError):
        sqlite_conn.exec_driver_sql(
            """
            INSERT INTO provider_quota_state (provider, circuit_state)
            VALUES (?, ?)
            """,
            ("openai-subscription", "tripped"),
        )


def test_no_tenant_or_user_foreign_key_columns(sqlite_conn) -> None:
    columns = _columns(sqlite_conn)
    assert "tenant_id" not in columns
    assert "user_id" not in columns


def test_no_foreign_keys_are_declared_for_global_state() -> None:
    source = MIGRATION_0199.read_text()
    assert "sa.ForeignKey" not in source
    assert "sa.ForeignKeyConstraint" not in source
    assert "REFERENCES " not in source


def test_no_rls_policy_is_declared_for_global_state() -> None:
    source = MIGRATION_0199.read_text()
    assert "No RLS policy is created here" in source
    assert "ENABLE ROW LEVEL SECURITY" not in source
    assert "FORCE ROW LEVEL SECURITY" not in source
    assert "CREATE POLICY" not in source


def test_downgrade_guard_accepts_provider_quota_migration() -> None:
    guard = _load_module(DOWNGRADE_GUARD, "_alembic_downgrade_guard_op22")
    assert guard.check_file(MIGRATION_0199) == []
