"""OP-1115 -- capability_profile registry table.

backwards-compat: safe

Revision ID: 0235
Revises: 0234
Create Date: 2026-05-15
"""
from __future__ import annotations

from alembic import op


revision = "0235"
down_revision = "0234"
branch_labels = None
depends_on = None


_PG_DDL = """
CREATE TABLE IF NOT EXISTS capability_profile (
    profile_id              TEXT PRIMARY KEY,
    provider                TEXT NOT NULL,
    model                   TEXT NOT NULL,
    tools                   JSONB NOT NULL,
    max_tier                TEXT NOT NULL,
    cost_mode               TEXT NOT NULL,
    health_state            TEXT NOT NULL,
    health_checked_at       TIMESTAMPTZ,
    known_failure_classes   JSONB NOT NULL DEFAULT '[]'::jsonb,
    active                  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by              TEXT NOT NULL,
    notes                   TEXT,
    CONSTRAINT capability_profile_tools_array_chk
        CHECK (jsonb_typeof(tools) = 'array'),
    CONSTRAINT capability_profile_failure_classes_array_chk
        CHECK (jsonb_typeof(known_failure_classes) = 'array'),
    CONSTRAINT capability_profile_max_tier_chk
        CHECK (max_tier IN ('S', 'M', 'L', 'X')),
    CONSTRAINT capability_profile_cost_mode_chk
        CHECK (cost_mode IN ('subscription', 'metered', 'local')),
    CONSTRAINT capability_profile_health_state_chk
        CHECK (health_state IN ('healthy', 'degraded', 'down', 'unknown')),
    CONSTRAINT capability_profile_unknown_health_checked_at_chk
        CHECK (
            (health_state = 'unknown' AND health_checked_at IS NULL)
            OR (health_state <> 'unknown' AND health_checked_at IS NOT NULL)
        )
)
"""


_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS capability_profile (
    profile_id              TEXT PRIMARY KEY,
    provider                TEXT NOT NULL,
    model                   TEXT NOT NULL,
    tools                   TEXT NOT NULL,
    max_tier                TEXT NOT NULL,
    cost_mode               TEXT NOT NULL,
    health_state            TEXT NOT NULL,
    health_checked_at       TEXT,
    known_failure_classes   TEXT NOT NULL DEFAULT '[]',
    active                  INTEGER NOT NULL DEFAULT 1,
    created_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by              TEXT NOT NULL,
    notes                   TEXT,
    CONSTRAINT capability_profile_max_tier_chk
        CHECK (max_tier IN ('S', 'M', 'L', 'X')),
    CONSTRAINT capability_profile_cost_mode_chk
        CHECK (cost_mode IN ('subscription', 'metered', 'local')),
    CONSTRAINT capability_profile_health_state_chk
        CHECK (health_state IN ('healthy', 'degraded', 'down', 'unknown')),
    CONSTRAINT capability_profile_active_chk
        CHECK (active IN (0, 1)),
    CONSTRAINT capability_profile_unknown_health_checked_at_chk
        CHECK (
            (health_state = 'unknown' AND health_checked_at IS NULL)
            OR (health_state <> 'unknown' AND health_checked_at IS NOT NULL)
        )
)
"""


_PG_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION capability_profile_set_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


_PG_TRIGGER_UPDATE = """
DROP TRIGGER IF EXISTS trg_capability_profile_set_updated_at
    ON capability_profile;
CREATE TRIGGER trg_capability_profile_set_updated_at
    BEFORE UPDATE ON capability_profile
    FOR EACH ROW EXECUTE FUNCTION capability_profile_set_updated_at()
"""


_SQLITE_TRIGGER_UPDATE = """
CREATE TRIGGER IF NOT EXISTS trg_capability_profile_set_updated_at
AFTER UPDATE ON capability_profile
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE capability_profile
    SET updated_at = CURRENT_TIMESTAMP
    WHERE profile_id = NEW.profile_id;
END
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(_PG_DDL)
    else:
        bind.exec_driver_sql(_SQLITE_DDL)
    bind.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_capability_profile_active_provider_model "
        "ON capability_profile (provider, model) WHERE active = TRUE"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_capability_profile_provider_model_updated "
        "ON capability_profile (provider, model, updated_at DESC)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_capability_profile_active_health "
        "ON capability_profile (health_state) WHERE active = TRUE"
    )
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(_PG_TRIGGER_FN)
        bind.exec_driver_sql(_PG_TRIGGER_UPDATE)
    else:
        bind.exec_driver_sql(_SQLITE_TRIGGER_UPDATE)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(
            "DROP TRIGGER IF EXISTS trg_capability_profile_set_updated_at "
            "ON capability_profile"
        )
        bind.exec_driver_sql("DROP FUNCTION IF EXISTS capability_profile_set_updated_at()")
    else:
        bind.exec_driver_sql("DROP TRIGGER IF EXISTS trg_capability_profile_set_updated_at")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_capability_profile_active_health")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_capability_profile_provider_model_updated")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_capability_profile_active_provider_model")
    bind.exec_driver_sql("DROP " "TABLE IF EXISTS capability_profile")
