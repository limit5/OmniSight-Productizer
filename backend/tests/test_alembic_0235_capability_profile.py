"""OP-1115 alembic 0235 ``capability_profile`` contract."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0235 = BACKEND_ROOT / "alembic" / "versions" / "0235_capability_profile.py"


def _load_module(path: Path, name: str):
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0235():
    return _load_module(MIGRATION_0235, "_alembic_test_0235")


@pytest.fixture()
def sqlite_conn(m0235):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            m0235.upgrade()
        yield conn


def test_revision_id_and_parent_are_declared() -> None:
    source = MIGRATION_0235.read_text(encoding="utf-8")
    assert 'revision = "0235"' in source
    assert 'down_revision = "0234"' in source
    assert "backwards-compat: safe" in source


def test_required_columns_and_indexes_present(sqlite_conn) -> None:
    columns = {
        row[1]: row
        for row in sqlite_conn.exec_driver_sql("PRAGMA table_info(capability_profile)")
    }
    expected = {
        "profile_id",
        "provider",
        "model",
        "tools",
        "max_tier",
        "cost_mode",
        "health_state",
        "health_checked_at",
        "known_failure_classes",
        "active",
        "created_at",
        "updated_at",
        "created_by",
        "notes",
    }
    assert expected.issubset(columns)
    assert columns["profile_id"][5] == 1
    assert columns["provider"][3] == 1
    assert columns["model"][3] == 1
    assert columns["tools"][3] == 1
    assert columns["created_by"][3] == 1

    indexes = {
        row[1]
        for row in sqlite_conn.exec_driver_sql("PRAGMA index_list(capability_profile)")
    }
    assert "idx_capability_profile_active_provider_model" in indexes
    assert "idx_capability_profile_provider_model_updated" in indexes
    assert "idx_capability_profile_active_health" in indexes


def test_unique_active_provider_model_contract(sqlite_conn) -> None:
    sqlite_conn.exec_driver_sql(
        """
        INSERT INTO capability_profile (
            profile_id, provider, model, tools, max_tier, cost_mode,
            health_state, health_checked_at, created_by
        ) VALUES (
            'p01', 'openai-subscription', '<unknown>', '["code_edit"]',
            'L', 'subscription', 'healthy', CURRENT_TIMESTAMP, 'system'
        )
        """
    )

    with pytest.raises(sa.exc.IntegrityError):
        sqlite_conn.exec_driver_sql(
            """
            INSERT INTO capability_profile (
                profile_id, provider, model, tools, max_tier, cost_mode,
                health_state, health_checked_at, created_by
            ) VALUES (
                'p02', 'openai-subscription', '<unknown>', '["run_tests"]',
                'L', 'subscription', 'healthy', CURRENT_TIMESTAMP, 'system'
            )
            """
        )

    sqlite_conn.exec_driver_sql(
        """
        INSERT INTO capability_profile (
            profile_id, provider, model, tools, max_tier, cost_mode,
            health_state, health_checked_at, active, created_by
        ) VALUES (
            'p03', 'openai-subscription', '<unknown>', '["run_tests"]',
            'L', 'subscription', 'healthy', CURRENT_TIMESTAMP, 0, 'system'
        )
        """
    )


def test_constraints_reject_unknown_enum_values(sqlite_conn) -> None:
    with pytest.raises(sa.exc.IntegrityError):
        sqlite_conn.exec_driver_sql(
            """
            INSERT INTO capability_profile (
                profile_id, provider, model, tools, max_tier, cost_mode,
                health_state, health_checked_at, created_by
            ) VALUES (
                'bad-tier', 'openai-subscription', '<unknown>', '[]',
                'Q', 'subscription', 'healthy', CURRENT_TIMESTAMP, 'system'
            )
            """
        )
    with pytest.raises(sa.exc.IntegrityError):
        sqlite_conn.exec_driver_sql(
            """
            INSERT INTO capability_profile (
                profile_id, provider, model, tools, max_tier, cost_mode,
                health_state, health_checked_at, created_by
            ) VALUES (
                'bad-health', 'openai-subscription', '<unknown>', '[]',
                'S', 'subscription', 'fizzled', CURRENT_TIMESTAMP, 'system'
            )
            """
        )


def test_unknown_health_requires_null_health_checked_at(sqlite_conn) -> None:
    sqlite_conn.exec_driver_sql(
        """
        INSERT INTO capability_profile (
            profile_id, provider, model, tools, max_tier, cost_mode,
            health_state, health_checked_at, created_by
        ) VALUES (
            'unknown-ok', 'grok-subscription', '<unknown>', '[]',
            'S', 'subscription', 'unknown', NULL, 'system'
        )
        """
    )
    with pytest.raises(sa.exc.IntegrityError):
        sqlite_conn.exec_driver_sql(
            """
            INSERT INTO capability_profile (
                profile_id, provider, model, tools, max_tier, cost_mode,
                health_state, health_checked_at, created_by
            ) VALUES (
                'healthy-bad', 'openai-subscription', 'gpt', '[]',
                'S', 'subscription', 'healthy', NULL, 'system'
            )
            """
        )


def test_downgrade_drops_table_and_indexes(m0235) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            m0235.upgrade()
            m0235.downgrade()
        names = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
            )
        }
        assert "capability_profile" not in names
        assert "idx_capability_profile_active_provider_model" not in names
