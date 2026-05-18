"""OP-1500 (WP.6) — ``settings_registry`` table.

Persists the WP.6 sync-scope metadata for every setting routed
through the J4 ``user_preferences`` table. The in-process registry
(``backend.settings_registry``) is the runtime source of truth; this
table is a queryable mirror so DB tooling / Grafana can join on
``pref_key`` without loading the Python module.

Schema notes
------------
* ``pref_key`` is the **base** key (not the ``@platform=`` /
  ``@device=``-suffixed effective key written into
  ``user_preferences``). Joins are against the base form.
* ``scope`` ∈ ``tenant`` / ``user`` / ``device`` — RBAC dimension.
  Today the ``user_preferences`` table itself is user-keyed; the
  ``tenant`` / ``device`` distinction is informational at this
  layer but will become meaningful as the WP.6 fold reaches the
  tenant-scoped surfaces (e.g. tenant-default theme).
* ``sync_mode`` ∈ ``globally`` / ``per_platform`` / ``never`` —
  governs cross-device broadcast (``never`` suppresses
  ``emit_preferences_updated``; ``per_platform`` partitions the
  effective pref_key by ``@platform=``).
* ``supported_platforms`` is a JSON array; an empty array is
  treated as "every platform" by the runtime helper.

The runtime ``rebuild_registry_table()`` performs an idempotent
upsert at startup so this table never drifts from the Python
registry — the migration only owns the **shape**.

Revision ID: 0244
Revises: 0243
Create Date: 2026-05-19
backwards-compat: safe
"""
from __future__ import annotations

from alembic import op


revision = "0245"
down_revision = "0244"
branch_labels = None
depends_on = None


_PG_DDL = """
CREATE TABLE IF NOT EXISTS settings_registry (
    pref_key            TEXT PRIMARY KEY,
    scope               TEXT NOT NULL,
    sync_mode           TEXT NOT NULL,
    supported_platforms TEXT NOT NULL DEFAULT '[]',
    description         TEXT NOT NULL DEFAULT '',
    default_value       TEXT NOT NULL DEFAULT '',
    updated_at          DOUBLE PRECISION NOT NULL,
    CONSTRAINT settings_registry_scope_chk
        CHECK (scope IN ('tenant', 'user', 'device')),
    CONSTRAINT settings_registry_sync_mode_chk
        CHECK (sync_mode IN ('globally', 'per_platform', 'never')),
    CONSTRAINT settings_registry_pref_key_chk
        CHECK (length(trim(pref_key)) > 0)
)
"""


_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS settings_registry (
    pref_key            TEXT PRIMARY KEY,
    scope               TEXT NOT NULL,
    sync_mode           TEXT NOT NULL,
    supported_platforms TEXT NOT NULL DEFAULT '[]',
    description         TEXT NOT NULL DEFAULT '',
    default_value       TEXT NOT NULL DEFAULT '',
    updated_at          REAL NOT NULL,
    CONSTRAINT settings_registry_scope_chk
        CHECK (scope IN ('tenant', 'user', 'device')),
    CONSTRAINT settings_registry_sync_mode_chk
        CHECK (sync_mode IN ('globally', 'per_platform', 'never')),
    CONSTRAINT settings_registry_pref_key_chk
        CHECK (length(trim(pref_key)) > 0)
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        _PG_DDL if bind.dialect.name == "postgresql" else _SQLITE_DDL
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_settings_registry_sync_mode "
        "ON settings_registry (sync_mode)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_settings_registry_scope "
        "ON settings_registry (scope)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_settings_registry_scope")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_settings_registry_sync_mode")
    bind.exec_driver_sql("DROP TABLE IF EXISTS settings_registry")
