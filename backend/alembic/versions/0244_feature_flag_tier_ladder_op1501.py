"""OP-1501 -- align feature flag tiers with rollout ladder.

WP.7 originally landed with the draft Warp labels
``debug/dogfood/preview/release/runtime``. OP-1501 makes the production
registry ladder explicit as ``debug/dogfood/early_access/staged/ga`` while
keeping existing rows by mapping the three renamed tiers forward.

Revision ID: 0244
Revises: 0243
Create Date: 2026-05-19
backwards-compat: safe
"""
from __future__ import annotations

from alembic import op


revision = "0244"
down_revision = "0243"
branch_labels = None
depends_on = None


_NEW_TIERS_SQL = "'debug','dogfood','early_access','staged','ga'"
_OLD_TIERS_SQL = "'debug','dogfood','preview','release','runtime'"

_PG_FORWARD_VALUES = """
UPDATE feature_flags
SET tier = CASE tier
    WHEN 'preview' THEN 'early_access'
    WHEN 'release' THEN 'staged'
    WHEN 'runtime' THEN 'ga'
    ELSE tier
END
WHERE tier IN ('preview', 'release', 'runtime')
"""

_PG_BACKWARD_VALUES = """
UPDATE feature_flags
SET tier = CASE tier
    WHEN 'early_access' THEN 'preview'
    WHEN 'staged' THEN 'release'
    WHEN 'ga' THEN 'runtime'
    ELSE tier
END
WHERE tier IN ('early_access', 'staged', 'ga')
"""


def _pg_replace_tier_check(bind, tiers_sql: str) -> None:
    bind.exec_driver_sql(
        "ALTER TABLE feature_flags DROP CONSTRAINT IF EXISTS feature_flags_tier_check"
    )
    bind.exec_driver_sql(
        "ALTER TABLE feature_flags ADD CONSTRAINT feature_flags_tier_check "
        f"CHECK (tier IN ({tiers_sql}))"
    )


def _sqlite_rebuild_feature_flags(bind, tiers_sql: str, direction: str) -> None:
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_feature_flags_expires_at")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_feature_flags_tier_state")
    bind.exec_driver_sql(
        "CREATE TABLE feature_flags__op1501 (\n"
        "    flag_name       TEXT PRIMARY KEY,\n"
        "    tier            TEXT NOT NULL\n"
        f"                    CHECK (tier IN ({tiers_sql})),\n"
        "    state           TEXT NOT NULL DEFAULT 'disabled'\n"
        "                    CHECK (state IN ('disabled','enabled')),\n"
        "    expires_at      TEXT,\n"
        "    owner           TEXT NOT NULL DEFAULT '',\n"
        "    rollout_pct     INTEGER NOT NULL DEFAULT 100\n"
        "                    CHECK (rollout_pct BETWEEN 0 AND 100),\n"
        "    allowed_tenants TEXT NOT NULL DEFAULT '[]',\n"
        "    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,\n"
        "    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP\n"
        ")"
    )
    if direction == "forward":
        tier_expr = (
            "CASE tier WHEN 'preview' THEN 'early_access' "
            "WHEN 'release' THEN 'staged' "
            "WHEN 'runtime' THEN 'ga' ELSE tier END"
        )
    else:
        tier_expr = (
            "CASE tier WHEN 'early_access' THEN 'preview' "
            "WHEN 'staged' THEN 'release' "
            "WHEN 'ga' THEN 'runtime' ELSE tier END"
        )
    bind.exec_driver_sql(
        "INSERT INTO feature_flags__op1501 (\n"
        "    flag_name, tier, state, expires_at, owner, rollout_pct,\n"
        "    allowed_tenants, created_at, updated_at\n"
        ")\n"
        "SELECT\n"
        f"    flag_name, {tier_expr}, state, expires_at, owner, rollout_pct,\n"
        "    allowed_tenants, created_at, updated_at\n"
        "FROM feature_flags"
    )
    bind.exec_driver_sql("DROP TABLE feature_flags")
    bind.exec_driver_sql("ALTER TABLE feature_flags__op1501 RENAME TO feature_flags")
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_feature_flags_tier_state "
        "ON feature_flags(tier, state)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_feature_flags_expires_at "
        "ON feature_flags(expires_at)"
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(
            "ALTER TABLE feature_flags "
            "DROP CONSTRAINT IF EXISTS feature_flags_tier_check"
        )
        bind.exec_driver_sql(_PG_FORWARD_VALUES)
        _pg_replace_tier_check(bind, _NEW_TIERS_SQL)
    else:
        _sqlite_rebuild_feature_flags(bind, _NEW_TIERS_SQL, "forward")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(
            "ALTER TABLE feature_flags "
            "DROP CONSTRAINT IF EXISTS feature_flags_tier_check"
        )
        bind.exec_driver_sql(_PG_BACKWARD_VALUES)
        _pg_replace_tier_check(bind, _OLD_TIERS_SQL)
    else:
        _sqlite_rebuild_feature_flags(bind, _OLD_TIERS_SQL, "backward")
