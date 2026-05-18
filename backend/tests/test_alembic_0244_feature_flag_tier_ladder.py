"""OP-1501 -- feature flag tier ladder migration contract."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0244 = (
    BACKEND_ROOT / "alembic" / "versions" / "0244_feature_flag_tier_ladder_op1501.py"
)


class _SQLiteBind:
    dialect = type("_Dialect", (), {"name": "sqlite"})()

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def exec_driver_sql(self, sql: str) -> None:
        self._conn.execute(sql)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0244():
    return _load_module(MIGRATION_0244, "_alembic_test_0244")


def _create_old_feature_flags(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE feature_flags (
            flag_name       TEXT PRIMARY KEY,
            tier            TEXT NOT NULL
                            CHECK (tier IN (
                                'debug','dogfood','preview','release','runtime'
                            )),
            state           TEXT NOT NULL DEFAULT 'disabled'
                            CHECK (state IN ('disabled','enabled')),
            expires_at      TEXT,
            owner           TEXT NOT NULL DEFAULT '',
            rollout_pct     INTEGER NOT NULL DEFAULT 100
                            CHECK (rollout_pct BETWEEN 0 AND 100),
            allowed_tenants TEXT NOT NULL DEFAULT '[]',
            created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_feature_flags_tier_state
            ON feature_flags(tier, state);
        CREATE INDEX idx_feature_flags_expires_at
            ON feature_flags(expires_at);
        INSERT INTO feature_flags (
            flag_name, tier, state, owner
        ) VALUES
            ('wp.debug', 'debug', 'enabled', 'wp'),
            ('wp.dogfood', 'dogfood', 'enabled', 'wp'),
            ('wp.preview', 'preview', 'enabled', 'wp'),
            ('wp.release', 'release', 'enabled', 'wp'),
            ('wp.runtime', 'runtime', 'enabled', 'wp');
        """
    )


def test_sqlite_upgrade_maps_legacy_tiers_to_op1501_ladder(m0244) -> None:
    conn = sqlite3.connect(":memory:")
    _create_old_feature_flags(conn)

    m0244._sqlite_rebuild_feature_flags(
        _SQLiteBind(conn),
        m0244._NEW_TIERS_SQL,
        "forward",
    )

    rows = dict(conn.execute("SELECT flag_name, tier FROM feature_flags"))
    assert rows == {
        "wp.debug": "debug",
        "wp.dogfood": "dogfood",
        "wp.preview": "early_access",
        "wp.release": "staged",
        "wp.runtime": "ga",
    }

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO feature_flags (flag_name, tier) VALUES (?, ?)",
            ("wp.legacy", "runtime"),
        )

    indexes = {row[1] for row in conn.execute("PRAGMA index_list(feature_flags)")}
    assert "idx_feature_flags_tier_state" in indexes
    assert "idx_feature_flags_expires_at" in indexes


def test_sqlite_downgrade_maps_op1501_ladder_to_legacy_tiers(m0244) -> None:
    conn = sqlite3.connect(":memory:")
    _create_old_feature_flags(conn)
    bind = _SQLiteBind(conn)
    m0244._sqlite_rebuild_feature_flags(bind, m0244._NEW_TIERS_SQL, "forward")
    m0244._sqlite_rebuild_feature_flags(bind, m0244._OLD_TIERS_SQL, "backward")

    rows = dict(conn.execute("SELECT flag_name, tier FROM feature_flags"))
    assert rows["wp.preview"] == "preview"
    assert rows["wp.release"] == "release"
    assert rows["wp.runtime"] == "runtime"
