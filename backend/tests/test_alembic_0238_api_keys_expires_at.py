"""OP-244 alembic 0238 ``api_keys.expires_at`` contract."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import sqlalchemy as sa


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0238 = BACKEND_ROOT / "alembic" / "versions" / "0238_api_keys_expires_at.py"


def _load_module(path: Path, name: str):
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _create_api_keys(conn) -> None:
    conn.exec_driver_sql(
        """
        CREATE TABLE api_keys (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            key_hash TEXT NOT NULL,
            key_lookup_index TEXT,
            key_prefix TEXT NOT NULL DEFAULT '',
            scopes TEXT NOT NULL DEFAULT '["*"]',
            created_by TEXT NOT NULL DEFAULT '',
            last_used_ip TEXT,
            last_used_at REAL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def test_revision_id_and_parent_are_declared() -> None:
    source = MIGRATION_0238.read_text(encoding="utf-8")
    assert 'revision = "0238"' in source
    assert 'down_revision = "m_2026_05_16_3head"' in source
    assert "backwards-compat: safe" in source


def test_upgrade_adds_nullable_expires_at_column() -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    m0238 = _load_module(MIGRATION_0238, "_alembic_test_0238")
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        _create_api_keys(conn)
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            m0238.upgrade()

        columns = {
            row[1]: row
            for row in conn.exec_driver_sql("PRAGMA table_info(api_keys)")
        }
        assert "expires_at" in columns
        assert columns["expires_at"][3] == 0


def test_upgrade_is_idempotent_when_column_already_exists() -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    m0238 = _load_module(MIGRATION_0238, "_alembic_test_0238_idempotent")
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        _create_api_keys(conn)
        conn.exec_driver_sql("ALTER TABLE api_keys ADD COLUMN expires_at FLOAT")
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            m0238.upgrade()

        columns = [
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(api_keys)")
            if row[1] == "expires_at"
        ]
        assert columns == ["expires_at"]


def test_downgrade_drops_expires_at_column() -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    m0238 = _load_module(MIGRATION_0238, "_alembic_test_0238_downgrade")
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        _create_api_keys(conn)
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            m0238.upgrade()
            m0238.downgrade()

        columns = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(api_keys)")
        }
        assert "expires_at" not in columns
