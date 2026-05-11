"""OP-884 D12 -- DB-backed % rollout + per-tenant allow-list for ``feature_flags``.

WP.7.1 (alembic 0194) landed the ``feature_flags`` registry with
``(flag_name PK, tier, state, expires_at, owner, created_at)`` and
WP.7.8 added the operator UI to flip ``state``. OP-884 / META OP-761
Sprint D requires the registry to express **partial / per-tenant**
enablement so a flag can be rolled out to a percentage of tenants
without flipping it globally enabled. That requires two new columns:

* ``rollout_pct`` -- integer in ``[0, 100]``. The hot-path SDK
  (:func:`backend.agents.feature_flags.is_enabled`) computes
  ``hash(tenant_id) % 100 < rollout_pct`` to gate per-tenant. Default
  ``100`` matches the WP.7 contract: when ``state='enabled'`` and no
  rollout was set, every tenant remains in the cohort (back-compat).
* ``allowed_tenants`` -- explicit allow-list serialised as JSON text on
  SQLite / JSONB on PG. ``[]`` (default) means "no explicit allow-list
  → the rollout_pct gate decides". A non-empty list short-circuits the
  bucket: ``tenant_id`` must appear or the flag resolves disabled even
  when ``state='enabled'``.

Both columns are added as NULL-tolerant ALTER TABLE so the migration is
safe to run against a populated prod table without backfilling and the
WP.7.8 router keeps reading existing rows. The SDK treats missing /
NULL ``rollout_pct`` as 100 (legacy on/off semantics) and
``allowed_tenants`` as the empty list.

Audit trail
-----------
Mutations from the operator UI still chain through
``audit_log`` with ``entity_kind='feature_flag'`` per WP.7.1's
contract; the ``before`` / ``after`` payloads will now carry the
``rollout_pct`` and ``allowed_tenants`` fields so the audit row is
complete.

Revision ID: 0225
Revises: 0221
Create Date: 2026-05-11
"""
from __future__ import annotations

from alembic import op


revision = "0225"
down_revision = "0221"
branch_labels = None
depends_on = None


# PG branch: rollout_pct INTEGER + JSONB allowed_tenants. The CHECK
# constraint pins ``rollout_pct`` to [0, 100] so a malformed PATCH from
# the UI fails at the DB boundary, not silently inside the SDK.
_PG_ADD_ROLLOUT = (
    "ALTER TABLE feature_flags "
    "ADD COLUMN IF NOT EXISTS rollout_pct INTEGER NOT NULL DEFAULT 100"
)
_PG_ADD_ALLOWED = (
    "ALTER TABLE feature_flags "
    "ADD COLUMN IF NOT EXISTS allowed_tenants JSONB NOT NULL DEFAULT '[]'::jsonb"
)
_PG_ADD_UPDATED_AT = (
    "ALTER TABLE feature_flags "
    "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
)
_PG_ADD_ROLLOUT_CHK = (
    "ALTER TABLE feature_flags "
    "ADD CONSTRAINT feature_flags_rollout_pct_chk "
    "CHECK (rollout_pct BETWEEN 0 AND 100)"
)

# SQLite branch: ``ALTER TABLE ADD COLUMN`` lacks ``IF NOT EXISTS`` and
# cannot add CHECK constraints inline. The CHECK on rollout_pct is
# enforced application-side (:func:`backend.agents.feature_flags.is_enabled`
# clamps + the router validates Pydantic), matching WP.7.1's posture for
# operator-only writes. ``updated_at`` is added as nullable because
# SQLite rejects non-constant defaults on ``ADD COLUMN``
# (``DEFAULT CURRENT_TIMESTAMP`` parses fine on CREATE but not on
# ALTER); the application populates it on UPDATE and the fresh-DB
# CREATE TABLE in :mod:`backend.db` carries the proper default.
_SQLITE_ADD_ROLLOUT = (
    "ALTER TABLE feature_flags ADD COLUMN rollout_pct INTEGER NOT NULL DEFAULT 100"
)
_SQLITE_ADD_ALLOWED = (
    "ALTER TABLE feature_flags ADD COLUMN allowed_tenants TEXT NOT NULL DEFAULT '[]'"
)
_SQLITE_ADD_UPDATED_AT = (
    "ALTER TABLE feature_flags ADD COLUMN updated_at TEXT"
)


def _column_exists(bind, column: str) -> bool:
    if bind.dialect.name == "postgresql":
        row = bind.exec_driver_sql(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'feature_flags' AND column_name = %s",
            (column,),
        ).fetchone()
        return row is not None
    rows = bind.exec_driver_sql(
        "PRAGMA table_info(feature_flags)"
    ).fetchall()
    return any(r[1] == column for r in rows)


def _constraint_exists_pg(bind, constraint: str) -> bool:
    row = bind.exec_driver_sql(
        "SELECT 1 FROM pg_constraint WHERE conname = %s",
        (constraint,),
    ).fetchone()
    return row is not None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(_PG_ADD_ROLLOUT)
        bind.exec_driver_sql(_PG_ADD_ALLOWED)
        bind.exec_driver_sql(_PG_ADD_UPDATED_AT)
        if not _constraint_exists_pg(bind, "feature_flags_rollout_pct_chk"):
            bind.exec_driver_sql(_PG_ADD_ROLLOUT_CHK)
    else:
        if not _column_exists(bind, "rollout_pct"):
            bind.exec_driver_sql(_SQLITE_ADD_ROLLOUT)
        if not _column_exists(bind, "allowed_tenants"):
            bind.exec_driver_sql(_SQLITE_ADD_ALLOWED)
        if not _column_exists(bind, "updated_at"):
            bind.exec_driver_sql(_SQLITE_ADD_UPDATED_AT)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(
            "ALTER TABLE feature_flags "
            "DROP CONSTRAINT IF EXISTS feature_flags_rollout_pct_chk"
        )
        bind.exec_driver_sql(
            "ALTER TABLE feature_flags DROP COLUMN IF EXISTS updated_at"
        )
        bind.exec_driver_sql(
            "ALTER TABLE feature_flags DROP COLUMN IF EXISTS allowed_tenants"
        )
        bind.exec_driver_sql(
            "ALTER TABLE feature_flags DROP COLUMN IF EXISTS rollout_pct"
        )
    # SQLite cannot DROP COLUMN portably; leave the columns in place on
    # downgrade. The defaults ensure pre-OP-884 readers ignore them.
